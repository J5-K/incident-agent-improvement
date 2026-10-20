"""Closed report contract: model selects IDs; code owns facts and applicability."""
import hashlib
import json
import math
import re
from pathlib import Path

POLICIES = json.loads((Path(__file__).resolve().parent.parent / "data/action_policies.json").read_text())
CAUSES = {
    "undetermined": "원인 미확정. 로그·배포 이력 등의 추가 확인이 필요합니다.",
    "load": "부하 증가 가능성. 모니터링만으로 원인을 확정할 수 없습니다.",
    "memory": "메모리 증가 또는 누수 가능성. 힙 분석 등 추가 확인이 필요합니다.",
    "dependency": "외부 의존성 또는 네트워크 문제 가능성. 호출 경로 확인이 필요합니다.",
}


def evidence_index(collected):
    records = {}
    for item in collected:
        source = item.get("source")
        if source not in ("monitoring", "runbook"):
            continue
        name = item.get("system_name") or (item.get("data", {}).get("system_name") if source == "monitoring" else None)
        if not name:
            continue
        key = item.get("source_id") or ("monitoring:" + name if source == "monitoring" else "runbook:" + name)
        records[key] = {**item, "system_name": name, "source_id": key}
    return records


def action_condition(book, step, monitoring):
    """Only reviewed policies are executable; absent metrics/text drift fail closed."""
    policy = POLICIES.get(book.get("incident_type"))
    if not policy or step != policy["step"]:
        return {"status": "needs_confirmation", "reason": "매뉴얼의 선행 조건·운영자 확인 필요"}
    digest = hashlib.sha256(book["data"].encode()).hexdigest()
    if digest != policy["sha256"]:
        return {"status": "unknown", "reason": "매뉴얼 변경 감지: 조건 정책 재검토 필요"}
    value = monitoring.get(policy["metric"])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return {"status": "unknown", "reason": "필요한 현재 수치가 없어 판단 불가"}
    met = value > policy["threshold"] if policy["operator"] == "gt" else value >= policy["threshold"]
    symbol = ">" if policy["operator"] == "gt" else ">="
    check = f"{policy['metric']} {value}% {symbol} {policy['threshold']}%"
    status = "not_met" if not met else "needs_confirmation" if policy["additional_confirmation"] else "met"
    reason = check + (": 수치 조건 미충족, 해당 조치 권고 보류" if not met else ": 수치 조건 충족")
    if met and policy["additional_confirmation"]:
        reason += ", 지속 여부 미수집이므로 추가 확인 전 조치 보류"
    return {"status": status, "reason": reason}


def book_steps(book):
    return {str(n): body for n, body in re.findall(r"^([0-9]+)\.\s+(.+)$", book["data"], re.MULTILINE)}


def report_contract(collected):
    records = evidence_index(collected)
    systems = []
    for key, mon in records.items():
        if mon["source"] != "monitoring":
            continue
        books = [b for b in records.values() if b["source"] == "runbook" and b["system_name"] == mon["system_name"]]
        systems.append({"system_name": mon["system_name"], "monitoring_source": key,
            "monitoring": mon["data"], "runbooks": [{"source_id": b["source_id"], "steps": book_steps(b),
                "conditions": {step: action_condition(b, int(step), mon["data"]) for step in book_steps(b)}} for b in books]})
    return systems


def writer_prompt(query, collected, feedback=None):
    system = ("장애 보고서의 제안 데이터를 JSON으로만 반환하세요. 자유 문장이나 수치는 생성하지 마세요. "
        "형식: {\"systems\":[{\"system_name\":\"정확한 이름\",\"monitoring_source\":\"제공된 ID\","
        "\"cause\":\"undetermined|load|memory|dependency 중 하나\",\"actions\":[{\"runbook_source\":\"제공된 ID\","
        "\"step\":\"제공된 단계 번호 문자열\",\"condition_status\":\"제공된 조건 status 그대로\"}]}]}. "
        "모든 시스템을 정확히 한 번 포함하고 각 시스템에 제공된 모든 매뉴얼마다 최소 한 단계를 선택하세요. "
        "not_met/unknown/needs_confirmation은 실행 승인이 아닙니다. 수치와 원문은 코드가 출력합니다.")
    user = json.dumps({"query": query, "evidence": report_contract(collected), "feedback": feedback}, ensure_ascii=False)
    return system, user


