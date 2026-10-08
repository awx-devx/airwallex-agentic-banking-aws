"""Make the kit's test-support module importable under pytest's importlib import mode."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
_REPO = Path(__file__).resolve().parents[3]  # for `infra` in the synth tests
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))
