"""OpenSea API v2 client: pagination, rate limiting (via Http), cache writes. Network only when called."""
import urllib.parse
from decimal import Decimal

from sources.cache import Cache
from sources.http import Http, HttpError, TokenBucket, urllib_transport

BASE = "https://api.opensea.io"
ACCOUNT_EVENT_TYPES = ("sale", "transfer", "mint")
MAX_PAGES = 2000


def make_http(api_key: str, transport=urllib_transport, bucket: TokenBucket | None = None, sleep=None) -> Http:
    kw = {"sleep": sleep} if sleep else {}
    return Http(transport=transport, bucket=bucket or TokenBucket(4.0, 4.0), headers={"X-API-KEY": api_key}, **kw)


def _url(path: str, params: list[tuple[str, str]]) -> str:
    return f"{BASE}{path}?{urllib.parse.urlencode(params)}" if params else f"{BASE}{path}"


def payment_wei(payment: dict | None) -> int:
    """OpenSea's payment quantity in base units. It is sometimes fractional (a per-item average): floor it."""
    q = (payment or {}).get("quantity") or 0
    return int(Decimal(str(q)))


def nft_of(e: dict) -> dict:
    return e.get("nft") or e.get("asset") or {}


def parties(e: dict) -> tuple[str, str]:
    if e.get("event_type") == "sale":
        return (e.get("seller") or "").lower(), (e.get("buyer") or "").lower()
    return (e.get("from_address") or "").lower(), (e.get("to_address") or "").lower()


def event_key(e: dict) -> str:
    n = nft_of(e)
    frm, to = parties(e)
    return ":".join(str(x) for x in (
        e.get("event_type"), (e.get("transaction") or "").lower(), (n.get("contract") or "").lower(),
        n.get("identifier"), frm, to, e.get("quantity"), e.get("order_hash") or ""))


def _rows(events: list[dict]):
    return [(event_key(e), e.get("event_type", ""), int(e.get("event_timestamp") or 0),
             (e.get("transaction") or "").lower() or None, e) for e in events]


def _paginate(http: Http, cache: Cache, scope: str, path: str, params: list[tuple[str, str]],
              after: int | None, max_pages: int = MAX_PAGES, before: int | None = None) -> tuple[int, bool]:
    """Pages newest-first through an events endpoint into the cache. Returns (count, reached_end)."""
    base = (list(params) + ([("after", str(after))] if after is not None else [])
            + ([("before", str(before))] if before is not None else []))
    cursor, n = None, 0
    for _ in range(max_pages):
        p = base + ([("next", cursor)] if cursor else [])
        data = http.get(_url(path, p), redact=f"opensea {path}") or {}
        events = data.get("asset_events") or []
        cache.upsert_events(scope, _rows(events))
        n += len(events)
        cursor = data.get("next")
        if not cursor or not events:
            return n, True
    return n, False


def sync_account_events(cache: Cache, http: Http, address: str) -> int:
    """Full sync on first run, then incremental with after = last synced timestamp."""
    address = address.lower()
    scope = f"account:{address}"
    state = cache.get_sync(scope)
    after = state[0] - 1 if state and state[1] else None
    params = [("event_type", t) for t in ACCOUNT_EVENT_TYPES] + [("chain", "ethereum"), ("limit", "50")]
    n, done = _paginate(http, cache, scope, f"/api/v2/events/accounts/{address}", params, after)
    last = cache.db.execute("SELECT COALESCE(MAX(ts), 0) FROM events WHERE scope=?", (scope,)).fetchone()[0]
    cache.set_sync(scope, last, done or bool(state and state[1]))
    return n


def sync_holdings(cache: Cache, http: Http, address: str) -> int:
    address = address.lower()
    nfts, cursor = [], None
    for _ in range(MAX_PAGES):
        p = [("limit", "200")] + ([("next", cursor)] if cursor else [])
        data = http.get(_url(f"/api/v2/chain/ethereum/account/{address}/nfts", p), redact="opensea nfts") or {}
        nfts.extend(data.get("nfts") or [])
        cursor = data.get("next")
        if not cursor:
            break
    cache.replace_nfts(address, nfts)
    return len(nfts)


def fetch_collection(cache: Cache, http: Http, slug: str):
    q = urllib.parse.quote(slug)
    raw = http.get(_url(f"/api/v2/collections/{q}", []), redact="opensea collection")
    stats = http.get(_url(f"/api/v2/collections/{q}/stats", []), redact="opensea stats")
    latest = http.get(_url(f"/api/v2/events/collection/{q}", [("event_type", "sale"), ("limit", "1")]),
                      redact="opensea collection events") or {}
    evs = latest.get("asset_events") or []
    last_sale = int(evs[0]["event_timestamp"]) if evs else None
    cache.put_collection(slug, raw=raw, stats=stats, last_sale_ts=last_sale)


def fetch_floor_prices(http: Http, slug: str, resolution: int | None = None):
    """OpenSea floor history. Returns the raw payload, or None if the endpoint is missing."""
    q = urllib.parse.quote(slug)
    try:
        return http.get(_url(f"/api/v2/collections/{q}/floor_prices", [("timeframe", "all_time")]
                             + ([("resolution", str(resolution))] if resolution else [])),
                        redact="opensea floor_prices")
    except HttpError as e:
        if " 404" in str(e) or " 400" in str(e) or " 403" in str(e):
            return None
        raise


def sync_collection_sales(cache: Cache, http: Http, slug: str, after: int, max_pages: int = 200) -> tuple[int, bool]:
    """Collection sale events since `after` (for the sales-proxy floor). Capped at max_pages."""
    scope = f"collection:{slug}"
    state = cache.get_sync(scope)
    if state and state[1] and state[0] >= after:
        after = state[0] - 1
    q = urllib.parse.quote(slug)
    n, done = _paginate(http, cache, scope, f"/api/v2/events/collection/{q}",
                        [("event_type", "sale"), ("limit", "50")], after, max_pages=max_pages)
    last = cache.db.execute("SELECT COALESCE(MAX(ts), 0) FROM events WHERE scope=?", (scope,)).fetchone()[0]
    cache.set_sync(scope, last, done)
    return n, done


def sync_collection_sales_window(cache: Cache, http: Http, slug: str, after: int, before: int,
                                 max_pages: int = 200) -> bool:
    """Collection sales strictly inside (after, before), fetched once: a completed window is remembered in meta
    and not re-read. Returns whether the window reached its start (False = capped at max_pages)."""
    key = f"sales_window:{slug}"
    done = cache.get_meta(key)
    if done:
        a, b, complete = done.split(":")
        if int(a) <= after and int(b) >= before and complete == "1":
            return True
    q = urllib.parse.quote(slug)
    _, complete = _paginate(http, cache, f"collection:{slug}", f"/api/v2/events/collection/{q}",
                            [("event_type", "sale"), ("limit", "50")], after, max_pages=max_pages, before=before)
    cache.set_meta(key, f"{after}:{before}:{int(complete)}")
    return complete
