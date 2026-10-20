"""Target resolution and clarification. Behavior preserved from graph.py."""
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, _cost
from ..targets import load_known_systems, resolve_targets

KNOWN_SYSTEMS = load_known_systems()


def _extract_system_name(query: str) -> str:
    targets, unknown = resolve_targets(query, KNOWN_SYSTEMS)
    if unknown or not targets:
        raise ValueError("A registered system must be confirmed before planning")
    return targets[0]

@traceable(name="target_check_node")
async def target_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    targets, unknown = resolve_targets(state["query"], KNOWN_SYSTEMS)
    return {"registered_targets": targets, "unknown_targets": unknown}

@traceable(name="clarify_target_node")
async def clarify_target_node(state: AgentState, deps: SupervisorDeps) -> dict:
    unknown = state.get("unknown_targets", [])
    if unknown:
        message = f"등록되지 않은 시스템: {', '.join(unknown)}. 다른 시스템으로 대체 조회하지 않았습니다."
        reason = "unknown_system"
    else:
        message = "조회할 시스템 이름을 알려주세요. 임의의 시스템을 대신 조회하지 않았습니다."
        reason = "missing_system"
    message += " 등록된 시스템: " + ", ".join(sorted(KNOWN_SYSTEMS)) + ". 정확한 시스템 이름을 포함해 다시 질문해 주세요."
    return {"route": "clarification", "final": message, "tasks": [],
            "events": state.get("events", []) + [
                {"type": "TargetClarification", "reason": reason, "unknown_targets": unknown},
                {"type": "Finalize", "pipeline": "target-clarification"}]}
