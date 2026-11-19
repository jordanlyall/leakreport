"""Slice a small real-data fixture out of cache.sqlite: every Seaport-era sale by the wallet plus chosen extra txs,
with their OpenSea events (trimmed to the fields the tool reads) and full receipts. No credentials are involved:
the cache holds only API responses. Run after a --refresh:
    uv run python scripts/slice_fixture.py <wallet> [extra_tx ...]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leaks.base import day_ts  # noqa: E402
from sources.cache import Cache  # noqa: E402
from sources.normalize import account_events  # noqa: E402
from sources.opensea import nft_of  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EVENT_FIELDS = ("event_type", "transaction", "event_timestamp", "seller", "buyer", "from_address", "to_address",
                "quantity", "order_hash", "payment", "transfer_type")


def trim(e: dict) -> dict:
    n = nft_of(e)
    out = {k: e[k] for k in EVENT_FIELDS if k in e}
    out["nft"] = {"contract": n.get("contract"), "identifier": n.get("identifier"), "collection": n.get("collection")}
    return out


def main():
    wallet = sys.argv[1].lower()
    extra = {h.lower() for h in sys.argv[2:]}
    cache = Cache(ROOT / "cache.sqlite")
    evs = account_events(cache, [wallet])
    since = day_ts("2022-06-14")
    sale_txs = {e["transaction"].lower() for e in evs if e.get("event_type") == "sale"
                and (e.get("seller") or "").lower() == wallet and int(e["event_timestamp"]) >= since}
    txs = sale_txs | extra
    events = sorted((trim(e) for e in evs if (e.get("transaction") or "").lower() in txs),
                    key=lambda e: (e["event_timestamp"], e["transaction"], str(e["nft"]["identifier"])))
    receipts = {h: cache.receipt(h) for h in sorted(txs)}
    missing = [h for h, r in receipts.items() if r is None]
    if missing:
        sys.exit(f"no cached receipt for {missing}; run leak --refresh first")
    out = ROOT / "tests" / "fixtures" / "wallet_sales.json"
    out.write_text(json.dumps({"wallet": wallet, "events": events, "receipts": receipts}, sort_keys=True, indent=1))
    print(f"wrote {out.relative_to(ROOT)}: {len(events)} events, {len(receipts)} receipts, "
          f"{out.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
