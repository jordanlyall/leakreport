"""Realised and unrealised P&L over lots, in integer wei."""
from ledger.lots import Ledger, Lot


def in_scope(lot: Lot, since_ts: int) -> bool:
    """A lot is in the report period unless it was disposed before the period began."""
    return lot.disposed_ts is None or lot.disposed_ts >= since_ts


def realized(ledger: Ledger, since_ts: int) -> int:
    """Σ (proceeds − basis) over priced sales in the period whose basis is known."""
    return sum(l.proceeds_wei - l.basis_wei for l in ledger.lots
               if l.dispose_kind == "sale" and l.disposed_ts >= since_ts
               and l.basis_wei is not None and l.proceeds_wei is not None)


def unrealized(ledger: Ledger, current_floor_wei: dict[str, int | None]) -> int:
    """Σ (current floor × qty − basis) over open lots with known basis and a known floor."""
    total = 0
    for l in ledger.open_lots():
        floor = current_floor_wei.get(l.collection)
        if l.basis_wei is None or floor is None:
            continue
        total += floor * l.qty - l.basis_wei
    return total


UNSCORED = ("disposed_unpriced", "sale_unpriced")   # left the wallet at a price no leak can see


def deployed(ledger: Ledger, since_ts: int) -> int:
    """Σ basis over in-scope lots with non-zero basis that the leaks can judge (the score denominator)."""
    return sum(l.basis_wei for l in ledger.lots
               if in_scope(l, since_ts) and l.basis_wei and l.dispose_kind not in UNSCORED)


def unscored_basis(ledger: Ledger, since_ts: int) -> int:
    """Basis left out of `deployed` because the lot exited unpriced."""
    return sum(l.basis_wei for l in ledger.lots
               if in_scope(l, since_ts) and l.basis_wei and l.dispose_kind in UNSCORED)
