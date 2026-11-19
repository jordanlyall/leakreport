"""Flag F: concentration of current floor value. No ETH amount, no weight."""
from leaks.base import Params, Prices


def compute(ledger, prices: Prices, params: Params) -> dict:
    value: dict[str, int] = {}
    for l in ledger.open_lots():
        f = prices.current_floor.get(l.collection) or 0
        value[l.collection] = value.get(l.collection, 0) + f * l.qty
    total = sum(value.values())
    ranked = sorted(value.items(), key=lambda kv: (-kv[1], kv[0]))
    if total == 0:
        return {"top1_share": 0.0, "top3_share": 0.0, "level": "none", "top": []}
    top1 = ranked[0][1] / total
    top3 = sum(v for _, v in ranked[:3]) / total
    level = "high" if top1 > 0.5 or top3 > 0.8 else "normal"
    return {"top1_share": round(top1, 4), "top3_share": round(top3, 4), "level": level,
            "top": [s for s, _ in ranked[:3]]}
