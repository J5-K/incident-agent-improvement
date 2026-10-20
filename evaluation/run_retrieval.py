"""Real local embedding retrieval evaluation; answer LLM is not called."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.runbook_search import RunbookSearch
from agent.nodes.research import _classify_incident_type


def metrics(rows, field):
    tp=fp=fn=0
    for r in rows:
        expected,actual=set(r["expected"]),set(r[field])
        tp+=len(expected&actual);fp+=len(actual-expected);fn+=len(expected-actual)
    empty=[r for r in rows if not r["expected"]]
    return {"exact_set_match":sum(set(r["expected"])==set(r[field]) for r in rows)/len(rows),
            "micro_precision":tp/(tp+fp) if tp+fp else None,"micro_recall":tp/(tp+fn) if tp+fn else None,
            "no_match_accuracy":sum(not r[field] for r in empty)/len(empty) if empty else None}


async def evaluate(args):
    search=RunbookSearch(cache_path=ROOT/"data/.cache/runbooks-index.json")
    fixture=ROOT/"evaluation/rag_cases.json"
    cases=json.loads(fixture.read_text())["cases"]
    started=time.perf_counter();await search.ensure_index();index_seconds=time.perf_counter()-started
    rows=[]
    for case in cases:
        started=time.perf_counter();ranked=await search.rank(case["query"]);elapsed=time.perf_counter()-started
        cutoff=max(search.min_score,ranked[0]["score"]-search.score_margin)
        hits=[h for h in ranked[:search.top_k] if h["score"]>=cutoff]
        started=time.perf_counter()
        old=_classify_incident_type([{"source":"monitoring","data":{"alerts":[case["query"]]}}])
        keyword_seconds=time.perf_counter()-started
        rows.append({**case,"rag":[h["incident_type"] for h in hits],"keyword":[old],
            "ranked":ranked,"embedding_search_seconds":elapsed,"keyword_seconds":keyword_seconds})
    result={"fixture_sha256":hashlib.sha256(fixture.read_bytes()).hexdigest(),"index_manifest":search._manifest,
        "settings":{"min_score":search.min_score,"score_margin":search.score_margin,"top_k":search.top_k,
                    "selected_using":"development only"},"index_ready_seconds":index_seconds,
        "note":"실제 로컬 임베딩 검색 평가. 같은 문장을 기존 키워드 분류기의 alert 문자열로도 입력한다. 운영 모니터링 전체나 답변 LLM 평가가 아님. 소규모 수작업 세트.",
        "summary":{split:{"count":len(rs),"rag":metrics(rs,"rag"),"keyword":metrics(rs,"keyword")} for split in ["development","evaluation"] if (rs:=[r for r in rows if r["split"]==split])},
        "cases":rows}
    output=ROOT/args.output;output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(result["summary"],ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="docs/evaluation/rag_retrieval.json")
    asyncio.run(evaluate(parser.parse_args()))
