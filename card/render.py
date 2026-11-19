"""Renders the 1200×630 share card from the report JSON alone."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
PAD = 64
FONT = Path(__file__).with_name("assets") / "Aileron-Regular.otf"
BG = (14, 16, 22)
PANEL = (24, 27, 36)
INK = (236, 238, 243)
MUTED = (140, 147, 163)
ACCENT = (255, 122, 69)
GOOD = (88, 204, 142)
RULE = (44, 48, 60)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def _fit(draw, text: str, max_w: int, size: int, min_size: int = 18, stroke: int = 0) -> ImageFont.FreeTypeFont:
    """Largest font ≤ size whose rendering of `text` fits max_w (never clips; shrinks instead)."""
    while size > min_size:
        f = _font(size)
        if draw.textlength(text, font=f) + 2 * stroke <= max_w:
            return f
        size -= 2
    return _font(min_size)


def short_addr(a: str) -> str:
    return f"{a[:6]}...{a[-4:]}"


def _eth(v: float | None, signed: bool = False) -> str:
    v = v or 0.0
    s = f"{abs(v):,.2f} ETH" if abs(v) >= 0.01 or v == 0 else f"{abs(v):.4f} ETH"
    if signed:
        return ("+" if v >= 0 else "-") + s
    return s


def _usd(v: float) -> str:
    return f"${v:,.0f}"


def score_color(v: int):
    return GOOD if v >= 80 else (ACCENT if v < 50 else (240, 196, 84))


def render(report: dict, path: str | Path) -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    w = report["wallet"]
    who = w.get("ens") or short_addr(w["address"])

    # Top: identity and period on the left, score on the right.
    score_box_w = 300
    left_w = W - 2 * PAD - score_box_w - 32
    d.text((PAD, PAD - 6), "NFT LEAK REPORT", font=_font(22), fill=ACCENT)
    fname = _fit(d, who, left_w, 64, stroke=1)
    d.text((PAD, PAD + 26), who, font=fname, fill=INK, stroke_width=1, stroke_fill=INK)
    p = report["period"]
    d.text((PAD, PAD + 106), f"{p['from']} to {p['to']}", font=_font(24), fill=MUTED)

    sx = W - PAD - score_box_w
    d.rounded_rectangle((sx, PAD - 10, W - PAD, PAD + 150), radius=18, fill=PANEL)
    sc = report["score"]
    num = str(sc["value"])
    f_num = _font(112)
    nw = d.textlength(num, font=f_num)
    f_den = _font(30)
    dw = d.textlength("/100", font=f_den)
    x0 = sx + (score_box_w - nw - dw - 6) / 2
    d.text((x0, PAD - 4), num, font=f_num, fill=score_color(sc["value"]), stroke_width=2,
           stroke_fill=score_color(sc["value"]))
    d.text((x0 + nw + 6, PAD + 70), "/100", font=f_den, fill=MUTED)
    label = "leak score" + (" · low sample" if sc.get("low_sample") else "")
    lw = d.textlength(label, font=_font(22))
    d.text((sx + (score_box_w - lw) / 2, PAD + 114), label, font=_font(22), fill=MUTED)

    # Middle: three largest leaks.
    y = 252
    d.line((PAD, y - 22, W - PAD, y - 22), fill=RULE, width=2)
    leaks = sorted(report["leaks"], key=lambda l: (-(l["eth"] or 0), l["id"]))[:3]
    for l in leaks:
        name = l["name"]
        amt = f"{_eth(l['eth'])}  ·  {_usd(l['usd'])}"
        d.ellipse((PAD, y + 14, PAD + 14, y + 28), fill=ACCENT if l["eth"] else RULE)
        d.text((PAD + 32, y), name, font=_font(36), fill=INK)
        f_amt = _fit(d, amt, 560, 36)
        d.text((W - PAD - d.textlength(amt, font=f_amt), y), amt, font=f_amt, fill=INK)
        y += 60
    d.line((PAD, y + 4, W - PAD, y + 4), fill=RULE, width=2)

    # Bottom: deployed, realised P&L, strength.
    t = report["totals"]
    y += 28
    stats = f"Deployed {_eth(t['deployed_eth'])}   ·   Realised P&L {_eth(t['realized_pnl_eth'], signed=True)}"
    d.text((PAD, y), stats, font=_fit(d, stats, W - 2 * PAD, 28), fill=MUTED)
    strength = "Strength: " + report["strength"]["text"]
    d.text((PAD, y + 44), strength, font=_fit(d, strength, W - 2 * PAD, 30), fill=GOOD)

    foot = "data: OpenSea · leak.report"
    ff = _font(20)
    d.text((W - PAD - d.textlength(foot, font=ff), H - 44), foot, font=ff, fill=MUTED)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


if __name__ == "__main__":
    import json
    import sys
    render(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
