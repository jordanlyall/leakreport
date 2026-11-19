"""Leak 5: sold into bids. For accepted offers: max(0, floor that day − sale price)."""
from leaks.base import LeakResult, Params, Prices, day_of


def compute(ledger, prices: Prices, params: Params) -> LeakResult:
    total, items, n = 0, [], 0
    for l in ledger.lots:
        if l.dispose_kind != "sale" or l.sale_type != "bid" or l.disposed_ts < params.since_ts:
            continue
        n += 1
        day = day_of(l.disposed_ts)
        f = prices.floor(l.collection, day)
        if f is None:
            continue
        leak = max(0, f * l.qty - l.sale_price_wei)
        if leak:
            total += leak
            items.append({"lot_id": l.lot_id, "collection": l.collection, "contract": l.contract,
                          "token_id": l.token_id, "tx": l.dispose_tx, "day": day, "floor_wei": f * l.qty,
                          "price_wei": l.sale_price_wei, "wei": leak})
    return LeakResult("sold_into_bids", "0.5", total, items, {"accepted_bids": n})
