"""Proposal generation, validation and report finalization. Behavior preserved from graph.py."""
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, _cost
from ..evidence import validate_proposal
from ..reporting import build_report, render_report


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Collect an untrusted JSON proposal; only validated reports reach the UI."""
    attempt = state.get("validation_attempts", 0) + 1
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, state.get("validation_feedback"),
        on_token=None)
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}

@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Validate the closed proposal; three failures never become approval."""
    attempts = state.get("validation_attempts", 0) + 1
    proposal, issues = validate_proposal(state["draft"], state.get("collected", []), state.get("registered_targets", []))
    passed = not issues
    return {"validation_attempts": attempts, "validation_passed": passed,
            "validated_proposal": proposal or {}, "validation_issues": issues,
            "validation_feedback": None if passed else " / ".join(issues),
            "events": state["events"] + [{"type": "Validation", "attempt": attempts,
                "verdict": "합격" if passed else "불합격", "exhausted": not passed and attempts >= 3,
                "issues": issues}]}

@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    passed = state.get("validation_passed", False)
    report = build_report(state.get("validated_proposal", {}), state.get("collected", []),
                          passed, state.get("validation_attempts", 0), state.get("validation_issues", []))
    return {"final": render_report(report), "report": report,
            "events": state["events"] + [{"type": "ReportReady", "verified": passed,
                "source_count": len(report["sources"]), "attempts": state.get("validation_attempts", 0)},
                {"type": "Finalize", "pipeline": "incident-v2", "verified": passed}]}
