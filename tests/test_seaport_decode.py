from sources.chain import attribute_sale, decode_order_fulfilled, fills_from_receipt, gas_paid
from tests.seaport_encode import encode_log

SEAPORT = "0x00000000000000adc04c56bf30ac9d3c0aaf14dc"
FEE = "0x0000a26b00c1f0df003000390027140000faa719"
SELLER = "0x" + "a" * 40
BUYER = "0x" + "c" * 40
CREATOR = "0x" + "d" * 40
NFT = "0x" + "1" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
Z = "0x" + "0" * 40
OH = "0x" + "e" * 64


def test_decode_roundtrip():
    log = encode_log(SEAPORT, OH, SELLER, Z, BUYER, [(2, NFT, 7, 1)],
                     [(0, Z, 0, 900, SELLER), (0, Z, 0, 25, FEE), (0, Z, 0, 75, CREATOR)], log_index=3)
    f = decode_order_fulfilled(log)
    assert f.offerer == SELLER and f.recipient == BUYER and f.order_hash == OH and f.log_index == 3
    assert [(i.item_type, i.token, i.identifier, i.amount) for i in f.offer] == [(2, NFT, 7, 1)]
    assert [(i.amount, i.recipient) for i in f.consideration] == [(900, SELLER), (25, FEE), (75, CREATOR)]


def test_listing_sale_split():
    log = encode_log(SEAPORT, OH, SELLER, Z, BUYER, [(2, NFT, 7, 1)],
                     [(0, Z, 0, 900, SELLER), (0, Z, 0, 25, FEE), (0, Z, 0, 75, CREATOR)])
    s = attribute_sale(fills_from_receipt({"logs": [log]}), NFT, "7", SELLER)
    assert (s.price_wei, s.fee_wei, s.royalty_wei, s.seller_wei, s.sale_type) == (1000, 25, 75, 900, "listing")
    assert s.price_wei == s.fee_wei + s.royalty_wei + s.seller_wei


def test_accepted_bid_split():
    # Buyer's offer: WETH 1000; consideration: NFT to buyer, fee and royalty in WETH. Seller fulfils.
    log = encode_log(SEAPORT, OH, BUYER, Z, SELLER, [(1, WETH, 0, 1000)],
                     [(2, NFT, 7, 1, BUYER), (1, WETH, 0, 25, FEE), (1, WETH, 0, 50, CREATOR)])
    s = attribute_sale(fills_from_receipt({"logs": [log]}), NFT, "7", SELLER)
    assert (s.price_wei, s.fee_wei, s.royalty_wei, s.seller_wei, s.sale_type) == (1000, 25, 50, 925, "bid")
    assert s.payment_token == WETH


def test_bundle_lists_all_items():
    log = encode_log(SEAPORT, OH, SELLER, Z, BUYER, [(2, NFT, 7, 1), (2, NFT, 8, 1)],
                     [(0, Z, 0, 1900, SELLER), (0, Z, 0, 100, FEE)])
    s = attribute_sale(fills_from_receipt({"logs": [log]}), NFT, "8", SELLER)
    assert [(i.token_id, i.qty) for i in s.items] == [("7", 1), ("8", 1)]
    assert s.royalty_wei == 0


def test_non_seaport_logs_ignored_and_gas():
    log = encode_log("0x" + "9" * 40, OH, SELLER, Z, BUYER, [(2, NFT, 7, 1)], [(0, Z, 0, 1, SELLER)])
    assert fills_from_receipt({"logs": [log]}) == []
    assert gas_paid({"gasUsed": "0x5208", "effectiveGasPrice": "0x3b9aca00"}) == 21000 * 10**9


def test_parse_floor_prices_shape():
    from sources.prices import parse_floor_prices
    s = parse_floor_prices({"floor_prices": [
        {"time": 1700000000, "token_unit": 0.5, "symbol": "ETH", "usd_price": "1000"},
        {"time": 1700000100, "token_unit": 0.4, "symbol": "ETH"},
        {"time": 1700090000, "token_unit": 9, "symbol": "APE"}]})
    assert s == {"2023-11-14": 4 * 10**17}
