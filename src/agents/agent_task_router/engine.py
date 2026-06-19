"""
Task Router Agent engine.

A single-shot classifier: given a task's title + description and the list of
dispatchable agents (:mod:`agents.registry`), the LLM picks the one agent best
suited to carry the task out and explains why. No tool-calling loop -- one
serving-endpoint call, JSON in / JSON out.

The orchestrator (:mod:`back.objects.registry.agent_task_runner`) consumes
:attr:`AgentResult.chosen_agent_key` to actually run the specialized agent.
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
_TRACE_NAME = "task_router"


@dataclass
class AgentResult:
    """Outcome of a routing decision."""

    success: bool
    chosen_agent_key: str = ""
    reasoning: str = ""
    steps: List[AgentStep] = field(default_factory=list)
    iterations: int = 0
    error: str = ""
    usage: Dict[str, int] = field(default_factory=dict)


_SYSTEM_PROMPT = """\
You are the Task Router for OntoBricks. A teammate assigned a work item to the
"AI Agent". Your only job is to decide which ONE specialized agent should carry
out the task, based on the task title and description.

You will be given the list of available agents, each with a key and a
description of what it does and when to pick it.

RULES
- Choose exactly one agent whose purpose matches the task intent.
- The ontology IS the domain model. Tasks that ask to design, model, create,
  add, structure, or restructure a domain concept / entity / class /
  relationship are ontology DESIGN tasks -- route them to the Ontology Assistant
  (the default for ontology design and editing), even when phrased as "build" or
  "create" something. A name you don't recognize is most likely a domain entity
  to model, not unrelated work.
- Only choose "none" for tasks clearly unrelated to the ontology / data model,
  its mappings, business rules, or entity icons (e.g. infrastructure, app code,
  or off-topic requests).
- Reply with ONLY a JSON object, no prose, no code fences:
  {"agent": "<agent_key or none>", "reasoning": "<one short sentence>"}
"""


def _build_user_prompt(
    task_title: str, task_description: str, available_agents: List[AgentSpec]
) -> str:
    lines = ["AVAILABLE AGENTS:"]
    for spec in available_agents:
        lines.append(f'- key="{spec.key}" ({spec.label}): {spec.description}')
    lines.append("")
    lines.append(f"TASK TITLE: {task_title}")
    if task_description:
        lines.append(f"TASK DESCRIPTION: {task_description}")
    lines.append("")
    lines.append('Respond with the JSON object: {"agent": "...", "reasoning": "..."}')
    return "\n".join(lines)


def _parse_decision(text: str) -> Optional[dict]:
    """Extract the ``{"agent": ..., "reasoning": ...}`` object from LLM text."""
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{[\s\S]*\}", cleaned)
    if brace:
        try:
            obj = json.loads(brace.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


@trace_agent(name="task_router")
def run_agent(
    host: str,
    token: str,
    endpoint_name: str,
    *,
    task_title: str,
    task_description: str,
    available_agents: List[AgentSpec],
    on_step: Optional[Callable[[str], None]] = None,
) -> AgentResult:
    """Pick the agent that should carry out *task_title* / *task_description*.

    Returns an :class:`AgentResult`; ``success`` is ``True`` only when a valid,
    known agent key was chosen. ``chosen_agent_key`` is empty when the router
    decided no agent fits (``reasoning`` explains why).
    """
    result = AgentResult(success=False)
    valid_keys = {spec.key for spec in available_agents}

    if not available_agents:
        result.error = "No dispatchable agents are registered"
        return result

    if on_step:
        on_step("Selecting the right agent for this task...")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_prompt(
                task_title, task_description, available_agents
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
            max_tokens=256,
            temperature=0.0,
            timeout=LLM_TIMEOUT,
            trace_name=_TRACE_NAME,
        )
    except Exception as exc:  # noqa: BLE001
        result.error = f"Router LLM request failed: {exc}"
        logger.error("task_router: %s", result.error)
        return result

    accumulate_usage(result.usage, llm_response.get("usage", {}))
    content = extract_message_content(llm_response)
    result.steps.append(AgentStep(step_type="output", content=content[:500]))

    decision = _parse_decision(content)
    if not decision:
        result.error = "Router returned an unparseable response"
        logger.warning("task_router: unparseable response: %s", content[:200])
        return result

    chosen = str(decision.get("agent", "")).strip()
    reasoning = str(decision.get("reasoning", "")).strip()
    result.reasoning = reasoning

    if chosen.lower() in ("", "none", "null"):
        result.error = reasoning or "No suitable agent for this task"
        logger.info("task_router: no agent chosen (%s)", result.error)
        return result

    if chosen not in valid_keys:
        result.error = f"Router chose an unknown agent: {chosen}"
        logger.warning("task_router: %s", result.error)
        return result

    result.success = True
    result.chosen_agent_key = chosen
    logger.info("task_router: chose '%s' — %s", chosen, reasoning)
    return result
