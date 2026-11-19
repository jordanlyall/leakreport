from leaks import (bought_at_top, concentration, dead_inventory, fee_drag, held_past_floor, listing_churn,
                   sold_into_bids)
from leaks.base import ChurnTx, LeakResult, Params, Prices, day_ts
from ledger.lots import LedgerEvent, replay
from score import score

ETH = 10**18
W = "0x" + "a" * 40
X = "0x" + "c" * 40
C = "0x" + "1" * 40
D = "0x" + "2" * 40
ALL = [fee_drag, held_past_floor, bought_at_top, listing_churn, sold_into_bids, dead_inventory]


def ev(day, kind, frm, to, token, price=0, gas=0, fee=0, roy=0, sale_type=None, coll="alpha", contract=C):
    return LedgerEvent(ts=day_ts(day) + 3600, tx_hash=f"0x{day}{token}{kind}", seq=0, kind=kind,
                       contract=contract, token_id=token, collection=coll, qty=1, from_addr=frm, to_addr=to,
                       price_wei=price, gas_wei=gas, fee_wei=fee, royalty_wei=roy, sale_type=sale_type)


def params(**kw):
    return Params(since="2022-06-14", as_of="2024-01-01", **kw)


def fixture():
    # alpha: floor 1.0 through 2022-12-31, 0.5 from 2023-01-01, 0.4 from 2023-06-01
    floors = {"alpha": {"2022-06-01": ETH, "2023-01-01": ETH // 2, "2023-06-01": 4 * ETH // 10},
              "beta": {"2022-06-01": 2 * ETH}}
    prices = Prices(floors=floors, current_floor={"alpha": 4 * ETH // 10, "beta": 0},
                    last_sale_ts={"alpha": day_ts("2023-12-30"), "beta": day_ts("2023-01-01")},
                    eth_usd={"2022-06-01": 1000.0, "2023-01-01": 2000.0}, as_of="2024-01-01")
    events = [
        # token 1: bought at 1.05 (median30 = 1.0 → 0.05 over), held while floor fell to 0.5 (< 0.9 × 1.05),
        # still held; current floor 0.4 → held_past_floor = 0.5 − 0.4 = 0.1
        ev("2022-07-01", "buy", X, W, "1", price=105 * ETH // 100, sale_type="listing"),
        # token 2: bought 1.0, sold into a bid on 2023-02-01 at 0.3 while floor 0.5 → sold_into_bids 0.2
        # fee 0.0075 on 0.3 after Blur launch: avoidable = 0.0075 − 0.0015 = 0.006
        ev("2022-07-02", "buy", X, W, "2", price=ETH, sale_type="listing"),
        ev("2023-02-01", "sale", W, X, "2", price=3 * ETH // 10, fee=75 * ETH // 10000, sale_type="bid"),
        # token 3 in beta: minted 0.2 + gas 0.01, floor now 0 → dead inventory = 0.21
        ev("2022-08-01", "mint", "0x" + "0" * 40, W, "3", price=2 * ETH // 10, gas=ETH // 100, coll="beta",
           contract=D),
    ]
    return replay(events, W), prices


def test_each_leak_zero_on_empty_ledger():
    led = replay([], W)
    prices = Prices({}, {}, {}, {}, "2024-01-01")
    for m in ALL:
        assert m.compute(led, prices, params()).wei == 0
    assert concentration.compute(led, prices, params())["level"] == "none"


def test_fee_drag():
    led, prices = fixture()
    r = fee_drag.compute(led, prices, params())
    assert r.detail["fee_total_wei"] == 75 * ETH // 10000
    assert r.wei == 75 * ETH // 10000 - 3 * ETH // 10 * 5 // 1000
    # Before Blur's launch no fee is avoidable.
    led2 = replay([ev("2022-07-01", "buy", X, W, "9", price=ETH),
                   ev("2022-09-01", "sale", W, X, "9", price=ETH, fee=ETH // 40)], W)
    assert fee_drag.compute(led2, prices, params()).wei == 0


def test_held_past_floor():
    led, prices = fixture()
    r = held_past_floor.compute(led, prices, params())
    item = next(i for i in r.items if i["token_id"] == "1")
    assert item["breach_day"] == "2023-01-01"
    assert item["wei"] == ETH // 2 - 4 * ETH // 10
    # token 2 also breached on 2023-01-01 (0.5 < 0.9) and sold for proceeds 0.3 − 0.0075 → 0.5 − 0.2925
    item2 = next(i for i in r.items if i["token_id"] == "2")
    assert item2["wei"] == ETH // 2 - (3 * ETH // 10 - 75 * ETH // 10000)
    assert r.wei == item["wei"] + item2["wei"]


def test_bought_at_top():
    led, prices = fixture()
    r = bought_at_top.compute(led, prices, params())
    assert r.wei == 5 * ETH // 100
    assert r.items[0]["floor_90d_later_over_price"] == round(1 / 1.05, 4)


def test_listing_churn():
    led, prices = fixture()
    txs = [ChurnTx("0xa", day_ts("2023-03-01"), "seaport", 10**15, "cancel"),
           ChurnTx("0xb", day_ts("2021-03-01"), "seaport", 10**15, "cancel"),   # before period
           ChurnTx("0xc", day_ts("2023-03-02"), "weth", 2 * 10**15, "failed")]
    r = listing_churn.compute(led, prices, params(churn_txs=txs))
    assert r.wei == 3 * 10**15 and r.detail["txs"] == 2


def test_sold_into_bids():
    led, prices = fixture()
    r = sold_into_bids.compute(led, prices, params())
    assert r.wei == ETH // 2 - 3 * ETH // 10


def test_dead_inventory():
    led, prices = fixture()
    r = dead_inventory.compute(led, prices, params())
    assert r.wei == 2 * ETH // 10 + ETH // 100


def test_concentration():
    led, prices = fixture()
    c = concentration.compute(led, prices, params())
    assert c["top1_share"] == 1.0 and c["level"] == "high" and c["top"][0] == "alpha"


def test_score_bounds_and_low_sample():
    assert score([LeakResult("x", "1.0", 0)], 10 * ETH, 12) == {
        "value": 100, "low_sample": False, "weighted_wei": 0, "deployed_wei": 10 * ETH, "acquisitions": 12}
    s = score([LeakResult("a", "1.0", 6 * ETH), LeakResult("b", "0.5", 8 * ETH)], 10 * ETH, 3)
    assert s["value"] == 0 and s["low_sample"] is True
    assert score([LeakResult("a", "1.0", 25 * ETH // 10)], 10 * ETH, 10)["value"] == 75
    assert score([LeakResult("a", "0.5", 1 * ETH)], 10 * ETH, 10)["value"] == 95
    assert score([LeakResult("a", "1.0", 5)], 1000, 10)["value"] == 100   # 99.5 rounds half up


def test_a_lot_in_both_held_past_floor_and_dead_inventory_counts_once():
    from leaks.base import count_each_lot_once
    held = LeakResult("held_past_floor", "0.5", 0, [
        {"lot_id": "a", "wei": 3 * ETH, "day": "2024-01-01"},     # bigger here: dead drops it
        {"lot_id": "b", "wei": ETH, "day": "2024-01-01"},         # bigger in dead: held drops it
        {"lot_id": "c", "wei": ETH, "day": "2024-01-01"}])        # only here: kept
    dead = LeakResult("dead_inventory", "0.5", 0, [
        {"lot_id": "a", "wei": 2 * ETH, "day": "2024-01-01"},
        {"lot_id": "b", "wei": 5 * ETH, "day": "2024-01-01"},
        {"lot_id": "d", "wei": ETH, "day": "2024-01-01"}])
    out = count_each_lot_once([held, dead])
    h, d = out
    assert [i["lot_id"] for i in h.items] == ["a", "c"] and h.wei == 4 * ETH
    assert [i["lot_id"] for i in d.items] == ["b", "d"] and d.wei == 6 * ETH
    assert h.detail["counted_in_dead_inventory"] == 1 and d.detail["counted_in_held_past_floor"] == 1


def test_strength_ignores_sale_leaks_when_nothing_was_sold():
    from report import _strength
    fee = LeakResult("fee_drag", "1.0", 0, [], {"sale_volume_wei": 0, "fee_total_wei": 0, "benchmark_rate": "0.005"})
    rs = [fee, LeakResult("held_past_floor", "0.5", 5 * ETH), LeakResult("listing_churn", "1.0", ETH // 10),
          LeakResult("sold_into_bids", "0.5", 0), LeakResult("dead_inventory", "0.5", 9 * ETH)]
    assert _strength(rs, 20 * ETH, fee)["id"] == "listing_churn"
    fee.detail["sale_volume_wei"] = ETH
    assert _strength(rs, 20 * ETH, fee)["id"] == "fee_drag"
