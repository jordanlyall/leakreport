import json
import urllib.parse

from sources.cache import Cache
from sources.http import TokenBucket
from sources.opensea import make_http, sync_account_events
from sources.keccak import keccak256, namehash

ADDR = "0x" + "a" * 40


def _event(i):
    return {"event_type": "transfer", "transaction": f"0x{i:064x}", "event_timestamp": 1_700_000_000 + i,
            "from_address": "0x" + "c" * 40, "to_address": ADDR, "quantity": 1,
            "nft": {"contract": "0x" + "1" * 40, "identifier": str(i), "collection": "c"}}


class FakeOpenSea:
    """Two pages newest-first; optionally a 429 with Retry-After before the second page."""

    def __init__(self, events, throttle_once=False):
        self.events = sorted(events, key=lambda e: -e["event_timestamp"])
        self.throttle_once = throttle_once
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if self.throttle_once and q.get("next") == "p2":
            self.throttle_once = False
            return 429, {"retry-after": "2"}, b""
        evs = [e for e in self.events if "after" not in q or e["event_timestamp"] > int(q["after"])]
        if q.get("next") == "p2":
            return 200, {}, json.dumps({"asset_events": evs[2:], "next": None}).encode()
        return 200, {}, json.dumps({"asset_events": evs[:2], "next": "p2" if len(evs) > 2 else None}).encode()


def _http(fake, sleeps):
    return make_http("k", transport=fake, bucket=TokenBucket(1000, 1000), sleep=sleeps.append)


