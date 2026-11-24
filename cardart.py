import io
import math
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps

CARD_RATIO = 1.5        # card height = width * 1.5
HAND_CARD_W = 104       # card width in the hand picture
SS = 2                  # cards are drawn 2x bigger, then shrunk, for smooth edges

RED = (214, 40, 40)
YELLOW = (244, 196, 30)
GREEN = (52, 160, 72)
BLUE = (34, 97, 190)
BLACK = (22, 22, 26)
WHITE = (255, 255, 255)
RGB = {"R": RED, "Y": YELLOW, "G": GREEN, "B": BLUE, "W": BLACK}


# ---------------------------------------------------------------- helpers

@lru_cache(maxsize=None)
def _font(size: int):
    size = max(8, int(size))
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _text(d, xy, s, size, fill, stroke=0, stroke_fill=None):
    f = _font(size)
    try:
        d.text(xy, s, font=f, fill=fill, anchor="mm",
               stroke_width=int(stroke), stroke_fill=stroke_fill)
    except Exception:
        d.text(xy, s, font=f, fill=fill)


def _layer(size):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _mini_card(img, cx, cy, w, h, angle, fill, border=WHITE):
    """A small rounded card, rotated around its own centre."""
    layer = _layer(img.size)
    d = ImageDraw.Draw(layer)
    r = max(2, int(w * 0.18))
    box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    d.rounded_rectangle(box, radius=r, fill=border)
    pad = max(1, int(w * 0.10))
    d.rounded_rectangle(
        [box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad],
        radius=max(1, r - pad // 2), fill=fill,
    )
    img.alpha_composite(layer.rotate(angle, resample=Image.BICUBIC, center=(cx, cy)))


def _arrow(d, x0, x1, y, th, head, fill):
    sign = 1 if x1 > x0 else -1
    neck = x1 - sign * head
    d.rectangle([min(x0, neck), y - th / 2, max(x0, neck), y + th / 2], fill=fill)
    d.polygon([(x1, y), (neck, y - head * 0.85), (neck, y + head * 0.85)], fill=fill)


def _symbol(img, value, cx, cy, size, color):
    """Draws a number or an action icon, centred at (cx, cy), about `size` pixels tall."""
    d = ImageDraw.Draw(img)

    if value.isdigit():
        fs = size * 1.5
        off = max(2, size * 0.05)
        _text(d, (cx + off, cy + off), value, fs, (25, 25, 25),
              stroke=max(1, size * 0.05), stroke_fill=(25, 25, 25))
        _text(d, (cx, cy), value, fs, color,
              stroke=max(1, size * 0.03), stroke_fill=color)
        if value in ("6", "9"):
            y = cy + size * 0.62
            d.rectangle([cx - size * 0.26, y, cx + size * 0.26, y + max(2, size * 0.07)],
                        fill=color)

    elif value == "skip":
        r = size * 0.52
        t = max(2, int(size * 0.15))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=t)
        k = r * 0.70
        d.line([cx - k, cy - k, cx + k, cy + k], fill=color, width=t)

    elif value == "reverse":
        layer = _layer(img.size)
        ld = ImageDraw.Draw(layer)
        th, head = size * 0.20, size * 0.30
        _arrow(ld, cx - size * 0.50, cx + size * 0.50, cy - size * 0.24, th, head, color)
        _arrow(ld, cx + size * 0.50, cx - size * 0.50, cy + size * 0.24, th, head, color)
        img.alpha_composite(layer.rotate(-38, resample=Image.BICUBIC, center=(cx, cy)))

    elif value == "draw2":
        w, h = size * 0.46, size * 0.66
        _mini_card(img, cx - size * 0.16, cy + size * 0.02, w, h, 14, color, border=(30, 30, 30))
        _mini_card(img, cx + size * 0.16, cy - size * 0.02, w, h, -14, color, border=(30, 30, 30))

    elif value == "wild":
        r = size * 0.52
        box = [cx - r, cy - r, cx + r, cy + r]
        d.pieslice(box, 180, 270, fill=RED)
        d.pieslice(box, 270, 360, fill=BLUE)
        d.pieslice(box, 0, 90, fill=GREEN)
        d.pieslice(box, 90, 180, fill=YELLOW)
        d.ellipse(box, outline=(25, 25, 25), width=max(1, int(size * 0.04)))

    elif value == "wild4":
        w, h = size * 0.42, size * 0.60
        spots = [
            (-0.20, -0.14, -24, RED), (0.16, -0.18, 14, BLUE),
            (-0.16, 0.16, 22, YELLOW), (0.20, 0.14, -12, GREEN),
        ]
        for dx, dy, ang, col in spots:
            _mini_card(img, cx + dx * size, cy + dy * size, w, h, ang, col, border=WHITE)


