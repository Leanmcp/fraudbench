"""Make local source importable and gate optional test dependencies."""

import importlib
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# Skip optional test surfaces when their dependencies are unavailable.
def _missing(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
        return False
    except ImportError:
        return True


collect_ignore = [
    "src/experiments"
]  # unrelated experimental code; needs `a2a`, not a repo dep
if _missing("pyaudio") or _missing("scipy"):
    collect_ignore += ["tests/test_voice", "tests/test_streaming"]
if _missing("gymnasium"):
    collect_ignore += ["tests/test_gym"]
