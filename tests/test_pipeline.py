"""Synthetic end-to-end: cache -> normalize -> ledger -> report, no network."""
import json

import jsonschema

import cli
from leaks.base import day_ts
from report import SCHEMA_PATH, dumps
from sources.cache import Cache
from sources.chain import WETH
from tests.seaport_encode import encode_log

ETH = 10**18
W = "0x" + "a" * 40
X = "0x" + "c" * 40
CRE = "0x" + "d" * 40
FEE = "0x0000a26b00c1f0df003000390027140000faa719"
SP = "0x00000000000000adc04c56bf30ac9d3c0aaf14dc"
NFT = "0x" + "1" * 40
Z = "0x" + "0" * 40


def h(n):
    return "0x" + f"{n:064x}"


def receipt(frm, logs=(), gas_used=100_000, price=10**10, status="0x1"):
    return {"from": frm, "gasUsed": hex(gas_used), "effectiveGasPrice": hex(price), "status": status,
            "logs": list(logs)}


def sale_ev(tx, day, token, seller, buyer, price, token_addr=Z):
    return {"event_type": "sale", "transaction": tx, "event_timestamp": day_ts(day) + 600, "seller": seller,
            "buyer": buyer, "quantity": 1, "order_hash": "0x" + "e" * 64,
            "payment": {"quantity": str(price), "token_address": token_addr, "decimals": 18},
            "nft": {"contract": NFT, "identifier": token, "collection": "alpha"}}


def xfer_ev(tx, day, token, frm, to, et="transfer"):
    return {"event_type": et, "transaction": tx, "event_timestamp": day_ts(day) + 600, "from_address": frm,
            "to_address": to, "quantity": 1, "nft": {"contract": NFT, "identifier": token, "collection": "alpha"}}


