"""Shared v2 graph state and cost accumulation; no node dependencies."""
from typing import TypedDict

class AgentState(TypedDict, total=False):
    query: str
    registered_targets: list[str]
    unknown_targets: list[str]
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    collection_failures: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_passed: bool
    report: dict
    validated_proposal: dict
    validation_issues: list[str]
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)
