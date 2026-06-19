"""Tests for the AI-agent task runner (router + dispatch orchestration).

We never call a real agent: the router and the per-agent dispatch are patched,
and the in-memory ``TaskManager`` singleton tracks the background job. We assert
the orchestration side effects -- task status transitions on the ``domain_tasks``
row, the audit comments, and the TaskManager terminal state.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from back.core.task_manager import get_task_manager
from back.objects.registry import agent_task_runner as runner


def _fake_svc():
    """A registry-service double recording status updates + posted comments."""
    statuses = []
    comments = []
    svc = MagicMock()
    svc.update_task_status.side_effect = lambda folder, tid, status: (
        statuses.append(status) or (True, "")
    )
    svc.list_comments.return_value = []
    svc.insert_comment.side_effect = lambda *a, **k: (
        comments.append(k.get("body", "")) or {"id": "x"}
    )
    svc.record_review_event.side_effect = lambda *a, **k: (True, "")
    return svc, statuses, comments


def _svc_with_thread(thread):
    """Service double whose list_comments returns *thread* (list of dicts)."""
    svc, statuses, comments = _fake_svc()
    svc.list_comments.return_value = thread
    return svc, statuses, comments


def _router_ok(key="owl_generator"):
    return SimpleNamespace(
        success=True, chosen_agent_key=key, reasoning="fits", error=""
    )


def _router_fail():
    return SimpleNamespace(
        success=False, chosen_agent_key="", reasoning="", error="no fit"
    )


def _plan_ready():
    return SimpleNamespace(success=True, ready=True, message="Running now.", error="")


# ----------------------------------------------------------------------
# Sentinel helper
# ----------------------------------------------------------------------


def test_is_ai_agent():
    assert runner.is_ai_agent("agent://router") is True
    assert runner.is_ai_agent("AGENT://ROUTER") is True
    assert runner.is_ai_agent("bob@acme.com") is False
    assert runner.is_ai_agent("") is False


# ----------------------------------------------------------------------
# start_agent_task setup guard
# ----------------------------------------------------------------------


def test_start_agent_task_returns_none_when_llm_unconfigured():
    svc, _, _ = _fake_svc()
    with patch(
        "back.core.helpers.require_serving_llm",
        side_effect=RuntimeError("Databricks LLM not configured"),
    ):
        bg = runner.start_agent_task(
            svc=svc, domain=MagicMock(), settings=MagicMock(),
            folder="acme", version="1", task_id="t1",
            title="do something", description="",
        )
    assert bg is None
    # The failure is surfaced as an audit row on the task.
    assert svc.record_review_event.called


# ----------------------------------------------------------------------
# Background worker
# ----------------------------------------------------------------------


def test_run_happy_path_routes_dispatches_and_completes():
    # Thread with a human reply beyond the root (2 user turns) so the
    # code-enforced first-pass park does NOT apply and the ready path runs.
    svc, statuses, comments = _svc_with_thread([
        {"id": "c1", "parent_id": "", "author": "alice@x.io",
         "body": "Generate ontology", "created_at": "t0"},
        {"id": "r1", "parent_id": "c1", "author": "alice@x.io",
         "body": "go ahead", "created_at": "t1"},
    ])
    tm = get_task_manager()
    task = tm.create_task("AI Agent: x", "task_router", steps=[
        {"name": "route", "description": "r"},
        {"name": "run", "description": "u"},
        {"name": "finalize", "description": "f"},
    ])

    with patch("agents.agent_task_router.run_agent", return_value=_router_ok()), \
         patch("agents.agent_task_planner.run_agent", return_value=_plan_ready()), \
         patch.object(
             runner, "_dispatch_agent",
             return_value=(
                 "generated ontology (10 chars)",
                 "Generated an ontology draft. Open the Ontology page.",
                 {"owl_content": "x"},
             ),
         ):
        runner._run(
            task, svc=svc, domain=MagicMock(), host="h", token="t",
            llm_endpoint="ep", warehouse_id="wh", folder="acme", version="1",
            domain_task_id="t1", title="Generate ontology", description="",
            comment_id="c1",
        )

    final = tm.get_task(task.id)
    assert final.status.value == "completed"
    assert final.result["agent"] == "owl_generator"
    # Task moved open -> in_progress -> done.
    assert statuses == ["in_progress", "done"]
    # The agent report is posted to the Discussion as a comment.
    assert any("Ontology Generator" in body for body in comments)
    assert any("Generated an ontology draft" in body for body in comments)


def test_run_router_failure_leaves_task_open():
    svc, statuses, comments = _fake_svc()
    tm = get_task_manager()
    task = tm.create_task("AI Agent: x", "task_router")

    with patch("agents.agent_task_router.run_agent", return_value=_router_fail()):
        runner._run(
            task, svc=svc, domain=MagicMock(), host="h", token="t",
            llm_endpoint="ep", warehouse_id="", folder="acme", version="1",
            domain_task_id="t1", title="ambiguous", description="",
            comment_id="",
        )

    final = tm.get_task(task.id)
    assert final.status.value == "failed"
    # Status never advanced past open.
    assert statuses == []
    assert any("could not route" in body.lower() for body in comments)


def test_run_dispatch_exception_is_recorded_as_failure():
    # A replied-to thread (2 user turns) so the ready path reaches dispatch.
    svc, statuses, comments = _svc_with_thread([
        {"id": "c1", "parent_id": "", "author": "alice@x.io",
         "body": "Generate ontology", "created_at": "t0"},
        {"id": "r1", "parent_id": "c1", "author": "alice@x.io",
         "body": "go ahead", "created_at": "t1"},
    ])
    tm = get_task_manager()
    task = tm.create_task("AI Agent: x", "task_router")

    with patch("agents.agent_task_router.run_agent", return_value=_router_ok()), \
         patch("agents.agent_task_planner.run_agent", return_value=_plan_ready()), \
         patch.object(runner, "_dispatch_agent", side_effect=RuntimeError("agent boom")):
        runner._run(
            task, svc=svc, domain=MagicMock(), host="h", token="t",
            llm_endpoint="ep", warehouse_id="", folder="acme", version="1",
            domain_task_id="t1", title="Generate ontology", description="",
            comment_id="c1",
        )

    final = tm.get_task(task.id)
    assert final.status.value == "failed"
    # It was picked up (in_progress) but never marked done.
    assert statuses == ["in_progress"]
    assert any("run failed" in body.lower() for body in comments)


def test_dispatch_unknown_agent_raises():
    with pytest.raises(RuntimeError):
        runner._dispatch_agent(
            "no_such_agent", domain=MagicMock(), host="h", token="t",
            llm_endpoint="ep", warehouse_id="", on_step=lambda m: None,
        )


def test_dispatch_auto_assignment_persists_mappings():
    # The mapper must APPLY+PERSIST its proposals (not merely propose), so the
    # mappings survive a page reload.
    res = SimpleNamespace(
        success=True, error="",
        entity_mappings=[{"ontology_class": "C"}],
        relationship_mappings=[],
        steps=[], iterations=1, usage={},
    )
    fake_mapping = MagicMock()
    fake_mapping.resolve_auto_assign_schema_context.return_value = {"tables": [1]}
    fake_mapping.auto_assign_with_agent.return_value = res
    fake_mapping.apply_agent_mappings.return_value = {"entities": 1, "relationships": 0}
    fake_ont = MagicMock()
    fake_ont.agent_ontology_context.return_value = {"entities": [{"uri": "C"}]}

    with patch("back.objects.mapping.Mapping", return_value=fake_mapping), \
         patch("back.objects.ontology.Ontology", return_value=fake_ont), \
         patch("back.core.databricks.DatabricksClient", return_value=MagicMock()):
        summary, report, payload = runner._dispatch_agent(
            "auto_assignment", domain=MagicMock(), host="h", token="t",
            llm_endpoint="ep", warehouse_id="wh", task_text="map it",
            on_step=lambda m: None,
        )

    fake_mapping.apply_agent_mappings.assert_called_once_with(
        res.entity_mappings, res.relationship_mappings
    )
    assert "Applied SQL mappings" in report
    assert payload["entity_mappings"] == res.entity_mappings


def test_dispatch_ontology_assistant_applies_and_saves_changes():
    res = SimpleNamespace(
        success=True, ontology_changed=True, reply="Removed the Person class.",
        classes=[{"uri": "a"}], properties=[], steps=[], iterations=2, usage={},
    )
    fake_ont = MagicMock()
    fake_ont.apply_agent_ontology_changes.return_value = {
        "classes": [{"uri": "a"}], "properties": [],
    }
    domain = MagicMock()
    domain.get_classes.return_value = [{"name": "Person"}]
    domain.get_properties.return_value = []
    domain.ontology = {"base_uri": "http://x#"}

    with patch("agents.agent_ontology_assistant.run_agent", return_value=res), \
         patch("back.objects.ontology.Ontology", return_value=fake_ont):
        summary, report, payload = runner._dispatch_agent(
            "ontology_assistant", domain=domain, host="h", token="t",
            llm_endpoint="ep", warehouse_id="", task_text="Is Person needed?",
            on_step=lambda m: None,
        )

    # The edits are applied + persisted (apply_agent_ontology_changes calls save()).
    fake_ont.apply_agent_ontology_changes.assert_called_once()
    assert "updated the ontology" in summary
    assert payload["ontology_changed"] is True
    assert "Removed the Person class." in report


def test_dispatch_ontology_assistant_no_change():
    res = SimpleNamespace(
        success=True, ontology_changed=False, reply="Person is still needed.",
        classes=[], properties=[], steps=[], iterations=1, usage={},
    )
    fake_ont = MagicMock()
    domain = MagicMock()
    domain.get_classes.return_value = [{"name": "Person"}]
    domain.get_properties.return_value = []
    domain.ontology = {"base_uri": "http://x#"}

    with patch("agents.agent_ontology_assistant.run_agent", return_value=res), \
         patch("back.objects.ontology.Ontology", return_value=fake_ont):
        summary, report, payload = runner._dispatch_agent(
            "ontology_assistant", domain=domain, host="h", token="t",
            llm_endpoint="ep", warehouse_id="", task_text="Is Person needed?",
            on_step=lambda m: None,
        )

    fake_ont.apply_agent_ontology_changes.assert_not_called()
    assert payload["ontology_changed"] is False
    assert "Person is still needed." in report


# ----------------------------------------------------------------------
# Plan-then-run worker (_run_for_task) + thread-driven resume
# ----------------------------------------------------------------------


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


def test_first_pass_parks_even_when_planner_ready(monkeypatch):
    # Only the root comment (one human turn) -> first pass. Even though the
    # planner says ready, the code-level guard must override and park.
    svc, statuses, comments = _svc_with_thread(
        [{"id": "root", "parent_id": "", "author": "alice@x.io",
          "body": "Remove Person", "created_at": "t0",
          "anchor_type": "domain", "anchor_ref": ""}]
    )
    router_res = SimpleNamespace(success=True, chosen_agent_key="ontology_assistant",
                                 reasoning="edit", error="")
    plan_res = SimpleNamespace(success=True, ready=True, message="Plan: confirm?",
                               error="")
    monkeypatch.setattr("agents.agent_task_router.run_agent", lambda *a, **k: router_res)
    monkeypatch.setattr("agents.agent_task_planner.run_agent", lambda *a, **k: plan_res)
    dispatched = []
    monkeypatch.setattr(runner, "_dispatch_agent",
                        lambda *a, **k: dispatched.append(k) or ("s", "r", {}))

    runner._run_for_task(
        svc=svc, domain=MagicMock(), host="h", token="t", llm_endpoint="ep",
        warehouse_id="", folder="d", version="v", domain_task_id="T1",
        title="Remove Person", description="", comment_id="root",
        on_step=lambda m: None, tm=None, tm_task_id=None,
    )

    assert dispatched == []                       # guard overrode planner readiness
    assert statuses[-1] == "in_progress"          # parked, never marked done
    assert "done" not in statuses


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


def test_is_unsupported_mapping_removal_detects_removal_intent():
    f = runner._is_unsupported_mapping_removal
    assert f("Unmap Customer") is True
    assert f("please remove the mapping for Order") is True
    assert f("clear the SQL mapping") is True
    assert f("unassign Person") is True
    # Additive / unrelated requests must NOT trip the guard.
    assert f("Map Customer to the orders table") is False
    assert f("create an Agent Manager entity") is False
    assert f("") is False


def test_run_for_task_refuses_unmap_and_parks(monkeypatch):
    # "Unmap Customer" routed to the Auto SQL Mapper must be refused (the mapper
    # only adds), parked for a rephrase -- never planned, never dispatched.
    svc, statuses, comments = _svc_with_thread(
        [{"id": "root", "parent_id": "", "author": "alice@x.io",
          "body": "Unmap Customer", "created_at": "t0"}]
    )
    router_res = SimpleNamespace(success=True, chosen_agent_key="auto_assignment",
                                 reasoning="map", error="")
    plan_called = []
    monkeypatch.setattr("agents.agent_task_router.run_agent", lambda *a, **k: router_res)
    monkeypatch.setattr(
        "agents.agent_task_planner.run_agent",
        lambda *a, **k: plan_called.append(1) or SimpleNamespace(
            success=True, ready=True, message="", error=""),
    )
    dispatched = []
    monkeypatch.setattr(runner, "_dispatch_agent",
                        lambda *a, **k: dispatched.append(k) or ("s", "r", {}))

    runner._run_for_task(
        svc=svc, domain=MagicMock(), host="h", token="t", llm_endpoint="ep",
        warehouse_id="wh", folder="d", version="v", domain_task_id="T1",
        title="Unmap Customer", description="", comment_id="root",
        on_step=lambda m: None, tm=None, tm_task_id=None,
    )

    assert dispatched == []                  # mapper never ran
    assert plan_called == []                  # guard short-circuits before planning
    assert statuses[-1] == "in_progress"      # parked for a rephrase
    assert "done" not in statuses
    assert any("unmap" in c.lower() for c in comments)


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


def test_claim_task_is_idempotent_per_id():
    assert "T9" not in runner._ACTIVE_TASKS
    try:
        assert runner._claim_task("T9") is True       # first claim wins
        assert runner._claim_task("T9") is False       # already active
        assert runner._claim_task("") is False         # empty id never claims
    finally:
        runner._ACTIVE_TASKS.discard("T9")


def test_launch_failure_releases_the_claim(monkeypatch):
    tm = MagicMock()
    tm.run_background_task.side_effect = RuntimeError("thread boom")
    monkeypatch.setattr(
        "back.core.task_manager.get_task_manager", lambda: tm
    )
    assert "T7" not in runner._ACTIVE_TASKS
    with pytest.raises(RuntimeError):
        runner._launch_worker(
            svc=MagicMock(), domain=MagicMock(), host="h", token="t",
            llm_endpoint="ep", warehouse_id="", folder="d", version="v",
            task_id="T7", title="x", description="", comment_id="root",
        )
    # The claim must NOT leak when the launch raises.
    assert "T7" not in runner._ACTIVE_TASKS
