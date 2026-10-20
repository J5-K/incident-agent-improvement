"""Offline graph comparison. No live MCP, Ollama, keys or random data."""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def rate(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return None if not values else round(sum(values) / len(values), 4)


def summarize(rows):
    return {"case_count": len(rows), "completion_rate": rate(rows, "completed"),
        "route_match_rate": rate(rows, "route_match"), "exact_target_match_rate": rate(rows, "target_match"),
        "per_system_runbook_match_rate": rate(rows, "book_match"),
        "structured_numeric_fact_match_rate": rate(rows, "numeric_match"),
        "structured_numeric_fact_case_count": sum(r.get("numeric_match") is not None for r in rows),
        "unsafe_injected_draft_published_rate": rate(rows, "unsafe_published"),
        "failure_case_error_observed_rate": rate(rows, "expected_failure_observed"),
        "elapsed_seconds_total": round(sum(r["elapsed_seconds"] for r in rows), 3)}


async def execute(fixture, source):
    sys.path.insert(0, str(source))
    from agent.graph import build_graph
    from agent.fake_llm import FastLLM, QualityLLM
    rows = []
    for case in fixture["cases"]:
        data = json.loads(json.dumps(fixture["monitoring"]))
        for name, changes in case.get("overrides", {}).items():
            data[name].update(changes)
        calls, reads = [], []
        async def tool(name, args):
            target = args["system_name"]; calls.append(target)
            if target == case.get("fail_target"):
                raise RuntimeError("fixture monitoring unavailable")
            return dict(data.get(target, {"system_name": target, "status": "unknown", "cpu_percent": 0, "memory_percent": 0, "alerts": ["not found"]}))
        async def read(uri, server):
            kind = uri.rsplit("/", 1)[-1]; reads.append(kind)
            return fixture["runbooks"][kind]
        mcp = SimpleNamespace(find_prompt=AsyncMock(return_value="fixture-ops"), get_prompt=AsyncMock(return_value="고정 표준 절차"),
            call_tool=AsyncMock(side_effect=tool), find_resource_template=AsyncMock(return_value=("fixture-ops", "runbook://incident-response/{incident_type}")),
            read_resource=AsyncMock(side_effect=read))
        quality = QualityLLM("fixed-quality-stub", 0)
        if case.get("injected_draft"):
            quality = SimpleNamespace(write=AsyncMock(return_value=(case["injected_draft"], {"usd":0})))
        deps = SimpleNamespace(fast_llm=FastLLM("fixed-fast-stub", 0), quality_llm=quality, mcp=mcp)
        started = time.perf_counter(); result = {}; error = None
        try:
            result = await build_graph(deps).ainvoke({"query":case["question"], "history":[], "events":[], "cost_usd":0}, {"recursion_limit":60})
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        elapsed = time.perf_counter() - started
        sources = [c for c in result.get("collected", []) if c.get("source") == "runbook"]
        books = {c.get("system_name"): c.get("incident_type") or next((k for k,v in fixture["runbooks"].items() if v == c["data"]), None) for c in sources}
        # Old graphs did not associate runbooks with systems; do not infer false attribution.
        numeric = None
        if result.get("report") and result["report"]["facts"]:
            numeric = all(f[metric] == data[f["system_name"]][metric] for f in result["report"]["facts"] for metric in ("cpu_percent", "memory_percent"))
        rows.append({"id":case["id"], "category":case["category"], "question":case["question"],
            "completed": bool(result.get("final")), "route_match": result.get("route") == case["expected_route"] if not case.get("fail_target") else None,
            "target_match": set(calls) == set(case["expected_targets"]) if not case.get("fail_target") else None,
            "book_match": books == case["expected_books"] if case["expected_books"] and not case.get("fail_target") else None,
            "numeric_match":numeric, "unsafe_published": case["unsafe_marker"] in result.get("final", "") if case.get("unsafe_marker") else None,
            "expected_failure_observed": bool(error) if case.get("fail_target") else None,
            "called_targets":calls, "runbook_reads":reads, "runbooks_by_system":books,
            "elapsed_seconds":round(elapsed, 4), "validation_passed":result.get("validation_passed"),
            "validation_attempts":result.get("validation_attempts", 0), "error":error,
            "reply":result.get("final"), "report":result.get("report"), "events":result.get("events", [])})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refs", nargs="*", default=["2c2973d", "f3836e2", "HEAD"])
    parser.add_argument("--output", default="docs/evaluation")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--fixture", type=Path, default=ROOT / "evaluation/fixtures.json")
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    if args.worker:
        print(json.dumps(asyncio.run(execute(fixture, args.worker)), ensure_ascii=False)); return
    output = (ROOT / args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false", PYTHONDONTWRITEBYTECODE="1")
    results = []
    for ref in args.refs:
        if ref == "WORKTREE":
            commit, source = "uncommitted-working-tree", ROOT
            rows = json.loads(subprocess.check_output([sys.executable, __file__, "--worker", str(source), "--fixture", str(args.fixture)], cwd=ROOT, env=env, text=True, timeout=90))
        else:
            commit = subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
            archive = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=ROOT)
            with tempfile.TemporaryDirectory(prefix="incident-eval-") as folder:
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    tar.extractall(folder, filter="data")
                rows = json.loads(subprocess.check_output([sys.executable, __file__, "--worker", folder, "--fixture", str(args.fixture)], cwd=ROOT, env=env, text=True, timeout=90))
        record = {"ref":ref, "commit":commit, "fixture_sha256":hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
            "mode":"offline deterministic fake/mock graph regression; not live LLM performance", "summary":summarize(rows), "cases":rows}
        results.append(record)
        (output / (ref.replace("/", "_") + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n")
        print(ref, json.dumps(record["summary"], ensure_ascii=False), flush=True)
    (output / "comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2)+"\n")
    with (output / "comparison.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer=csv.DictWriter(f, fieldnames=["ref", "commit", *results[0]["summary"]], lineterminator="\n"); writer.writeheader()
        for r in results: writer.writerow({"ref":r["ref"],"commit":r["commit"],**r["summary"]})
    text = ["# 固定データ評価".replace("固定データ評価", "고정 데이터 자동 평가"), "", "동일 질문·고정 지표·고정 매뉴얼로 그래프를 실행했다. 실제 MCP/LLM/네트워크는 사용하지 않는다.",
        "실행 시간은 fake/mock 및 그래프 내 교육용 지연을 포함하며 실제 모델 지연/서비스 성능 비교가 아니다.",
        "버전별 fake 작성기는 계약 변경에 맞춘 테스트 더블이므로 LLM 정확도 향상 수치로 사용할 수 없다.",
        "원래 기준(2c2973d), 앞선 개선 후(f3836e2), 새 개선 버전을 구분한다.", "",
        "| 버전 | 경로 일치 | 대상 집합 일치 | 시스템별 매뉴얼 일치 | 구조화 수치 일치(대상 수) | 주입 오류 노출 |", "|---|---:|---:|---:|---:|---:|"]
    for r in results:
        s=r["summary"]; number=lambda x: "미측정" if x is None else f"{x:.1%}"
        text.append(f"| {r['ref']} ({r['commit'][:7]}) | {number(s['route_match_rate'])} | {number(s['exact_target_match_rate'])} | {number(s['per_system_runbook_match_rate'])} | {number(s['structured_numeric_fact_match_rate'])} ({s['structured_numeric_fact_case_count']}) | {number(s['unsafe_injected_draft_published_rate'])} |")
    text += ["", "수치 일치는 구조화 보고서의 CPU/메모리 필드와 fixture의 정확한 일치를 확인한다. 기존 자유 문장 버전은 미측정이며 0점으로 처리하지 않는다.",
        "매뉴얼 일치는 시스템별 출처 연결까지 요구한다. 구버전에 연결 정보가 없으면 불일치로 기록한다. 의미 검색 정확도/recall을 측정한 것이 아니다.",
        "13·14는 명백히 틀린 초안을 강제 주입하여 최종 응답에 잘못된 문장이 그대로 노출되는지 확인한다.",
        "15 도구 실패는 현재도 요청 전체가 실패한다. 실패를 관측했다는 의미이며 부분 실패 응답/복구 구현을 뜻하지 않는다.",
        "10개 기본 시나리오 + CPU/메모리 경계 2개 + 오류 초안 2개 + 도구 실패 1개 = 15개. 오류 예외는 결과 JSON에 저장한다.",
        "판정은 시스템 동작의 회귀 검사이며 원인 추정의 진실성·실제 운영 조치 안전성은 평가하지 않는다."]
    (output / "README.md").write_text("\n".join(text)+"\n")


if __name__ == "__main__":
    main()
