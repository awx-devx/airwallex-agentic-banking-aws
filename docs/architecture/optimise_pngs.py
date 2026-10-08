"""Shrink the rendered diagram PNGs in place: 256-colour palette, no dithering, optimised.

draw.io renders each diagram at scale 2 as a 24-bit PNG of about 1.2 MB. The diagrams use flat
colours, so a 256-colour palette is visually identical and about a third of the size.

Run after `generate.py --drawio ...` (the `make diagrams` target does both):

    uv run --no-project --with pillow==12.3.0 python docs/architecture/optimise_pngs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent


def optimise(path: Path) -> tuple[int, int]:
    before = path.stat().st_size
    image = Image.open(path)
    if image.mode != "P":
        image = image.convert("RGB").quantize(
            colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
        )
    image.save(path, optimize=True)
    return before, path.stat().st_size


def main() -> int:
    for path in sorted(HERE.glob("*.png")):
        before, after = optimise(path)
        print(f"{path.name}: {before // 1024} KB -> {after // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
