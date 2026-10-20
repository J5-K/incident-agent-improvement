"""모델 계획을 사용자가 명시한 조사 대상과 대조한다."""
def reconcile_monitoring_tasks(tasks: list[dict], targets: list[str]) -> list[dict]:
    """요청 대상은 정확히 한 번씩 조사한다. 모델이 추가한 다른 대상은 제외한다."""
    proposed = {t.get("target"): t for t in tasks
                if t.get("agent") == "research" and t.get("target") in targets}
    monitoring = [dict(proposed.get(target, {}), id=f"task-monitoring-{i}",
                       agent="research", kind="monitoring", target=target, status="pending")
                  for i, target in enumerate(dict.fromkeys(targets))]
    # 현재 파이프라인에서 허용한 나머지 역할은 analysis뿐이다.
    analysis = [dict(t, id=f"task-analysis-{i}", status="pending")
                for i, t in enumerate(tasks) if t.get("agent") == "analysis"]
    return monitoring + analysis
