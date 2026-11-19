"""Leak 1: fee and royalty drag. Only `avoidable` fees count toward the score."""
from fractions import Fraction

from leaks.base import LeakResult, Params, Prices, day_of


def compute(ledger, prices: Prices, params: Params) -> LeakResult:
    rate = Fraction(params.benchmark_rate)
    fee_total = royalty_total = price_total = avoidable_total = 0
    n_sales = 0
    items = []
    for l in ledger.lots:
        if l.dispose_kind != "sale" or l.disposed_ts < params.since_ts:
            continue
        n_sales += 1
        day = day_of(l.disposed_ts)
        fee_total += l.fee_wei
        royalty_total += l.royalty_wei
        price_total += l.sale_price_wei
        avoidable = 0
        if day >= params.blur_launch:
            avoidable = max(0, l.fee_wei - int(l.sale_price_wei * rate))
        avoidable_total += avoidable
        if avoidable:
            items.append({"lot_id": l.lot_id, "collection": l.collection, "contract": l.contract,
                          "token_id": l.token_id, "tx": l.dispose_tx, "day": day, "wei": avoidable,
                          "fee_wei": l.fee_wei, "price_wei": l.sale_price_wei})
    return LeakResult("fee_drag", "1.0", avoidable_total, items, {
        "fee_total_wei": fee_total, "royalty_total_wei": royalty_total, "avoidable_wei": avoidable_total,
        "sale_volume_wei": price_total, "sales": n_sales, "benchmark_rate": params.benchmark_rate})
