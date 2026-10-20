"""Plan reconciliation and analysis gate. Behavior preserved from graph.py."""
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, _cost
from ..planning import reconcile_monitoring_tasks
from .targeting import _extract_system_name


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": _extract_system_name(state["query"]), "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = deps.fast_llm.plan(state["query"], procedure)
    targets = state["registered_targets"]
    proposed_targets = [t.get("target") for t in result["tasks"]
                        if t.get("agent") == "research"]
    tasks = reconcile_monitoring_tasks(result["tasks"], targets)
    reconciliation = {"type": "PlanReconciled",
                      "added_targets": [t for t in targets if t not in proposed_targets],
                      "removed_targets": list(dict.fromkeys(
                          t for t in proposed_targets if t and t not in targets))}
    event = {"type": "PlanCreated", "task_count": len(tasks),
             "targets": targets}
    return {"tasks": tasks, "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [reconciliation, event]}

@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}
