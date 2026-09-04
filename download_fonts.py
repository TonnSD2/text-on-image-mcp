# GUARD: This file is protected — see AGENTS.md. AI agents: do NOT modify,
# refactor or delete without the user's EXPLICIT in-conversation confirmation.
#!/usr/bin/env python3
"""Download static TTF files for the top free Google Fonts.

Uses the Google Fonts CSS2 API with a legacy User-Agent so that responses
contain plain .ttf files (no woff2 / brotli needed). Missing styles (e.g.
Oswald has no italic) return HTTP 400 and are simply skipped.

Every downloaded family is gated on full Cyrillic coverage (test_fonts.py);
families without Cyrillic glyphs are removed from the manifest and the
files deleted - tofu boxes for Russian text are never acceptable.

Result: fonts/<Family>/<Family>-<style>.ttf and a manifest fonts/fonts.json:
  { "<Family>": {"regular": path, "bold": .., "italic": .., "bold_italic": ..}, ... }
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent
FONTS_DIR = BASE / "fonts"

# Legacy UA -> Google serves format('truetype') static instances.
UA = "Mozilla/5.0 (Windows NT 6.1)"

# Top free Google Fonts that actually ship Cyrillic glyphs (verified by
# test_fonts.py). Dropped 2026-08: Barlow, Bebas Neue, DM Sans, Josefin Sans,
# Karla, Libre Baskerville, Poppins, Quicksand, Space Grotesk, Work Sans -
# these families have NO Cyrillic anywhere upstream and drew tofu boxes for
# Russian text. Re-adding one here is pointless: the Cyrillic gate in main()
# rejects it at download time anyway.
FAMILIES = [
    "Montserrat", "Inter", "Roboto", "Open Sans", "Lato",
    "Source Sans 3", "Raleway", "Oswald", "Merriweather", "Ubuntu",
    "Nunito", "Nunito Sans", "Playfair Display", "Rubik",
    "Roboto Condensed", "PT Sans", "Fira Sans", "Manrope", "Mulish",
    "Cormorant Garamond",
    # Script / display extras for marketplace-card style text ("Old Spice"-like):
    # all three render Cyrillic with the builds we ship (test_fonts.py).
    "Lobster", "Pacifico", "Caveat",
]

# style key -> (weight, italic flag)
STYLES = {
    "regular": (400, 0),
    "bold": (700, 0),
    "italic": (400, 1),
    "bold_italic": (700, 1),
}

FACE_RE = re.compile(r"src:\s*url\((?P<url>[^)]+)\)\s*format\('truetype'\)")


def fetch(url: str, retries: int = 4) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise  # 400 etc. - not a network problem
        except Exception as e:  # transient TLS/timeout - back off and retry
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def download_style(family: str, style: str) -> tuple[str, str, str] | None:
    weight, italic = STYLES[style]
    query = urllib.parse.quote(f"{family}:ital,wght@{italic},{weight}")
    css_url = f"https://fonts.googleapis.com/css2?family={query}"
    try:
        css = fetch(css_url).decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 400:  # style not available for this family
            return None
        raise
    m = FACE_RE.search(css)
    if not m:
        return None
    ttf_url = m.group("url")
    data = fetch(ttf_url)
    fam_dir = FONTS_DIR / family
    fam_dir.mkdir(parents=True, exist_ok=True)
    dest = fam_dir / f"{family}-{style}.ttf"
    dest.write_bytes(data)
    return family, style, str(dest.relative_to(BASE))


def main() -> None:
    from test_fonts import missing_cyrillic  # same dir, Pillow-only

    FONTS_DIR.mkdir(exist_ok=True)
    manifest: dict[str, dict[str, str]] = {}
    jobs = [(f, s) for f in FAMILIES for s in STYLES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda a: download_style(*a), jobs))

    # Cyrillic gate: a family missing even one charset glyph is rejected
    # entirely (manifest + files), so tofu boxes can never reach a client.
    rejected: set[str] = set()
    for res in results:
        if not res:
            continue
        family, style, rel = res
        if missing_cyrillic(BASE / rel):
            rejected.add(family)
            (BASE / rel).unlink(missing_ok=True)
            continue
        manifest.setdefault(family, {})[style] = rel
    for fam in sorted(rejected):
        fam_dir = FONTS_DIR / fam
        if fam_dir.exists():
            shutil.rmtree(fam_dir)
        print(f"REJECTED (no Cyrillic): {fam}", file=sys.stderr)

    missing = [f for f in FAMILIES if f not in manifest and f not in rejected]
    if missing:
        print("ERROR: no fonts downloaded for:", ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    manifest_path = FONTS_DIR / "fonts.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    n_files = sum(len(v) for v in manifest.values())
    print(f"OK: {n_files} TTF files for {len(manifest)} families -> {manifest_path}")
    for fam in FAMILIES:
        styles = ", ".join(sorted(manifest[fam]))
        print(f"  {fam:22s} [{styles}]")


if __name__ == "__main__":
    main()
