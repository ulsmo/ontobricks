"""
AI-Agent task runner -- the bridge between a task assigned to the "AI Agent"
and the specialized OntoBricks agents.

When a collaborative task is created with the AI-agent sentinel assignee
(:data:`AI_AGENT_PRINCIPAL`), :func:`start_agent_task` spins up a background
:class:`~back.core.task_manager.TaskManager` job that:

1. runs :mod:`agents.agent_task_router` to pick the best agent for the task,
2. dispatches that agent against the task's domain session (blocking),
3. records the outcome back on the ``domain_tasks`` row + the review audit log.

This mirrors the existing background-agent pattern in the ontology / mapping
routers, but is keyed off task assignment instead of a dedicated endpoint.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, Tuple

from back.core.logging import get_logger

logger = get_logger(__name__)

# Virtual assignee that means "let the AI figure out and run the right agent".
# Not a real Databricks principal -- it never appears in domain permissions.
AI_AGENT_PRINCIPAL = "agent://router"
AI_AGENT_LABEL = "AI Agent"

# In-process guard: domain_task ids with a background pass currently running.
# Prevents a reply from launching a second concurrent run for the same task.
# (Single-process app; reset on restart, which is fine — a stale entry only
# delays one resume.)
_ACTIVE_TASKS: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def _claim_task(task_id: str) -> bool:
    """Atomically mark *task_id* active. Returns False if already running."""
    if not task_id:
        return False
    with _ACTIVE_LOCK:
        if task_id in _ACTIVE_TASKS:
            return False
        _ACTIVE_TASKS.add(task_id)
        return True


def _launch_worker(
    *,
    svc: Any,
    domain: Any,
    host: str,
    token: str,
    llm_endpoint: str,
    warehouse_id: str,
    folder: str,
    version: str,
    task_id: str,
    title: str,
    description: str,
    comment_id: str,
) -> Optional[str]:
    """Claim the task and launch the background plan-or-run worker.

    Returns the background task id, or ``None`` when the task is already running.
    On launch failure the claim is released and the exception propagates to the
    caller (``start_agent_task`` / ``resume_agent_task``) to handle.
    """
    from back.core.task_manager import get_task_manager

    if not _claim_task(task_id):
        return None
    try:
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
        return bg.id
    except Exception:
        _ACTIVE_TASKS.discard(task_id)
        raise


def is_ai_agent(assignee: str) -> bool:
    """Return ``True`` when *assignee* is the AI-agent sentinel."""
    return (assignee or "").strip().lower() == AI_AGENT_PRINCIPAL


def start_agent_task(
    *,
    svc: Any,
    domain: Any,
    settings: Any,
    folder: str,
    version: str,
    task_id: str,
    title: str,
    description: str = "",
    comment_id: str = "",
) -> Optional[str]:
    """Launch the background router+dispatch job for an AI-agent task.

    Best-effort: any setup failure is logged and recorded as a comment, and the
    function returns ``None`` so the (already created) task still succeeds.

    Returns the background :class:`~back.core.task_manager.models.Task` id, or
    ``None`` when the job could not be started.
    """
    from back.core.helpers import require_serving_llm, resolve_warehouse_id

    try:
        host, token, llm_endpoint = require_serving_llm(domain, settings)
    except Exception as exc:  # noqa: BLE001
        msg = f"AI Agent could not start: {exc}"
        logger.warning("agent_task_runner: %s (task=%s)", msg, task_id)
        _report(
            svc, folder, version, task_id, comment_id,
            body=f"**AI Agent**\n\n{msg}", event="agent_failed",
        )
        return None

    try:
        warehouse_id = resolve_warehouse_id(domain, settings)
    except Exception:  # noqa: BLE001
        warehouse_id = ""

    bg_id = _launch_worker(
        svc=svc, domain=domain, host=host, token=token,
        llm_endpoint=llm_endpoint, warehouse_id=warehouse_id,
        folder=folder, version=version, task_id=task_id,
        title=title, description=description, comment_id=comment_id,
    )
    if bg_id is None:
        return None
    logger.info(
        "agent_task_runner: started background task %s for domain_task %s",
        bg_id,
        task_id,
    )
    return bg_id


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
    if not task_id:
        return None

    from back.core.helpers import require_serving_llm, resolve_warehouse_id

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

    bg_id = _launch_worker(
        svc=svc, domain=domain, host=host, token=token,
        llm_endpoint=llm_endpoint, warehouse_id=warehouse_id,
        folder=folder, version=version, task_id=task_id,
        title=title, description=description, comment_id=comment_id,
    )
    if bg_id is None:
        return None
    logger.info("agent_task_runner: resumed task %s (bg=%s)", task_id, bg_id)
    return bg_id


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-agent dispatch
# ---------------------------------------------------------------------------


def _dispatch_agent(
    key: str,
    *,
    domain: Any,
    host: str,
    token: str,
    llm_endpoint: str,
    warehouse_id: str,
    task_text: str = "",
    on_step: Callable[[str], None],
) -> Tuple[str, str, Dict[str, Any]]:
    """Run the specialized agent identified by *key* against the domain session.

    Returns ``(summary, report, result_payload)`` where ``summary`` is a short
    one-liner (TaskManager message) and ``report`` is the markdown posted to the
    domain Discussion. Raises on agent failure so the worker records it.
    """
    from agents.serialization import serialize_agent_steps
    from back.objects.ontology import Ontology

    if key == "ontology_assistant":
        from shared.config.constants import DEFAULT_BASE_URI

        ont = Ontology(domain)
        classes = list(domain.get_classes())
        properties = list(domain.get_properties())
        base_uri = domain.ontology.get("base_uri") or DEFAULT_BASE_URI
        from agents.agent_ontology_assistant import run_agent as run_assistant

        res = run_assistant(
            host=host,
            token=token,
            endpoint_name=llm_endpoint,
            classes=classes,
            properties=properties,
            base_uri=base_uri,
            user_message=task_text,
            on_step=on_step,
        )
        if not res.success:
            raise RuntimeError(res.error or "Ontology assistant failed")
        if res.ontology_changed:
            # Apply + persist the edits directly to the domain ontology.
            cfg = ont.apply_agent_ontology_changes(
                res.classes, res.properties, prune_orphan_mappings=True
            )
            summary = (
                f"updated the ontology "
                f"({len(cfg['classes'])} classes, {len(cfg['properties'])} properties)"
            )
            report = (
                (res.reply.strip() + "\n\n" if res.reply else "")
                + f"Applied the changes to the ontology — it now has "
                f"**{len(cfg['classes'])} class(es)** and "
                f"**{len(cfg['properties'])} property(ies)**."
            )
        else:
            summary = "reviewed the ontology (no change needed)"
            report = res.reply.strip() or "No ontology changes were necessary."
        return summary, report, {
            "ontology_changed": res.ontology_changed,
            "reply": res.reply,
            "agent_steps": serialize_agent_steps(res.steps),
            "agent_iterations": res.iterations,
            "agent_usage": res.usage,
        }

    if key == "owl_generator":
        res = Ontology(domain).generate_with_agent(
            host=host,
            token=token,
            endpoint_name=llm_endpoint,
            metadata=domain.catalog_metadata,
            warehouse_id=warehouse_id,
            on_step=on_step,
        )
        if not res.success:
            raise RuntimeError(res.error or "Ontology generation produced no output")
        summary = (
            f"generated ontology draft ({len(res.owl_content)} chars, "
            f"{res.iterations} iteration(s))"
        )
        report = (
            f"Generated an ontology draft in {res.iterations} iteration(s) "
            f"({len(res.owl_content):,} characters of Turtle). "
            "Open the **Ontology** page to review and apply it."
        )
        return summary, report, {
            "owl_content": res.owl_content,
            "agent_steps": serialize_agent_steps(res.steps),
            "agent_iterations": res.iterations,
            "agent_usage": res.usage,
        }

    if key == "business_rules_generator":
        res = Ontology(domain).generate_rules_with_agent(
            host=host,
            token=token,
            endpoint_name=llm_endpoint,
            warehouse_id=warehouse_id,
            on_step=on_step,
        )
        if not res.success:
            raise RuntimeError(res.error or "Business-rules generation failed")
        summary = f"proposed {res.total_rules()} business rule(s)"
        report = (
            f"Proposed {res.total_rules()} business rule(s): "
            f"{len(res.swrl_rules)} SWRL, "
            f"{len(res.decision_tables)} decision table(s), "
            f"{len(res.sparql_rules)} SPARQL, "
            f"{len(res.aggregate_rules)} aggregate. "
            "Open **Ontology -> Business Rules** to review and accept them."
        )
        return summary, report, {
            "swrl_rules": res.swrl_rules,
            "decision_tables": res.decision_tables,
            "sparql_rules": res.sparql_rules,
            "aggregate_rules": res.aggregate_rules,
            "agent_steps": serialize_agent_steps(res.steps),
            "agent_iterations": res.iterations,
            "agent_usage": res.usage,
        }

    if key == "icon_assign":
        entity_names = [
            c.get("name", "") for c in domain.get_classes() if c.get("name")
        ]
        if not entity_names:
            raise RuntimeError("No ontology entities to assign icons to")
        res = Ontology(domain).assign_icons_with_agent(
            host=host,
            token=token,
            endpoint_name=llm_endpoint,
            entity_names=entity_names,
            on_step=on_step,
        )
        if not res.success:
            raise RuntimeError(res.error or "Icon assignment failed")
        summary = f"assigned icons to {len(res.icons)} entity(ies)"
        preview = " ".join(
            f"{name} {emoji}" for name, emoji in list(res.icons.items())[:10]
        )
        report = (
            f"Proposed icons for {len(res.icons)} entity(ies)"
            + (f": {preview}" if preview else "")
            + ". Open the **Ontology** page to review them."
        )
        return summary, report, {
            "icons": res.icons,
            "agent_steps": serialize_agent_steps(res.steps),
            "agent_iterations": res.iterations,
            "agent_usage": res.usage,
        }

    if key == "auto_assignment":
        from back.core.databricks import DatabricksClient
        from back.objects.mapping import Mapping

        mapping_svc = Mapping(domain)
        schema_context = mapping_svc.resolve_auto_assign_schema_context({})
        ontology_ctx = Ontology(domain).agent_ontology_context()
        if not ontology_ctx.get("entities"):
            raise RuntimeError("No ontology entities to map")
        if not warehouse_id:
            raise RuntimeError("No SQL warehouse configured for auto-mapping")
        client = DatabricksClient(
            host=host, token=token, warehouse_id=warehouse_id
        )
        res = mapping_svc.auto_assign_with_agent(
            host=host,
            token=token,
            endpoint_name=llm_endpoint,
            client=client,
            metadata=schema_context,
            ontology=ontology_ctx,
            on_step=lambda m, pct=0: on_step(m),
        )
        if not res.success and res.error:
            raise RuntimeError(res.error)
        summary = (
            f"proposed {len(res.entity_mappings)} entity and "
            f"{len(res.relationship_mappings)} relationship mapping(s)"
        )
        report = (
            f"Proposed {len(res.entity_mappings)} entity and "
            f"{len(res.relationship_mappings)} relationship mapping(s). "
            "Open the **Mapping** page to review and save them."
        )
        return summary, report, {
            "entity_mappings": res.entity_mappings,
            "relationship_mappings": res.relationship_mappings,
            "agent_steps": serialize_agent_steps(res.steps),
            "agent_iterations": res.iterations,
            "agent_usage": res.usage,
        }

    raise RuntimeError(f"No dispatch wiring for agent '{key}'")


# ---------------------------------------------------------------------------
# Persistence helpers (run outside a request -> no human authorization)
# ---------------------------------------------------------------------------


def _set_status(svc: Any, folder: str, task_id: str, status: str) -> None:
    """Update the domain_tasks row status, swallowing storage errors."""
    try:
        svc.update_task_status(folder, task_id, status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_task_runner: status update skipped: %s", exc)


def _report(
    svc: Any,
    folder: str,
    version: str,
    task_id: str,
    comment_id: str,
    *,
    body: str,
    event: str = "agent_progress",
) -> None:
    """Surface the AI Agent's outcome.

    Posts *body* as a real comment in the domain Discussion (threaded under the
    originating comment when there is one, otherwise a domain-level note) so the
    team sees the report, and appends a matching review-audit row for the
    Validation timeline. Both are best-effort.
    """
    try:
        anchor_type, anchor_ref, parent_id = _resolve_anchor(
            svc, folder, version, comment_id
        )
        svc.insert_comment(
            folder,
            version,
            anchor_type=anchor_type,
            anchor_ref=anchor_ref,
            author=AI_AGENT_LABEL,
            body=body,
            parent_id=parent_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_task_runner: discussion comment skipped: %s", exc)
    try:
        svc.record_review_event(
            folder,
            version,
            AI_AGENT_LABEL,
            "commented",
            comment=body,
            meta={"task_id": task_id, "comment_id": comment_id or "", "event": event},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_task_runner: audit append skipped: %s", exc)


def _resolve_anchor(
    svc: Any, folder: str, version: str, comment_id: str
) -> Tuple[str, str, Optional[str]]:
    """Pick where to post the report: reply under the originating comment when
    available, otherwise a top-level domain note.

    Returns ``(anchor_type, anchor_ref, parent_id)``.
    """
    if comment_id:
        try:
            for c in svc.list_comments(folder, version):
                if str(c.get("id")) == str(comment_id):
                    return (
                        c.get("anchor_type") or "domain",
                        c.get("anchor_ref") or "",
                        comment_id,
                    )
        except Exception:  # noqa: BLE001
            pass
    return ("domain", "", None)
