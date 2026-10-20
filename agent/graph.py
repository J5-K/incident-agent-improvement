"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

target_check → (미등록 확인 안내 | route → chat/incident)
incident: plan(MCP Prompt 절차 반영) → gate(권위 게이트) → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import time
from functools import partial
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .retry import with_retry
from .targets import load_known_systems, resolve_targets
from .planning import reconcile_monitoring_tasks


KNOWN_SYSTEMS = load_known_systems()


def _extract_system_name(query: str) -> str:
    targets, unknown = resolve_targets(query, KNOWN_SYSTEMS)
    if unknown or not targets:
        raise ValueError("A registered system must be confirmed before planning")
    return targets[0]


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
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


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


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    result, usage = deps.fast_llm.route(state["query"])
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": state.get("events", []) + [event]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


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


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 + Wave 2 Tool/Resource 실호출."""
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다 (not research_agent 버그의 수정판)
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    # URI를 코드에 하드코딩하지 않는다. 여러 MCP 서버가 연결돼 있고 그중 어느 서버가
    # "get_incident_runbook" resource를 제공하는지 모른다고 가정하고, 먼저 연결된
    # 서버들을 조회(list)해 유효한 서버를 찾은 뒤에만 그 서버로 요청한다.
    needs_runbook = any(tasks[i]["kind"] != "monitoring" for i in research_indices)
    runbook_server = runbook_uri_template = None
    if needs_runbook:
        runbook_server, runbook_uri_template = await deps.mcp.find_resource_template(
            "get_incident_runbook")

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        await asyncio.sleep(0.35)   # 태스크당 고정 지연 — 병렬 효과를 실측하기 위함
        if task["kind"] == "monitoring":
            data, retries = await with_retry(
                deps.mcp.call_tool, "get_monitoring_status",
                {"system_name": task["target"]})
            return {"idx": idx, "source": "monitoring", "data": data, "retries": retries}
        uri = runbook_uri_template.format(incident_type=task["target"])
        data = await deps.mcp.read_resource(uri, server=runbook_server)
        return {"idx": idx, "source": "runbook", "data": data, "retries": 0}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in research_indices))
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    for r in results:
        new_tasks[r["idx"]]["status"] = "done"
        task = new_tasks[r["idx"]]
        system_name = (task["target"] if r["source"] == "monitoring"
                       else task["system_name"])
        collected.append({"source": r["source"], "data": r["data"],
                          "system_name": system_name})
        retry_total += r["retries"]
        events.append({"type": "TaskCompleted", "id": new_tasks[r["idx"]]["id"],
                       "source": r["source"], "target": new_tasks[r["idx"]]["target"],
                       "system_name": system_name,
                       "retries": r["retries"]})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events}


# 수집된 모니터링 결과(상태·알림 문구)를 보고 어떤 runbook이 맞는지 고른다. LLM에게
# 다시 묻는 대신 코드 레벨 규칙으로 판단한다 — gate(Wave 6)·replan(Wave 7)과 같은 설계
# 원칙: 모델의 판단을 그대로 믿지 않고, 실측 데이터로 검증 가능한 결정은 코드가 내린다.
_INCIDENT_TYPE_RULES = [
    ("latency", "high-latency"),
    ("pool", "high-latency"),
    ("cpu", "cpu-spike"),
    ("memory", "memory-leak"),
    ("heap", "memory-leak"),
    ("timeout", "network-timeout"),
    ("network", "network-timeout"),
    ("disk", "disk-full"),
    ("down", "db-down"),
]


def _classify_incident_type(collected: list[dict]) -> str:
    text = " ".join(
        f"{c['data'].get('status', '')} " + " ".join(c["data"].get("alerts", []))
        for c in collected if c.get("source") == "monitoring"
    ).lower()
    for keyword, incident_type in _INCIDENT_TYPE_RULES:
        if keyword in text:
            return incident_type
    return "db-down"   # 매칭되는 증상이 없으면 가장 일반적인 장애 대응 절차로 폴백


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """각 요청 시스템의 매뉴얼을 따로 확인하고 누락분을 한 라운드에 수집한다."""
    collected = state.get("collected", [])
    covered = {c.get("system_name") for c in collected if c["source"] == "runbook"}
    missing = [name for name in state["registered_targets"] if name not in covered]
    rounds = state.get("replan_rounds", 0)
    if not missing:
        return {"events": state["events"] + [
            {"type": "ReplanCheck", "round": rounds, "is_sufficient": True}]}
    if rounds >= 1:
        raise RuntimeError("매뉴얼 수집 누락: " + ", ".join(missing))
    gap_tasks = []
    for i, name in enumerate(missing):
        monitoring = [c for c in collected if c["source"] == "monitoring"
                      and c["data"].get("system_name") == name]
        if not monitoring:
            raise RuntimeError("모니터링 수집 누락: " + name)
        gap_tasks.append({"id": f"task-gap-runbook-{i}", "agent": "research",
                          "kind": "runbook", "system_name": name,
                          "target": _classify_incident_type(monitoring), "status": "pending"})
    verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
               "target": gap_tasks[0]["target"],
               "targets": [t["target"] for t in gap_tasks],
               "systems": missing}
    return {"tasks": state["tasks"] + gap_tasks, "replan_rounds": rounds + 1,
            "events": state["events"] + [verdict]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 8/10a — 고품질 모델로 초안 작성 (1차는 의도적 저품질).

    토큰마다 attempt 번호를 같이 실어 보낸다 — validate가 반려해 재작성(attempt 증가)
    될 때 프론트가 새 말풍선으로 끊어서 보여줄 수 있게 하기 위함(Wave 8 재작성 루프를
    그대로 노출)."""
    attempt = state.get("validation_attempts", 0) + 1
    writer = get_stream_writer()
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, state.get("validation_feedback"),
        on_token=lambda piece: writer(
            {"type": "AnswerToken", "attempt": attempt, "delta": piece}))
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """형식과 복수 대상 이름 누락을 검사한다. 의미·수치의 정확성 검증은 별개다."""
    draft = state["draft"]
    attempts = state.get("validation_attempts", 0) + 1
    missing_targets = [name for name in state.get("registered_targets", [])
                       if len(state.get("registered_targets", [])) > 1
                       and name.lower() not in draft.lower()]
    ok = len(draft) >= 30 and "1)" in draft and not missing_targets
    verdict = "합격" if ok else "불합격"
    if not ok and attempts >= 3:   # 실모델 무한 재작성 방지 — 최대 3회 후 통과 처리
        ok, verdict = True, "합격(최대 시도 도달 — 통과 처리)"
    issues = []
    if len(draft) < 30:
        issues.append(f"응답이 너무 짧습니다({len(draft)}자, 최소 30자).")
    if "1)" not in draft:
        issues.append("절차를 1) 2) 3) 번호로 설명하세요.")
    if missing_targets:
        issues.append("누락된 시스템: " + ", ".join(missing_targets) +
                      ". 각 시스템의 이름, 관측 상태, 해당 매뉴얼의 대응 절차를 포함하세요.")
    feedback = None if ok else " ".join(issues)
    event = {"type": "Validation", "attempt": attempts, "verdict": verdict}
    return {"validation_attempts": attempts, "validation_feedback": feedback,
            "events": state["events"] + [event]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    final = state["draft"]
    # 재작성 한도 이후에도 빠진 대상은 관측 근거로만 보충한다.
    missing = [name for name in state.get("registered_targets", [])
               if len(state.get("registered_targets", [])) > 1
               and name.lower() not in final.lower()]
    for name in missing:
        facts = [c for c in state.get("collected", []) if c.get("system_name") == name]
        monitoring = next((c["data"] for c in facts if c["source"] == "monitoring"), None)
        runbook = next((c["data"] for c in facts if c["source"] == "runbook"), "")
        if monitoring:
            final += (f"\n\n[누락 대상 근거 보충: {name}] 상태={monitoring['status']}, "
                      f"CPU {monitoring['cpu_percent']}%, 알림={monitoring['alerts']}.")
        if runbook:
            final += "\n해당 시스템 대응 매뉴얼:\n" + runbook.strip()
    events = state["events"]
    if missing:
        events += [{"type": "AnswerCoverageSupplemented", "systems": missing}]
    return {"final": final,
            "events": events + [{"type": "Finalize", "pipeline": "incident-v2"}]}


def _after_route(state: AgentState) -> str:
    if state["route"] == "chat":
        return "chat_answer"
    return "plan" if state.get("registered_targets") else "clarify_target"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") else "research"


def _after_validate(state: AgentState) -> str:
    return "finalize" if state.get("validation_feedback") is None else "write"


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
