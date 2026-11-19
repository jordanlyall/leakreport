"""Fee attribution interface. Venue adapters (e.g. sources/chain.py for Seaport) implement it.

All amounts are integers in wei of the payment token.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SaleItem:
    contract: str
    token_id: str
    qty: int


@dataclass(frozen=True)
class FeeSplit:
    """How one fill's gross price divides. price = fee + royalty + seller (+ other)."""
    price_wei: int
    fee_wei: int
    royalty_wei: int
    seller_wei: int
    payment_token: str          # "0x0000000000000000000000000000000000000000" for native ETH
    sale_type: str              # "listing" (taker bought a listing) or "bid" (seller accepted an offer)
    items: tuple[SaleItem, ...] = field(default_factory=tuple)

    @property
    def other_wei(self) -> int:
        return self.price_wei - self.fee_wei - self.royalty_wei - self.seller_wei


class FeeAttributor(Protocol):
    def attribute(self, tx_hash: str, contract: str, token_id: str, seller: str) -> FeeSplit | None:
        ...


def split_pro_rata(total: int, weights: list[int]) -> list[int]:
    """Split an integer total by integer weights, exactly, deterministically.

    Largest-remainder method; ties go to the earlier index. Zero or empty weights split equally.
    """
    n = len(weights)
    if n == 0:
        return []
    if sum(weights) <= 0 or any(w < 0 for w in weights):
        weights = [1] * n
    wsum = sum(weights)
    base = [total * w // wsum for w in weights]
    rems = [(total * w % wsum, -i) for i, w in enumerate(weights)]
    short = total - sum(base)
    for _, neg_i in sorted(rems, reverse=True)[:short]:
        base[-neg_i] += 1
    return base
