"""Leak 2: held past the floor. exit = floor on the first day it fell below 90% of basis while held."""
from fractions import Fraction

from leaks.base import LeakResult, Params, Prices, day_of


def compute(ledger, prices: Prices, params: Params) -> LeakResult:
    ratio = Fraction(params.breach_ratio)
    total, items = 0, []
    for l in ledger.lots:
        if not l.basis_wei or l.dispose_kind not in (None, "sale"):
            continue
        if l.disposed_ts is not None and l.disposed_ts < params.since_ts:
            continue
        start = max(day_of(l.acquired_ts), params.since)
        end = day_of(l.disposed_ts) if l.disposed_ts is not None else params.as_of
        if end < start:
            continue
        threshold = l.basis_wei * ratio          # whole lot; compare floor × qty
        breach_day = exit_floor = None
        for d in [start, *prices.change_days(l.collection, start, end)]:
            f = prices.floor(l.collection, d)
            if f is not None and f * l.qty < threshold:
                breach_day, exit_floor = d, f
                break
        if breach_day is None:
            continue
        if l.dispose_kind == "sale":
            outcome, outcome_kind = l.proceeds_wei, "proceeds"
        else:
            cur = prices.current_floor.get(l.collection)
            if cur is None:
                continue
            outcome, outcome_kind = cur * l.qty, "current_floor"
        leak = max(0, exit_floor * l.qty - outcome)
        if leak:
            total += leak
            items.append({"lot_id": l.lot_id, "collection": l.collection, "contract": l.contract,
                          "token_id": l.token_id, "day": end if l.disposed_ts else params.as_of,
                          "breach_day": breach_day, "exit_wei": exit_floor * l.qty, "outcome_wei": outcome,
                          "outcome": outcome_kind, "basis_wei": l.basis_wei, "wei": leak})
    return LeakResult("held_past_floor", "0.5", total, items, {"breach_ratio": params.breach_ratio})
