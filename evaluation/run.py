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
    print("JSON·CSV 저장 완료. 결과의 commit·fixture SHA를 확인하고 docs/프로젝트_보고서.md의 평가 절을 갱신하세요.")



if __name__ == "__main__":
    main()
