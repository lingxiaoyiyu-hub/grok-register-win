"""Shared fixtures. Keeps sys.path set up so tests can import project modules."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
PANEL = ROOT / "panel"

for _p in (str(ROOT), str(LIB), str(PANEL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 面板导入时会建 data/ 目录并起后台线程，指到临时目录避免污染工作区
os.environ.setdefault("PANEL_HOST", "127.0.0.1")
os.environ.setdefault("PANEL_PORT", "8787")