def build_cache(path):
    c = Cache(path)
    evs = [
        # mint token 1 for 0.1 ETH, wallet sends tx
        xfer_ev(h(1), "2023-01-05", "1", Z, W, et="mint"),
        # buy token 2 for 1 ETH on a listing, wallet sends tx
        sale_ev(h(2), "2023-01-10", "2", X, W, ETH),
        # sell token 1 on a listing for 2 ETH (buyer sends tx): fee 0.05 to OpenSea, 0.1 royalty
        sale_ev(h(3), "2023-03-01", "1", W, X, 2 * ETH),
        # accept a WETH bid of 0.5 for token 2 (wallet sends tx): fee 0.0125, royalty 0.025
        sale_ev(h(4), "2023-04-01", "2", W, X, ETH // 2, WETH),
        # gift token 3 in, buy tokens 4+5 as... no: sell 4 and 5 as a bundle for 3 ETH
        xfer_ev(h(5), "2023-01-02", "3", X, W),
        xfer_ev(h(6), "2023-01-03", "4", X, W),
        xfer_ev(h(7), "2023-01-03", "5", X, W),
        sale_ev(h(8), "2023-05-01", "4", W, X, 3 * ETH),
        sale_ev(h(8), "2023-05-01", "5", W, X, 3 * ETH),
        # the transfer half of a sale must be dropped as a duplicate
        xfer_ev(h(3), "2023-03-01", "1", W, X),
    ]
    c.upsert_events(f"account:{W}", [(f"k{i}", e["event_type"], e["event_timestamp"], e["transaction"], e)
                                     for i, e in enumerate(evs)])
    c.put_txs(W, [
        {"hash": h(1), "from": W, "to": NFT, "value": str(ETH // 10), "isError": "0", "input": "0x1249c58b",
         "timeStamp": str(day_ts("2023-01-05") + 600), "blockNumber": "1", "gasUsed": "1", "gasPrice": "1"},
        {"hash": h(2), "from": W, "to": SP, "value": str(ETH), "isError": "0", "input": "0xfb0f3ee1",
         "timeStamp": str(day_ts("2023-01-10") + 600), "blockNumber": "2", "gasUsed": "1", "gasPrice": "1"},
        {"hash": h(4), "from": W, "to": SP, "value": "0", "isError": "0", "input": "0x87201b41",
         "timeStamp": str(day_ts("2023-04-01") + 600), "blockNumber": "4", "gasUsed": "1", "gasPrice": "1"},
        {"hash": h(9), "from": W, "to": SP, "value": "0", "isError": "0", "input": "0xfd9f1e10",
         "timeStamp": str(day_ts("2023-02-01")), "blockNumber": "9", "gasUsed": "1", "gasPrice": "1"},
        {"hash": h(10), "from": W, "to": WETH, "value": str(ETH), "isError": "0", "input": "0xd0e30db0",
         "timeStamp": str(day_ts("2023-02-02")), "blockNumber": "10", "gasUsed": "1", "gasPrice": "1"},
    ])
    g = 100_000 * 10**10   # 0.001 ETH
    c.put_receipt(h(1), receipt(W))
    c.put_receipt(h(2), receipt(W, [encode_log(SP, "0x" + "1" * 64, X, Z, W, [(2, NFT, 2, 1)],
                                               [(0, Z, 0, 975 * ETH // 1000, X), (0, Z, 0, 25 * ETH // 1000, FEE)])]))
    c.put_receipt(h(3), receipt(X, [encode_log(SP, "0x" + "2" * 64, W, Z, X, [(2, NFT, 1, 1)],
                                               [(0, Z, 0, 185 * ETH // 100, W), (0, Z, 0, 5 * ETH // 100, FEE),
                                                (0, Z, 0, ETH // 10, CRE)])]))
    c.put_receipt(h(4), receipt(W, [encode_log(SP, "0x" + "3" * 64, X, Z, W, [(1, WETH, 0, ETH // 2)],
                                               [(2, NFT, 2, 1, X), (1, WETH, 0, 125 * ETH // 10000, FEE),
                                                (1, WETH, 0, 25 * ETH // 1000, CRE)])]))
    c.put_receipt(h(5), receipt(X))
    c.put_receipt(h(6), receipt(X))
    c.put_receipt(h(7), receipt(X))
    c.put_receipt(h(8), receipt(X, [encode_log(SP, "0x" + "4" * 64, W, Z, X, [(2, NFT, 4, 1), (2, NFT, 5, 1)],
                                               [(0, Z, 0, 29 * ETH // 10, W), (0, Z, 0, ETH // 10, FEE)])]))
    c.put_receipt(h(9), receipt(W))
    c.put_receipt(h(10), receipt(W, gas_used=50_000))
    c.put_collection("alpha", raw={}, stats={"total": {"floor_price": 0.4, "floor_price_symbol": "ETH"}},
                     last_sale_ts=day_ts("2023-12-01"), floor_source="opensea")
    c.put_floors("alpha", "opensea", {"2022-12-01": ETH, "2023-03-15": 6 * ETH // 10})
    c.put_eth_usd("coingecko", {"2022-12-01": 1500.0})
    c.put_reverse(W, None)
    c.replace_nfts(W, [{"contract": NFT, "identifier": "3"}])
    c.set_meta(f"as_of:{W}", "2024-01-01")
    c.set_meta("rpc_label", "test")
    return c, g


def test_pipeline_numbers(tmp_path):
    cache, g = build_cache(tmp_path / "c.sqlite")
    r = cli.make_report(cache, W, None, [], "2022-06-14", "0.005")
    jsonschema.validate(r, json.loads(SCHEMA_PATH.read_text()))
    lots = {l["token_id"]: l for l in r["lots"]}
    # mint: 0.1 + gas 0.001
    assert lots["1"]["basis_eth"] == 0.101 and lots["1"]["acquire_kind"] == "mint"
    # listing sale: price 2.0, fee 0.05, royalty 0.1, buyer paid gas
    assert (lots["1"]["sale_price_eth"], lots["1"]["fees_eth"], lots["1"]["royalty_eth"]) == (2.0, 0.05, 0.1)
    assert lots["1"]["proceeds_eth"] == 1.85 and lots["1"]["sale_type"] == "listing"
    # buy: 1.0 + gas
    assert lots["2"]["basis_eth"] == 1.001
    # accepted bid: price 0.5, fee 0.0125, royalty 0.025, seller paid gas 0.001
    assert lots["2"]["sale_type"] == "bid"
    assert lots["2"]["proceeds_eth"] == round(0.5 - 0.0125 - 0.025 - 0.001, 6)
    # gift
    assert lots["3"]["basis_eth"] == 0 and "gift_or_airdrop" in lots["3"]["flags"]
    # bundle: floors equal so 3.0 splits 1.5 / 1.5 and fee 0.1 → 0.05 each
    assert lots["4"]["sale_price_eth"] == 1.5 and lots["5"]["fees_eth"] == 0.05
    assert "bundle" in lots["4"]["flags"]
    assert r["quality"]["bundles"] == 2
    # churn: the cancel (0.001) and the WETH wrap (0.0005); fulfilments excluded
    churn = next(l for l in r["leaks"] if l["id"] == "listing_churn")
    assert churn["eth"] == 0.0015 and churn["detail"]["txs"] == 2
    # sold into bids: floor on 2023-04-01 is 0.6, sale 0.5 → 0.1
    assert next(l for l in r["leaks"] if l["id"] == "sold_into_bids")["eth"] == 0.1
    assert r["quality"]["holdings_check"]["held_not_in_ledger"] == 0
    assert r["quality"]["holdings_check"]["open_but_not_held"] == 0
    assert r["totals"]["fees_eth"] == round(0.05 + 0.0125 + 0.1, 6)


def test_pipeline_deterministic(tmp_path):
    cache, _ = build_cache(tmp_path / "c.sqlite")
    a = dumps(cli.make_report(cache, W, None, [], "2022-06-14", "0.005"))
    b = dumps(cli.make_report(cache, W, None, [], "2022-06-14", "0.005"))
    assert a == b


def test_cli_without_refresh_makes_no_network_calls(tmp_path, monkeypatch):
    cache, _ = build_cache(tmp_path / "c.sqlite")
    cache.close()

    def boom(*a, **k):
        raise AssertionError("network used without --refresh")

    monkeypatch.setattr("sources.http.urllib_transport", boom)
    monkeypatch.setattr("sources.http.Http.request", boom)
    out = tmp_path / "out"
    cli.main([W, "--cache", str(tmp_path / "c.sqlite"), "--json", str(out / "r.json"), "--card", str(out / "c.png")])
    assert (out / "r.json").exists() and (out / "c.png").exists()


def test_floor_history_only_where_it_can_move_a_number():
    from ledger.lots import LedgerEvent
    from sources.prices import floor_history_needed

    def ev(coll, kind, price=0, gas=0):
        return LedgerEvent(0, "0x", 0, kind, NFT, "1", coll, 1, X, W, price_wei=price, gas_wei=gas)

    evs = [ev("airdrop", "mint"), ev("airdrop", "transfer"), ev("freemint", "mint", gas=1),
           ev("bought", "buy", price=5), ev("gifted_then_sold", "transfer"), ev("gifted_then_sold", "sale")]
    assert floor_history_needed(evs) == {"freemint", "bought", "gifted_then_sold"}


def test_shared_storefront_twin_sale_counts_once(tmp_path):
    from sources.normalize import normalize
    OLD, NEW = "0x495f947276749ce646f68ac8c248420045cb7b5e", "0xa604060890923ff400e8c6f5290461a83aedacec"
    tok = "6388977119871529862102609514833496739136469992724674426608027308496329166053"
    c = Cache(tmp_path / "c.sqlite")
    evs = []
    for contract in (OLD, NEW):
        e = sale_ev(h(1), "2023-01-10", tok, X, W, ETH // 10)
        e["nft"] = {"contract": contract, "identifier": tok, "collection": "ssf"}
        evs.append(e)
    c.upsert_events(f"account:{W}", [(f"k{i}", "sale", e["event_timestamp"], h(1), e) for i, e in enumerate(evs)])
    c.put_receipt(h(1), receipt(W, [encode_log(SP, "0x" + "1" * 64, X, Z, W, [(3, NEW, int(tok), 1)],
                                               [(0, Z, 0, 975 * ETH // 10000, X), (0, Z, 0, 25 * ETH // 10000, FEE)])]))
    n = normalize(c, [W], lambda s, d: None, lambda d: None)
    buys = [e for e in n.events if e.kind == "buy"]
    assert [(e.contract, e.price_wei, e.sale_type) for e in buys] == [(OLD, ETH // 10, "listing")]
    assert n.quality["sales_without_seaport_decode"] == 0


def test_bad_opensea_1155_quantity_is_read_from_the_transfer_log(tmp_path):
    from sources.normalize import TRANSFER_SINGLE, normalize
    amt = 0x1e80d69be14ae8be784fafa7
    word = lambda a: "0x" + a[2:].rjust(64, "0")
    log = {"address": NFT, "topics": [TRANSFER_SINGLE, word(W), word(Z), word(W)],
           "data": "0x" + (14).to_bytes(32, "big").hex() + amt.to_bytes(32, "big").hex()}
    c = Cache(tmp_path / "c.sqlite")
    e = xfer_ev(h(1), "2025-09-28", "14", Z, W, et="mint")
    e["quantity"] = -2212700198871605248          # OpenSea's int64 wrap of `amt`
    c.upsert_events(f"account:{W}", [("k", "mint", e["event_timestamp"], h(1), e)])
    c.put_receipt(h(1), receipt(W, [log]))
    (ev,) = normalize(c, [W], lambda s, d: None, lambda d: None).events
    assert ev.qty == amt and "qty_unknown" not in ev.flags
    c.put_receipt(h(1), receipt(W, []))              # no log to read: fall back to 1 and say so
    (ev,) = normalize(c, [W], lambda s, d: None, lambda d: None).events
    assert ev.qty == 1 and "qty_unknown" in ev.flags
