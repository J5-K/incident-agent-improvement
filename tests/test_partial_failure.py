import asyncio
import unittest
from unittest.mock import AsyncMock
from agent.graph import build_graph
from agent.nodes.research import research_node
import test_multi_target as helpers
PAY, CACHE = helpers.PAY, helpers.CACHE


class PartialFailureTests(unittest.IsolatedAsyncioTestCase):
    def deps(self):
        return helpers.MultiTargetTests().deps()

    async def run_graph(self, deps):
        return await build_graph(deps).ainvoke({"query": f"{PAY}와 {CACHE} 장애 확인", "history": [], "events": [], "cost_usd": 0})

    async def test_one_monitor_fails_other_facts_and_runbook_survive(self):
        deps = self.deps(); original = deps.mcp.call_tool.side_effect
        async def tool(name, args):
            if args["system_name"] == CACHE:
                raise RuntimeError("secret-detail-that-must-not-leak")
            return await original(name, args)
        deps.mcp.call_tool.side_effect = tool
        deps.quality_llm = type("Quality", (), {"write": AsyncMock()})()
        result = await self.run_graph(deps)
        self.assertEqual(result["report"]["collection"]["status"], "partial")
        self.assertEqual([f["system_name"] for f in result["report"]["facts"]], [PAY])
        self.assertIn("CPU 매뉴얼", result["final"])
        self.assertIn(CACHE, result["final"])
        self.assertNotIn("secret-detail", result["final"])
        self.assertEqual(result["report"]["recommendations"], [])
        deps.quality_llm.write.assert_not_awaited()
        self.assertEqual(result["validation_attempts"], 0)
        self.assertEqual([t["status"] for t in result["tasks"] if t["kind"] == "monitoring"], ["done", "failed"])

    async def test_one_runbook_fails_all_monitoring_and_other_book_survive(self):
        deps = self.deps(); original = deps.mcp.read_resource.side_effect
        async def read(uri, server):
            if "memory-leak" in uri:
                raise ValueError("unavailable")
            return await original(uri, server)
        deps.mcp.read_resource.side_effect = read
        result = await self.run_graph(deps)
        self.assertEqual(len(result["report"]["facts"]), 2)
        self.assertEqual(len([c for c in result["collected"] if c["source"] == "runbook"]), 1)
        self.assertEqual(result["collection_failures"][0]["source"], "runbook")
        self.assertEqual(deps.mcp.read_resource.await_count, 2)
        self.assertIn("조회 실패 대상", result["final"])

    async def test_all_monitoring_fail_returns_explicit_no_data_report(self):
        deps = self.deps(); deps.mcp.call_tool.side_effect = RuntimeError("down")
        result = await self.run_graph(deps)
        self.assertEqual(result["report"]["collection"]["status"], "failed")
        self.assertEqual(result["report"]["facts"], [])
        self.assertEqual(result["report"]["sources"], [])
        self.assertIn("정상 상태로 판단하지 않습니다", result["final"])
        deps.mcp.read_resource.assert_not_awaited()
        self.assertEqual(result["replan_rounds"] if "replan_rounds" in result else 0, 0)

    async def test_resource_discovery_failure_preserves_prior_metrics(self):
        deps = self.deps(); deps.mcp.find_resource_template.side_effect = RuntimeError("discovery")
        result = await self.run_graph(deps)
        self.assertEqual(len(result["report"]["facts"]), 2)
        self.assertEqual(len(result["collection_failures"]), 2)
        self.assertTrue(all(f["source"] == "runbook" for f in result["collection_failures"]))
        deps.mcp.read_resource.assert_not_awaited()

    async def test_transient_timeout_recovers_without_partial_flag(self):
        deps = self.deps(); original = deps.mcp.call_tool.side_effect; first = True
        async def tool(name, args):
            nonlocal first
            if first:
                first = False
                raise asyncio.TimeoutError()
            return await original(name, args)
        deps.mcp.call_tool.side_effect = tool
        result = await self.run_graph(deps)
        self.assertEqual(result["collection_failures"], [])
        self.assertTrue(result["validation_passed"])
        self.assertEqual(result["report"]["collection"]["status"], "complete")
        self.assertEqual(result["retry_count"], 1)

    async def test_exhausted_timeout_becomes_failed_task(self):
        deps = self.deps(); original = deps.mcp.call_tool.side_effect
        async def tool(name, args):
            if args["system_name"] == CACHE:
                raise asyncio.TimeoutError()
            return await original(name, args)
        deps.mcp.call_tool.side_effect = tool
        result = await self.run_graph(deps)
        self.assertEqual(result["collection_failures"][0]["error_type"], "TimeoutError")
        self.assertEqual(result["report"]["collection"]["status"], "partial")

    async def test_task_cancellation_is_not_converted_to_tool_failure(self):
        deps = self.deps(); deps.mcp.call_tool.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await research_node({"tasks": [{"id": "cancel", "agent": "research", "kind": "monitoring", "target": PAY, "status": "pending"}], "events": []}, deps)

    async def test_stream_contains_failures_success_and_final_report(self):
        deps = self.deps(); original = deps.mcp.call_tool.side_effect
        async def tool(name, args):
            if args["system_name"] == CACHE: raise RuntimeError("down")
            return await original(name, args)
        deps.mcp.call_tool.side_effect = tool
        events=[]; final=None
        async for mode,payload in build_graph(deps).astream({"query":f"{PAY}와 {CACHE} 장애", "events":[], "cost_usd":0}, stream_mode=["updates", "custom"]):
            if mode=="updates":
                for delta in payload.values():
                    events.extend(delta.get("events", []))
                    if delta.get("final"): final=delta["final"]
        self.assertTrue(any(e["type"]=="TaskFailed" for e in events))
        self.assertTrue(any(e["type"]=="TaskCompleted" for e in events))
        self.assertIn("확보된 사실만 제공", final)