def _glyph(img, code, cx, cy, size):
    """The small corner marking: white with a dark outline."""
    value = code[2:]
    d = ImageDraw.Draw(img)
    if value.isdigit() or value in ("draw2", "wild4"):
        s = {"draw2": "+2", "wild4": "+4"}.get(value, value)
        _text(d, (cx, cy), s, size * 1.6, WHITE,
              stroke=max(1, size * 0.12), stroke_fill=(0, 0, 0))
    else:
        _symbol(img, value, cx, cy, size * 1.1, WHITE)


# ---------------------------------------------------------------- cards

@lru_cache(maxsize=160)
def card_image(code: str, width: int = HAND_CARD_W) -> Image.Image:
    """One card, e.g. 'R:5' (red 5), 'Y:skip', 'W:wild4'."""
    color, value = code[0], code[2:]
    w = width * SS
    h = int(w * CARD_RATIO)
    img = _layer((w, h))
    d = ImageDraw.Draw(img)

    r = int(w * 0.11)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=WHITE)
    inset = int(w * 0.075)
    d.rounded_rectangle([inset, inset, w - 1 - inset, h - 1 - inset],
                        radius=int(r * 0.75), fill=RGB[color])

    # the tilted oval in the middle
    cx, cy = w / 2, h / 2
    rx, ry = w * 0.34, h * 0.40
    oval = _layer((w, h))
    od = ImageDraw.Draw(oval)
    od.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=WHITE)
    if value == "wild":
        box = [cx - rx * 0.88, cy - ry * 0.88, cx + rx * 0.88, cy + ry * 0.88]
        od.pieslice(box, 180, 270, fill=RED)
        od.pieslice(box, 270, 360, fill=BLUE)
        od.pieslice(box, 0, 90, fill=GREEN)
        od.pieslice(box, 90, 180, fill=YELLOW)
    elif value == "wild4":
        od.ellipse([cx - rx * 0.88, cy - ry * 0.88, cx + rx * 0.88, cy + ry * 0.88], fill=BLACK)
    img.alpha_composite(oval.rotate(-32, resample=Image.BICUBIC))

    if value != "wild":
        scale = {"draw2": 1.35, "wild4": 1.6}.get(value, 1.0)
        sym_color = RGB[color] if color != "W" else WHITE
        _symbol(img, value, cx, cy, h * 0.30 * scale, sym_color)

    # corner markings (top-left, and the same turned upside down at bottom-right)
    gx, gy, cs = w * 0.17, h * 0.115, h * 0.075
    _glyph(img, code, gx, gy, cs)
    corner = _layer((w, h))
    _glyph(corner, code, gx, gy, cs)
    img.alpha_composite(corner.rotate(180, resample=Image.BICUBIC))

    return img.resize((width, int(width * CARD_RATIO)), Image.LANCZOS)


