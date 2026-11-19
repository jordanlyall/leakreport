import ast
from pathlib import Path

from ledger.basis import basis, proceeds
from ledger.fees import split_pro_rata
from ledger.lots import GIFT, BASIS_UNKNOWN, LedgerEvent, replay
from ledger import pnl

ETH = 10**18
W = "0x" + "a" * 40
LINKED = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
ZERO = "0x" + "0" * 40
C721 = "0x" + "1" * 40
C1155 = "0x" + "2" * 40


def ev(ts, kind, frm, to, contract=C721, token="1", qty=1, price=0, gas=0, fee=0, roy=0,
       sale_type=None, tx=None):
    return LedgerEvent(ts=ts, tx_hash=tx or f"0x{ts:064x}", seq=0, kind=kind, contract=contract,
                       token_id=token, collection="coll", qty=qty, from_addr=frm, to_addr=to,
                       price_wei=price, gas_wei=gas, fee_wei=fee, royalty_wei=roy, sale_type=sale_type)


def test_ledger_imports_nothing_from_sources_or_leaks():
    root = Path(__file__).resolve().parent.parent / "ledger"
    banned = {"sources", "leaks", "report", "cli", "card", "score"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.level:
                    continue  # relative imports stay inside ledger/
            else:
                continue
            for n in names:
                assert n.split(".")[0] not in banned, f"{path.name} imports {n}"


def test_basis_and_proceeds():
    assert basis(ETH, 10) == ETH + 10
    assert basis(None, 10) is None
    assert proceeds(ETH, 25, 50, 5) == ETH - 80


def test_split_pro_rata_exact_and_deterministic():
    assert split_pro_rata(10, [1, 1, 1]) == [4, 3, 3]
    assert sum(split_pro_rata(10**18 + 7, [3, 5, 11])) == 10**18 + 7
    assert split_pro_rata(9, [0, 0, 0]) == [3, 3, 3]
    assert split_pro_rata(5, []) == []


def test_mint_basis_includes_gas():
    led = replay([ev(100, "mint", ZERO, W, price=ETH // 10, gas=ETH // 100)], W)
    (lot,) = led.lots
    assert lot.basis_wei == ETH // 10 + ETH // 100
    assert GIFT not in lot.flags


def test_transfer_in_without_payment_is_gift_with_zero_basis():
    led = replay([ev(100, "transfer", OTHER, W)], W)
    (lot,) = led.lots
    assert lot.basis_wei == 0
    assert GIFT in lot.flags
    assert led.quality["gifts"] == 1


def test_erc1155_fifo():
    events = [
        ev(100, "buy", OTHER, W, contract=C1155, qty=3, price=3 * ETH // 10),
        ev(200, "buy", OTHER, W, contract=C1155, qty=2, price=2 * 2 * ETH // 10),
        ev(300, "sale", W, OTHER, contract=C1155, qty=4, price=4 * ETH),
    ]
    led = replay(events, W)
    sold = [l for l in led.lots if l.dispose_kind == "sale"]
    assert sum(l.qty for l in sold) == 4
    assert sum(l.basis_wei for l in sold) == 3 * ETH // 10 + 1 * 2 * ETH // 10
    still = led.open_lots()
    assert [(l.qty, l.basis_wei) for l in still] == [(1, 2 * ETH // 10)]
    assert sum(l.sale_price_wei for l in sold) == 4 * ETH


def test_linked_transfer_is_not_disposal_nor_new_lot():
    events = [
        ev(100, "buy", OTHER, W, price=ETH),
        ev(200, "transfer", W, LINKED),
    ]
    led = replay(events, W, linked=[LINKED])
    assert len(led.lots) == 1
    assert led.lots[0].is_open
    assert led.quality["internal_transfers"] == 1
    # Without --linked the same transfer closes the lot as an unpriced disposal.
    led2 = replay(events, W)
    assert led2.lots[0].dispose_kind == "disposed_unpriced"
    assert led2.quality["unpriced_disposals"] == 1


def test_sale_to_linked_wallet_is_wash_and_ignored():
    events = [ev(100, "buy", OTHER, W, price=ETH), ev(200, "sale", W, LINKED, price=2 * ETH, fee=1)]
    led = replay(events, W, linked=[LINKED])
    assert led.lots[0].is_open and led.quality["wash_trades"] == 1


def test_sale_proceeds_and_pnl():
    events = [
        ev(100, "buy", OTHER, W, price=ETH, gas=10),
        ev(200, "sale", W, OTHER, price=2 * ETH, fee=ETH // 40, roy=ETH // 20, gas=5, sale_type="bid"),
    ]
    led = replay(events, W)
    (lot,) = led.lots
    assert lot.proceeds_wei == 2 * ETH - ETH // 40 - ETH // 20 - 5
    assert lot.sale_type == "bid"
    assert pnl.realized(led, since_ts=0) == lot.proceeds_wei - (ETH + 10)
    assert pnl.realized(led, since_ts=201) == 0


def test_orphan_disposal_has_unknown_basis():
    led = replay([ev(100, "sale", W, OTHER, price=ETH)], W)
    (lot,) = led.lots
    assert BASIS_UNKNOWN in lot.flags and lot.basis_wei is None
    assert led.quality["orphan_disposals"] == 1


def test_unpriced_sale_closes_lot_and_counts():
    led = replay([ev(100, "buy", OTHER, W, price=ETH), ev(200, "sale", W, OTHER, price=None)], W)
    assert led.lots[0].dispose_kind == "sale_unpriced"
    assert led.quality["unpriced_sales"] == 1


def test_unrealized_and_deployed():
    events = [ev(100, "buy", OTHER, W, price=ETH), ev(50, "transfer", OTHER, W, token="2")]
    led = replay(events, W)
    assert pnl.unrealized(led, {"coll": ETH // 2}) == (ETH // 2 - ETH) + (ETH // 2 - 0)
    assert pnl.deployed(led, 0) == ETH


def test_deployed_leaves_out_lots_no_leak_can_judge():
    events = [ev(100, "buy", OTHER, W, price=ETH, token="1"), ev(110, "buy", OTHER, W, price=2 * ETH, token="2"),
              ev(120, "buy", OTHER, W, price=4 * ETH, token="3"),
              ev(200, "transfer", W, OTHER, token="2"),             # left unpriced (e.g. sold on another venue)
              ev(210, "sale", W, OTHER, token="3", price=None)]     # sold for an unknown token
    led = replay(events, W)
    assert pnl.deployed(led, 0) == ETH
    assert pnl.unscored_basis(led, 0) == 6 * ETH


def test_replay_is_deterministic_under_input_order():
    events = [ev(100, "buy", OTHER, W, price=ETH, tx="0x01"), ev(100, "buy", OTHER, W, token="2", price=ETH, tx="0x02"),
              ev(300, "sale", W, OTHER, price=ETH)]
    a = replay(events, W)
    b = replay(list(reversed(events)), W)
    assert [l.__dict__ for l in a.lots] == [l.__dict__ for l in b.lots]
