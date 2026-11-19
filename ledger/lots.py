"""Lots: acquire on mint/buy/transfer-in, dispose on sale/transfer-out, FIFO for ERC-1155.

Venue-agnostic. Input is a list of LedgerEvent built by a venue adapter. All money is integer wei.
A lot is one token (ERC-721) or a run of FIFO units of one ERC-1155 token id, split on partial disposal.
"""
from dataclasses import dataclass, field

from ledger.basis import basis as basis_of, proceeds as proceeds_of
from ledger.fees import split_pro_rata

GIFT = "gift_or_airdrop"
BASIS_UNKNOWN = "basis_unknown"
UNPRICED_PAYMENT = "unpriced_payment"


@dataclass(frozen=True)
class LedgerEvent:
    ts: int                 # unix seconds
    tx_hash: str
    seq: int                # order within the tx
    kind: str               # "mint" | "buy" | "sale" | "transfer"
    contract: str
    token_id: str
    collection: str
    qty: int
    from_addr: str
    to_addr: str
    price_wei: int | None = 0   # gross price for all qty in this event; None = unknown payment token
    gas_wei: int = 0            # gas the owner paid, attributed to this event
    fee_wei: int = 0
    royalty_wei: int = 0
    sale_type: str | None = None   # "listing" | "bid" for sales/buys
    flags: tuple[str, ...] = ()


@dataclass
class Lot:
    lot_id: str
    contract: str
    token_id: str
    collection: str
    qty: int
    acquired_ts: int | None
    acquire_tx: str | None
    acquire_kind: str | None
    price_wei: int | None          # paid, excluding gas (total for qty)
    acquire_gas_wei: int
    basis_wei: int | None          # price + gas (total for qty)
    flags: list[str] = field(default_factory=list)
    disposed_ts: int | None = None
    dispose_tx: str | None = None
    dispose_kind: str | None = None   # "sale" | "sale_unpriced" | "disposed_unpriced"
    sale_price_wei: int | None = None
    fee_wei: int = 0
    royalty_wei: int = 0
    sale_gas_wei: int = 0
    proceeds_wei: int | None = None
    sale_type: str | None = None
    acquire_sale_type: str | None = None

    @property
    def is_open(self) -> bool:
        return self.disposed_ts is None

    @property
    def key(self) -> tuple[str, str]:
        return (self.contract, self.token_id)


@dataclass
class Ledger:
    owners: frozenset[str]
    lots: list[Lot]
    quality: dict[str, int]

    def open_lots(self) -> list[Lot]:
        return [l for l in self.lots if l.is_open]

    def open_keys(self) -> set[tuple[str, str]]:
        return {l.key for l in self.lots if l.is_open}


def _sort_key(e: LedgerEvent, owners: frozenset[str]):
    incoming = e.to_addr in owners and e.from_addr not in owners
    return (e.ts, e.tx_hash, 0 if incoming else 1, e.seq)


def _split_lot(lot: Lot, take: int, new_id: str) -> Lot:
    """Split `take` units off `lot` into a new Lot (returned); `lot` keeps the rest."""
    def part(v):
        if v is None:
            return None, None
        a, b = split_pro_rata(v, [take, lot.qty - take])
        return a, b
    taken = Lot(**{**lot.__dict__, "flags": list(lot.flags)})
    taken.lot_id = new_id
    taken.qty = take
    taken.price_wei, lot.price_wei = part(lot.price_wei)
    taken.basis_wei, lot.basis_wei = part(lot.basis_wei)
    taken.acquire_gas_wei, lot.acquire_gas_wei = part(lot.acquire_gas_wei)
    lot.qty -= take
    return taken


