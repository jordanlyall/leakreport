"""Leak 4: listing churn. Σ gas on non-fulfilment txs to Seaport, Blur or WETH (approvals, cancels, reverts)."""
from leaks.base import LeakResult, Params, Prices, day_of


def compute(ledger, prices: Prices, params: Params) -> LeakResult:
    total, items, by_reason = 0, [], {}
    for t in sorted(params.churn_txs, key=lambda t: (t.ts, t.tx_hash)):
        if t.ts < params.since_ts or t.ts > params.as_of_ts:
            continue
        total += t.gas_wei
        by_reason[t.reason] = by_reason.get(t.reason, 0) + t.gas_wei
        items.append({"tx": t.tx_hash, "day": day_of(t.ts), "to": t.to, "reason": t.reason, "wei": t.gas_wei})
    return LeakResult("listing_churn", "1.0", total, items,
                      {"txs": len(items), "by_reason_wei": dict(sorted(by_reason.items()))})