@lru_cache(maxsize=16)
def back_image(width: int = HAND_CARD_W) -> Image.Image:
    """The back of a card: black, with the red oval and the UNO lettering."""
    w = width * SS
    h = int(w * CARD_RATIO)
    img = _layer((w, h))
    d = ImageDraw.Draw(img)
    r = int(w * 0.11)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=WHITE)
    inset = int(w * 0.075)
    d.rounded_rectangle([inset, inset, w - 1 - inset, h - 1 - inset],
                        radius=int(r * 0.75), fill=BLACK)

    cx, cy = w / 2, h / 2
    oval = _layer((w, h))
    od = ImageDraw.Draw(oval)
    od.ellipse([cx - w * 0.40, cy - h * 0.27, cx + w * 0.40, cy + h * 0.27], fill=(214, 40, 40))
    img.alpha_composite(oval.rotate(28, resample=Image.BICUBIC))

    txt = _layer((w, h))
    td = ImageDraw.Draw(txt)
    fs = h * 0.17
    _text(td, (cx + w * 0.015, cy + h * 0.012), "UNO", fs, (0, 0, 0),
          stroke=max(2, w * 0.02), stroke_fill=(0, 0, 0))
    _text(td, (cx, cy), "UNO", fs, (255, 222, 0),
          stroke=max(2, w * 0.02), stroke_fill=(0, 0, 0))
    img.alpha_composite(txt.rotate(28, resample=Image.BICUBIC))

    return img.resize((width, int(width * CARD_RATIO)), Image.LANCZOS)


def _dim(card):
    shade = Image.new("RGBA", card.size, (0, 0, 0, 0))
    shade.paste((0, 0, 0, 150), (0, 0), card.getchannel("A"))
    return Image.alpha_composite(card, shade)


# ---------------------------------------------------------------- your hand

