"""v2 graph construction and transitions.

Node implementations live in agent.nodes; AgentState lives in agent.state.
Node names, dependency injection, trace decorators and transition rules are unchanged.
Imported nodes/helpers remain available here for existing callers.
"""
from functools import partial

from langgraph.graph import END, StateGraph

from .deps import SupervisorDeps
from .state import AgentState
from .nodes.targeting import (
    KNOWN_SYSTEMS, _extract_system_name, target_check_node, clarify_target_node,
)
from .nodes.routing import route_node, chat_answer_node
from .nodes.planning import plan_node, gate_node
from .nodes.research import research_node, replan_check_node, _classify_incident_type
from .nodes.analysis import analysis_node
from .nodes.writing import write_node, validate_node, finalize_node


def _after_route(state: AgentState) -> str:
    if state["route"] == "chat":
        return "chat_answer"
    return "plan" if state.get("registered_targets") else "clarify_target"

def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("collection_finished") or last.get("is_sufficient") else "research"

def _after_validate(state: AgentState) -> str:
    return "finalize" if state.get("collection_failures") or state.get("validation_passed") or state.get("validation_attempts", 0) >= 3 else "write"

def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("target_check", node(target_check_node))
    g.add_node("clarify_target", node(clarify_target_node))
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("target_check")
    g.add_conditional_edges("target_check",
                            lambda state: "clarify_target" if state.get("unknown_targets") else "route",
                            {"clarify_target": "clarify_target", "route": "route"})
    g.add_edge("clarify_target", END)
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan",
                             "clarify_target": "clarify_target"})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
