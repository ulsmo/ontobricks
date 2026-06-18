# AI Agent Clarifying Questions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an AI-Agent task post a plan + clarifying questions in its Discussion thread, park itself, and resume automatically when the assignee replies, iterating until confident before running the chosen specialized agent.

**Architecture:** A new single-shot `agent_task_planner` LLM decides "ready vs ask". The `agent_task_runner` background worker does route -> reconstruct thread history -> plan -> branch (ask again, or dispatch the agent). A resume hook in `CommentService.add_comment` relaunches the worker when a human replies on a parked AI-Agent task thread. Zero schema change: the parked state reuses `in_progress`, linkage uses the existing `comment_id` (a kickoff comment is created for standalone tasks), and the deterministic router is re-run on each pass.

**Tech Stack:** Python 3.11, FastAPI, the OntoBricks agent engine (`agents.engine_base.call_serving_endpoint`), `TaskManager` background jobs, pytest.

---

## File structure

| File | Responsibility |
|------|----------------|
| `src/agents/agent_task_planner/engine.py` (new) | Single-shot LLM: given task + chosen agent + Q&A history, return `PlanResult{ready, message}` |
| `src/agents/agent_task_planner/__init__.py` (new) | Re-export `run_agent`, `PlanResult` |
| `src/agents/agent_task_planner/tools.py` (new) | Empty tool tables (consistency with other agents) |
| `src/back/objects/registry/agent_task_runner.py` (modify) | Plan-or-run worker, thread-history reconstruction, `resume_agent_task`, concurrency guard, answer fold-in |
| `src/back/objects/registry/CommentService.py` (modify) | Kickoff comment for standalone AI tasks; resume hook in `add_comment` |
| `tests/units/agents/test_agent_task_planner.py` (new) | Planner ready/ask + degradation |
| `tests/units/registry/test_agent_task_runner.py` (modify) | Plan/park/resume/run + history reconstruction + guard |
| `tests/units/registry/test_comment_service.py` (modify) | Kickoff comment + resume-hook trigger conditions |

---

### Task 1: `agent_task_planner` agent

Mirrors `src/agents/agent_task_router/` (single-shot, JSON in/out). Decides whether the AI Agent has enough to act, or must ask the assignee more.

**Files:**
- Create: `src/agents/agent_task_planner/engine.py`
- Create: `src/agents/agent_task_planner/__init__.py`
- Create: `src/agents/agent_task_planner/tools.py`
- Test: `tests/units/agents/test_agent_task_planner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/units/agents/test_agent_task_planner.py
"""Unit tests for the task-planner agent (ready vs. ask decision)."""
from __future__ import annotations

from unittest.mock import patch

from agents.agent_task_planner import PlanResult, run_agent
from agents.registry import get_agent


def _spec():
    return get_agent("ontology_assistant")


def _llm(content: str):
    return {"choices": [{"message": {"content": content}}], "usage": {}}


def test_first_turn_asks_when_no_answers():
    with patch(
        "agents.agent_task_planner.engine.call_serving_endpoint",
        return_value=_llm('{"ready": false, "message": "Plan: remove Person? Confirm scope."}'),
    ):
        res = run_agent(
            "h", "t", "ep",
            task_title="Is Person needed?",
            task_description="",
            agent=_spec(),
            history=[],
        )
    assert isinstance(res, PlanResult)
    assert res.success is True
    assert res.ready is False
    assert "Plan" in res.message


def test_ready_when_user_approved():
    history = [
        {"role": "assistant", "text": "Shall I remove Person?"},
        {"role": "user", "text": "yes go ahead"},
    ]
    with patch(
        "agents.agent_task_planner.engine.call_serving_endpoint",
        return_value=_llm('{"ready": true, "message": "Running now."}'),
    ):
        res = run_agent(
            "h", "t", "ep",
            task_title="Is Person needed?",
            task_description="",
            agent=_spec(),
            history=history,
        )
    assert res.success is True
    assert res.ready is True


def test_unparseable_response_degrades_to_ask():
    with patch(
        "agents.agent_task_planner.engine.call_serving_endpoint",
        return_value=_llm("not json at all"),
    ):
        res = run_agent(
            "h", "t", "ep",
            task_title="x", task_description="", agent=_spec(), history=[],
        )
    # Degrade safe: never auto-run on a parse failure.
    assert res.ready is False
    assert res.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/units/agents/test_agent_task_planner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agents.agent_task_planner'`

