import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for p in (_HERE, _HERE / "fixtures", _REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
