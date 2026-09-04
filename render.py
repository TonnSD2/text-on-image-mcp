# GUARD: This file is protected — see AGENTS.md. AI agents: do NOT modify,
# refactor or delete without the user's EXPLICIT in-conversation confirmation.
"""Rendering pipeline: output image = pure function of scene state.

All drawing is native Pillow (FreeType text, ImageDraw shapes, ImageOps /
ImageEnhance / ImageChops effects) - CPU only, no OpenGL, no extra deps.

Per object a full-canvas RGBA layer is produced, then: shadow -> glow ->
opacity -> alpha_composite. Z-order is the objects list order.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageFont

import scene as S

KINDS = {"rectangle", "rounded_rectangle", "ellipse", "line", "polygon",
         "regular_polygon", "arrow"}


# ---------------------------------------------------------------------------
# shared layer helpers
# ---------------------------------------------------------------------------

def _hex_rgb(color: str) -> tuple[int, int, int]:
    return S.hex_to_rgb(color)


def _tinted_blur(layer: Image.Image, color: str, opacity: float,
                 blur: float) -> Image.Image:
    """Copy of layer's silhouette, tinted `color` and blurred (glow/shadow)."""
    rgb = _hex_rgb(color)
    out = Image.new("RGBA", layer.size, (rgb[0], rgb[1], rgb[2], 0))
    alpha = layer.getchannel("A").point(lambda a: int(a * opacity))
    out.putalpha(alpha)
    if blur > 0:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    return out