- [ ] **Step 3: Write `tools.py`**

```python
# src/agents/agent_task_planner/tools.py
"""The planner is single-shot and uses no tools (kept for package symmetry)."""
from __future__ import annotations

from typing import Callable, Dict, List

TOOL_DEFINITIONS: List[dict] = []
TOOL_HANDLERS: Dict[str, Callable] = {}
```

- [ ] **Step 4: Write `engine.py`**

```python
# src/agents/agent_task_planner/engine.py
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
```

- [ ] **Step 5: Write `__init__.py`**

```python
# src/agents/agent_task_planner/__init__.py
from agents.agent_task_planner.engine import PlanResult, run_agent  # noqa: F401

__all__ = ["run_agent", "PlanResult"]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/units/agents/test_agent_task_planner.py -q`
Expected: PASS (3 passed)

- [ ] **Step 7: Commit**

```bash
git add src/agents/agent_task_planner tests/units/agents/test_agent_task_planner.py
git commit -m "feat(agents): add task-planner agent (ready vs. ask gate)"
```

---

### Task 2: Plan-or-run worker + resume in `agent_task_runner`

Rework the background worker so it routes, reconstructs the thread Q&A, calls the planner, and either parks (posts a question, stays `in_progress`) or runs the chosen agent. Add `resume_agent_task` (same worker, relaunched on a reply) and an in-process guard so a reply can't start a second concurrent run.

**Files:**
- Modify: `src/back/objects/registry/agent_task_runner.py`
- Test: `tests/units/registry/test_agent_task_runner.py`

- [ ] **Step 1: Write the failing tests** (append to the existing test module)

