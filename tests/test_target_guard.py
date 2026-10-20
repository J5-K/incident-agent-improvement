import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

from agent.targets import resolve_targets
from agent.graph import build_graph, _extract_system_name
from agent.fake_llm import FastLLM, QualityLLM


class ResolutionTests(unittest.TestCase):
    def test_unknown(self):
        self.assertEqual(resolve_targets("billing-service 응답이 느려요", {"auth-service"}), ([], ["billing-service"]))

    def test_boundary_and_korean_particle(self):
        self.assertEqual(resolve_targets("myauth-service와 auth-service가 느려요", {"auth-service"}), (["auth-service"], ["myauth-service"]))

    def test_order_case_and_duplicates(self):
        self.assertEqual(resolve_targets("CACHE-service, auth-service, cache-service", {"auth-service", "cache-service"}), (["cache-service", "auth-service"], []))

    def test_custom_registered_name(self):
        self.assertEqual(resolve_targets("database가 느려요", {"database"}), (["database"], []))

    def test_greeting_has_no_target(self):
        self.assertEqual(resolve_targets("안녕", {"auth-service"}), ([], []))

    def test_unknown_host_and_underscore(self):
        self.assertEqual(resolve_targets("web-99, auth_service", {"web-01"}), ([], ["web-99", "auth_service"]))

    def test_no_default_in_prompt_target(self):
        for q in ["billing-service 장애", "로그인이 느려요", "auth-service와 billing-service 장애"]:
            with self.assertRaises(ValueError):
                _extract_system_name(q)


class GraphGuardTests(unittest.IsolatedAsyncioTestCase):
    def deps(self):
        return SimpleNamespace(fast_llm=Mock(), quality_llm=Mock(), mcp=Mock())

    async def invoke(self, deps, query):
        return await build_graph(deps).ainvoke({"query": query, "history": [], "events": [], "cost_usd": 0.0})

    async def test_unknown_bypasses_model_and_mcp(self):
        deps = self.deps()
        result = await self.invoke(deps, "billing-service 응답이 느려요")
        self.assertEqual(result["route"], "clarification")
        self.assertIn("billing-service", result["final"])
        self.assertEqual(result["tasks"], [])
        self.assertEqual(deps.fast_llm.mock_calls, [])
        self.assertEqual(deps.mcp.mock_calls, [])

    async def test_mixed_unknown_blocks_partial_investigation(self):
        deps = self.deps()
        result = await self.invoke(deps, "auth-service와 billing-service 장애")
        self.assertEqual(result["route"], "clarification")
        self.assertEqual(deps.mcp.mock_calls, [])

    async def test_missing_target_incident_asks_name(self):
        deps = self.deps()
        deps.fast_llm.route.return_value = ({"route": "incident", "needs_analysis": False}, {"usd": 0.0})
        result = await self.invoke(deps, "로그인이 느려요")
        self.assertEqual(result["route"], "clarification")
        deps.fast_llm.plan.assert_not_called()
        self.assertEqual(deps.mcp.mock_calls, [])

    async def test_greeting_still_chat(self):
        deps = self.deps()
        deps.fast_llm.route.return_value = ({"route": "chat", "needs_analysis": False}, {"usd": 0.0})
        deps.fast_llm.chat = AsyncMock(return_value=("안녕하세요", {"usd": 0.0}))
        result = await self.invoke(deps, "안녕")
        self.assertEqual(result["final"], "안녕하세요")
        self.assertEqual(deps.mcp.mock_calls, [])

    async def test_unknown_stream_delivers_final_and_event(self):
        deps = self.deps(); graph = build_graph(deps)
        chunks = [x async for x in graph.astream({"query": "billing-service 장애", "events": [], "cost_usd": 0.0}, stream_mode=["updates", "custom"])]
        deltas = [d for mode, payload in chunks if mode == "updates" for d in payload.values()]
        self.assertTrue(any(d.get("final") for d in deltas))
        self.assertTrue(any(e["type"] == "TargetClarification" for d in deltas for e in d.get("events", [])))
        self.assertEqual(deps.mcp.mock_calls, [])

    async def test_known_incident_keeps_existing_pipeline(self):
        fast = FastLLM("fake", 0); quality = QualityLLM("fake-quality", 0)
        mcp = SimpleNamespace(
            find_prompt=AsyncMock(return_value="ops"),
            get_prompt=AsyncMock(return_value="표준 진단 절차"),
            call_tool=AsyncMock(side_effect=lambda name, args: {"system_name": args["system_name"], "status": "degraded", "cpu_percent": 91, "alerts": ["CPU high"]}),
            find_resource_template=AsyncMock(return_value=("ops", "runbook://{incident_type}")),
            read_resource=AsyncMock(return_value="# CPU 매뉴얼\n1. CPU 확인\n2. 부하 분산"))
        result = await self.invoke(SimpleNamespace(fast_llm=fast, quality_llm=quality, mcp=mcp), "payments-service CPU 급증")
        self.assertEqual(result["route"], "incident")
        self.assertTrue(result["final"])
        self.assertTrue(mcp.call_tool.called)
        self.assertEqual(mcp.get_prompt.call_args.args[1]["system_name"], "payments-service")


if __name__ == "__main__":
    unittest.main()
