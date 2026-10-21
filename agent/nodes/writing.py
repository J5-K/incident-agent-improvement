"""Proposal generation, validation and report finalization. Behavior preserved from graph.py."""
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, _cost
from ..evidence import validate_proposal
from ..reporting import build_report, render_report
from ..runbook_search import request_scope, scoped_query


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Collect an untrusted JSON proposal; only validated reports reach the UI."""
    if state.get("collection_failures") or state.get("runbook_unmatched"):
        return {"draft": "", "last_prompt": None,
                "events": state["events"] + [{"type": "DraftSkipped", "reason": "자료 조회 실패·매뉴얼 근거 부족: 확보된 사실만 제공"}]}
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
    if state.get("collection_failures") or state.get("runbook_unmatched"):
        return {"validation_attempts": 0, "validation_passed": False, "validated_proposal": {},
                "validation_feedback": None, "validation_issues": ["자료 조회 실패 또는 적합한 매뉴얼 없음으로 원인·조치 제안 검증을 수행하지 않았습니다."],
                "events": state["events"] + [{"type": "Validation", "attempt": 0, "verdict": "미수행", "skipped": True}]}
    attempts = state.get("validation_attempts", 0) + 1
    proposal, issues = validate_proposal(state["draft"], state.get("collected", []), state.get("registered_targets", []))
    for name in state.get("registered_targets", []):
        scope = request_scope(scoped_query(state["query"], name, state["registered_targets"]))
        books = {c.get("incident_type") for c in state.get("collected", [])
                 if c.get("source") == "runbook" and c.get("system_name") == name}
        if books.intersection(scope["excluded"]):
            issues.append(name + ": 사용자가 제외한 대응 포함")
        if scope["included"] and books - set(scope["included"]):
            issues.append(name + ": 요청 증상 범위 밖의 매뉴얼 포함")
        if scope["included"] and not set(scope["included"]).issubset(books):
            issues.append(name + ": 요청 증상에 필요한 매뉴얼 누락")
        if scope["unsupported"]:
            issues.append(name + ": 인증서 조회 기능·매뉴얼 없음")
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
                          passed, state.get("validation_attempts", 0), state.get("validation_issues", []),
                          failures=state.get("collection_failures", []), targets=state.get("registered_targets", []),
                          searches=state.get("runbook_searches", []), unmatched=state.get("runbook_unmatched", []))
    return {"final": render_report(report), "report": report,
            "events": state["events"] + [{"type": "ReportReady", "verified": passed,
                "source_count": len(report["sources"]), "attempts": state.get("validation_attempts", 0),
                "collection_status": report["collection"]["status"]},
                {"type": "Finalize", "pipeline": "incident-v2", "verified": passed}]}