```python
# tests/units/registry/test_agent_task_runner.py  (additions)
def _svc_with_thread(thread):
    """Service double whose list_comments returns *thread* (list of dicts)."""
    svc, statuses, comments = _fake_svc()
    svc.list_comments.return_value = thread
    return svc, statuses, comments


def test_first_pass_parks_with_plan_and_stays_in_progress(monkeypatch):
    svc, statuses, comments = _svc_with_thread(
        [{"id": "root", "parent_id": "", "author": "alice@x.io",
          "body": "Is Person needed?", "created_at": "t0",
          "anchor_type": "domain", "anchor_ref": ""}]
    )
    router_res = SimpleNamespace(success=True, chosen_agent_key="ontology_assistant",
                                 reasoning="edit", error="")
    plan_res = SimpleNamespace(success=True, ready=False,
                               message="Plan: remove Person? Confirm.", error="")
    monkeypatch.setattr("agents.agent_task_router.run_agent", lambda *a, **k: router_res)
    monkeypatch.setattr("agents.agent_task_planner.run_agent", lambda *a, **k: plan_res)
    dispatched = []
    monkeypatch.setattr(runner, "_dispatch_agent",
                        lambda *a, **k: dispatched.append(k) or ("s", "r", {}))

    runner._run_for_task(
        svc=svc, domain=MagicMock(), host="h", token="t", llm_endpoint="ep",
        warehouse_id="", folder="d", version="v", domain_task_id="T1",
        title="Is Person needed?", description="", comment_id="root",
        on_step=lambda m: None, tm=None, tm_task_id=None,
    )

    assert dispatched == []                      # agent did NOT run
    assert "in_progress" in statuses             # parked
    assert any("Plan: remove Person?" in c for c in comments)


def test_resume_runs_agent_when_planner_ready(monkeypatch):
    svc, statuses, comments = _svc_with_thread([
        {"id": "root", "parent_id": "", "author": "alice@x.io",
         "body": "Is Person needed?", "created_at": "t0",
         "anchor_type": "domain", "anchor_ref": ""},
        {"id": "q1", "parent_id": "root", "author": "AI Agent",
         "body": "Remove Person?", "created_at": "t1",
         "anchor_type": "domain", "anchor_ref": ""},
        {"id": "a1", "parent_id": "root", "author": "alice@x.io",
         "body": "yes remove it", "created_at": "t2",
         "anchor_type": "domain", "anchor_ref": ""},
    ])
    router_res = SimpleNamespace(success=True, chosen_agent_key="ontology_assistant",
                                 reasoning="edit", error="")
    plan_res = SimpleNamespace(success=True, ready=True, message="Running now.", error="")
    monkeypatch.setattr("agents.agent_task_router.run_agent", lambda *a, **k: router_res)
    monkeypatch.setattr("agents.agent_task_planner.run_agent", lambda *a, **k: plan_res)
    captured = {}
    monkeypatch.setattr(
        runner, "_dispatch_agent",
        lambda key, **k: captured.update(k) or ("updated", "Removed Person.", {}),
    )

    runner._run_for_task(
        svc=svc, domain=MagicMock(), host="h", token="t", llm_endpoint="ep",
        warehouse_id="", folder="d", version="v", domain_task_id="T1",
        title="Is Person needed?", description="", comment_id="root",
        on_step=lambda m: None, tm=None, tm_task_id=None,
    )

    assert statuses[-1] == "done"                       # solved
    assert "yes remove it" in captured["task_text"]     # answer folded in
    assert any("Removed Person." in c for c in comments)


def test_thread_history_maps_authors_to_roles():
    svc, _, _ = _svc_with_thread([
        {"id": "root", "parent_id": "", "author": "alice@x.io",
         "body": "do X", "created_at": "t0"},
        {"id": "q1", "parent_id": "root", "author": "AI Agent",
         "body": "clarify?", "created_at": "t1"},
        {"id": "a1", "parent_id": "root", "author": "alice@x.io",
         "body": "answer", "created_at": "t2"},
        {"id": "other", "parent_id": "elsewhere", "author": "bob@x.io",
         "body": "unrelated", "created_at": "t3"},
    ])
    hist = runner._thread_history(svc, "d", "v", "root")
    assert [h["role"] for h in hist] == ["user", "assistant", "user"]
    assert hist[1]["text"] == "clarify?"
    assert all(h["text"] != "unrelated" for h in hist)   # other thread excluded


def test_resume_skips_when_already_running(monkeypatch):
    runner._ACTIVE_TASKS.add("T1")
    try:
        started = runner.resume_agent_task(
            svc=MagicMock(), domain=MagicMock(), settings=MagicMock(),
            folder="d", version="v", task={"id": "T1", "comment_id": "root",
                                            "title": "x", "description": ""},
        )
    finally:
        runner._ACTIVE_TASKS.discard("T1")
    assert started is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/units/registry/test_agent_task_runner.py -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_run_for_task'` (and `_thread_history`, `resume_agent_task`, `_ACTIVE_TASKS`).

- [ ] **Step 3: Add the module-level guard + imports** (top of `agent_task_runner.py`, after `AI_AGENT_LABEL`)

```python
# In-process guard: domain_task ids with a background pass currently running.
# Prevents a reply from launching a second concurrent run for the same task.
# (Single-process app; reset on restart, which is fine — a stale entry only
# delays one resume.)
_ACTIVE_TASKS: set[str] = set()
```

- [ ] **Step 4: Replace the `_run` worker with route -> history -> plan -> branch**

