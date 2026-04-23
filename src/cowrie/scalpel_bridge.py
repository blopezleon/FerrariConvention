# ABOUTME: Bridge from cowrie source into the sibling scalpel/ package so
# ABOUTME: cowrie's protocol can call the three-tier router without needing
# ABOUTME: scalpel installed as a proper Python package.
"""Resolve scalpel/ (a sibling of src/) on sys.path and re-export the hook.

scalpel/ isn't listed in pyproject's setuptools.packages.find, so it's not
importable after `pip install -e .` on its own. Inserting the repo root on
sys.path at import time lets cowrie run with no changes to packaging. If
scalpel isn't present (e.g. running cowrie outside this repo), on_command
always returns None and cowrie keeps its existing behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

# src/cowrie/scalpel_bridge.py -> parents[0]=src/cowrie, [1]=src, [2]=repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from scalpel.cowrie_hook import on_command  # type: ignore[import-not-found]
except ImportError:
    def on_command(command: str, session_id: str) -> str | None:
        return None


__all__ = ["on_command"]
