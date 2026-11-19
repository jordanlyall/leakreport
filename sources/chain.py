"""Chain data: receipts, Seaport OrderFulfilled decode and fee attribution, tx enumeration, ENS.

Implements ledger.fees.FeeAttributor for Seaport.
"""
import unicodedata
import urllib.parse
from dataclasses import dataclass

from ledger.fees import FeeSplit, SaleItem
from sources.cache import Cache
from sources.http import Http, TokenBucket, urllib_transport
from sources.keccak import keccak256, namehash

ZERO = "0x" + "0" * 40
ORDER_FULFILLED = "0x" + keccak256(
    b"OrderFulfilled(bytes32,address,address,address,(uint8,address,uint256,uint256)[],"
    b"(uint8,address,uint256,uint256,address)[])").hex()

SEAPORT = {
    "0x00000000006c3852cbef3e08e8df289169ede581",  # 1.1
    "0x00000000000006c7676171937c444f6bde3d6282",  # 1.2
    "0x0000000000000ad24e80fd803c6ac37206a45f15",  # 1.3
    "0x00000000000001ad428e4906ae43d8f9852d0dd6",  # 1.4
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc",  # 1.5
    "0x0000000000000068f116a894984e2db1123eb395",  # 1.6
}
# OpenSea fee recipients seen in Seaport consideration items. Confirmed against fixture sales (see README).
OPENSEA_FEE_COLLECTORS = {
    "0x0000a26b00c1f0df003000390027140000faa719",
    "0x8de9c5a032463c561423387a9648c5c7bcc5bc90",
}
BLUR = {
    "0x000000000000ad05ccc4f10045630fb830b95127",  # Blur marketplace
    "0x39da41747a83aee658334415666f3ef92dd0d541",  # Blur marketplace v2 proxy
    "0xb2ecfe4e4d61f8790bbb9de2d1259b9e2410cea5",  # Blur exchange v2
    "0x29469395eaf6f95920e59f858042f0e28d98a20b",  # Blend
    "0x0000000000a39bb272e79075ade125fd351887ac",  # Blur pool
}
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
# setApprovalForAll operators that only exist to list on these venues.
MARKET_OPERATORS = {
    "0x1e0049783f008a0085193e00003d00cd54003c71",  # OpenSea conduit
    "0x00000000000111abe46ff893f3b2fdf1f759a8a8",  # Blur execution delegate
    *SEAPORT,
}
SET_APPROVAL_FOR_ALL = "0xa22cb465"

NATIVE, ERC20, ERC721, ERC1155, ERC721_CRIT, ERC1155_CRIT = range(6)
PAYMENT_TYPES = {NATIVE, ERC20}
NFT_TYPES = {ERC721, ERC1155, ERC721_CRIT, ERC1155_CRIT}


@dataclass(frozen=True)
class Item:
    item_type: int
    token: str
    identifier: int
    amount: int
    recipient: str | None = None


@dataclass(frozen=True)
class Fill:
    order_hash: str
    offerer: str
    zone: str
    recipient: str
    offer: tuple[Item, ...]
    consideration: tuple[Item, ...]
    log_index: int


def _addr(word: bytes) -> str:
    return "0x" + word[12:].hex()


def decode_order_fulfilled(log: dict) -> Fill:
    data = bytes.fromhex(log["data"][2:])
    w = lambda i: data[32 * i:32 * i + 32]
    u = lambda i: int.from_bytes(w(i), "big")

    def items(offset_word: int, width: int):
        start = u(offset_word) // 32
        n = u(start)
        out = []
        for k in range(n):
            b = start + 1 + k * width
            out.append(Item(u(b), _addr(w(b + 1)), u(b + 2), u(b + 3),
                            _addr(w(b + 4)) if width == 5 else None))
        return tuple(out)

    topics = log["topics"]
    return Fill(order_hash="0x" + w(0).hex(), offerer=_addr(bytes.fromhex(topics[1][2:])),
                zone=_addr(bytes.fromhex(topics[2][2:])), recipient=_addr(w(1)),
                offer=items(2, 4), consideration=items(3, 5), log_index=int(log.get("logIndex", "0x0"), 16))


def fills_from_receipt(receipt: dict) -> list[Fill]:
    return [decode_order_fulfilled(lg) for lg in receipt.get("logs", [])
            if lg["address"].lower() in SEAPORT and lg["topics"] and lg["topics"][0].lower() == ORDER_FULFILLED]