Replace the entire body of `_run(...)` (the `try/except` block) so it delegates to a shared `_run_for_task`, and add `_run_for_task`, `_thread_history`, and `_fold_answers`:

```python
def _run(
    task: Any,
    *,
    svc: Any,
    domain: Any,
    host: str,
    token: str,
    llm_endpoint: str,
    warehouse_id: str,
    folder: str,
    version: str,
    domain_task_id: str,
    title: str,
    description: str,
    comment_id: str,
) -> None:
    """TaskManager entry point: delegate to the shared plan-or-run worker."""
    from back.core.task_manager import get_task_manager

    tm = get_task_manager()
    try:
        _run_for_task(
            svc=svc, domain=domain, host=host, token=token,
            llm_endpoint=llm_endpoint, warehouse_id=warehouse_id,
            folder=folder, version=version, domain_task_id=domain_task_id,
            title=title, description=description, comment_id=comment_id,
            on_step=lambda m: tm.update_progress(task.id, task.progress, m),
            tm=tm, tm_task_id=task.id,
        )
    finally:
        _ACTIVE_TASKS.discard(domain_task_id)


def _run_for_task(
    *,
    svc: Any,
    domain: Any,
    host: str,
    token: str,
    llm_endpoint: str,
    warehouse_id: str,
    folder: str,
    version: str,
    domain_task_id: str,
    title: str,
    description: str,
    comment_id: str,
    on_step: Callable[[str], None],
    tm: Any,
    tm_task_id: Optional[str],
) -> None:
    """Route, reconstruct the thread Q&A, plan, then ask-again or run the agent.

    ``tm``/``tm_task_id`` are the optional TaskManager handle for progress; the
    worker is fully functional without them (used directly in tests).
    """
    from agents.agent_task_planner import run_agent as run_planner
    from agents.agent_task_router import run_agent as run_router
    from agents.registry import get_agent, list_agents

    def _tm(method: str, *args: Any) -> None:
        if tm is not None and tm_task_id is not None:
            getattr(tm, method)(tm_task_id, *args)

    try:
        _tm("start_task", "Selecting the right agent...")

        # 1) Route (deterministic — same choice on every pass => locked route).
        router_result = run_router(
            host, token, llm_endpoint,
            task_title=title, task_description=description,
            available_agents=list_agents(), on_step=on_step,
        )
        if not router_result.success or not router_result.chosen_agent_key:
            reason = router_result.error or "No suitable agent for this task"
            _tm("fail_task", reason)
            _report(svc, folder, version, domain_task_id, comment_id,
                    body=f"**AI Agent**\n\nI could not route this task: {reason}",
                    event="agent_failed")
            return
        spec = get_agent(router_result.chosen_agent_key)
        if spec is None:
            _tm("fail_task", "Router chose an unknown agent")
            return

        # 2) Plan against the conversation reconstructed from the thread.
        _set_status(svc, folder, domain_task_id, "in_progress")
        history = _thread_history(svc, folder, version, comment_id)
        plan = run_planner(
            host, token, llm_endpoint,
            task_title=title, task_description=description,
            agent=spec, history=history, on_step=on_step,
        )

        # 3a) Not ready -> post the plan/question and park (stay in_progress).
        if not plan.ready:
            question = plan.message or (
                "Could you clarify the scope of this task before I proceed?"
            )
            _report(svc, folder, version, domain_task_id, comment_id,
                    body=f"**AI Agent — {spec.label}**\n\n{question}",
                    event="agent_progress")
            _tm("complete_task", {"agent": spec.key, "state": "waiting_input"},
                "Waiting for your reply")
            return

        # 3b) Ready -> run the chosen agent with the answers folded in.
        _tm("advance_step", f"Running {spec.label}...")
        task_text = _fold_answers(title, description, history)
        summary, report, result = _dispatch_agent(
            spec.key, domain=domain, host=host, token=token,
            llm_endpoint=llm_endpoint, warehouse_id=warehouse_id,
            task_text=task_text, on_step=on_step,
        )
        _tm("advance_step", "Recording the result...")
        _set_status(svc, folder, domain_task_id, "done")
        body = f"**AI Agent — {spec.label}**\n\n"
        if router_result.reasoning:
            body += f"_Why this agent:_ {router_result.reasoning}\n\n"
        body += report
        _report(svc, folder, version, domain_task_id, comment_id,
                body=body, event="task_done")
        _tm("complete_task",
            {"agent": spec.key, "agent_label": spec.label, **result}, summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent_task_runner: task %s crashed", domain_task_id)
        _tm("fail_task", f"AI Agent run failed: {exc}")
        _report(svc, folder, version, domain_task_id, comment_id,
                body=f"**AI Agent**\n\nThe run failed: {exc}",
                event="agent_failed")


def _thread_history(
    svc: Any, folder: str, version: str, root_comment_id: str
) -> list[dict]:
    """Reconstruct the ordered clarification Q&A from the task's thread.

    The thread is the root comment (``root_comment_id``) plus its direct replies,
    ordered by ``created_at``. The AI Agent's own comments map to ``assistant``;
    everyone else maps to ``user``.
    """
    if not root_comment_id:
        return []
    try:
        comments = list(svc.list_comments(folder, version))
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_task_runner: thread read skipped: %s", exc)
        return []
    rid = str(root_comment_id)
    thread = [
        c for c in comments
        if str(c.get("id")) == rid or str(c.get("parent_id") or "") == rid
    ]
    thread.sort(key=lambda c: str(c.get("created_at") or ""))
    history: list[dict] = []
    for c in thread:
        role = "assistant" if (c.get("author") or "") == AI_AGENT_LABEL else "user"
        history.append({"role": role, "text": (c.get("body") or "").strip()})
    return history


def _fold_answers(title: str, description: str, history: list[dict]) -> str:
    """Build the agent input from the task plus the teammate's answers."""
    parts = [title]
    if description:
        parts.append(description)
    answers = [h["text"] for h in history if h["role"] == "user" and h["text"]]
    # Drop the first 'user' turn — it is the task statement itself, already above.
    extra = answers[1:] if answers else []
    if extra:
        parts.append("Clarifications from the assignee:\n- " + "\n- ".join(extra))
    return "\n\n".join(parts)
```

