"""Make the kit's test fixtures (and, for the synth tests, the repo's `infra` package)
importable under pytest's importlib import mode."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (_HERE, _HERE.parents[2]):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
