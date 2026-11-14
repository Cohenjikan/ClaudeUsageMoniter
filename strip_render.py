"""Pixel renderer for the modern taskbar widget styles ("bars" and "rings").

Pure PIL, no Win32 and no tkinter, so it can be previewed to a PNG and tested
on its own. Produces a straight-alpha RGBA image at PHYSICAL pixel size
(logical layout × scale); taskbar_widget.py premultiplies and hands it to
UpdateLayeredWindow, which gives real per-pixel alpha: anti-aliased rings and
text with no colour-key fringes, legible on both light and dark taskbars.

Shapes and text are both drawn 4× supersampled and box-filtered down. For
shapes that is the only way to anti-alias PIL arcs; for text it removes the
hinting snap that made 1× FreeType text look over-sharpened next to the
taskbar's own DirectWrite text (see TEXT_GAMMA).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(r"C:\Windows\Fonts")
SS = 4  # supersampling factor for shapes

# Logical heights. The Win11 taskbar band is 48 logical px; leave a few px of
# breathing room top and bottom like the native taskbar buttons do.
HEIGHT = 40


@dataclass(frozen=True)
class Item:
    """One thing to show: a quota (pct set) or a plain value (pct None)."""
    label: str              # "周" / "Fable" / "今日"
    value: str              # "98%" / "$3.21"
    pct: float | None       # 0-100 for quota items; None for plain values
    sub: str = ""           # secondary line (reset time, stale marker, …)
    level: str = "ok"       # ok | warn | danger | stale — drives colour
    sub_warn: bool = False  # draw `sub` in the warning colour
    # % of the quota window's TIME already elapsed (0-100), or None when the
    # window isn't running / data is stale. Drawn as a marker on the gauge so
    # usage vs. time reads at a glance: fill past the marker = burning faster
    # than the window is passing.
    time_pct: float | None = None


@dataclass(frozen=True)
class Theme:
    fg: tuple
    dim: tuple
    track: tuple
    hover: tuple
    ok: tuple
    warn: tuple
    danger: tuple
    stale: tuple
    brand: tuple


DARK = Theme(
    fg=(243, 243, 243, 255), dim=(176, 176, 182, 255),
    track=(255, 255, 255, 46), hover=(255, 255, 255, 22),
    ok=(155, 140, 255, 255), warn=(240, 168, 96, 255),
    danger=(247, 113, 111, 255), stale=(140, 140, 148, 255),
    brand=(217, 119, 87, 255),
)
LIGHT = Theme(
    fg=(26, 26, 26, 255), dim=(92, 92, 98, 255),
    track=(0, 0, 0, 34), hover=(0, 0, 0, 16),
    ok=(104, 84, 230, 255), warn=(201, 118, 30, 255),
    danger=(206, 60, 58, 255), stale=(130, 130, 136, 255),
    brand=(201, 100, 66, 255),
)


def _level_color(theme: Theme, level: str) -> tuple:
    return {"warn": theme.warn, "danger": theme.danger,
            "stale": theme.stale}.get(level, theme.ok)


# Text is drawn SS× supersampled and box-averaged down (no hinting snap, no
# Lanczos ringing), then its coverage gets a mild gamma lift — that is what
# makes it read like the taskbar's own DirectWrite text instead of the
# over-sharpened look of 1× FreeType hinting.
TEXT_GAMMA = 0.85


@lru_cache(maxsize=64)
def _font(px: int, bold: bool, latin: bool) -> ImageFont.FreeTypeFont:
    """Segoe UI Variable (the Win11 shell font, "Text" optical size) for
    pure-ASCII strings, Microsoft YaHei UI for anything with CJK. Falls back
    to classic Segoe UI / PIL default if a font is missing."""
    if latin:
        try:
            f = ImageFont.truetype(str(FONT_DIR / "SegUIVar.ttf"), px)
            f.set_variation_by_name("Semibold Text" if bold else "Regular")
            return f
        except (OSError, ValueError):
            name, index = ("seguisb.ttf" if bold else "segoeui.ttf"), 0
    else:
        name = "msyhbd.ttc" if bold else "msyh.ttc"
        index = 1  # "Microsoft YaHei UI" face — tighter UI metrics
    try:
        return ImageFont.truetype(str(FONT_DIR / name), px, index=index)
    except OSError:
        return ImageFont.load_default()


class _Painter:
    """Two-layer painter: supersampled shape layer + 1× text layer."""

    def __init__(self, w: int, h: int, scale: float) -> None:
        self.s = scale
        self.w, self.h = max(1, round(w * scale)), max(1, round(h * scale))
        self.shapes = Image.new("RGBA", (self.w * SS, self.h * SS), (0, 0, 0, 0))
        self.sd = ImageDraw.Draw(self.shapes)
        self.texts: list[tuple] = []

    # -- coordinate helpers (inputs in logical px) --
    def _p(self, v: float) -> float:
        return v * self.s * SS

    def rrect(self, x0, y0, x1, y1, r, fill) -> None:
        self.sd.rounded_rectangle(
            (self._p(x0), self._p(y0), self._p(x1), self._p(y1)),
            radius=self._p(r), fill=fill)

    def arc(self, cx, cy, radius, width, start, end, fill) -> None:
        box = (self._p(cx - radius), self._p(cy - radius),
               self._p(cx + radius), self._p(cy + radius))
        self.sd.arc(box, start, end, fill=fill, width=max(1, round(self._p(width))))

    def dot(self, cx, cy, r, fill) -> None:
        self.sd.ellipse((self._p(cx - r), self._p(cy - r),
                         self._p(cx + r), self._p(cy + r)), fill=fill)

    def text(self, x, y, s, px, bold, fill, anchor="lm") -> None:
        self.texts.append((x, y, s, px, bold, fill, anchor))

    def measure(self, s: str, px: int, bold: bool) -> float:
        """Advance width in LOGICAL px (measured at the supersampled size it
        will actually be drawn at)."""
        f = _font(max(1, round(px * self.s * SS)), bold, s.isascii())
        return f.getlength(s) / (self.s * SS)

    def finish(self) -> Image.Image:
        # Box filter = exact area coverage. (Pillow resizes RGBA in
        # premultiplied space, so transparent edges don't go dark.)
        img = self.shapes.resize((self.w, self.h), Image.BOX)
        text = Image.new("RGBA", self.shapes.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(text)
        for x, y, s, px, bold, fill, anchor in self.texts:
            f = _font(max(1, round(px * self.s * SS)), bold, s.isascii())
            d.text((self._p(x), self._p(y)), s, font=f, fill=fill, anchor=anchor)
        text = text.resize((self.w, self.h), Image.BOX)
        if TEXT_GAMMA != 1:
            r, g, b, a = text.split()
            a = a.point(lambda v: round(255 * (v / 255) ** TEXT_GAMMA))
            text = Image.merge("RGBA", (r, g, b, a))
        img.alpha_composite(text)
        return img


def _brand_mark(p: _Painter, cx: float, cy: float, r: float, color) -> None:
    """A small 8-spoke spark: identifies the widget at a glance."""
    import math
    for i in range(8):
        a = math.pi * i / 4
        x1, y1 = cx + math.cos(a) * r, cy + math.sin(a) * r
        # draw spoke as a thin rounded capsule via successive dots
        steps = 6
        for k in range(steps + 1):
            tt = 0.25 + 0.75 * k / steps
            p.dot(cx + (x1 - cx) * tt, cy + (y1 - cy) * tt, r * 0.17, color)
    p.dot(cx, cy, r * 0.22, color)


# ---------------------------------------------------------------- layouts

PAD_X = 10
BRAND_W = 22


def _time_txt(it: Item) -> str:
    """'/45%' — elapsed share of the quota window, or '' when not running."""
    return "" if it.pct is None or it.time_pct is None else f"/{it.time_pct:.0f}%"


def _hit_backdrop(p: _Painter, w: float, hover: bool, theme: Theme) -> None:
    # Alpha-1 fill everywhere: invisible, but makes the whole widget clickable
    # (fully transparent pixels of a layered window are click-through).
    p.rrect(0, 0, w, HEIGHT, 6, (0, 0, 0, 1))
    if hover:
        p.rrect(0, 0, w, HEIGHT, 6, theme.hover)


def render_bars(items: list[Item], theme: Theme, scale: float,
                hover: bool = False) -> Image.Image:
    """Each quota: label + percentage on top, slim progress bar underneath.
    Plain values (today's cost) get label on top, value underneath."""
    probe = _Painter(1, 1, scale)
    COL_GAP = 16
    BAR_W = 64
    cols = []
    for it in items:
        top_w = (probe.measure(it.label, 11, False) + 6
                 + probe.measure(it.value, 12, True)
                 + probe.measure(_time_txt(it), 11, False))
        if it.pct is None:
            w = max(probe.measure(it.label, 11, False), probe.measure(it.value, 13, True))
        else:
            w = max(BAR_W, top_w)
        cols.append(w)
    total = PAD_X + BRAND_W + sum(cols) + COL_GAP * (len(cols) - 1) + PAD_X
    p = _Painter(total, HEIGHT, scale)
    _hit_backdrop(p, total, hover, theme)
    _brand_mark(p, PAD_X + 7, HEIGHT / 2, 7, theme.brand)
    x = PAD_X + BRAND_W
    for it, w in zip(items, cols):
        col = _level_color(theme, it.level)
        if it.pct is None:
            p.text(x, 13, it.label, 11, False, theme.dim)
            p.text(x, 28, it.value, 13, True, theme.fg)
        else:
            p.text(x, 13, it.label, 11, False, theme.dim)
            # Right-aligned "usage%" + dim "/time%" (time part only while the
            # window is running) — the same comparison as classic mode 4.
            tt = _time_txt(it)
            right = x + w
            if tt:
                p.text(right, 13, tt, 11, False, theme.dim, anchor="rm")
                right -= probe.measure(tt, 11, False)
            p.text(right, 13, it.value, 12, True,
                   theme.dim if it.level == "stale" else col, anchor="rm")
            by = 26
            p.rrect(x, by, x + w, by + 5, 2.5, theme.track)
            frac = max(0.0, min(1.0, it.pct / 100))
            if frac > 0:
                p.rrect(x, by, x + max(5, w * frac), by + 5, 2.5, col)
            if it.time_pct is not None:
                # Time marker: a slim pin through the bar at elapsed-time %.
                # Fill reaching past it = using quota faster than time passes.
                mx = x + 1.25 + (w - 2.5) * max(0.0, min(1.0, it.time_pct / 100))
                p.rrect(mx - 1.25, by - 3, mx + 1.25, by + 8, 1.25, theme.fg)
            # (it.sub doesn't fit under a bar; stale shows via the grey colour.)
        x += w + COL_GAP
    return p.finish()


def render_rings(items: list[Item], theme: Theme, scale: float,
                 hover: bool = False) -> Image.Image:
    """Each quota: a ring gauge with the % inside, label + reset time beside it.
    Plain values: two lines of text."""
    probe = _Painter(1, 1, scale)
    RING_D = 30
    GAP = 7
    ITEM_GAP = 18
    widths = []
    for it in items:
        tw = max(probe.measure(it.label, 12, True)
                 + (3 + probe.measure(_time_txt(it), 10, False) if _time_txt(it) else 0),
                 probe.measure(it.sub, 10, False) if it.sub else 0)
        if it.pct is None:
            tw = max(probe.measure(it.label, 10, False), probe.measure(it.value, 13, True))
            widths.append(tw)
        else:
            widths.append(RING_D + GAP + tw)
    total = PAD_X + BRAND_W + sum(widths) + ITEM_GAP * (len(widths) - 1) + PAD_X
    p = _Painter(total, HEIGHT, scale)
    _hit_backdrop(p, total, hover, theme)
    _brand_mark(p, PAD_X + 7, HEIGHT / 2, 7, theme.brand)
    x = PAD_X + BRAND_W
    cy = HEIGHT / 2
    for it, w in zip(items, widths):
        col = _level_color(theme, it.level)
        if it.pct is None:
            p.text(x, 13, it.label, 10, False, theme.dim)
            p.text(x, 28, it.value, 13, True, theme.fg)
        else:
            r = RING_D / 2
            rcx = x + r
            stroke = 3.4
            p.arc(rcx, cy, r - stroke / 2, stroke, 0, 360, theme.track)
            frac = max(0.0, min(1.0, it.pct / 100))
            if frac > 0:
                p.arc(rcx, cy, r - stroke / 2, stroke, -90, -90 + 360 * frac, col)
            if it.time_pct is not None:
                # Time marker: a dot on the ring at elapsed-time %.
                import math
                ang = math.radians(-90 + 360 * max(0.0, min(1.0, it.time_pct / 100)))
                rr = r - stroke / 2
                p.dot(rcx + math.cos(ang) * rr, cy + math.sin(ang) * rr,
                      stroke * 0.75, theme.fg)
            num = it.value.rstrip("%")
            p.text(rcx, cy + 0.5, num, 10 if len(num) >= 3 else 11, True,
                   theme.dim if it.level == "stale" else theme.fg, anchor="mm")
            tx = x + RING_D + GAP
            tt = _time_txt(it)
            if tt:
                p.text(tx + probe.measure(it.label, 12, True) + 3,
                       13 if it.sub else cy, tt, 10, False, theme.dim)
            if it.sub:
                p.text(tx, 13, it.label, 12, True, theme.fg)
                p.text(tx, 28, it.sub, 10, False,
                       theme.warn if it.sub_warn else theme.dim)
            else:
                p.text(tx, cy, it.label, 12, True, theme.fg)
        x += w + ITEM_GAP
    return p.finish()


STYLES = {"bars": render_bars, "rings": render_rings}


def render(style: str, items: list[Item], theme: Theme, scale: float,
           hover: bool = False) -> Image.Image:
    return STYLES.get(style, render_bars)(items, theme, scale, hover)


if __name__ == "__main__":
    # Preview: python strip_render.py out_dir
    import sys
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    demo = [
        Item("周配额", "98%", 98, "2d 3h后重置", "danger", time_pct=70),
        Item("Fable", "2%", 2, "3d 4h后重置", "ok", time_pct=45),
        Item("今日", "$3.21", None),
    ]
    demo_5h = [Item("5h 窗口", "44%", 44, "2h 13m后重置", "ok", time_pct=56)] + demo
    for style in STYLES:
        for name, th, bg in (("dark", DARK, (32, 32, 32)), ("light", LIGHT, (238, 238, 238))):
            for tag, items in (("ultra", demo), ("max", demo_5h)):
                img = render(style, items, th, 1.25)
                canvas = Image.new("RGBA", (img.width + 40, 60), bg + (255,))
                canvas.alpha_composite(img, (20, (60 - img.height) // 2))
                canvas.save(out / f"{style}_{name}_{tag}.png")
    print("ok")