- [ ] **Step 5: Update `start_agent_task` step labels** (the `steps=[...]` list in `run_background_task`)

```python
        steps=[
            {"name": "route", "description": "Selecting the right agent"},
            {"name": "plan", "description": "Confirming scope with you"},
            {"name": "run", "description": "Running the selected agent"},
        ],
```

Also, just before `tm.run_background_task(...)`, mark the task active so a racing reply won't double-launch:

```python
    _ACTIVE_TASKS.add(task_id)
    tm = get_task_manager()
    task = tm.run_background_task(
```

- [ ] **Step 6: Add `resume_agent_task`** (after `start_agent_task`)

```python
def resume_agent_task(
    *,
    svc: Any,
    domain: Any,
    settings: Any,
    folder: str,
    version: str,
    task: Dict[str, Any],
) -> Optional[str]:
    """Relaunch the plan-or-run worker for a parked AI-Agent *task*.

    Called when a teammate replies on the task's thread. No-op (returns ``None``)
    when a pass is already running for this task. Best-effort: setup failures are
    logged and surfaced as a comment.
    """
    task_id = str(task.get("id") or "")
    if not task_id or task_id in _ACTIVE_TASKS:
        return None

    from back.core.helpers import require_serving_llm, resolve_warehouse_id
    from back.core.task_manager import get_task_manager

    comment_id = str(task.get("comment_id") or "")
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    try:
        host, token, llm_endpoint = require_serving_llm(domain, settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_task_runner: resume blocked: %s (task=%s)", exc, task_id)
        return None
    try:
        warehouse_id = resolve_warehouse_id(domain, settings)
    except Exception:  # noqa: BLE001
        warehouse_id = ""

    _ACTIVE_TASKS.add(task_id)
    tm = get_task_manager()
    bg = tm.run_background_task(
        f"AI Agent: {title}"[:80],
        "task_router",
        _run,
        steps=[
            {"name": "route", "description": "Selecting the right agent"},
            {"name": "plan", "description": "Confirming scope with you"},
            {"name": "run", "description": "Running the selected agent"},
        ],
        svc=svc, domain=domain, host=host, token=token,
        llm_endpoint=llm_endpoint, warehouse_id=warehouse_id,
        folder=folder, version=version, domain_task_id=task_id,
        title=title, description=description, comment_id=comment_id,
    )
    logger.info("agent_task_runner: resumed task %s (bg=%s)", task_id, bg.id)
    return bg.id
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/units/registry/test_agent_task_runner.py -q`
Expected: PASS (existing + 4 new tests). If `_fake_svc` lacks `list_comments`, ensure it returns `[]` by default (it already does per the current module).

