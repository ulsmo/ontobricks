"""
Static registry of the agents that the task router may dispatch to.

OntoBricks discovers agents statically (one package per agent under
``src/agents/agent_*``); there is no dynamic factory. This module mirrors that
convention: it lists the *dispatchable* agents -- the ones the
:mod:`agents.agent_task_router` can pick and the orchestrator
(:mod:`back.objects.registry.agent_task_runner`) knows how to run from a
domain session.

Only agents that already have background-task + domain-context wiring are
listed here. Interactive chat agents (dtwin chat, ontology assistant, cohort)
are intentionally excluded -- they need a live conversation, not a task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class AgentSpec:
    """One dispatchable agent the router can choose.

    Attributes:
        key: Stable identifier used by the router output and the orchestrator
            dispatch table.
        label: Human-readable name surfaced in task comments / UI.
        description: What the agent does and *when to pick it* -- fed verbatim
            to the router LLM, so phrase it as routing guidance.
        task_type: The :class:`~back.core.task_manager.models.Task` ``task_type``
            the underlying agent runs under (kept for parity with the existing
            background routes).
    """

    key: str
    label: str
    description: str
    task_type: str


DISPATCHABLE_AGENTS: List[AgentSpec] = [
    AgentSpec(
        key="ontology_assistant",
        label="Ontology Assistant",
        description=(
            "Design and edit the domain ontology (the data model) in place and "
            "save the changes. This is the default agent for any ontology "
            "MODELING or DESIGN task: create, add, model, or design new "
            "classes/entities/relationships (e.g. 'create an Agent Manager "
            "entity'); rename, merge, split, or remove existing ones; evaluate "
            "whether a class is necessary; adjust the class hierarchy or a "
            "relationship's domain/range. Pick this whenever the task is about "
            "shaping what concepts/entities exist in the ontology, including "
            "targeted edits and incremental design (changes are applied "
            "directly). Prefer this over the Generator unless the task asks to "
            "(re)build the WHOLE ontology from scratch."
        ),
        task_type="ontology_assistant",
    ),
    AgentSpec(
        key="owl_generator",
        label="Ontology Generator",
        description=(
            "Generate the WHOLE OWL/Turtle ontology from scratch from the "
            "imported Unity Catalog metadata and domain documents. Pick this "
            "only to bootstrap a brand-new ontology or fully regenerate "
            "everything -- NOT for editing or tweaking a few classes (use the "
            "Ontology Assistant for targeted edits)."
        ),
        task_type="ontology_generation",
    ),
    AgentSpec(
        key="business_rules_generator",
        label="Business Rules Generator",
        description=(
            "Propose business rules (SWRL inferences, decision tables, SPARQL "
            "queries, aggregate constraints) from the existing ontology "
            "design. Pick this when the task asks to add, define, generate, or "
            "suggest business rules, logic, inferences, constraints, or "
            "validations."
        ),
        task_type="business_rules_generation",
    ),
    AgentSpec(
        key="icon_assign",
        label="Icon Assigner",
        description=(
            "Assign a representative emoji icon to each ontology entity. Pick "
            "this when the task asks to set, assign, choose, or refresh icons "
            "or emojis for entities / classes."
        ),
        task_type="auto_assign_icons",
    ),
    AgentSpec(
        key="auto_assignment",
        label="Auto SQL Mapper",
        description=(
            "Map ontology entities and relationships to validated SQL queries "
            "over the warehouse tables. Pick this when the task asks to map, "
            "assign, or connect entities / relationships to tables, columns, "
            "or data, or to build the SQL mappings."
        ),
        task_type="auto_assign",
    ),
]


def list_agents() -> List[AgentSpec]:
    """Return the dispatchable agent specs (a copy, safe to mutate)."""
    return list(DISPATCHABLE_AGENTS)


def get_agent(key: str) -> Optional[AgentSpec]:
    """Return the :class:`AgentSpec` for *key*, or ``None`` when unknown."""
    key = (key or "").strip()
    for spec in DISPATCHABLE_AGENTS:
        if spec.key == key:
            return spec
    return None
