"""The planner is single-shot and uses no tools (kept for package symmetry)."""
from __future__ import annotations

from typing import Callable, Dict, List

TOOL_DEFINITIONS: List[dict] = []
TOOL_HANDLERS: Dict[str, Callable] = {}