def validate_proposal(draft, collected, targets):
    issues = []
    try:
        proposal = json.loads(draft)
    except (ValueError, TypeError):
        return None, ["순수 JSON 계약 형식으로 작성하세요."]
    if not isinstance(proposal, dict) or set(proposal) != {"systems"} or not isinstance(proposal["systems"], list):
        return None, ["systems 배열만 포함한 객체가 필요합니다."]
    records = evidence_index(collected)
    names = []
    for row in proposal["systems"]:
        if not isinstance(row, dict) or set(row) != {"system_name", "monitoring_source", "cause", "actions"}:
            issues.append("시스템 필드 계약 위반"); continue
        name = row["system_name"]
        if not isinstance(name, str):
            issues.append("시스템 이름은 문자열이어야 합니다."); continue
        names.append(name)
        mon = records.get(row["monitoring_source"]) if isinstance(row["monitoring_source"], str) else None
        if not mon or mon["source"] != "monitoring" or mon["system_name"] != name:
            issues.append(f"{name}: 모니터링 출처 불일치"); continue
        if not isinstance(row["cause"], str) or row["cause"] not in CAUSES:
            issues.append(f"{name}: 허용되지 않은 원인 코드")
        if not isinstance(row["actions"], list) or not row["actions"]:
            issues.append(f"{name}: 매뉴얼 단계 선택 필요"); continue
        seen = set()
        for action in row["actions"]:
            if not isinstance(action, dict) or set(action) != {"runbook_source", "step", "condition_status"}:
                issues.append(f"{name}: 조치 필드 계약 위반"); continue
            bid, step = action["runbook_source"], action["step"]
            if not isinstance(bid, str) or not isinstance(step, str):
                issues.append(f"{name}: 출처·단계는 문자열이어야 합니다."); continue
            book = records.get(bid)
            if not book or book["source"] != "runbook" or book["system_name"] != name or step not in book_steps(book):
                issues.append(f"{name}: 매뉴얼 출처/단계 불일치"); continue
            if (bid, step) in seen:
                issues.append(f"{name}: 중복 조치")
            seen.add((bid, step))
            status = action_condition(book, int(step), mon["data"])["status"]
            if action["condition_status"] != status:
                issues.append(f"{name}: 조건 판단 불일치({bid} #{step}: {status})")
        required = {key for key, book in records.items() if book["source"] == "runbook" and book["system_name"] == name}
        selected = {bid for bid, _ in seen}
        if required - selected:
            issues.append(f"{name}: 검색된 매뉴얼 누락: " + ", ".join(sorted(required-selected)))
    if sorted(names) != sorted(targets) or len(names) != len(set(names)):
        issues.append("요청 시스템 누락·중복·추가 금지: " + ", ".join(targets))
    return proposal, issues


def render_checked(proposal, collected, passed):
    records = evidence_index(collected)
    lines = ["검증 완료(등록된 출처·대상·조건 코드 확인)" if passed else "검증 미통과: 생성된 제안은 제외하고 확인 가능한 사실만 제공합니다."]
    for c in records.values():
        if c["source"] == "monitoring":
            d = c["data"]
            lines.append(f"1) {c['system_name']} 상태={d.get('status', '미수집')}, CPU {d.get('cpu_percent', '미수집')}%, 메모리 {d.get('memory_percent', '미수집')}% [{c['source_id']}]")
        else:
            lines.append(f"참고 매뉴얼: {c['data'].splitlines()[0]} [{c['source_id']}]")
    if passed:
        for row in proposal["systems"]:
            lines.append(row["system_name"] + ": " + CAUSES[row["cause"]])
            mon = records[row["monitoring_source"]]["data"]
            for a in row["actions"]:
                book = records[a["runbook_source"]]
                condition = action_condition(book, int(a["step"]), mon)
                lines.append(f"2) [{a['runbook_source']} #{a['step']}] {condition['reason']} / 원문 조건 유지: {book_steps(book)[a['step']]}")
    return "\n".join(lines)
