"""Ensure the repo root is on ``sys.path`` so ``benchmarks.routing.*``
imports resolve when pytest is invoked with a path under benchmarks/.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
