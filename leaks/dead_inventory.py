"""Leak 6: dead inventory. Open lots in collections with no sale in 90 days or a zero floor: leak = basis."""
from leaks.base import LeakResult, Params, Prices


def is_dead(slug: str, prices: Prices, params: Params) -> bool:
    if prices.current_floor.get(slug) == 0:
        return True
    if slug not in prices.last_sale_ts:
        return False                     # never fetched: unknown, not dead
    last = prices.last_sale_ts[slug]
    return last is None or params.as_of_ts - last > params.dead_days * 86400


def compute(ledger, prices: Prices, params: Params) -> LeakResult:
    total, items = 0, []
    for l in ledger.open_lots():
        if not l.basis_wei or not is_dead(l.collection, prices, params):
            continue
        total += l.basis_wei
        items.append({"lot_id": l.lot_id, "collection": l.collection, "contract": l.contract,
                      "token_id": l.token_id, "day": params.as_of, "basis_wei": l.basis_wei, "wei": l.basis_wei})
    return LeakResult("dead_inventory", "0.5", total, items, {"no_sale_days": params.dead_days})
