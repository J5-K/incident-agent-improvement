"""Render source-backed facts, tentative causes and conditional recommendations."""
import hashlib
from .evidence import CAUSES, POLICIES, action_condition, book_steps, evidence_index

LABELS = {"met": "수치 조건 충족 / 실행 승인 아님", "not_met": "조건 미충족 / 조치 권고 보류",
          "unknown": "판단 불가 / 조치 권고 보류", "needs_confirmation": "추가 확인 필요 / 자동 실행 불가"}


def build_report(proposal, collected, passed, attempts=0, issues=None, *, failures=None, targets=None):
    records = evidence_index(collected)
    report = {"schema_version": 1, "verification": {"passed": passed, "attempts": attempts,
        "issues": issues or [], "scope": "시스템·출처·단계·검토된 수치 조건의 일치 확인. 원인 확정 검증 아님"},
        "facts": [], "hypotheses": [], "recommendations": [], "condition_checks": [], "sources": []}
    for source in records.values():
        ref = {"id": source["source_id"], "system_name": source["system_name"], "kind": source["source"]}
        if source["source"] == "monitoring":
            data = source["data"]
            report["facts"].append({"system_name": source["system_name"], "status": data.get("status", "미수집"),
                "cpu_percent": data.get("cpu_percent"), "memory_percent": data.get("memory_percent"),
                "alerts": data.get("alerts", []), "source_id": source["source_id"]})
            ref["tool"] = source.get("tool", "get_monitoring_status")
        else:
            ref.update(title=source["data"].splitlines()[0] if source["data"].splitlines() else "빈 매뉴얼",
                uri=source.get("uri", "미기록"), server=source.get("server", "미기록"),
                sha256=hashlib.sha256(source["data"].encode()).hexdigest())
            policy = POLICIES.get(source.get("incident_type"))
            if policy:
                mon = next((r["data"] for r in records.values() if r["source"] == "monitoring" and r["system_name"] == source["system_name"]), {})
                report["condition_checks"].append({"system_name": source["system_name"], "runbook_source": source["source_id"],
                    "step": str(policy["step"]), **action_condition(source, policy["step"], mon)})
        report["sources"].append(ref)
    failures = failures or []
    observed = {f["system_name"] for f in report["facts"]}
    report["collection"] = {"status": ("partial" if observed else "failed") if failures else "complete",
        "requested_targets": targets or sorted(observed), "failed_tasks": failures,
        "limitation": "실패 대상·매뉴얼을 포함한 원인 추정과 조치 제안은 제공하지 않습니다." if failures else None}
    if passed:
        for row in proposal["systems"]:
            report["hypotheses"].append({"system_name": row["system_name"], "text": CAUSES[row["cause"]],
                "source_id": row["monitoring_source"], "confirmed": False})
            mon = records[row["monitoring_source"]]["data"]
            for action in row["actions"]:
                book = records[action["runbook_source"]]
                report["recommendations"].append({"system_name": row["system_name"],
                    "source_id": action["runbook_source"], "step": action["step"],
                    "original_text": book_steps(book)[action["step"]],
                    **action_condition(book, int(action["step"]), mon)})
    return report


def metric(value):
    import math
    return str(value) + "%" if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else "미수집"


def render_report(report):
    v = report["verification"]
    collection = report.get("collection", {"status": "complete", "failed_tasks": []})
    lines = ["[출처·조건 검증 완료]" if v["passed"] else "[검증 미통과 — 확인된 사실만 제공]",
             f"작성 시도 {v['attempts']}회 / {v['scope']}", "", "1) 확인된 사실"]
    if collection["status"] != "complete":
        lines[0] = "[조사 일부 실패 — 확보된 사실만 제공]" if collection["status"] == "partial" else "[조사 실패 — 확보된 모니터링 정보 없음]"
        lines[1] = "원인·조치 제안 생성 및 검증 미수행 / 조회 실패 후 결과 보존"
    if not report["facts"]:
        lines.append("- 확보된 모니터링 정보가 없습니다. 정상 상태로 판단하지 않습니다.")
    for fact in report["facts"]:
        lines.append(f"- {fact['system_name']}: 상태={fact['status']}, CPU {metric(fact['cpu_percent'])}, 메모리 {metric(fact['memory_percent'])} [{fact['source_id']}]")
        lines.append("  원본 경고(현재 수치와 별개이며 시점 미확인): " + ("; ".join(fact["alerts"]) or "없음"))
    if v["passed"]:
        lines += ["", "2) 원인 추정 — 확정 사실 아님"]
        for h in report["hypotheses"]:
            lines.append(f"- {h['system_name']}: {h['text']} [{h['source_id']}]")
        lines += ["", "3) 권장 점검·조건부 조치 — 실행은 운영자 판단"]
        for a in report["recommendations"]:
            lines.append(f"- {a['system_name']} [{a['source_id']} #{a['step']}]: {LABELS[a['status']]} / {a['reason']}")
            lines.append("  참고 원문(즉시 실행 지시 아님): " + a["original_text"])
        if report["condition_checks"]:
            lines += ["", "수치 조건 대조(선택 단계와 별도로 표시)"]
            for check in report["condition_checks"]:
                lines.append(f"- {check['system_name']}: {LABELS[check['status']]} / {check['reason']} [{check['runbook_source']} #{check['step']}]")
    else:
        lines += ["", "원인 추정·권고안은 검증되지 않아 제공하지 않습니다.", "검증 사유: " + " / ".join(v["issues"])]
    if collection["failed_tasks"]:
        lines += ["", "조회 실패 대상 및 분석 한계"]
        for failure in collection["failed_tasks"]:
            lines.append(f"- {failure['system_name']}: {failure['source']} / {failure['target']} 조회 실패 ({failure['error_type']}). {failure['reason']}")
        lines.append(collection["limitation"])
        lines.append("성공한 조회 결과는 보존했습니다. 재요청 전 실패한 도구·연결 상태를 확인하세요.")
    lines += ["", "4) 참고 출처"]
    for s in report["sources"]:
        if s["kind"] == "monitoring":
            lines.append(f"- [{s['id']}] MCP Tool {s['tool']} / 대상 {s['system_name']}")
        else:
            lines.append(f"- [{s['id']}] {s['title']} / MCP Resource {s['uri']} / 서버 {s['server']}")
    lines += ["", "추가 로그·배포 이력·시계열 없이 원인을 확정할 수 없습니다. 모니터링은 교육용 샘플 데이터입니다."]
    return "\n".join(lines)
