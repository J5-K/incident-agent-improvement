"""MCP collection and per-system runbook replanning. Behavior preserved from graph.py."""
import asyncio
import time

from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, _cost
from ..retry import with_retry

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
                          "system_name": system_name,
                          "source_id": ("monitoring:" + system_name if r["source"] == "monitoring" else "runbook:" + system_name + ":" + task["target"]),
                          **({"incident_type": task["target"], "uri": runbook_uri_template.format(incident_type=task["target"]), "server": runbook_server} if r["source"] == "runbook" else {"tool": "get_monitoring_status"})})
        retry_total += r["retries"]
        events.append({"type": "TaskCompleted", "id": new_tasks[r["idx"]]["id"],
                       "source": r["source"], "target": new_tasks[r["idx"]]["target"],
                       "system_name": system_name,
                       "retries": r["retries"]})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events}

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