def test_sync_then_incremental(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    fake = FakeOpenSea([_event(i) for i in range(5)])
    sleeps = []
    sync_account_events(cache, _http(fake, sleeps), ADDR)
    assert cache.count_events(f"account:{ADDR}") == 5
    first_calls = len(fake.calls)
    assert first_calls == 2
    # Second refresh: only asks for events after the last synced timestamp; nothing new, one call.
    sync_account_events(cache, _http(fake, sleeps), ADDR)
    assert "after=" in fake.calls[-1]
    assert cache.count_events(f"account:{ADDR}") == 5


def test_429_retry_after_pauses_and_no_duplicates(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    fake = FakeOpenSea([_event(i) for i in range(5)], throttle_once=True)
    sleeps = []
    sync_account_events(cache, _http(fake, sleeps), ADDR)
    assert 2.0 in sleeps
    assert cache.count_events(f"account:{ADDR}") == 5
    # Re-ingesting the same pages is idempotent.
    sync_account_events(cache, _http(FakeOpenSea([_event(i) for i in range(5)]), []), ADDR)
    assert cache.count_events(f"account:{ADDR}") == 5


def test_token_bucket_limits_rate():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    b = TokenBucket(4.0, 4.0, clock=lambda: t[0], sleep=sleep)
    for _ in range(12):
        b.take()
    assert abs(t[0] - 2.0) < 1e-9   # 4 free, then 8 more at 4/s


def test_keccak_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert namehash("eth").hex() == "93cdeb708b7545dc668eb9280176169d1c33cfd8ed6f04690a0bcc88a93fc4ae"


def test_receipts_fall_back_and_never_cache_null(tmp_path):
    from sources.chain import Rpc, sync_receipts
    a, b, c = (f"0x{i:064x}" for i in (1, 2, 3))

    def node(have):
        def t(method, url, headers, body):
            q = json.loads(body)
            r = {"blockNumber": "0x1"} if q["params"][0] in have else None
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": q["id"], "result": r}).encode()
        return t

    cache = Cache(tmp_path / "c.sqlite")
    primary = Rpc("https://primary", transport=node({a}), bucket=TokenBucket(1000, 1000))
    fallback = Rpc("https://fallback", transport=node({b}), bucket=TokenBucket(1000, 1000))
    stats = sync_receipts(cache, primary, [a, b, c], fallback=fallback)
    assert stats == {"fetched": 2, "from_fallback": 1, "missing": 1}
    assert cache.has_receipt(a) and cache.has_receipt(b) and not cache.has_receipt(c)
    cache.put_receipt(c, None)          # an old cache row holding null must not count as fetched
    assert not cache.has_receipt(c)


def test_ens_goes_through_the_universal_resolver():
    from sources.chain import UNIVERSAL_RESOLVER, Rpc, resolve_ens, reverse_ens
    from sources.keccak import keccak256
    main = "0x00000000000000000000000000000000000000aa"
    word = lambda n: n.to_bytes(32, "big")
    blob = lambda b: word(len(b)) + b + b"\0" * ((32 - len(b) % 32) % 32)
    seen = []

    def node(method, url, headers, body):
        q = json.loads(body)
        call = q["params"][0]
        seen.append((call["to"], call["data"][:10]))
        sel = call["data"][2:10]
        if sel == keccak256(b"resolve(bytes,bytes)")[:4].hex():
            if b"example".hex() not in call["data"]:
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": q["id"], "error": {"code": 3, "message": "revert"}}).encode()
            out = word(64) + word(0) + blob(bytes.fromhex(main[2:]).rjust(32, b"\0"))
        else:
            out = word(96) + word(0) + word(0) + blob(b"example.eth")
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": q["id"], "result": "0x" + out.hex()}).encode()

    rpc = Rpc("https://node", transport=node, bucket=TokenBucket(1000, 1000))
    assert resolve_ens(rpc, "example.eth") == main
    assert resolve_ens(rpc, "nobody.eth") is None          # resolver revert = unresolved, not a crash
    assert reverse_ens(rpc, main) == "example.eth"
    assert {to for to, _ in seen} == {UNIVERSAL_RESOLVER}


def test_ens_dns_encoding_uses_byte_lengths_and_rejects_bad_labels():
    from sources.chain import dns_encode
    assert dns_encode("Example.ETH") == b"\x07example\x03eth\x00"
    assert dns_encode("é.eth") == b"\x02\xc3\xa9\x03eth\x00"      # 1 char, 2 bytes
    assert dns_encode("a..eth") is None and dns_encode("x" * 256 + ".eth") is None


def test_network_error_retries_then_redacts():
    import urllib.error

    import pytest

    from sources.http import Http, HttpError
    url = "https://rpc.example/v2/KEYPART123456"
    calls, sleeps = [], []

    def flaky(method, u, headers, body):
        calls.append(u)
        if len(calls) == 1:
            raise urllib.error.URLError(TimeoutError(60, "Operation timed out"))
        return 200, {}, b'{"ok": 1}'

    assert Http(transport=flaky, sleep=sleeps.append).request("POST", url, {}, redact="rpc x") == {"ok": 1}
    assert len(calls) == 2 and len(sleeps) == 1

    def dead(method, u, headers, body):
        raise TimeoutError(60, f"timed out talking to {u}")

    with pytest.raises(HttpError) as ei:
        Http(transport=dead, sleep=lambda s: None, max_retries=2).request("POST", url, {}, redact="rpc x")
    assert "KEYPART" not in str(ei.value) and "rpc x" in str(ei.value)
    assert ei.value.__suppress_context__


def test_floor_gap_before_opensea_history_filled_from_window_of_sales(tmp_path, monkeypatch):
    from leaks.base import day_ts
    from sources import prices
    E = 10**18
    calls = []

    def sale(day, wei):
        return {"event_type": "sale", "event_timestamp": day_ts(day) + 3600, "quantity": 1,
                "transaction": f"0x{wei:064x}", "payment": {"quantity": str(wei), "token_address": "0x" + "0" * 40},
                "nft": {"contract": "0x" + "1" * 40, "identifier": str(wei), "collection": "c"}}

    sales = [sale("2022-07-01", 3 * E), sale("2022-07-01", 2 * E), sale("2023-05-01", E), sale("2025-01-01", E // 2)]

    def fake(method, url, headers, body):
        calls.append(url)
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/floor_prices"):
            pts = [{"time": "2024-09-16T00:00:00Z", "token_unit": 0.7, "symbol": "ETH"},
                   {"time": "2024-10-01T00:00:00Z", "token_unit": 0.6, "symbol": "ETH"}]
            return 200, {}, json.dumps({"floor_prices": pts}).encode()
        evs = [s for s in sales if int(q.get("after", 0)) < s["event_timestamp"] < int(q.get("before", 10**12))]
        return 200, {}, json.dumps({"asset_events": sorted(evs, key=lambda e: -e["event_timestamp"])}).encode()

    monkeypatch.setattr(prices, "_now", lambda: day_ts("2026-11-13"))
    cache = Cache(tmp_path / "c.sqlite")
    http = make_http("k", transport=fake, bucket=TokenBucket(1000, 1000), sleep=lambda s: None)
    assert prices.sync_floors(cache, http, "c", needed_from="2022-08-01") == "opensea+sales_proxy"
    cache.put_collection("c", raw={}, stats=None, last_sale_ts=None)
    px, src, _ = prices.load_prices(cache, ["c"], "2026-11-13")
    assert src == {"c": "opensea+sales_proxy"}
    assert px.floor("c", "2022-07-01") == 2 * E           # daily minimum sale
    assert px.floor("c", "2023-06-01") == E               # carried forward
    assert px.floor("c", "2024-09-16") == 7 * E // 10     # OpenSea from its first day
    assert all("before=" in u for u in calls if "/events/" in u)   # never re-reads recent sales
    n = len(calls)
    prices.sync_floors(cache, http, "c", needed_from="2022-08-01")
    assert [u for u in calls[n:] if "/events/" in u] == []           # window remembered


def test_fractional_wei_payment_quantity_is_floored():
    from sources.opensea import payment_wei
    from sources.prices import daily_min_sales
    assert payment_wei({"quantity": "111111111111111.12"}) == 111111111111111
    assert payment_wei({"quantity": "5"}) == 5 and payment_wei({}) == 0
    ev = {"event_timestamp": 1_700_000_000, "quantity": 1,
          "payment": {"quantity": "83333333333.33333", "token_address": "0x" + "0" * 40}}
    assert list(daily_min_sales([ev]).values()) == [83333333333]
