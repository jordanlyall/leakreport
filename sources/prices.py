"""ETH/USD daily closes and per-collection floor history (OpenSea floor_prices, else daily min sale)."""
import csv
import time
from decimal import Decimal
from pathlib import Path

from leaks.base import Prices, add_days, day_of, day_ts
from sources.cache import Cache
from sources.chain import WETH, ZERO
from sources.http import Http
from sources.opensea import fetch_floor_prices, nft_of, payment_wei, sync_collection_sales, sync_collection_sales_window

COINGECKO = "https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=usd&days=365"
ETH_USD_CSV = Path(__file__).with_name("data") / "eth_usd.csv"


def _now() -> int:
    return int(time.time())


def eth_to_wei(x) -> int:
    return int(Decimal(str(x)) * Decimal(10**18))


def closes_from_points(points: list[list[float]]) -> dict[str, float]:
    """CoinGecko points are [ms, price]. A point stamped 00:00 UTC is the previous day's close."""
    out = {}
    for ms, price in sorted(points):
        out[day_of(int((ms - 1) // 1000))] = float(price)
    return out


def sync_eth_usd(cache: Cache, http: Http) -> dict:
    data = http.get(COINGECKO, redact="coingecko market_chart") or {}
    series = closes_from_points(data.get("prices") or [])
    cache.put_eth_usd("coingecko", series)
    return {"days": len(series), "first": min(series) if series else None}


def load_csv(path: Path = ETH_USD_CSV) -> dict[str, float]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["date"]: float(r["close"]) for r in csv.DictReader(f)}


def eth_usd_series(cache: Cache) -> tuple[dict[str, float], str]:
    cg = cache.eth_usd("coingecko")
    fallback = load_csv()
    merged = {**fallback, **cg}
    used = [s for s, d in (("coingecko", cg), ("coinbase_csv", fallback)) if d]
    return merged, "+".join(used) or "none"


# ---- floors ----

def parse_floor_prices(payload) -> dict[str, int]:
    """Defensive parse of OpenSea floor_prices: a list of points with a timestamp/date and a floor value."""
    if not payload:
        return {}
    pts = payload
    if isinstance(payload, dict):
        for k in ("floor_prices", "prices", "data", "results", "floorPrices"):
            if isinstance(payload.get(k), list):
                pts = payload[k]
                break
        else:
            return {}
    out = {}
    for p in pts:
        if not isinstance(p, dict):
            continue
        t = p.get("timestamp") or p.get("time") or p.get("date") or p.get("event_timestamp")
        if (p.get("symbol") or "ETH").upper() not in ("ETH", "WETH"):
            continue
        v = p.get("token_unit", p.get("floor_price", p.get("floor", p.get("price", p.get("value")))))
        if isinstance(v, dict):
            v = v.get("value", v.get("amount"))
        if t is None or v is None:
            continue
        day = t[:10] if isinstance(t, str) else day_of(int(t) // 1000 if int(t) > 10**11 else int(t))
        out[day] = min(out.get(day, eth_to_wei(v)), eth_to_wei(v))
    return out


def daily_min_sales(sale_events: list[dict]) -> dict[str, int]:
    """Sales-proxy floor: daily minimum per-unit ETH/WETH sale price."""
    out: dict[str, int] = {}
    for e in sale_events:
        pay = e.get("payment") or {}
        if (pay.get("token_address") or ZERO).lower() not in (ZERO, WETH):
            continue
        qty = int(e.get("quantity") or 1)
        unit = payment_wei(pay) // max(qty, 1)
        if unit <= 0:
            continue
        d = day_of(int(e["event_timestamp"]))
        out[d] = min(out.get(d, unit), unit)
    return out


def sync_floors(cache: Cache, http: Http, slug: str, needed_from: str, max_pages: int = 200) -> str:
    """Fills cache floors for slug. Returns the source used: "opensea" or "sales_proxy"."""
    days = (day_ts(add_days(day_of(_now()), 0)) - day_ts(add_days(needed_from, -31))) // 86400 + 1
    series = parse_floor_prices(fetch_floor_prices(http, slug, resolution=days))
    if series:
        cache.put_floors(slug, "opensea", series)
        start, first = add_days(needed_from, -31), min(series)
        if start >= first:
            cache.put_collection(slug, floor_source="opensea")
            return "opensea"
        # OpenSea's history starts ~2024-09 for most collections: fill the days before it from that window's sales.
        sync_collection_sales_window(cache, http, slug, after=day_ts(start), before=day_ts(first),
                                     max_pages=max_pages)
        gap = {d: v for d, v in daily_min_sales(cache.events(f"collection:{slug}")).items() if start <= d < first}
        cache.put_floors(slug, "sales_proxy_gap", gap)
        cache.put_collection(slug, floor_source="opensea+sales_proxy")
        return "opensea+sales_proxy"
    sync_collection_sales(cache, http, slug, after=day_ts(add_days(needed_from, -31)), max_pages=max_pages)
    cache.put_floors(slug, "sales_proxy", daily_min_sales(cache.events(f"collection:{slug}")))
    cache.put_collection(slug, floor_source="sales_proxy")
    return "sales_proxy"


def current_floor_from_stats(stats: dict | None) -> int | None:
    if not stats:
        return None
    total = stats.get("total") or {}
    v = total.get("floor_price")
    sym = (total.get("floor_price_symbol") or "ETH").upper()
    if v is None or sym not in ("ETH", "WETH"):
        return None
    return eth_to_wei(v)


def load_prices(cache: Cache, slugs: list[str], as_of: str) -> tuple[Prices, dict[str, str], str]:
    floors, current, last_sale, sources = {}, {}, {}, {}
    for slug in sorted(set(slugs)):
        c = cache.collection(slug)
        src = (c or {}).get("floor_source") or "none"
        sources[slug] = src
        if src == "opensea+sales_proxy":
            floors[slug] = {**cache.floors(slug, "sales_proxy_gap"), **cache.floors(slug, "opensea")}
        else:
            floors[slug] = cache.floors(slug, src) if src != "none" else {}
        if c is not None:
            cur = current_floor_from_stats(c.get("stats"))
            if cur is None and floors[slug]:
                cur = floors[slug][max(floors[slug])]
            current[slug] = cur
            last_sale[slug] = c.get("last_sale_ts")
    usd, usd_src = eth_usd_series(cache)
    return Prices(floors=floors, current_floor=current, last_sale_ts=last_sale, eth_usd=usd, as_of=as_of), \
        sources, usd_src


def floor_history_needed(ledger_events) -> set[str]:
    """Collections whose floor history can move a number: any trade, or any lot with a cost (price or gas).
    Pure airdrops (basis 0, never sold on OpenSea) only need current stats."""
    return {e.collection for e in ledger_events if e.kind in ("sale", "buy") or e.price_wei or e.gas_wei}


def collections_needed(events: list[dict]) -> dict[str, str]:
    """slug -> earliest event day involving it (floors are needed from 30 days before)."""
    out: dict[str, str] = {}
    for e in events:
        slug = nft_of(e).get("collection")
        if not slug:
            continue
        d = day_of(int(e.get("event_timestamp") or 0))
        out[slug] = min(out.get(slug, d), d)
    return out