- [ ] **Step 8: Commit**

```bash
git add src/back/objects/registry/agent_task_runner.py tests/units/registry/test_agent_task_runner.py
git commit -m "feat(registry): plan-then-run worker with thread-driven resume for AI-Agent tasks"
```

---

### Task 3: Kickoff comment + resume hook in `CommentService`

Give every standalone AI-Agent task a thread root (a kickoff comment), and resume the worker when a human replies on a parked AI-Agent task thread.

**Files:**
- Modify: `src/back/objects/registry/CommentService.py`
- Test: `tests/units/registry/test_comment_service.py`

- [ ] **Step 1: Write the failing tests** (append to the existing module; reuse its `_call`/`_svc` helpers — see the existing tests for their shape)

```python
# tests/units/registry/test_comment_service.py  (additions)
from back.objects.registry.agent_task_runner import AI_AGENT_PRINCIPAL


def test_create_ai_task_without_comment_inserts_kickoff_comment(monkeypatch):
    svc = _svc()  # existing helper used by this module
    svc.insert_comment.return_value = {"id": "kick1", "anchor_type": "domain",
                                       "anchor_ref": ""}
    svc.insert_task.return_value = {"id": "T1"}
    started = {}
    monkeypatch.setattr(
        "back.objects.registry.CommentService.start_agent_task",
        lambda **k: started.update(k) or "bg1",
    )
    _call("create_task", svc, assignee=AI_AGENT_PRINCIPAL, title="Is Person needed?",
          description="evaluate", due_date=None, comment_id=None)

    svc.insert_comment.assert_called_once()                 # kickoff created
    _, kwargs = svc.insert_task.call_args
    assert kwargs["comment_id"] == "kick1"                  # task linked to it
    assert started["comment_id"] == "kick1"                 # agent anchored to it


def test_reply_on_active_ai_task_triggers_resume(monkeypatch):
    svc = _svc()
    svc.insert_comment.return_value = {"id": "r2", "parent_id": "root",
                                       "anchor_type": "domain", "anchor_ref": ""}
    svc.list_tasks.return_value = [
        {"id": "T1", "assignee": AI_AGENT_PRINCIPAL, "status": "in_progress",
         "comment_id": "root", "title": "x", "description": ""},
    ]
    resumed = {}
    monkeypatch.setattr(
        "back.objects.registry.CommentService.resume_agent_task",
        lambda **k: resumed.update(k) or "bg2",
    )
    _call("add_comment", svc, anchor_type="domain", anchor_ref="",
          body="yes go ahead", parent_id="root")

    assert resumed.get("task", {}).get("id") == "T1"


def test_reply_on_done_ai_task_does_not_resume(monkeypatch):
    svc = _svc()
    svc.insert_comment.return_value = {"id": "r3", "parent_id": "root",
                                       "anchor_type": "domain", "anchor_ref": ""}
    svc.list_tasks.return_value = [
        {"id": "T1", "assignee": AI_AGENT_PRINCIPAL, "status": "done",
         "comment_id": "root", "title": "x", "description": ""},
    ]
    called = {"n": 0}
    monkeypatch.setattr(
        "back.objects.registry.CommentService.resume_agent_task",
        lambda **k: called.__setitem__("n", called["n"] + 1),
    )
    _call("add_comment", svc, anchor_type="domain", anchor_ref="",
          body="thanks", parent_id="root")

    assert called["n"] == 0
```