def _apply_opacity(layer: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1:
        return layer
    alpha = layer.getchannel("A").point(lambda a: int(a * opacity))
    out = layer.copy()
    out.putalpha(alpha)
    return out


def _finish(obj: dict, layer: Image.Image) -> Image.Image:
    """Common tail: drop shadow, glow, per-object opacity."""
    sh = obj.get("shadow")
    if sh:
        shadow = _tinted_blur(layer, sh.get("color", "#000000"),
                              float(sh.get("opacity", 0.6)),
                              float(sh.get("blur", 0)))
        layer.alpha_composite(shadow,
                              (int(sh.get("dx", 4)), int(sh.get("dy", 6))))
    glow = obj.get("glow")
    if glow:
        g = _tinted_blur(layer, glow.get("color", "#ffffff"),
                         float(glow.get("opacity", 0.8)),
                         float(glow.get("blur", 12)))
        layer.alpha_composite(g)
    return _apply_opacity(layer, float(obj.get("opacity", 1.0)))


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

def _faux_italic(layer: Image.Image) -> Image.Image:
    """CPU skew of the tight text bbox to fake italics."""
    bbox = layer.getbbox()
    if not bbox:
        return layer
    x0, y0, x1, y1 = bbox
    pad = 4
    box = (max(0, x0 - pad), max(0, y0 - pad),
           min(layer.width, x1 + pad), min(layer.height, y1 + pad))
    crop = layer.crop(box)
    w, h = crop.size
    shear = S.FAKE_ITALIC_SHEAR
    new_w = w + int(shear * h)
    # output(x,y) samples input(x + shear*y - shear*(h-1), y): top leans right
    sheared = crop.transform((new_w, h), Image.AFFINE,
                             (1, shear, -shear * (h - 1), 0, 1, 0),
                             resample=Image.BICUBIC)
    out = layer.copy()
    out.paste(sheared, (box[0], box[1]), sheared)
    return out


def _draw_text(draw, xy, text, font, fill, anchor, spacing, align,
               stroke_width, stroke_fill, angle: float = 0) -> None:
    """ImageDraw.text with multiline-safe anchors.

    Pillow raises 'bad anchor specified: mo' even for single-line text with a
    bottom-middle anchor (and for any multiline m/o anchor), so emulate those
    by drawing at the ascender anchor with a measured vertical offset."""
    multiline = "\n" in str(text)
    if len(anchor) == 2 and anchor[1] in "mo" and (anchor[1] == "o" or multiline):
        base = anchor[0] + "a"
        if angle:
            if multiline:
                draw.multiline_text(xy, text, font=font, fill=fill, anchor=base,
                                    spacing=spacing, align=align,
                                    stroke_width=stroke_width,
                                    stroke_fill=stroke_fill, angle=angle)
            else:
                draw.text(xy, text, font=font, fill=fill, anchor=base,
                          stroke_width=stroke_width, stroke_fill=stroke_fill,
                          angle=angle)
            return
        if multiline:
            _, t, _, b = draw.multiline_textbbox(
                xy, text, font=font, spacing=spacing, align=align,
                stroke_width=stroke_width)
        else:
            _, t, _, b = draw.textbbox(xy, text, font=font, anchor=base,
                                       stroke_width=stroke_width)
        dy = -((b - t) // 2 if anchor[1] == "m" else (b - t))
        xy = (xy[0], xy[1] + dy)
        if multiline:
            draw.multiline_text(xy, text, font=font, fill=fill, anchor=base,
                                spacing=spacing, align=align,
                                stroke_width=stroke_width,
                                stroke_fill=stroke_fill)
        else:
            draw.text(xy, text, font=font, fill=fill, anchor=base,
                      stroke_width=stroke_width, stroke_fill=stroke_fill)
        return
    draw.text(xy, text, font=font, fill=fill, anchor=anchor, spacing=spacing,
              align=align, stroke_width=stroke_width,
              stroke_fill=stroke_fill, angle=angle)


# --- rich runs: several styled segments on shared baselines ----------------

_ANCHOR_POS = {
    "top-left": ("top", "left"), "top-center": ("top", "center"),
    "top-right": ("top", "right"), "center-left": ("center", "left"),
    "center": ("center", "center"), "center-right": ("center", "right"),
    "bottom-left": ("bottom", "left"), "bottom-center": ("bottom", "center"),
    "bottom-right": ("bottom", "right"),
}


def _font_for(family: str, size: int, bold: bool, italic: bool):
    path, faux_bold, faux_italic = S.font_path(family, bold, italic)
    return S.get_font(path, int(size)), faux_bold, faux_italic


def _resolve_runs(obj: dict) -> list[list[dict]]:
    """obj['runs'] -> visual lines; run style fields default to the object."""
    base = {"family": obj["family"], "size": int(obj["size"]),
            "bold": bool(obj.get("bold", False)),
            "italic": bool(obj.get("italic", False)),
            "color": obj.get("color", "#ffffff")}
    lines: list[list[dict]] = [[]]
    for run in obj["runs"]:
        r = {
            "family": run.get("family", base["family"]),
            "size": int(run.get("size", base["size"])),
            "bold": run.get("bold", base["bold"]),
            "italic": run.get("italic", base["italic"]),
            "color": run.get("color", base["color"]),
        }
        parts = str(run.get("text", "")).split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append([])
            if part:
                lines[-1].append({**r, "text": part})
    return [ln for ln in lines if ln]


def _run_metrics(obj: dict):
    """Per line: [(run, font, faux_bold, faux_italic, w, ascent, descent)]."""
    out = []
    for ln in _resolve_runs(obj):
        infos = []
        for r in ln:
            f, fb, fi = _font_for(r["family"], r["size"], r["bold"], r["italic"])
            a, d = f.getmetrics()
            infos.append((r, f, fb, fi, f.getlength(r["text"]), a, d))
        out.append(infos)
    return out


def _run_gap(prev_run: dict) -> float:
    return 0.12 * prev_run["size"]


def text_runs_width(obj: dict) -> float:
    """Widest rendered line of a runs object (for auto-fit/measure)."""
    best = 0.0
    for infos in _run_metrics(obj):
        w = sum(i[4] for i in infos)
        w += sum(_run_gap(infos[i][0]) for i in range(len(infos) - 1))
        best = max(best, w)
    return best


def _draw_runs(layer: Image.Image, obj: dict, plain: bool = False) -> None:
    """Draw runs onto `layer` honoring obj anchor/align/outline/line_spacing.
    faux-italic runs get their own micro-layer + CPU shear. angle is ignored
    for runs (baseline model assumes horizontal text)."""
    draw = ImageDraw.Draw(layer)
    outline = obj.get("outline")
    ls = float(obj.get("line_spacing", 1.2))
    factor = max(1.0, 1.0 + 0.45 * (ls - 1.0))
    lines = _run_metrics(obj)
    widths, heights = [], []
    for infos in lines:
        w = sum(i[4] for i in infos)
        w += sum(_run_gap(infos[i][0]) for i in range(len(infos) - 1))
        widths.append(w)
        heights.append(max((i[5] + i[6] for i in infos), default=10) * factor)
    block_w = max(widths, default=0)
    block_h = sum(heights)
    v, hpos = _ANCHOR_POS.get(obj.get("anchor", "top-left"), ("top", "left"))
    ty = {"top": obj["y"], "center": obj["y"] - block_h / 2,
          "bottom": obj["y"] - block_h}[v]
    bx = {"left": obj["x"], "center": obj["x"] - block_w / 2,
          "right": obj["x"] - block_w}[hpos]
    align = obj.get("align", "left")
    acc = 0.0
    for infos, w, lh in zip(lines, widths, heights):
        shift = {"left": 0.0, "center": (block_w - w) / 2,
                 "right": block_w - w}[align]
        px = bx + shift
        baseline = ty + acc + max(i[5] for i in infos)
        for r, f, fb, fi, rw, _a, _d in infos:
            if plain:
                sw, sc = 0, r["color"]
            elif outline:
                sw = int(outline.get("width", 0))
                sc = outline.get("color", "#000000")
            elif fb:
                sw, sc = max(1, r["size"] // 25), r["color"]
            else:
                sw, sc = 0, r["color"]
            if fi and not plain:
                sub = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                ImageDraw.Draw(sub).text(
                    (px, baseline), r["text"], font=f, fill=r["color"],
                    anchor="ls", stroke_width=sw, stroke_fill=sc)
                layer.alpha_composite(_faux_italic(sub))
            else:
                draw.text((px, baseline), r["text"], font=f, fill=r["color"],
                          anchor="ls", stroke_width=sw, stroke_fill=sc)
            px += rw + _run_gap(r)
        acc += lh


def text_layer(size: tuple[int, int], obj: dict) -> Image.Image:
    if obj.get("runs"):
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        _draw_runs(layer, obj)
        return _finish(obj, layer)
    font, faux_bold, faux_italic = _load_font(obj)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    anchor = S.ANCHORS.get(obj.get("anchor", "top-left"), "la")
    color = obj.get("color", "#ffffff")
    outline = obj.get("outline")
    if outline:
        stroke_w = int(outline.get("width", 0))
        stroke_c = outline.get("color", "#000000")
    elif faux_bold:
        stroke_w, stroke_c = max(1, obj["size"] // 25), color
    else:
        stroke_w, stroke_c = 0, color
    angle = float(obj.get("angle", 0))
    if faux_italic and angle != 0:
        faux_italic = False  # shear assumes horizontal baseline
    _draw_text(draw, (obj["x"], obj["y"]), obj["text"], font=font, fill=color,
               anchor=anchor, spacing=float(obj.get("line_spacing", 1.2)),
               align=obj.get("align", "left"), stroke_width=stroke_w,
               stroke_fill=stroke_c, angle=angle)
    if faux_italic:
        layer = _faux_italic(layer)
    return _finish(obj, layer)


def _load_font(obj: dict):
    path, faux_bold, faux_italic = S.font_path(
        obj["family"], obj.get("bold", False), obj.get("italic", False))
    return S.get_font(path, obj["size"]), faux_bold, faux_italic


def text_bbox(state: dict, obj: dict):
    size = (max(1, state.get("width", 1000)),
            max(1, state.get("height", 1000)))
    if obj.get("runs"):
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        _draw_runs(layer, obj, plain=True)
        return layer.getbbox()
    font, _, _ = _load_font(obj)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    _draw_text(ImageDraw.Draw(layer), (obj["x"], obj["y"]), obj["text"],
               font=font, fill="#ffffff",
               anchor=S.ANCHORS.get(obj.get("anchor", "top-left"), "la"),
               spacing=float(obj.get("line_spacing", 1.2)),
               align=obj.get("align", "left"), stroke_width=0,
               stroke_fill="#ffffff")
    return layer.getbbox()



# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------

def _draw_shape(draw: ImageDraw.ImageDraw, obj: dict, dx: float = 0, dy: float = 0):
    """Draw shape content onto `draw` (dx/dy offset for temp-canvas mode)."""
    kind = obj["kind"]
    fill = obj.get("fill", "#ffffff")
    stroke = obj.get("stroke")
    sw = int(obj.get("stroke_width", 0))
    x, y = obj["x"] + dx, obj["y"] + dy
    w, h = obj["w"], obj["h"]
    if kind == "rectangle":
        draw.rectangle((x, y, x + w, y + h), fill=fill, outline=stroke, width=sw)
    elif kind == "rounded_rectangle":
        r = min(obj.get("corner_radius", 12), w / 2, h / 2)
        draw.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=fill,
                               outline=stroke, width=sw)
    elif kind == "ellipse":
        draw.ellipse((x, y, x + w, y + h), fill=fill, outline=stroke, width=sw)
    elif kind == "line":
        (x1, y1), (x2, y2) = obj["points"]
        draw.line((x1 + dx, y1 + dy, x2 + dx, y2 + dy), fill=fill,
                  width=int(obj.get("stroke_width", 4) or 4), joint="curve")
    elif kind == "arrow":
        (x1, y1), (x2, y2) = obj["points"]
        x1, y1, x2, y2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        width = int(obj.get("stroke_width", 4) or 4)
        head = int(obj.get("head_size", max(14, width * 4)))
        ang = math.atan2(y2 - y1, x2 - x1)
        bx, by = x2 - head * 0.6 * math.cos(ang), y2 - head * 0.6 * math.sin(ang)
        draw.line((x1, y1, bx, by), fill=fill, width=width, joint="curve")
        perp = ang + math.pi / 2
        p1 = (x2, y2)
        p2 = (bx + head / 2 * math.cos(perp), by + head / 2 * math.sin(perp))
        p3 = (bx - head / 2 * math.cos(perp), by - head / 2 * math.sin(perp))
        draw.polygon([p1, p2, p3], fill=fill)
    elif kind == "polygon":
        pts = [(px + dx, py + dy) for px, py in obj["points"]]
        draw.polygon(pts, fill=fill, outline=stroke)
        if sw > 0 and stroke:
            draw.line(pts + [pts[0]], fill=stroke, width=sw, joint="curve")
    elif kind == "regular_polygon":
        cx, cy, r = x + w / 2, y + h / 2, min(w, h) / 2
        draw.regular_polygon((cx, cy, r), n_sides=int(obj.get("sides", 6)),
                             rotation=float(obj.get("sides_rotation", 0)),
                             fill=fill, outline=stroke)
    else:
        raise ValueError(f"Unknown shape kind '{kind}'")


def _shape_bbox(obj: dict) -> tuple[float, float, float, float]:
    """Tight-ish drawing box (padded) used for gradients/rotation."""
    if obj["kind"] in ("line", "arrow", "polygon"):
        xs = [p[0] for p in obj["points"]]
        ys = [p[1] for p in obj["points"]]
        pad = max(20, int(obj.get("stroke_width", 0)))
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    return (obj["x"] - 20, obj["y"] - 20,
            obj["x"] + obj["w"] + 20, obj["y"] + obj["h"] + 20)


def _gradient_image(w: int, h: int, grad: dict) -> Image.Image:
    """Linear/radial RGB gradient between two hex colors (native Pillow).
    linear: `from` -> `to`; `angle` (deg, clockwise) points from `from` to
    `to`: 0 = down, 90 = right, 180 = up, 270 = left. radial: from at center."""
    c1 = _hex_rgb(grad["from"])
    c2 = _hex_rgb(grad["to"])
    s1 = Image.new("RGB", (w, h), c1)
    s2 = Image.new("RGB", (w, h), c2)
    if grad.get("kind", "linear") == "radial":
        g = Image.radial_gradient("L").resize((w, h), Image.BILINEAR)
        return Image.composite(s1, s2, g)  # 255 at center -> from
    angle = math.radians(float(grad.get("angle", 0)))
    ux, uy = math.sin(angle), math.cos(angle)   # 0 deg -> increase downward
    # exact range of the rectangle's projection onto the gradient axis
    L = w * abs(ux) + h * abs(uy) or 1.0
    n_w, n_h = max(2, min(w, 200)), max(2, min(h, 200))
    px = bytearray(n_w * n_h)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    for yy in range(n_h):
        y = yy * (h - 1) / (n_h - 1)
        base = (y - cy) * uy
        row = yy * n_w
        for xx in range(n_w):
            x = xx * (w - 1) / (n_w - 1)
            p = (x - cx) * ux + base
            px[row + xx] = max(0, min(255, int(255 * (p / L + 0.5))))
    g = Image.frombytes("L", (n_w, n_h), bytes(px)).resize((w, h),
                                                           Image.BILINEAR)
    return Image.composite(s2, s1, g)  # 255 -> to, 0 -> from


def _draw_gradient_shape(layer: Image.Image, obj: dict) -> None:
    """Fill the shape silhouette with a gradient; solid stroke on top."""
    x0, y0, x1, y1 = (int(v) for v in _shape_bbox(obj))
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    mask = Image.new("L", (w, h), 0)
    _draw_shape(ImageDraw.Draw(mask),
                {**obj, "fill": 255, "stroke": None, "stroke_width": 0},
                dx=-x0, dy=-y0)
    grad = _gradient_image(w, h, obj["fill_gradient"]).convert("RGBA")
    grad.putalpha(mask)
    layer.alpha_composite(grad, (x0, y0))
    if obj.get("stroke") and int(obj.get("stroke_width", 0)) > 0:
        _draw_shape(ImageDraw.Draw(layer), {**obj, "fill": None})


def shape_layer(size, obj):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    rotation = float(obj.get("rotation", 0))
    if obj.get("fill_gradient"):
        _draw_gradient_shape(layer, obj)
        if rotation != 0:  # rotate the built content around its bbox center
            box = tuple(int(v) for v in _shape_bbox(obj))
            rot = layer.crop(box).rotate(-rotation, expand=True,
                                         resample=Image.BICUBIC)
            layer = Image.new("RGBA", size, (0, 0, 0, 0))
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            layer.paste(rot, (int(cx - rot.width / 2),
                              int(cy - rot.height / 2)), rot)
        return _finish(obj, layer)
    if rotation == 0:
        _draw_shape(ImageDraw.Draw(layer), obj)
        return _finish(obj, layer)
    # rotate around the shape's bbox center: draw on a temp canvas, rotate,
    # paste back centered on the same point (all native Pillow).
    if obj["kind"] in ("line", "arrow", "polygon"):
        xs = [p[0] for p in obj["points"]]
        ys = [p[1] for p in obj["points"]]
        pad = 20
        box = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    else:
        box = (obj["x"] - 20, obj["y"] - 20,
               obj["x"] + obj["w"] + 20, obj["y"] + obj["h"] + 20)
    w = int(box[2] - box[0]); h = int(box[3] - box[1])
    tmp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    _draw_shape(ImageDraw.Draw(tmp), obj, dx=-box[0], dy=-box[1])
    tmp = tmp.rotate(-rotation, expand=True, resample=Image.BICUBIC)
    cx = (box[0] + box[2]) / 2; cy = (box[1] + box[3]) / 2
    layer.paste(tmp, (int(cx - tmp.width / 2), int(cy - tmp.height / 2)), tmp)
    return _finish(obj, layer)


# ---------------------------------------------------------------------------
# badge & callout (composite: plate + text, plate size derived from text)
# ---------------------------------------------------------------------------

def _text_metrics(font, text: str, line_spacing: float):
    lines = text.split("\n")
    widths = [font.getlength(ln) for ln in lines]
    ascent, descent = font.getmetrics()
    line_h = (ascent + descent) * line_spacing
    return max(widths, default=0), line_h * len(lines)


def _plate_text(size, obj, tail: str | None = None):
    font = S.get_font(
        S.font_path(obj["family"], obj.get("bold", True), False)[0],
        int(obj["size"]))
    ls = 1.15
    tw, th = _text_metrics(font, obj["text"], ls)
    pad_x = float(obj.get("padding_x", obj["size"] * 0.9))
    pad_y = float(obj.get("padding_y", obj["size"] * 0.45))
    x, y = obj["x"], obj["y"]
    w = tw + 2 * pad_x
    h = th + 2 * pad_y
    obj = {**obj, "w": w, "h": h}  # for _finish/shadow consumers
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    color = obj.get("color", "#e53935")
    text_color = obj.get("text_color", "#ffffff")
    if tail == "down":
        draw.polygon([(x + w * 0.25, y + h - 2), (x + w * 0.45, y + h - 2),
                      (x + w * 0.3, y + h + h * 0.25)], fill=color)
    elif tail == "up":
        draw.polygon([(x + w * 0.25, y + 2), (x + w * 0.45, y + 2),
                      (x + w * 0.3, y - h * 0.25)], fill=color)
    elif tail == "left":
        draw.polygon([(x + 2, y + h * 0.3), (x + 2, y + h * 0.55),
                      (x - w * 0.12, y + h * 0.42)], fill=color)
    elif tail == "right":
        draw.polygon([(x + w - 2, y + h * 0.3), (x + w - 2, y + h * 0.55),
                      (x + w + w * 0.12, y + h * 0.42)], fill=color)
    radius = h / 2 if obj["type"] == "badge" else float(obj.get("corner_radius", 14))
    draw.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=color)
    draw.text((x + w / 2, y + h / 2), obj["text"], font=font, fill=text_color,
              anchor="mm", spacing=ls, align="center")
    return _finish(obj, layer)


# ---------------------------------------------------------------------------
# image object
# ---------------------------------------------------------------------------

def image_layer(size, obj):
    src = Image.open(obj["asset"]).convert("RGBA")
    w = int(obj["w"])
    if obj.get("fit", "contain") == "cover" and obj.get("h"):
        src = ImageOps.fit(src, (w, int(obj["h"])), method=Image.BICUBIC)
    else:
        h = max(1, round(src.height * w / src.width))
        src = src.resize((w, h), Image.BICUBIC)
    cr = int(obj.get("corner_radius", 0))
    if cr > 0:
        mask = Image.new("L", src.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, src.width - 1, src.height - 1),
            radius=min(cr, src.height // 2), fill=255)
        src.putalpha(ImageChops.multiply(src.getchannel("A"), mask))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.alpha_composite(src, (int(obj["x"]), int(obj["y"])))
    return _finish(obj, layer)



# ---------------------------------------------------------------------------
# global effects applied to the base photo before objects
# ---------------------------------------------------------------------------

def _vignette_mask(w: int, h: int, strength: float) -> Image.Image:
    """Radial alpha mask computed on a tiny 256x256 grid, then upscaled."""
    n = 256
    px = bytearray(n * n)
    max_d = math.hypot(n / 2, n / 2)
    for yy in range(n):
        for xx in range(n):
            d = math.hypot(xx - n / 2, yy - n / 2) / max_d
            a = max(0.0, min(1.0, (d - 0.45) / 0.55)) ** 1.5 * strength * 255
            px[yy * n + xx] = int(a)
    return Image.frombytes("L", (n, n), bytes(px)).resize((w, h), Image.BILINEAR)


def apply_effect(img: Image.Image, eff: dict) -> Image.Image:
    kind = eff["kind"]
    if kind in ("brightness", "contrast", "saturation", "sharpness"):
        cls = {"brightness": ImageEnhance.Brightness,
               "contrast": ImageEnhance.Contrast,
               "saturation": ImageEnhance.Color,
               "sharpness": ImageEnhance.Sharpness}[kind]
        return cls(img).enhance(float(eff["factor"]))
    if kind == "grayscale":
        return img.convert("L").convert("RGBA")
    if kind == "sepia":
        g = img.convert("L")
        r = g.point(lambda v: min(255, int(v * 1.2 + 20)))
        b = g.point(lambda v: int(v * 0.82))
        return Image.merge("RGB", (r, g, b)).convert("RGBA")
    if kind == "rotate":
        return img.rotate(float(eff["angle"]), expand=True,
                          resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))
    if kind == "flip":
        m = (Image.FLIP_LEFT_RIGHT if eff.get("direction", "h") == "h"
             else Image.FLIP_TOP_BOTTOM)
        return img.transpose(m)
    if kind == "resize":
        return img.resize((int(eff["width"]), int(eff["height"])), Image.BICUBIC)
    if kind == "crop":
        return img.crop(tuple(int(v) for v in eff["box"]))
    if kind == "pad":
        return ImageOps.expand(img, border=int(eff.get("px", 20)),
                               fill=eff.get("color", "#ffffff"))
    if kind == "tint":
        solid = Image.new("RGB", img.size, _hex_rgb(eff["color"]))
        multiplied = ImageChops.multiply(img.convert("RGB"), solid).convert("RGBA")
        return Image.blend(img, multiplied, float(eff.get("strength", 0.3)))
    if kind == "vignette":
        v = Image.new("RGBA", img.size, (0, 0, 0, 0))
        v.putalpha(_vignette_mask(*img.size, float(eff.get("strength", 0.5))))
        out = img.copy()
        out.alpha_composite(v)
        return out
    if kind == "blur_area":
        x, y, w, h = (int(v) for v in eff["box"])
        region = img.crop((x, y, x + w, y + h)).filter(
            ImageFilter.GaussianBlur(float(eff.get("radius", 15))))
        out = img.copy()
        out.paste(region, (x, y))
        return out
    raise ValueError(f"Unknown effect '{kind}'")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

RENDERERS = {
    "text": text_layer,
    "shape": shape_layer,
    "badge": lambda s, o: _plate_text(s, o),
    "callout": lambda s, o: _plate_text(s, o, tail=o.get("tail", "down")),
    "image": image_layer,
}


def render(state: dict, output_path: Path) -> str:
    """Full deterministic re-render: photo -> effects -> objects in z-order."""
    if not state.get("image"):
        raise ValueError("No base image loaded - call load_image first.")
    img = Image.open(state["image"]).convert("RGBA")
    for eff in state.get("effects", []):
        img = apply_effect(img, eff)
    for obj in state.get("objects", []):
        fn = RENDERERS.get(obj.get("type"))
        if fn is None:
            continue
        img.alpha_composite(fn(img.size, obj))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(output_path)
    state["width"], state["height"] = img.size
    return str(output_path)

