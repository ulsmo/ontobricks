"""
Tool definitions for the Task Router agent.

The router is a single-shot classifier and intentionally exposes **no** tools:
it reads the task + the static agent registry and returns a routing decision in
one LLM call. These empty collections exist only for parity with the other
agent packages (every ``agent_*`` package ships a ``tools`` module).
"""

from typing import Callable, Dict, List

TOOL_DEFINITIONS: List[dict] = []
TOOL_HANDLERS: Dict[str, Callable] = {}