def _is_nft(it: Item, contract: str, token_id: str) -> bool:
    return it.item_type in NFT_TYPES and it.token == contract and str(it.identifier) == str(token_id)


def _nft_items(items) -> tuple[SaleItem, ...]:
    return tuple(SaleItem(i.token, str(i.identifier), i.amount) for i in items if i.item_type in NFT_TYPES)


def _token(items) -> str:
    pays = [i.token for i in items if i.item_type in PAYMENT_TYPES]
    return pays[0] if pays else ZERO


def attribute_sale(fills: list[Fill], contract: str, token_id: str, seller: str,
                   fee_collectors=OPENSEA_FEE_COLLECTORS) -> FeeSplit | None:
    """Fee split for the fill in which `seller` sold (contract, token_id).

    Bid (seller accepted an offer): the NFT is a consideration item owed to the offerer, the price is the
    offer's payment items, and fee/royalty are consideration payments. Listing: the NFT is in the offer of
    the seller's order and every consideration payment is part of the price.
    Fee = paid to an OpenSea fee collector, seller = paid to the seller, royalty = everything else.
    """
    contract, seller = contract.lower(), seller.lower()
    for f in fills:
        if any(_is_nft(c, contract, token_id) and c.recipient == f.offerer for c in f.consideration) \
                and any(o.item_type in PAYMENT_TYPES for o in f.offer) and f.offerer != seller:
            price = sum(o.amount for o in f.offer if o.item_type in PAYMENT_TYPES)
            pays = [c for c in f.consideration if c.item_type in PAYMENT_TYPES]
            fee = sum(c.amount for c in pays if c.recipient in fee_collectors)
            royalty = sum(c.amount for c in pays if c.recipient not in fee_collectors and c.recipient != seller)
            return FeeSplit(price, fee, royalty, price - fee - royalty, _token(f.offer), "bid",
                            _nft_items(f.consideration))
    for f in fills:
        if f.offerer == seller and any(_is_nft(o, contract, token_id) for o in f.offer):
            pays = [c for c in f.consideration if c.item_type in PAYMENT_TYPES]
            price = sum(c.amount for c in pays)
            fee = sum(c.amount for c in pays if c.recipient in fee_collectors)
            seller_amt = sum(c.amount for c in pays if c.recipient == seller)
            return FeeSplit(price, fee, price - fee - seller_amt, seller_amt, _token(f.consideration), "listing",
                            _nft_items(f.offer))
    return None


def gas_paid(receipt: dict) -> int:
    price = receipt.get("effectiveGasPrice") or receipt.get("gasPrice") or "0x0"
    return int(receipt["gasUsed"], 16) * int(price, 16)


class SeaportAttributor:
    """ledger.fees.FeeAttributor backed by cached receipts."""

    def __init__(self, cache: Cache):
        self.cache = cache

    def attribute(self, tx_hash, contract, token_id, seller):
        r = self.cache.receipt(tx_hash)
        return attribute_sale(fills_from_receipt(r), contract, token_id, seller) if r else None


# ---- network ----

class Rpc:
    def __init__(self, url: str, transport=urllib_transport, bucket: TokenBucket | None = None):
        self.url = url
        self.http = Http(transport=transport, bucket=bucket or TokenBucket(10.0, 10.0))
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        res = self.http.request("POST", self.url, {"jsonrpc": "2.0", "id": self._id, "method": method,
                                                   "params": params}, redact=f"rpc {method}")
        if res.get("error"):
            raise RuntimeError(f"rpc {method}: {res['error']}")
        return res.get("result")


# Free keyless full-history RPC for receipts the configured node lacks. Blockscout's eth-rpc allows ~10 calls
# per 40 minutes, and publicnode is pruned like most free nodes.
FALLBACK_RPC = ("drpc", "https://eth.drpc.org")


def sync_receipts(cache: Cache, rpc: Rpc, tx_hashes, fallback: Rpc | None = None) -> dict:
    """Receipts from the RPC; ones it lacks (pruned nodes keep only recent txs) come from `fallback`.
    A receipt neither has is left uncached so the next --refresh retries it."""
    stats = {"fetched": 0, "from_fallback": 0, "missing": 0}
    for h in sorted({h.lower() for h in tx_hashes if h}):
        if cache.has_receipt(h):
            continue
        r = rpc.call("eth_getTransactionReceipt", [h])
        if r is None and fallback is not None:
            r = fallback.call("eth_getTransactionReceipt", [h])
            stats["from_fallback"] += r is not None
        if r is None:
            stats["missing"] += 1
            continue
        cache.put_receipt(h, r)
        stats["fetched"] += 1
    return stats


