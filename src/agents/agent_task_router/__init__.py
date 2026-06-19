"""
Task Router Agent -- picks which specialized OntoBricks agent should carry
out a task assigned to the "AI Agent".

Single-shot classifier (no tool loop). Driven by
:func:`back.objects.registry.agent_task_runner.start_agent_task`, which runs
the chosen agent against the task's domain in a background thread.

Exports:
    run_agent / AgentResult
"""

from agents.agent_task_router.engine import AgentResult, run_agent  # noqa: F401

__all__ = ["run_agent", "AgentResult"]
