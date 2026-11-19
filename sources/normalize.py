"""Venue adapter: cached OpenSea events + receipts + tx list -> ledger events and listing-churn txs."""
from dataclasses import dataclass, field
from decimal import Decimal

from ledger.fees import split_pro_rata
from ledger.lots import LedgerEvent
from leaks.base import ChurnTx, day_of
from sources.cache import Cache
from sources.chain import (BLUR, MARKET_OPERATORS, SEAPORT, SET_APPROVAL_FOR_ALL, WETH, ZERO, attribute_sale,
                           fills_from_receipt, gas_paid)
from sources.opensea import event_key, nft_of, parties, payment_wei

ETH_TOKENS = {ZERO, WETH}
STABLES = {  # token -> decimals; priced at $1 and converted at the day's ETH/USD close
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d82b19ba1ad0e3a5a"
# OpenSea Shared Storefront: OpenSea's feed lists some trades twice, once on the legacy contract and once on its
# successor; only the successor appears in the Seaport fill. Keep the legacy row (the NFTs endpoint lists holdings
# there) and decode its price from the successor's fill.
SHARED_STOREFRONT = "0x495f947276749ce646f68ac8c248420045cb7b5e"
SHARED_STOREFRONT_NEXT = "0xa604060890923ff400e8c6f5290461a83aedacec"
CANCEL_SELECTORS = {"0xfd9f1e10": "cancel", "0x5b34b966": "cancel"}   # cancel(), incrementCounter()


@dataclass
class Normalized:
    events: list[LedgerEvent]
    churn: list[ChurnTx]
    quality: dict = field(default_factory=dict)
    tx_hashes: set = field(default_factory=set)


def account_events(cache: Cache, owners: list[str]) -> list[dict]:
    """Union of owners' event feeds, de-duplicated, with transfers already covered by a sale dropped."""
    seen, out = set(), []
    for o in owners:
        for e in cache.events(f"account:{o.lower()}"):
            k = event_key(e)
            if k not in seen:
                seen.add(k)
                out.append(e)
    sold = {_transfer_key(e) for e in out if e.get("event_type") == "sale"}
    out = [e for e in out if e.get("event_type") == "sale" or _transfer_key(e) not in sold]
    legacy = {_twin_key(e) for e in out if (nft_of(e).get("contract") or "").lower() == SHARED_STOREFRONT}
    return [e for e in out if not ((nft_of(e).get("contract") or "").lower() == SHARED_STOREFRONT_NEXT
                                   and _twin_key(e) in legacy)]


def _twin_key(e):
    return (e.get("event_type"), (e.get("transaction") or "").lower(), str(nft_of(e).get("identifier")))


def _transfer_key(e):
    n = nft_of(e)
    frm, to = parties(e)
    return ((e.get("transaction") or "").lower(), (n.get("contract") or "").lower(), str(n.get("identifier")),
            frm, to)


def wanted_receipts(cache: Cache, owners: list[str]) -> set[str]:
    hashes = {(e.get("transaction") or "").lower() for e in account_events(cache, owners)}
    for o in owners:
        for t in cache.txs(o):
            if _churn_candidate(t, set(owners)):
                hashes.add(t["hash"].lower())
    hashes.discard("")
    return hashes


def _churn_candidate(t: dict, owners: set[str]) -> str | None:
    frm, to = (t.get("from") or "").lower(), (t.get("to") or "").lower()
    if frm not in owners or not to:
        return None
    inp = (t.get("input") or "0x").lower()
    if to in SEAPORT or to in BLUR:
        return "venue"
    if to == WETH:
        return "weth"
    if inp.startswith(SET_APPROVAL_FOR_ALL) and len(inp) >= 74 and "0x" + inp[34:74] in MARKET_OPERATORS:
        return "approval"
    return None


def _nft_moves_owner(receipt: dict, owners: set[str]) -> bool:
    for lg in receipt.get("logs", []):
        t = [x.lower() for x in lg.get("topics", [])]
        if not t:
            continue
        if t[0] == TRANSFER_TOPIC and len(t) == 4:          # ERC-721 (ERC-20 has 3 topics)
            ends = {"0x" + t[1][-40:], "0x" + t[2][-40:]}
        elif t[0] in (TRANSFER_SINGLE, TRANSFER_BATCH) and len(t) == 4:
            ends = {"0x" + t[2][-40:], "0x" + t[3][-40:]}
        else:
            continue
        if ends & owners:
            return True
    return False


def churn_txs(cache: Cache, owners: list[str]) -> list[ChurnTx]:
    owner_set = {o.lower() for o in owners}
    out, seen = [], set()
    for o in owners:
        for t in cache.txs(o):
            kind = _churn_candidate(t, owner_set)
            h = t["hash"].lower()
            if not kind or h in seen:
                continue
            seen.add(h)
            r = cache.receipt(h)
            failed = str(t.get("isError")) == "1" or (r is not None and r.get("status") == "0x0")
            if r is not None:
                gas = gas_paid(r)
            else:
                gas = int(t.get("gasUsed") or 0) * int(t.get("gasPrice") or 0)
            if failed:
                reason = "failed"
            elif kind == "approval":
                reason = "approval"
            elif kind == "weth":
                inp = (t.get("input") or "0x").lower()
                reason = "approval" if inp.startswith("0x095ea7b3") else "weth"
            else:
                if r is None or fills_from_receipt(r) or _nft_moves_owner(r, owner_set):
                    continue      # a fulfilment (or unknown): not churn
                reason = CANCEL_SELECTORS.get((t.get("input") or "")[:10].lower(), "other")
            out.append(ChurnTx(h, int(t["timeStamp"]), (t.get("to") or "").lower(), gas, reason))
    return sorted(out, key=lambda c: (c.ts, c.tx_hash))


def _logged_qty(receipt: dict | None, contract: str, token: str, frm: str, to: str) -> int | None:
    """Amount of an ERC-1155 TransferSingle for (contract, id, from, to) in the receipt, if there is one."""
    for lg in (receipt or {}).get("logs", []):
        t = [x.lower() for x in lg.get("topics", [])]
        if (len(t) == 4 and t[0] == TRANSFER_SINGLE and (lg.get("address") or "").lower() == contract
                and "0x" + t[2][-40:] == frm and "0x" + t[3][-40:] == to):
            data = bytes.fromhex(lg["data"][2:])
            if str(int.from_bytes(data[:32], "big")) == token:
                return int.from_bytes(data[32:64], "big")
    return None


def _to_wei_eth(amount: int, token: str, day: str, usd_rate) -> int | None:
    token = token.lower()
    if token in ETH_TOKENS:
        return amount
    if token in STABLES:
        rate = usd_rate(day)
        if not rate:
            return None
        usd = Decimal(amount) / (Decimal(10) ** STABLES[token])
        return int(usd / Decimal(str(rate)) * Decimal(10**18))
    return None


def normalize(cache: Cache, owners: list[str], floor_at, usd_rate) -> Normalized:
    """floor_at(slug, day) -> wei|None (bundle weights); usd_rate(day) -> float|None (stablecoin sales)."""
    owners = [o.lower() for o in owners]
    owner_set = set(owners)
    evs = account_events(cache, owners)
    coll_of = {((nft_of(e).get("contract") or "").lower(), str(nft_of(e).get("identifier"))):
               nft_of(e).get("collection") or "unknown" for e in evs}
    txmap = {t["hash"].lower(): t for o in owners for t in cache.txs(o)}
    q = {"events_missing_receipt": 0, "sales_without_seaport_decode": 0, "bundles": 0}

    rows = []   # (event, kind, contract, token, qty, frm, to, tx)
    qty_unknown: set[int] = set()
    for e in evs:
        n = nft_of(e)
        frm, to = parties(e)
        if not ({frm, to} & owner_set):
            continue
        et = e.get("event_type")
        if et == "sale":
            kind = "sale" if frm in owner_set else "buy"
        elif et == "mint" or frm == ZERO or e.get("transfer_type") == "mint":
            kind = "mint"
        else:
            kind = "transfer"
        contract, token, tx = (n.get("contract") or "").lower(), str(n.get("identifier")), (e.get("transaction") or "").lower()
        qty = int(e.get("quantity") or 1)
        if qty < 1:      # OpenSea wraps huge ERC-1155 amounts into negative int64s: read the transfer log instead
            qty = _logged_qty(cache.receipt(tx) if tx else None, contract, token, frm, to)
            if qty is None:
                qty = 1
                qty_unknown.add(len(rows))
        rows.append((e, kind, contract, token, qty, frm, to, tx))

    # Gas: the tx sender pays; split equally across the owner's items in that tx (by units).
    per_tx: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        per_tx.setdefault(r[7], []).append(i)
    gas_of = [0] * len(rows)
    value_of = [0] * len(rows)
    for tx, idxs in per_tx.items():
        rec = cache.receipt(tx) if tx else None
        if rec is None:
            if any(rows[i][1] in ("sale", "buy", "mint") for i in idxs):
                q["events_missing_receipt"] += 1
            continue
        if (rec.get("from") or "").lower() not in owner_set:
            continue
        units = [rows[i][4] for i in idxs]
        for i, g in zip(idxs, split_pro_rata(gas_paid(rec), units)):
            gas_of[i] = g
        val = int(txmap.get(tx, {}).get("value") or 0)
        pay_idxs = [i for i in idxs if rows[i][1] in ("mint", "transfer") and rows[i][6] in owner_set]
        if val and pay_idxs:
            for i, v in zip(pay_idxs, split_pro_rata(val, [rows[i][4] for i in pay_idxs])):
                value_of[i] = v

    out = []
    for i, (e, kind, contract, token, qty, frm, to, tx) in enumerate(rows):
        ts = int(e.get("event_timestamp") or 0)
        day = day_of(ts)
        slug = coll_of.get((contract, token), "unknown")
        price, fee, roy, sale_type, flags = 0, 0, 0, None, (["qty_unknown"] if i in qty_unknown else [])
        if kind in ("sale", "buy"):
            seller = frm
            rec = cache.receipt(tx) if tx else None
            fill_contract = contract
            split = attribute_sale(fills_from_receipt(rec), contract, token, seller) if rec else None
            if split is None and rec and contract == SHARED_STOREFRONT:
                fill_contract = SHARED_STOREFRONT_NEXT
                split = attribute_sale(fills_from_receipt(rec), fill_contract, token, seller)
            if split:
                sale_type = split.sale_type
                p, f, r = split.price_wei, split.fee_wei, split.royalty_wei
                if len(split.items) > 1:
                    q["bundles"] += 1
                    flags.append("bundle")
                    floors = [floor_at(coll_of.get((it.contract, it.token_id), "unknown"), day) for it in split.items]
                    weights = ([(fl or 0) * it.qty for fl, it in zip(floors, split.items)]
                               if all(floors) else [it.qty for it in split.items])
                    k = next(j for j, it in enumerate(split.items)
                             if it.contract == fill_contract and it.token_id == token)
                    p, f, r = (split_pro_rata(v, weights)[k] for v in (p, f, r))
                price = _to_wei_eth(p, split.payment_token, day, usd_rate)
                if price is not None and split.payment_token.lower() not in ETH_TOKENS:
                    f = _to_wei_eth(f, split.payment_token, day, usd_rate)
                    r = _to_wei_eth(r, split.payment_token, day, usd_rate)
                fee, roy = (f, r) if kind == "sale" else (0, 0)
            else:
                q["sales_without_seaport_decode"] += 1
                flags.append("fees_unknown")
                pay = e.get("payment") or {}
                price = _to_wei_eth(payment_wei(pay), pay.get("token_address") or ZERO, day, usd_rate)
        elif kind == "mint":
            price = value_of[i]
        else:
            price = value_of[i] if to in owner_set else 0
            if price:
                flags.append("paid_unattributed")
        out.append(LedgerEvent(ts=ts, tx_hash=tx, seq=i, kind=kind, contract=contract, token_id=token,
                               collection=slug, qty=qty, from_addr=frm, to_addr=to, price_wei=price,
                               gas_wei=gas_of[i], fee_wei=fee or 0, royalty_wei=roy or 0, sale_type=sale_type,
                               flags=tuple(flags)))
    return Normalized(events=out, churn=churn_txs(cache, owners), quality=q,
                      tx_hashes={r[7] for r in rows if r[7]})