BLOCKSCOUT = "https://eth.blockscout.com/api"


def sync_txlist(cache: Cache, http: Http, address: str) -> int:
    """Wallet's transactions (incl. failed) from Blockscout's free Etherscan-compatible txlist."""

    address = address.lower()
    have = cache.txs(address)
    start = max((int(t["blockNumber"]) for t in have), default=0)
    total, page = 0, 1   # page stays 1: page*offset is capped upstream, so advance startblock instead
    while True:
        q = urllib.parse.urlencode({"module": "account", "action": "txlist", "address": address,
                                    "startblock": start, "sort": "asc", "page": page, "offset": 1000})
        data = http.get(f"{BLOCKSCOUT}?{q}", redact="blockscout txlist") or {}
        rows = data.get("result") or []
        if not isinstance(rows, list):
            break
        cache.put_txs(address, rows)
        total += len(rows)
        last = max((int(r["blockNumber"]) for r in rows), default=start)
        if len(rows) < 1000 or last == start:
            break
        start = last      # re-reads the last block; upserts make the overlap harmless
    return total


def _eth_call(rpc: Rpc, to: str, data: str) -> bytes:
    res = rpc.call("eth_call", [{"to": to, "data": data}, "latest"])
    return bytes.fromhex((res or "0x")[2:])


# ENS Universal Resolver: follows the registry, wildcard and newer ENS contracts, and forward-verifies reverse
# names. The legacy registry alone misses names managed through newer contracts (it had no resolver for one).
UNIVERSAL_RESOLVER = "0xeeeeeeee14d718c2b47d9923deab1335e144eeee"
_RESOLVE = "0x" + keccak256(b"resolve(bytes,bytes)")[:4].hex()
_REVERSE = "0x" + keccak256(b"reverse(bytes,uint256)")[:4].hex()


def _abi_bytes(b: bytes) -> bytes:
    return len(b).to_bytes(32, "big") + b + b"\0" * ((32 - len(b) % 32) % 32)


def _dyn(out: bytes, i: int) -> bytes:
    """The i-th head word of an ABI result, read as an offset to a dynamic bytes/string."""
    off = int.from_bytes(out[32 * i:32 * i + 32], "big")
    return out[off + 32: off + 32 + int.from_bytes(out[off:off + 32], "big")]


def _ur_call(rpc: Rpc, data: str) -> bytes | None:
    try:
        return _eth_call(rpc, UNIVERSAL_RESOLVER, data)
    except RuntimeError:          # the resolver reverts for names or addresses with no record
        return None


def dns_encode(name: str) -> bytes | None:
    """DNS wire format with byte-length labels; None for an empty or over-long label. Lowercasing is the only
    normalisation (no full ENSIP-15), and the same string feeds namehash."""
    out = b""
    for label in name.lower().split("."):
        lb = label.encode("utf-8")
        if not 0 < len(lb) <= 255:
            return None
        out += bytes([len(lb)]) + lb
    return out + b"\0"


def resolve_ens(rpc: Rpc, name: str) -> str | None:
    name = name.lower()
    dns = dns_encode(name)
    if dns is None:
        return None
    a, b = _abi_bytes(dns), _abi_bytes(bytes.fromhex("3b3b57de") + namehash(name))
    out = _ur_call(rpc, _RESOLVE + (64).to_bytes(32, "big").hex() + (64 + len(a)).to_bytes(32, "big").hex()
                   + a.hex() + b.hex())
    res = _dyn(out, 0) if out and len(out) >= 64 else b""
    addr = _addr(res[:32]) if len(res) >= 32 else ZERO
    return None if addr == ZERO else addr


def reverse_ens(rpc: Rpc, address: str) -> str | None:
    out = _ur_call(rpc, _REVERSE + (64).to_bytes(32, "big").hex() + (60).to_bytes(32, "big").hex()
                   + _abi_bytes(bytes.fromhex(address.lower()[2:])).hex())
    name = _dyn(out, 0).decode("utf-8", "replace") if out and len(out) >= 96 else ""
    # The name is whatever the address owner set on-chain: drop control and format characters so it
    # can't carry terminal escapes into the printed report or the card.
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf"))
    return name or None