Note: if this test module does not already expose `_svc`/`_call` helpers in the shape used above, adapt these three tests to the module's existing harness (it already monkeypatches `start_agent_task` in `test_create_task_ai_agent_triggers_runner`; copy that pattern).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/units/registry/test_comment_service.py -q`
Expected: FAIL — kickoff comment not inserted; `resume_agent_task` not imported/called.

- [ ] **Step 3: Import `resume_agent_task`** (extend the existing import block)

```python
from back.objects.registry.agent_task_runner import (
    AI_AGENT_PRINCIPAL,
    is_ai_agent,
    resume_agent_task,
    start_agent_task,
)
```

- [ ] **Step 4: Create the kickoff comment in `create_task`**

Replace the AI-agent block in `create_task` (currently `insert_task(...)` then the `is_ai_agent` `start_agent_task(...)`) so that, for a standalone AI-Agent task, a kickoff comment is created first and used as the task's `comment_id`:

```python
        # A standalone AI-Agent task needs a thread root so its clarifying
        # questions and your replies live in one place. Create a kickoff
        # comment (the task statement) and anchor the task to it.
        effective_comment_id = comment_id or None
        if is_ai_agent(assignee) and not effective_comment_id:
            kickoff_body = title + (
                f"\n\n{(description or '').strip()}" if (description or "").strip() else ""
            )
            kickoff = svc.insert_comment(
                folder, version,
                anchor_type="domain", anchor_ref="",
                author=CommentService._email(request),
                body=kickoff_body, parent_id=None,
            )
            if kickoff:
                effective_comment_id = str(kickoff.get("id") or "") or None

        created = svc.insert_task(
            folder,
            version,
            assignee=assignee,
            created_by=CommentService._email(request),
            title=title,
            description=(description or "").strip(),
            due_date=(due_date or None),
            comment_id=effective_comment_id,
        )
        if not created:
            raise InfrastructureError("Failed to create task")
```

Then update the audit + `start_agent_task` call below to use `effective_comment_id`:

```python
        agent_task_id = None
        if is_ai_agent(assignee):
            agent_task_id = start_agent_task(
                svc=svc,
                domain=get_domain(session_mgr),
                settings=settings,
                folder=folder,
                version=version,
                task_id=created.get("id", ""),
                title=title,
                description=(description or "").strip(),
                comment_id=effective_comment_id or "",
            )
```

(Use `effective_comment_id` in the `_audit(... meta={"comment_id": ...})` call too, for consistency.)

- [ ] **Step 5: Add the resume hook to `add_comment`**

After the existing successful-insert block in `add_comment` (right before `return {"success": True, "comment": created}`), add:

```python
        CommentService._maybe_resume_agent(
            svc, session_mgr, settings, folder, version, created,
            author=CommentService._email(request),
        )
        return {"success": True, "comment": created}
```

And add the helper (next to the other private helpers):

