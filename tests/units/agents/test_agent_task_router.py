"""Tests for the Task Router agent + the dispatchable-agent registry.

The router is a single-shot classifier; we patch ``call_serving_endpoint``
to return a scripted JSON decision and assert the parsed routing outcome.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agents import registry
from agents.agent_task_router import engine as router_engine


def _llm(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _run(content: str):
    with patch.object(router_engine, "call_serving_endpoint", return_value=_llm(content)):
        return router_engine.run_agent(
            "https://host",
            "tok",
            "ep",
            task_title="Generate the ontology from the metadata",
            task_description="",
            available_agents=registry.list_agents(),
        )


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_registry_lists_agents_with_unique_keys():
    agents = registry.list_agents()
    assert agents, "registry must not be empty"
    keys = [a.key for a in agents]
    assert len(keys) == len(set(keys)), "agent keys must be unique"
    assert {"owl_generator", "business_rules_generator", "icon_assign",
            "auto_assignment", "ontology_assistant"} <= set(keys)


def test_get_agent_known_and_unknown():
    assert registry.get_agent("owl_generator").label == "Ontology Generator"
    assert registry.get_agent("does_not_exist") is None
    assert registry.get_agent("") is None


def test_ontology_assistant_is_the_design_default():
    # Ontology design/modeling tasks ("create/model an entity") must route to the
    # Ontology Assistant, so its routing guidance has to advertise design intent.
    desc = registry.get_agent("ontology_assistant").description.lower()
    assert "design" in desc
    assert any(verb in desc for verb in ("create", "model", "add"))
    assert "new" in desc


def test_router_prompt_treats_ontology_as_domain_model():
    # The router must not bail to "none" on ontology design tasks; the system
    # prompt has to steer design/modeling toward an agent and reserve "none" for
    # clearly unrelated work.
    prompt = router_engine._SYSTEM_PROMPT.lower()
    assert "domain model" in prompt
    assert "design" in prompt
    assert "none" in prompt


# ----------------------------------------------------------------------
# Router decisions
# ----------------------------------------------------------------------


def test_router_picks_valid_agent():
    res = _run('{"agent": "owl_generator", "reasoning": "asks to generate ontology"}')
    assert res.success is True
    assert res.chosen_agent_key == "owl_generator"
    assert res.reasoning
    assert res.usage["prompt_tokens"] == 10


def test_router_handles_code_fenced_json():
    res = _run('```json\n{"agent": "icon_assign", "reasoning": "icons"}\n```')
    assert res.success is True
    assert res.chosen_agent_key == "icon_assign"


def test_router_none_decision_is_unsuccessful():
    res = _run('{"agent": "none", "reasoning": "nothing fits"}')
    assert res.success is False
    assert res.chosen_agent_key == ""
    assert "nothing fits" in res.error


def test_router_rejects_unknown_agent_key():
    res = _run('{"agent": "make_coffee", "reasoning": "?"}')
    assert res.success is False
    assert "unknown agent" in res.error.lower()


def test_router_unparseable_response():
    res = _run("I think you should run the owl generator.")
    assert res.success is False
    assert "unparseable" in res.error.lower()


def test_router_llm_failure_is_captured():
    with patch.object(
        router_engine, "call_serving_endpoint", side_effect=RuntimeError("boom")
    ):
        res = router_engine.run_agent(
            "h", "t", "ep",
            task_title="x",
            task_description="",
            available_agents=registry.list_agents(),
        )
    assert res.success is False
    assert "boom" in res.error


def test_router_no_agents_registered():
    res = router_engine.run_agent(
        "h", "t", "ep", task_title="x", task_description="", available_agents=[]
    )
    assert res.success is False
    assert "No dispatchable agents" in res.error
