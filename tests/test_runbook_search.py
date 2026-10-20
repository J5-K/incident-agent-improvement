import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from agent.runbook_search import RunbookSearch, normalize, scoped_query, system_search_query
from agent.graph import build_graph
from agent.evidence import validate_proposal
import test_multi_target as helpers
from test_evidence import evidence, proposal

ROOT=Path(__file__).resolve().parents[1]


class TinyEmbedder:
    """Synthetic vectors test selection/cache only, not language understanding."""
    def __init__(self): self.document_calls=0
    async def identity(self): return "synthetic-v1"
    async def embed(self, texts):
        if texts[0].startswith("title:"): self.document_calls+=1
        return [[1,1,0] if "compound" in t else [1,0,0] if "CPU" in t else [0,1,0] if "MEM" in t else [0,0,1] for t in texts]


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.docs=self.root/"docs";self.docs.mkdir()
        (self.docs/"cpu-spike.md").write_text("# CPU\n1. Check CPU")
        (self.docs/"memory-leak.md").write_text("# MEM\n1. Check memory")
        self.embedder=TinyEmbedder()
        self.search=RunbookSearch(self.embedder,directory=self.docs,cache_path=self.root/"index.json")
    def tearDown(self): self.tmp.cleanup()

    async def test_single_compound_and_no_match(self):
        self.assertEqual([h["incident_type"] for h in await self.search.search("CPU")],["cpu-spike"])
        self.assertEqual({h["incident_type"] for h in await self.search.search("compound")},{"cpu-spike","memory-leak"})
        self.assertEqual(await self.search.search("unrelated"),[])

    async def test_cached_index_reused_and_source_change_rebuilds(self):
        await self.search.search("CPU");await self.search.search("CPU")
        self.assertEqual(self.embedder.document_calls,1)
        other=RunbookSearch(self.embedder,directory=self.docs,cache_path=self.root/"index.json")
        await other.search("CPU");self.assertEqual(self.embedder.document_calls,1)
        (self.docs/"cpu-spike.md").write_text("# CPU\n1. New text")
        await other.search("CPU");self.assertEqual(self.embedder.document_calls,2)

    async def test_concurrent_initialization_embeds_documents_once(self):
        await asyncio.gather(self.search.search("CPU"),self.search.search("MEM"))
        self.assertEqual(self.embedder.document_calls,1)

    async def test_corrupt_cache_rebuilt(self):
        (self.root/"index.json").write_text("invalid")
        self.assertTrue(await self.search.search("CPU"))
        self.assertEqual(self.embedder.document_calls,1)

    async def test_embedding_error_is_not_keyword_fallback(self):
        self.embedder.embed=AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):await self.search.search("CPU")

    async def test_dimension_mismatch_rejected(self):
        await self.search.search("CPU")
        self.embedder.embed=AsyncMock(return_value=[[1,0]])
        with self.assertRaises(ValueError):await self.search.search("CPU")

    async def test_invalid_vectors_rejected(self):
        for vector in [[0,0],[float("nan"),1],[True,1],[],[float("inf")]]:
            with self.subTest(vector=vector):
                with self.assertRaises(ValueError):normalize(vector)

    async def test_empty_system_clause_keeps_only_useful_alert(self):
        text=system_search_query("payments-service와 cache-service 상태 확인", "payments-service",
            ["payments-service","cache-service"], {"status":"degraded","alerts":["CPU 91% (threshold 80%)", "payments-service-3 instance responding slow"]})
        self.assertNotIn("payments-service",text)
        self.assertNotIn("degraded",text)
        self.assertTrue(text.startswith("CPU"))

    async def test_multi_system_query_excludes_other_system_clause(self):
        q="payments-service CPU가 급증하고 cache-service 메모리가 늘어요"
        text=scoped_query(q,helpers.CACHE,[helpers.PAY,helpers.CACHE])
        self.assertNotIn("CPU",text)
        self.assertNotIn(helpers.PAY,text)
        self.assertIn("메모리",text)


def hit(kind):
    body=(ROOT/f"data/runbooks/{kind}.md").read_text()
    return {"document_id":kind,"incident_type":kind,"title":body.splitlines()[0],
            "sha256":hashlib.sha256(body.encode()).hexdigest(),"score":0.75}


class RagGraphTests(unittest.IsolatedAsyncioTestCase):
    def deps(self, responses):
        deps=helpers.MultiTargetTests().deps()
        deps.runbook_search=type("Retriever",(),{"search":AsyncMock(side_effect=responses)})()
        async def read(uri,server):return (ROOT/f"data/runbooks/{uri.rsplit('/',1)[-1]}.md").read_text()
        deps.mcp.find_resource_template=AsyncMock(return_value=("ops","runbook://incident-response/{incident_type}"))
        deps.mcp.read_resource.side_effect=read
        return deps

    async def run_graph(self,deps,query="payments-service CPU와 메모리 장애"):
        return await build_graph(deps).ainvoke({"query":query,"events":[],"cost_usd":0,"history":[]})

    async def test_multiple_runbooks_collected_validated_and_reported(self):
        deps=self.deps([[hit("cpu-spike"),hit("memory-leak")]])
        result=await self.run_graph(deps)
        self.assertTrue(result["validation_passed"])
        self.assertEqual(deps.mcp.read_resource.await_count,2)
        self.assertEqual(len([s for s in result["report"]["sources"] if s["kind"]=="runbook"]),2)
        self.assertEqual(len(result["report"]["condition_checks"]),2)

    async def test_no_match_keeps_facts_without_default_book_or_generation(self):
        deps=self.deps([[]]); result=await self.run_graph(deps)
        self.assertEqual(result["report"]["collection"]["status"],"insufficient")
        self.assertEqual(result["collection_failures"],[])
        self.assertIn("적합한 문서 없음",result["final"])
        self.assertEqual(result["validation_attempts"],0)
        deps.mcp.read_resource.assert_not_awaited()

    async def test_embedding_failure_keeps_monitoring_and_records_search_failure(self):
        deps=self.deps([RuntimeError("embedding offline")]); result=await self.run_graph(deps)
        self.assertEqual(result["collection_failures"][0]["source"],"runbook_search")
        self.assertTrue(result["report"]["facts"])
        deps.mcp.read_resource.assert_not_awaited()

    async def test_one_system_search_failure_does_not_discard_other_book(self):
        deps=self.deps([[hit("cpu-spike")],RuntimeError("offline")])
        result=await self.run_graph(deps,f"{helpers.PAY}와 {helpers.CACHE} 장애 확인")
        self.assertEqual(len(result["report"]["facts"]),2)
        self.assertEqual(deps.mcp.read_resource.await_count,1)
        self.assertIn("CPU 급증",result["final"])
        self.assertEqual(result["report"]["recommendations"],[])

    async def test_index_mcp_source_mismatch_is_partial_failure(self):
        deps=self.deps([[hit("cpu-spike")]])
        deps.mcp.read_resource.side_effect=None;deps.mcp.read_resource.return_value="# stale content"
        result=await self.run_graph(deps)
        self.assertEqual(result["collection_failures"][0]["source"],"runbook")
        self.assertFalse(any(s["kind"]=="runbook" for s in result["report"]["sources"]))

    async def test_proposal_cannot_omit_a_retrieved_book(self):
        items=evidence(); second=evidence("memory-leak")[1]
        second["system_name"]=items[0]["system_name"]
        second["source_id"]="runbook:payments-service:memory-leak"
        _,issues=validate_proposal(json.dumps(proposal(items)),items+[second],[items[0]["system_name"]])
        self.assertIn("검색된 매뉴얼 누락"," ".join(issues))