def replay(events: list[LedgerEvent], wallet: str, linked: list[str] | None = None) -> Ledger:
    owners = frozenset(a.lower() for a in [wallet, *(linked or [])])
    lots: list[Lot] = []
    open_by_key: dict[tuple[str, str], list[Lot]] = {}
    counter: dict[tuple[str, str], int] = {}
    q = {"internal_transfers": 0, "wash_trades": 0, "orphan_disposals": 0,
         "unpriced_sales": 0, "unpriced_disposals": 0, "gifts": 0}

    def next_id(key):
        counter[key] = counter.get(key, 0) + 1
        return f"{key[0]}:{key[1]}:{counter[key]}"

    for e in sorted(events, key=lambda e: _sort_key(e, owners)):
        frm, to = e.from_addr.lower(), e.to_addr.lower()
        key = (e.contract.lower(), str(e.token_id))
        if frm in owners and to in owners:
            q["wash_trades" if e.kind in ("sale", "buy") else "internal_transfers"] += 1
            continue
        if to in owners:
            flags = list(e.flags)
            price = e.price_wei
            if e.kind == "transfer" and price is None:
                price = 0
            if price is None:
                flags.append(UNPRICED_PAYMENT)
                q["unpriced_sales"] += 1
            elif price == 0 and e.gas_wei == 0 and e.kind in ("transfer", "mint"):
                flags.append(GIFT)
                q["gifts"] += 1
            lot = Lot(lot_id=next_id(key), contract=key[0], token_id=key[1], collection=e.collection,
                      qty=e.qty, acquired_ts=e.ts, acquire_tx=e.tx_hash, acquire_kind=e.kind,
                      price_wei=price, acquire_gas_wei=e.gas_wei, basis_wei=basis_of(price, e.gas_wei),
                      flags=flags, acquire_sale_type=e.sale_type if e.kind == "buy" else None)
            lots.append(lot)
            open_by_key.setdefault(key, []).append(lot)
            continue
        if frm not in owners:
            continue

        # Disposal, FIFO across open lots of this token.
        remaining = e.qty
        consumed: list[Lot] = []
        queue = open_by_key.get(key, [])
        while remaining > 0 and queue:
            lot = queue[0]
            if lot.qty <= remaining:
                queue.pop(0)
                consumed.append(lot)
                remaining -= lot.qty
            else:
                taken = _split_lot(lot, remaining, next_id(key))
                lots.append(taken)
                consumed.append(taken)
                remaining = 0
        if remaining > 0:
            q["orphan_disposals"] += 1
            orphan = Lot(lot_id=next_id(key), contract=key[0], token_id=key[1], collection=e.collection,
                         qty=remaining, acquired_ts=None, acquire_tx=None, acquire_kind=None,
                         price_wei=None, acquire_gas_wei=0, basis_wei=None, flags=[BASIS_UNKNOWN])
            lots.append(orphan)
            consumed.append(orphan)

        if e.kind == "sale" and e.price_wei is not None:
            kind = "sale"
        elif e.kind == "sale":
            kind = "sale_unpriced"
            q["unpriced_sales"] += 1
        else:
            kind = "disposed_unpriced"
            q["unpriced_disposals"] += 1

        weights = [l.qty for l in consumed]
        prices = split_pro_rata(e.price_wei or 0, weights)
        fees = split_pro_rata(e.fee_wei, weights)
        roys = split_pro_rata(e.royalty_wei, weights)
        gases = split_pro_rata(e.gas_wei, weights)
        for lot, p, f, r, g in zip(consumed, prices, fees, roys, gases):
            lot.disposed_ts = e.ts
            lot.dispose_tx = e.tx_hash
            lot.flags.extend(f for f in e.flags if f not in lot.flags)
            lot.dispose_kind = kind
            if kind == "sale":
                lot.sale_price_wei = p
                lot.fee_wei = f
                lot.royalty_wei = r
                lot.sale_gas_wei = g
                lot.proceeds_wei = proceeds_of(p, f, r, g)
                lot.sale_type = e.sale_type
            else:
                lot.sale_gas_wei = g

    return Ledger(owners=owners, lots=lots, quality=q)
