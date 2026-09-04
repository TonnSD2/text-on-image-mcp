#!/usr/bin/env python3
"""Fonts regression test: Cyrillic coverage of every shipped TTF.

Every font listed in fonts/fonts.json is asked to rasterize the full 69-char
Cyrillic charset; each glyph is compared against a definitely-unmapped
codepoint (FreeType .notdef - the "tofu box" itself). A char that renders
identically to .notdef, or renders nothing, is missing from the font.

Families without Cyrillic upstream (latin-only Google Fonts builds: Poppins,
Bebas Neue, DM Sans, ...) must never enter the manifest - this test catches
that. It is also imported by download_fonts.py as a download-time gate.

Usage: python test_fonts.py [--sheet out.png]   (no server needed)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
CYR = ("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
       "абвгдеёжзийклмнопрстуфхцчшщъыьэюя«»№")
NOTDEF = "\U0010FFFF"  # never mapped: getBestCmap-safe .notdef probe


def _render(font: ImageFont.FreeTypeFont, ch: str):
    """Rasterize one char; None on failure, b'' when nothing is drawn."""
    im = Image.new("L", (140, 140), 0)
    try:
        ImageDraw.Draw(im).text((20, 20), ch, font=font, fill=255)
    except Exception:
        return None
    return im if im.getbbox() else b""


def missing_cyrillic(ttf_path) -> list[str]:
    """Chars of CYR the font cannot render (tofu box or blank)."""
    font = ImageFont.truetype(str(ttf_path), 36)
    nd = _render(font, NOTDEF)
    nd_bytes = nd.tobytes() if isinstance(nd, Image.Image) else None
    miss = []
    for ch in CYR:
        r = _render(font, ch)
        if r is None or r == b"" or (nd_bytes and r.tobytes() == nd_bytes):
            miss.append(ch)
    return miss


def cyrillic_ok(ttf_path) -> bool:
    return not missing_cyrillic(ttf_path)


def _sample_sheet(manifest: dict, out_path: str) -> None:
    fams = sorted(manifest)
    row_h, w = 64, 900
    img = Image.new("RGB", (w, row_h * len(fams)), "#122033")
    d = ImageDraw.Draw(img)
    for i, fam in enumerate(fams):
        rel = manifest[fam].get("regular") or next(iter(manifest[fam].values()))
        y = i * row_h + 12
        try:
            f = ImageFont.truetype(str(BASE / rel), 30)
            d.text((20, y), f"{fam}: Съешь ещё этих мягких булок, да выпей чаю 12345",
                   font=f, fill="#ffd54f")
        except Exception as e:
            d.text((20, y), f"{fam}: ERROR {e}", fill="#ff5252")
    img.save(out_path)


def main() -> None:
    manifest = json.loads((BASE / "fonts" / "fonts.json").read_text())
    bad = []
    for fam in sorted(manifest):
        missing: list[str] = []
        for style, rel in sorted(manifest[fam].items()):
            for ch in missing_cyrillic(BASE / rel):
                if ch not in missing:
                    missing.append(ch)
        if missing:
            bad.append((fam, len(missing)))
        print(f"{fam:22s} " + ("OK" if not missing else
              f"MISSING {len(missing)}/{len(CYR)}: {''.join(missing[:14])}"))
    if "--sheet" in sys.argv:
        out = sys.argv[sys.argv.index("--sheet") + 1]
        _sample_sheet(manifest, out)
        print("sample sheet:", out)
    if bad:
        print(f"FONTS TEST FAILED: {len(bad)} family/families without Cyrillic: "
              + ", ".join(f"{f} ({n})" for f, n in bad))
        sys.exit(1)
    print(f"FONTS TEST PASSED: {sum(len(v) for v in manifest.values())} TTFs / "
          f"{len(manifest)} families, every family renders the full Cyrillic charset")


if __name__ == "__main__":
    main()
