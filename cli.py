"""`leak` entrypoint. Only --refresh touches the network; the report is computed from the cache."""
import argparse
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from leaks.base import Params
from ledger.lots import replay
from report import build_report, dumps, table
from sources import chain, opensea, prices
from sources.cache import Cache
from sources.http import Http, TokenBucket
from sources.normalize import account_events, normalize, wanted_receipts

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def rpc_label(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for name in ("alchemy", "infura", "quicknode", "ankr", "llamarpc", "drpc", "publicnode", "cloudflare"):
        if name in host:
            return name
    return "custom"


def _env(*names: str) -> dict[str, str]:
    vals = {n: (os.environ.get(n) or "").strip() for n in names}
    missing = [n for n, v in vals.items() if not v]
    if missing:
        sys.exit(f"leak: missing environment values: {', '.join(missing)} (set them in the env file)")
    return vals


def resolve_target(cache: Cache, target: str, rpc: "chain.Rpc | None") -> tuple[str, str | None]:
    if ADDR_RE.match(target):
        return target.lower(), None
    if not target.lower().endswith(".eth"):
        sys.exit(f"leak: '{target}' is neither an address nor an .eth name")
    addr = cache.ens(target)
    if addr is None and rpc is not None:
        addr = chain.resolve_ens(rpc, target)
        cache.put_ens(target, addr)
    if not addr:
        sys.exit(f"leak: could not resolve {target}" + ("" if rpc else " from cache; run with --refresh"))
    return addr.lower(), target.lower()


def refresh(cache: Cache, owners: list[str], since: str, env: dict, transport=None, rate_scale: float = 1.0,
            today: str | None = None):
    """The only network path. `transport`/`rate_scale` exist so recorded fixtures can replay it fast."""
    tkw = {"transport": transport} if transport else {}
    os_http = opensea.make_http(env["OPENSEA_API_KEY"], bucket=TokenBucket(4.0 * rate_scale, 4.0), **tkw)
    rpc = chain.Rpc(env["ETH_RPC_URL"], bucket=TokenBucket(10.0 * rate_scale, 10.0), **tkw)
    open_http = Http(bucket=TokenBucket(2.0 * rate_scale, 2.0), **tkw)
    for o in owners:
        log(f"opensea events {o} ...")
        log(f"  +{opensea.sync_account_events(cache, os_http, o)} events")
        log(f"  {opensea.sync_holdings(cache, os_http, o)} current NFTs")
        try:
            log(f"  {chain.sync_txlist(cache, open_http, o)} txs from blockscout")
        except Exception as e:   # incremental: the next --refresh picks up where this stopped
            log(f"  blockscout txlist skipped ({e}); using {len(cache.txs(o))} cached txs")
    hashes = wanted_receipts(cache, owners)
    log(f"receipts: {len(hashes)} wanted ...")
    fb_name, fb_url = chain.FALLBACK_RPC
    fallback = chain.Rpc(fb_url, bucket=TokenBucket(5.0 * rate_scale, 5.0), **tkw)
    st = chain.sync_receipts(cache, rpc, hashes, fallback=fallback)
    log(f"  +{st['fetched']} fetched ({st['from_fallback']} from {fb_name}), {st['missing']} not found")
    if st["from_fallback"]:
        cache.set_meta("receipt_fallback", fb_name)
    need = prices.collections_needed(account_events(cache, owners))
    with_history = prices.floor_history_needed(normalize(cache, owners, lambda s, d: None, lambda d: None).events)
    log(f"collections: {len(need)}, {len(with_history)} need floor history")
    for slug, first in sorted(need.items()):
        log(f"collection {slug} ...")
        try:
            opensea.fetch_collection(cache, os_http, slug)
            if slug not in with_history:
                log("  floors: not needed (no basis, no trade)")
                continue
            src = prices.sync_floors(cache, os_http, slug, needed_from=max(first, since))
            log(f"  floors: {src}")
        except Exception as e:     # one bad collection must not sink the run; it shows up as floor_source none
            log(f"  skipped: {e}")
    log("eth/usd ...")
    try:
        info = prices.sync_eth_usd(cache, open_http)
        log(f"  coingecko days={info['days']} first={info['first']}")
    except Exception as e:      # the bundled Coinbase CSV still covers every day
        log(f"  coingecko skipped: {e}")
    main = owners[0]
    if not cache.reverse(main)[1]:      # unknown or no name yet: ask again (one call)
        try:
            cache.put_reverse(main, chain.reverse_ens(rpc, main))
        except Exception as e:
            log(f"  reverse ENS skipped: {e}")
    cache.set_meta("rpc_label", rpc_label(env["ETH_RPC_URL"]))
    cache.set_meta(f"as_of:{main}", today or datetime.now(timezone.utc).date().isoformat())


def make_report(cache: Cache, address: str, ens: str | None, linked: list[str], since: str,
                benchmark: str) -> dict:
    owners = [address, *linked]
    as_of = cache.get_meta(f"as_of:{address}")
    if as_of is None:
        sys.exit(f"leak: no cached data for {address}; run again with --refresh")
    slugs = sorted(prices.collections_needed(account_events(cache, owners)))
    px, floor_sources, usd_src = prices.load_prices(cache, slugs, as_of)
    norm = normalize(cache, owners, px.floor, px.usd_rate)
    ledger = replay(norm.events, address, linked)
    held = {(n["contract"].lower(), str(n["identifier"])) for o in owners for n in cache.nfts(o)}
    open_keys = ledger.open_keys()
    quality = {**norm.quality,
               "holdings_check": {"open_lots": len(open_keys), "held_now": len(held),
                                  "held_not_in_ledger": len(held - open_keys),
                                  "open_but_not_held": len(open_keys - held)},
               "listing_churn_txs": len(norm.churn)}
    if ens is None:
        ens = cache.reverse(address)[1]
    params = Params(since=since, as_of=as_of, benchmark_rate=benchmark, churn_txs=norm.churn)
    wallet = {"address": address, "ens": ens, "as_of": as_of, "linked": linked}
    rpc_src = cache.get_meta("rpc_label") or "none"
    if cache.get_meta("receipt_fallback"):
        rpc_src += "+" + cache.get_meta("receipt_fallback")
    srcs = {"opensea_api": "v2", "rpc": rpc_src, "eth_usd": usd_src,
            "txlist": "blockscout"}
    return build_report(ledger, px, params, wallet, quality, srcs, floor_sources)


def main(argv=None):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(prog="leak", description="NFT wallet leak report (read-only)")
    ap.add_argument("target", help="0x address or ENS name")
    ap.add_argument("--since", default="2022-06-14")
    ap.add_argument("--refresh", action="store_true", help="fetch new data (the only path that uses the network)")
    ap.add_argument("--benchmark-fee", default="0.005")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--card")
    ap.add_argument("--linked", default="", help="comma-separated other wallets of the same owner")
    ap.add_argument("--cache", default="cache.sqlite")
    a = ap.parse_args(argv)

    linked = [x.strip().lower() for x in a.linked.split(",") if x.strip()]
    for x in linked:
        if not ADDR_RE.match(x):
            sys.exit(f"leak: --linked value '{x}' is not an address")
    cache = Cache(a.cache)
    env = _env("OPENSEA_API_KEY", "ETH_RPC_URL") if a.refresh else None
    rpc = chain.Rpc(env["ETH_RPC_URL"]) if env else None
    address, ens = resolve_target(cache, a.target, rpc)
    if a.refresh:
        refresh(cache, [address, *linked], a.since, env)
    rep = make_report(cache, address, ens, linked, a.since, a.benchmark_fee)
    if a.json_out:
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(dumps(rep))
    if a.card:
        from card.render import render
        render(rep, a.card)
    print(table(rep))
    cache.close()


if __name__ == "__main__":
    main()
