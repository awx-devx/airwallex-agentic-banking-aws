"""Shrink the rendered diagram PNGs in place: 256-colour palette, no dithering, optimised.

draw.io renders each diagram at scale 2 as a 24-bit PNG of about 1.2 MB. The diagrams use flat
colours, so a 256-colour palette is visually identical and about a third of the size.

Run after `generate.py --drawio ...` (the `make diagrams` target does both). With no arguments
it shrinks every PNG here and in `overview/`; given paths, only those:

    uv run --no-project --with pillow==12.3.0 python docs/architecture/optimise_pngs.py [PNG ...]
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


def main(argv: list[str]) -> int:
    """Every PNG here and in overview/, or only the paths given."""
    paths = [Path(a).resolve() for a in argv] or [
        *sorted(HERE.glob("*.png")),
        *sorted((HERE / "overview").glob("*.png")),
    ]
    for path in paths:
        before, after = optimise(path)
        print(f"{path.relative_to(HERE)}: {before // 1024} KB -> {after // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
