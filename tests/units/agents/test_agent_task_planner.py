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
