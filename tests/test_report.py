import json

import jsonschema
from PIL import Image, ImageDraw

from card import render as card
from leaks.base import Params
from report import SCHEMA_PATH, build_report, dumps, table
from tests.test_leaks import W, fixture

ENS24 = "averyveryverylongname.eth"[:20] + ".eth"   # 24 characters


def make_report(ens=None):
    led, prices = fixture()
    return build_report(led, prices, Params(since="2022-06-14", as_of="2024-01-01"),
                        wallet={"address": W, "ens": ens, "as_of": "2024-01-01", "linked": []},
                        quality={"events_missing_receipt": 0},
                        sources={"opensea_api": "v2", "rpc": "test", "eth_usd": "test"},
                        floor_sources={"alpha": "opensea", "beta": "opensea"})


def test_report_validates_against_schema():
    r = make_report()
    jsonschema.validate(r, json.loads(SCHEMA_PATH.read_text()))
    assert [l["id"] for l in r["leaks"]] == ["fee_drag", "held_past_floor", "bought_at_top", "listing_churn",
                                             "sold_into_bids", "dead_inventory"]
    assert r["quality"]["floor_source"] == "opensea"


def test_report_is_deterministic():
    assert dumps(make_report()) == dumps(make_report())


def test_report_numbers():
    r = make_report()
    by = {l["id"]: l for l in r["leaks"]}
    assert by["sold_into_bids"]["eth"] == 0.2
    assert by["dead_inventory"]["eth"] == 0.21
    # USD at the event's daily close: 2023-02-01 close carried from 2023-01-01 = 2000
    assert by["sold_into_bids"]["usd"] == 400.0
    assert r["totals"]["deployed_eth"] == round(1.05 + 1.0 + 0.21, 6)
    assert r["score"]["low_sample"] is True
    assert "Score" in table(r)


def test_card_renders_from_json_alone_with_24_char_ens(tmp_path):
    assert len(ENS24) == 24
    r = json.loads(dumps(make_report(ens=ENS24)))
    out = card.render(r, tmp_path / "card.png")
    img = Image.open(out)
    assert img.size == (1200, 630)
    d = ImageDraw.Draw(img)
    left_w = card.W - 2 * card.PAD - 300 - 32
    f = card._fit(d, ENS24, left_w, 64, stroke=1)
    assert d.textlength(ENS24, font=f) + 2 <= left_w
    assert f.size >= 40     # still large, not shrunk to nothing


def test_card_without_ens_uses_short_address(tmp_path):
    out = card.render(json.loads(dumps(make_report())), tmp_path / "c.png")
    assert out.exists()
    assert card.short_addr(W) == "0xaaaa...aaaa"
