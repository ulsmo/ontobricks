"""
Task Planner agent engine.

Single-shot gate that runs BEFORE a specialized agent does any work. Given the
task, the agent already chosen by the router, and the clarification Q&A so far
(reconstructed from the Discussion thread), the LLM decides whether it has
enough to act confidently (``ready=true``) or must ask the assignee more
(``ready=false`` + a short plan/question ``message``). JSON in / JSON out.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from agents.engine_base import (
    AgentStep,
    accumulate_usage,
    call_serving_endpoint,
    extract_message_content,
)
from agents.registry import AgentSpec
from agents.tracing import trace_agent
from back.core.logging import get_logger

logger = get_logger(__name__)

LLM_TIMEOUT = 60
_TRACE_NAME = "task_planner"


@dataclass
class PlanResult:
    """Outcome of a single planning turn."""

    success: bool
    ready: bool = False
    message: str = ""
    steps: List[AgentStep] = field(default_factory=list)
    iterations: int = 0
    error: str = ""
    usage: Dict[str, int] = field(default_factory=dict)


_SYSTEM_PROMPT = """\
You are the Task Planner for OntoBricks. A teammate assigned a work item to the
"AI Agent", and a specialized agent has already been selected to carry it out.
Before that agent runs, you confirm scope with the teammate.

You are given: the task, the selected agent's purpose, and the conversation so
far (your earlier questions and the teammate's replies, if any).

RULES
- On the FIRST turn (no teammate replies yet) you are NOT ready: briefly state
  what you will do and ask any clarifying question(s) needed to act safely.
- Mark ready=true ONLY once the teammate has replied with enough to proceed
  (an approval or the missing detail). When unsure, ask one more focused
  question rather than guessing.
- Keep messages short and concrete. No code fences.
- Reply with ONLY a JSON object:
  {"ready": <true|false>, "message": "<plan/question, or a one-line 'running now'>"}
"""


def _build_user_prompt(
    task_title: str,
    task_description: str,
    agent: AgentSpec,
    history: List[dict],
) -> str:
    lines = [
        f"SELECTED AGENT: {agent.label} — {agent.description}",
        "",
        f"TASK TITLE: {task_title}",
    ]
    if task_description:
        lines.append(f"TASK DESCRIPTION: {task_description}")
    lines.append("")
    if history:
        lines.append("CONVERSATION SO FAR:")
        for turn in history:
            who = "AI Agent" if turn.get("role") == "assistant" else "Teammate"
            lines.append(f"- {who}: {turn.get('text', '')}")
    else:
        lines.append("CONVERSATION SO FAR: (none yet — this is the first turn)")
    lines.append("")
    lines.append('Respond with the JSON object: {"ready": ..., "message": "..."}')
    return "\n".join(lines)


def _parse_plan(text: str) -> Optional[dict]:
    """Extract the ``{"ready": ..., "message": ...}`` object from LLM text."""
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    for candidate in (cleaned, None):
        if candidate is None:
            brace = re.search(r"\{[\s\S]*\}", cleaned)
            candidate = brace.group(0) if brace else None
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


@trace_agent(name="task_planner")
def run_agent(
    host: str,
    token: str,
    endpoint_name: str,
    *,
    task_title: str,
    task_description: str,
    agent: AgentSpec,
    history: List[dict],
    on_step: Optional[Callable[[str], None]] = None,
) -> PlanResult:
    """Decide whether to run the selected agent now or ask the teammate more.

    ``history`` is the ordered clarification Q&A: ``[{"role": "assistant"|"user",
    "text": str}, ...]``. ``success`` is ``True`` when the LLM replied parseably;
    ``ready`` gates the actual run. On any failure ``ready`` stays ``False`` so
    the agent never runs without a confident go-ahead.
    """
    result = PlanResult(success=False)

    if on_step:
        on_step("Reviewing the task scope...")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_prompt(
                task_title, task_description, agent, history
            ),
        },
    ]

    result.iterations = 1
    try:
        llm_response = call_serving_endpoint(
            host,
            token,
            endpoint_name,
            messages,
            max_tokens=512,
            temperature=0.0,
            timeout=LLM_TIMEOUT,
            trace_name=_TRACE_NAME,
        )
    except Exception as exc:  # noqa: BLE001
        result.error = f"Planner LLM request failed: {exc}"
        logger.error("task_planner: %s", result.error)
        return result

    accumulate_usage(result.usage, llm_response.get("usage", {}))
    content = extract_message_content(llm_response)
    result.steps.append(AgentStep(step_type="output", content=content[:500]))

    plan = _parse_plan(content)
    if not plan:
        result.error = "Planner returned an unparseable response"
        result.message = (
            "I need a bit more detail before I proceed — could you clarify the "
            "scope of this task?"
        )
        logger.warning("task_planner: unparseable response: %s", content[:200])
        return result

    result.success = True
    result.ready = bool(plan.get("ready", False))
    result.message = str(plan.get("message", "")).strip()
    logger.info("task_planner: ready=%s", result.ready)
    return result