```python
    @staticmethod
    def _maybe_resume_agent(
        svc,
        session_mgr: SessionManager,
        settings,
        folder: str,
        version: str,
        comment: Dict[str, Any],
        *,
        author: str,
    ) -> None:
        """Resume a parked AI-Agent task when a teammate replies on its thread.

        Best-effort: the AI Agent's own outcome comments are written through the
        store (not this method), so only human replies reach here. Matches the
        new comment's thread root against an active AI-Agent task's ``comment_id``.
        """
        from back.objects.registry.agent_task_runner import AI_AGENT_LABEL

        try:
            if (author or "") == AI_AGENT_LABEL:
                return
            root = str(comment.get("parent_id") or comment.get("id") or "")
            if not root:
                return
            for task in svc.list_tasks(folder, version):
                if not is_ai_agent(task.get("assignee") or ""):
                    continue
                if (task.get("status") or "") != "in_progress":
                    continue
                if str(task.get("comment_id") or "") != root:
                    continue
                resume_agent_task(
                    svc=svc,
                    domain=get_domain(session_mgr),
                    settings=settings,
                    folder=folder,
                    version=version,
                    task=task,
                )
                break
        except Exception as exc:  # noqa: BLE001
            logger.debug("CommentService: agent resume skipped: %s", exc)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/units/registry/test_comment_service.py -q`
Expected: PASS (existing + 3 new).

- [ ] **Step 7: Commit**

```bash
git add src/back/objects/registry/CommentService.py tests/units/registry/test_comment_service.py
git commit -m "feat(registry): kickoff comment + reply-driven resume for AI-Agent tasks"
```

---

### Task 4: Docs, changelog, full test run

**Files:**
- Modify: `docs/architecture.md`
- Create/append: `changelogs/v0.6.0/benoitcayladbx_2026-06-18.log`

- [ ] **Step 1: Update `docs/architecture.md`**

In the "AI Agent task assignment" subsection, document the clarify-then-run loop: route -> planner (`agent_task_planner`) -> park (`in_progress`, plan/question posted) -> resume on reply (`CommentService.add_comment` -> `resume_agent_task`) -> run when ready. Add `agent_task_planner/` to the `src/agents/` listing.

- [ ] **Step 2: Append a changelog section** (version from `pyproject.toml`)

Title: "Feature: AI Agent asks clarifying questions before running". Include context, the numbered change list (planner agent, plan-or-run worker + resume, kickoff comment + reply hook), modified files, and the test result.

- [ ] **Step 3: Run the targeted suites**

Run: `python -m pytest tests/units/agents/test_agent_task_planner.py tests/units/agents/test_agent_task_router.py tests/units/registry/test_agent_task_runner.py tests/units/registry/test_comment_service.py -q`
Expected: PASS.

- [ ] **Step 4: Run the full suite and record the result**

Run: `python -m pytest -q`
Expected: no NEW failures vs. baseline. (The repo currently has ~59 pre-existing failures in `tests/units/api/*` caused by global test-ordering pollution — they pass in isolation and are unrelated to this change. Record the count and note it, do not try to fix it here.)

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md changelogs/v0.6.0/benoitcayladbx_2026-06-18.log
git commit -m "docs: document AI-Agent clarify-then-run loop + changelog"
```

---

## Self-review notes

- **Spec coverage:** planner (Task 1), park/resume/multi-round/locked-route/answer-fold-in (Task 2), kickoff comment + reply hook + active-only/done-excluded triggers (Task 3), docs/changelog/tests (Task 4). Always-confirm is enforced because the first pass has no `user` answers in history, so the planner returns `ready=false`.
- **Type consistency:** `PlanResult{success, ready, message, ...}` (Task 1) is consumed in Task 2 as `plan.ready`/`plan.message`. `_thread_history` returns `[{"role", "text"}]`, consumed by the planner's `history` param and by `_fold_answers`. `resume_agent_task(svc, domain, settings, folder, version, task)` signature matches the `CommentService._maybe_resume_agent` call.
- **Guard:** `_ACTIVE_TASKS` is added in both `start_agent_task` and `resume_agent_task` and cleared in `_run`'s `finally`.
- **Known caveat:** `_thread_history` assumes one-level threads (replies parent to the root). The agent posts with `parent_id = comment_id` and the UI replies to the root, so this holds for the current comments panel.
