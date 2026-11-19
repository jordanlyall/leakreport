"""Assembles the report JSON (the single source of truth) and the terminal table."""
import json
from pathlib import Path

from leaks import (bought_at_top, concentration, dead_inventory, fee_drag, held_past_floor, listing_churn,
                   sold_into_bids)
from leaks.base import ETH, LeakResult, Params, Prices, count_each_lot_once, day_of
from ledger import pnl
from ledger.lots import Ledger
from score import score

LEAK_MODULES = [fee_drag, held_past_floor, bought_at_top, listing_churn, sold_into_bids, dead_inventory]
LEAK_NAMES = {"fee_drag": "Fee drag", "held_past_floor": "Held past floor", "bought_at_top": "Bought at top",
              "listing_churn": "Listing churn", "sold_into_bids": "Sold into bids",
              "dead_inventory": "Dead inventory"}
SCHEMA_PATH = Path(__file__).with_name("report.schema.json")


def eth(wei: int | None) -> float | None:
    return None if wei is None else round(wei / ETH, 6)


def _convert(d: dict) -> dict:
    """Turn *_wei ints into *_eth floats; `wei` becomes `eth`."""
    out = {}
    for k, v in d.items():
        if k == "wei":
            out["eth"] = eth(v)
        elif k.endswith("_wei") and isinstance(v, int):
            out[k[:-4] + "_eth"] = eth(v)
        elif k.endswith("_wei") and isinstance(v, dict):
            out[k[:-4] + "_eth"] = {kk: eth(vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def _usd(prices: Prices, wei: int, day: str) -> float:
    rate = prices.usd_rate(day)
    return 0.0 if rate is None else wei / ETH * rate


def _strength(results: list[LeakResult], deployed: int, fee: LeakResult) -> dict:
    d = fee.detail
    vol = d["sale_volume_wei"]
    texts = {
        "fee_drag": (f"paid {d['fee_total_wei'] / vol:.1%} in fees vs the {float(d['benchmark_rate']):.1%} benchmark"
                     if vol else "no marketplace fees paid"),
        "held_past_floor": "cut losers before the floor fell away",
        "bought_at_top": "bought near the 30-day floor",
        "listing_churn": "little gas burnt on listing churn",
        "sold_into_bids": "rarely sold into bids under the floor",
        "dead_inventory": "no capital stuck in dead collections",
    }
    order = [r.id for r in results]
    pool = [r for r in results if vol or r.id not in ("fee_drag", "sold_into_bids")]   # no sales: zero by default
    best = min(pool, key=lambda r: (r.wei, order.index(r.id)))
    return {"id": best.id, "text": texts[best.id]}


def build_report(ledger: Ledger, prices: Prices, params: Params, wallet: dict, quality: dict,
                 sources: dict, floor_sources: dict[str, str]) -> dict:
    results = count_each_lot_once([m.compute(ledger, prices, params) for m in LEAK_MODULES])
    flag = concentration.compute(ledger, prices, params)
    since_ts = params.since_ts
    in_scope = [l for l in ledger.lots if pnl.in_scope(l, since_ts)]
    deployed = pnl.deployed(ledger, since_ts)
    acquisitions = sum(1 for l in in_scope if l.basis_wei)
    sc = score(results, deployed, acquisitions)
    fee = results[0]

    leaks_json = []
    for r in results:
        items = []
        for it in sorted(r.items, key=lambda i: (-i["wei"], i.get("lot_id") or i.get("tx") or "")):
            j = _convert(it)
            j["usd"] = round(_usd(prices, it["wei"], it["day"]), 2)
            items.append(j)
        usd = round(sum(_usd(prices, it["wei"], it["day"]) for it in r.items), 2)
        leaks_json.append({"id": r.id, "name": LEAK_NAMES[r.id], "eth": eth(r.wei), "usd": usd,
                           "weight": float(r.weight), "detail": _convert(r.detail), "items": items})

    gas = (sum(l.acquire_gas_wei for l in in_scope if l.acquired_ts and l.acquired_ts >= since_ts)
           + sum(l.sale_gas_wei for l in in_scope if l.disposed_ts is not None)
           + results[3].wei)
    totals = {"deployed_eth": eth(deployed), "realized_pnl_eth": eth(pnl.realized(ledger, since_ts)),
              "unrealized_pnl_eth": eth(pnl.unrealized(ledger, prices.current_floor)),
              "fees_eth": eth(fee.detail["fee_total_wei"]), "royalties_eth": eth(fee.detail["royalty_total_wei"]),
              "gas_eth": eth(gas)}

    per_coll: dict[str, dict] = {}
    for l in in_scope:
        c = per_coll.setdefault(l.collection, {"slug": l.collection, "lots": 0, "open_lots": 0, "basis_wei": 0})
        c["lots"] += 1
        c["open_lots"] += 1 if l.is_open else 0
        c["basis_wei"] += l.basis_wei or 0
    for r in results:
        for it in r.items:
            if it.get("collection") in per_coll:
                lk = per_coll[it["collection"]].setdefault("leaks_wei", {})
                lk[r.id] = lk.get(r.id, 0) + it["wei"]
    collections = []
    for slug, c in sorted(per_coll.items(), key=lambda kv: (-kv[1]["basis_wei"], kv[0])):
        collections.append({"slug": slug, "lots": c["lots"], "open_lots": c["open_lots"],
                            "basis_eth": eth(c["basis_wei"]), "floor_eth": eth(prices.current_floor.get(slug)),
                            "floor_source": floor_sources.get(slug, "none"),
                            "leaks_eth": {k: eth(v) for k, v in sorted(c.get("leaks_wei", {}).items())}})

    lots = []
    for l in sorted(in_scope, key=lambda l: (l.acquired_ts or 0, l.lot_id)):
        lots.append({
            "lot_id": l.lot_id, "contract": l.contract, "token_id": l.token_id, "collection": l.collection,
            "qty": l.qty, "acquired": day_of(l.acquired_ts) if l.acquired_ts else None,
            "acquire_kind": l.acquire_kind, "acquire_tx": l.acquire_tx, "basis_eth": eth(l.basis_wei),
            "price_eth": eth(l.price_wei), "acquire_gas_eth": eth(l.acquire_gas_wei), "flags": sorted(l.flags),
            "disposed": day_of(l.disposed_ts) if l.disposed_ts else None, "dispose_kind": l.dispose_kind,
            "dispose_tx": l.dispose_tx, "sale_type": l.sale_type, "sale_price_eth": eth(l.sale_price_wei),
            "proceeds_eth": eth(l.proceeds_wei), "fees_eth": eth(l.fee_wei), "royalty_eth": eth(l.royalty_wei),
            "sale_gas_eth": eth(l.sale_gas_wei)})

    used = {floor_sources.get(c["slug"], "none") for c in collections if c["open_lots"] or c["lots"]}
    used.discard("none")
    floor_source = used.pop() if len(used) == 1 else ("mixed" if used else "none")
    in_period = [l for l in ledger.lots if l.disposed_ts is not None and l.disposed_ts >= since_ts]
    q = {"unpriced_sales": sum(1 for l in in_period if l.dispose_kind == "sale_unpriced")
                           + sum(1 for l in in_scope if "unpriced_payment" in l.flags),
         "unpriced_disposals": sum(1 for l in in_period if l.dispose_kind == "disposed_unpriced"),
         "unscored_basis_eth": eth(pnl.unscored_basis(ledger, since_ts)),
         "valuation": "collection_level", "floor_source": floor_source,
         "orphan_disposals": sum(1 for l in in_period if "basis_unknown" in l.flags),
         "wash_trades": ledger.quality["wash_trades"], **quality}

    return {
        "wallet": wallet,
        "period": {"from": params.since, "to": params.as_of},
        "totals": totals,
        "score": {"value": sc["value"], "low_sample": sc["low_sample"], "acquisitions": acquisitions},
        "leaks": leaks_json,
        "strength": _strength(results, deployed, fee),
        "flags": {"concentration": flag},
        "collections": collections,
        "lots": lots,
        "quality": dict(sorted(q.items())),
        "sources": sources,
    }


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def top_leaks(report: dict, n: int = 3) -> list[dict]:
    return sorted(report["leaks"], key=lambda l: (-(l["eth"] or 0), l["id"]))[:n]


def table(report: dict) -> str:
    w = report["wallet"]
    who = w.get("ens") or w["address"]
    lines = [f"Leak report  {who}  {report['period']['from']} → {report['period']['to']}",
             f"Score {report['score']['value']}/100" + ("  (low sample)" if report["score"]["low_sample"] else ""),
             "", f"{'Leak':<18}{'ETH':>12}{'USD':>14}{'Weight':>8}"]
    for l in report["leaks"]:
        lines.append(f"{l['name']:<18}{l['eth']:>12.4f}{l['usd']:>14,.0f}{l['weight']:>8.1f}")
    t = report["totals"]
    c = report["flags"]["concentration"]
    lines += ["", f"Deployed {t['deployed_eth']:.4f} ETH · realised P&L {t['realized_pnl_eth']:+.4f} ETH"
                  f" · fees {t['fees_eth']:.4f} · royalties {t['royalties_eth']:.4f} · gas {t['gas_eth']:.4f}",
              f"Concentration: top1 {c['top1_share']:.0%}, top3 {c['top3_share']:.0%} ({c['level']})",
              f"Strength: {report['strength']['text']}"]
    q = report["quality"]
    lines.append(f"Quality: floor source {q['floor_source']}, unpriced sales {q['unpriced_sales']}, "
                 f"unpriced disposals {q['unpriced_disposals']}, missing receipts {q.get('events_missing_receipt', 0)}")
    return "\n".join(lines)
