"""Leak 3: bought at the top. Per purchase: max(0, price − median30 floor at purchase)."""
from leaks.base import LeakResult, Params, Prices, add_days, day_of


def compute(ledger, prices: Prices, params: Params) -> LeakResult:
    total, items, no_floor = 0, [], 0
    for l in ledger.lots:
        if l.acquire_kind != "buy" or not l.price_wei or l.acquired_ts < params.since_ts:
            continue
        day = day_of(l.acquired_ts)
        m = prices.median30(l.collection, day)
        if m is None:
            no_floor += 1
            continue
        leak = max(0, l.price_wei - m * l.qty)
        later_day = add_days(day, 90)
        later = prices.floor(l.collection, later_day) if later_day <= params.as_of else None
        ctx = round(later * l.qty / l.price_wei, 4) if later is not None else None
        if leak:
            total += leak
            items.append({"lot_id": l.lot_id, "collection": l.collection, "contract": l.contract,
                          "token_id": l.token_id, "tx": l.acquire_tx, "day": day, "price_wei": l.price_wei,
                          "median30_wei": m * l.qty, "floor_90d_later_over_price": ctx, "wei": leak})
    return LeakResult("bought_at_top", "0.5", total, items, {"purchases_without_floor": no_floor})