def render_hand(cards, playable, offset=0) -> bytes:
    """A picture of your cards. Cards you can play get a green outline, the rest are dimmed.
    Every card has a number tag (matching the buttons under the picture)."""
    n = len(cards)
    cols = max(3, min(5, n))
    rows = max(1, -(-n // 5))
    pad, gap, tag = 16, 12, 30
    cw = HAND_CARD_W
    ch = int(cw * CARD_RATIO)
    width = pad * 2 + cols * cw + (cols - 1) * gap
    height = pad * 2 + rows * (ch + tag) + (rows - 1) * gap

    img = Image.new("RGB", (width, height), (30, 33, 40))
    d = ImageDraw.Draw(img)

    for i, code in enumerate(cards):
        row, col = divmod(i, 5)
        x = pad + col * (cw + gap)
        y = pad + row * (ch + tag + gap)
        card = card_image(code, cw)
        if playable[i]:
            d.rounded_rectangle([x - 4, y - 4, x + cw + 3, y + ch + 3],
                                radius=14, outline=(110, 240, 150), width=3)
        else:
            card = _dim(card)
        img.paste(card, (x, y), card)

        tx, ty = x + cw // 2, y + ch + tag // 2 + 1
        d.ellipse([tx - 12, ty - 12, tx + 12, ty + 12],
                  fill=(70, 195, 110) if playable[i] else (62, 66, 78))
        _text(d, (tx, ty), str(offset + i + 1), 17, WHITE)

    return _png(img)


# ---------------------------------------------------------------- the table

@lru_cache(maxsize=2)
def _felt(w, h):
    grad = Image.radial_gradient("L").resize((w, h), Image.BILINEAR)   # black centre -> white edges
    light = Image.new("RGB", (w, h), (34, 120, 78))
    dark = Image.new("RGB", (w, h), (14, 62, 42))
    return Image.composite(dark, light, grad)


def _direction_layer(radius, direction):
    """Two curved arrows going around a circle. Clockwise, or mirrored for counter-clockwise."""
    pad = int(radius * 0.25)
    size = radius * 2 + pad * 2
    layer = _layer((size, size))
    d = ImageDraw.Draw(layer)
    c = size / 2
    col = (255, 255, 255, 200)
    t = max(3, int(radius * 0.05))
    box = [c - radius, c - radius, c + radius, c + radius]
    for start, end in ((215, 325), (35, 145)):
        d.arc(box, start, end, fill=col, width=t)
        a = math.radians(end)
        ex, ey = c + radius * math.cos(a), c + radius * math.sin(a)
        tx, ty = -math.sin(a), math.cos(a)     # direction of travel
        nx, ny = math.cos(a), math.sin(a)
        hl, hw = radius * 0.13, radius * 0.08
        d.polygon([(ex + tx * hl, ey + ty * hl),
                   (ex + nx * hw, ey + ny * hw),
                   (ex - nx * hw, ey - ny * hw)], fill=col)
    if direction == -1:
        layer = ImageOps.mirror(layer)
    return layer


def render_table(top, color, direction, deck_count, counts, current, pending=0) -> bytes:
    """The table: draw pile, top card, colour ring, direction, and a chip per player."""
    S = 2

    def px(v):
        return int(v * S)

    W, H = px(640), px(360)
    img = _felt(W, H).copy()
    d = ImageDraw.Draw(img, "RGBA")

    cx, cy = px(400), px(140)
    ring_r = px(108)
    rgb = RGB[color]

    # colour ring and direction arrows
    d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
              fill=rgb + (95,), outline=rgb + (255,), width=px(5))
    arrows = _direction_layer(px(126), direction)
    img.paste(arrows, (cx - arrows.width // 2, cy - arrows.height // 2), arrows)

    # draw pile (a little stack of card backs)
    pile_x, pile_y = px(150), px(140)
    back = back_image(px(112))
    for k in (3, 2, 1, 0):
        img.paste(back, (pile_x - back.width // 2 - k * px(3),
                         pile_y - back.height // 2 - k * px(3)), back)
    _text(d, (pile_x, pile_y + back.height // 2 + px(18)), str(deck_count),
          px(15), (255, 255, 255, 230))

    # top card, slightly tilted, with a shadow
    angle = sum(map(ord, top)) % 19 - 9
    card = card_image(top, px(116)).rotate(angle, resample=Image.BICUBIC, expand=True)
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), (0, 0), card.getchannel("A"))
    px0, py0 = cx - card.width // 2, cy - card.height // 2
    img.paste(shadow, (px0 + px(5), py0 + px(7)), shadow)
    img.paste(card, (px0, py0), card)

    # cards waiting to be drawn (only with stacking)
    if pending:
        bx, by = cx + int(ring_r * 0.80), cy - int(ring_r * 0.80)
        d.ellipse([bx - px(27), by - px(27), bx + px(27), by + px(27)],
                  fill=(200, 30, 30, 255), outline=(255, 255, 255, 255), width=px(3))
        _text(d, (bx, by), f"+{pending}", px(20), WHITE, stroke=px(1), stroke_fill=(0, 0, 0))

    # one chip per player: number, a small card back, and how many cards they hold
    n = len(counts)
    if n:
        pad, gap, chip_h = px(14), px(8), px(46)
        chip_w = min(px(104), (W - 2 * pad - (n - 1) * gap) // n)
        y0 = H - pad - chip_h
        x0 = (W - (n * chip_w + (n - 1) * gap)) // 2
        mini = back_image(px(24))
        for i, count in enumerate(counts):
            x = x0 + i * (chip_w + gap)
            active = i == current
            d.rounded_rectangle(
                [x, y0, x + chip_w, y0 + chip_h], radius=px(10), fill=(0, 0, 0, 150),
                outline=(255, 226, 90, 255) if active else (255, 255, 255, 70),
                width=px(3) if active else px(1),
            )
            bx, by = x + px(18), y0 + chip_h // 2
            d.ellipse([bx - px(12), by - px(12), bx + px(12), by + px(12)],
                      fill=(255, 226, 90, 255) if active else (255, 255, 255, 215))
            _text(d, (bx, by), str(i + 1), px(15), (20, 20, 20, 255))
            img.paste(mini, (x + px(38), y0 + (chip_h - mini.height) // 2), mini)
            _text(d, (x + chip_w - px(22), y0 + chip_h // 2), str(count), px(20),
                  (255, 110, 110, 255) if count == 1 else (255, 255, 255, 255))

    return _png(img.resize((W // S, H // S), Image.LANCZOS))


# ---------------------------------------------------------------- lobby banner

@lru_cache(maxsize=1)
def render_banner() -> bytes:
    W, H = 560, 250
    img = Image.new("RGB", (W, H), (30, 33, 40))
    cards = ["R:7", "Y:skip", "W:wild", "G:reverse", "B:draw2"]
    for i, code in enumerate(cards):
        card = card_image(code, 104).rotate((2 - i) * 11, resample=Image.BICUBIC, expand=True)
        cx = W // 2 + (i - 2) * 82
        cy = H // 2 + abs(i - 2) * 8
        img.paste(card, (cx - card.width // 2, cy - card.height // 2), card)
    return _png(img)
