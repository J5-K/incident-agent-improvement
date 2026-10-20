import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from agent.planning import reconcile_monitoring_tasks
from agent.graph import build_graph, replan_check_node
from agent.fake_llm import QualityLLM

PAY = "payments-service"
CACHE = "cache-service"


class PlanningTests(unittest.TestCase):
    def test_partial_plan_recovers_missing_and_removes_extra(self):
        tasks = [{"agent": "research", "target": PAY},
                 {"agent": "research", "target": PAY},
                 {"agent": "research", "target": "auth-service"}]
        result = reconcile_monitoring_tasks(tasks, [PAY, CACHE])
        self.assertEqual([t["target"] for t in result], [PAY, CACHE])
        self.assertEqual(len({t["id"] for t in result}), 2)

    def test_empty_plan_and_duplicate_mentions(self):
        self.assertEqual([t["target"] for t in reconcile_monitoring_tasks([], [PAY, PAY, CACHE])], [PAY, CACHE])

    def test_analysis_retained(self):
        tasks = reconcile_monitoring_tasks([{"agent": "analysis", "kind": "pattern-analysis"}], [PAY])
        self.assertEqual([t["agent"] for t in tasks], ["research", "analysis"])


class MultiTargetTests(unittest.IsolatedAsyncioTestCase):
    def deps(self, *, targets=(PAY,), same_type=False, omit_answer=False):
        fast = SimpleNamespace(
            route=Mock(return_value=({"route": "incident", "needs_analysis": False}, {"usd": 0})),
            plan=Mock(return_value=({"needs_analysis": False, "tasks": [
                {"agent": "research", "target": name} for name in targets]}, {"usd": 0})))
        async def monitoring(tool, args):
            name = args["system_name"]
            return {"system_name": name, "status": "degraded", "cpu_percent": 91,
                    "alerts": ["CPU high" if name == PAY or same_type else "memory climbing"]}
        async def read(uri, server):
            return "# " + ("CPU 매뉴얼" if "cpu-spike" in uri else "메모리 매뉴얼") + "\n1. 상태 확인\n2. 해당 원인 점검"
        mcp = SimpleNamespace(find_prompt=AsyncMock(return_value="ops"),
            get_prompt=AsyncMock(return_value="표준 절차"), call_tool=AsyncMock(side_effect=monitoring),
            find_resource_template=AsyncMock(return_value=("ops", "runbook://{incident_type}")),
            read_resource=AsyncMock(side_effect=read))
        quality = SimpleNamespace(write=AsyncMock(return_value=("1) payments-service 상태 확인 및 CPU 대응 절차를 안내합니다. 추가 원인도 점검하세요.", {"usd": 0}))) if omit_answer else QualityLLM("fake-quality", 0)
        return SimpleNamespace(fast_llm=fast, quality_llm=quality, mcp=mcp)

    async def run_graph(self, deps, query=None):
        return await build_graph(deps).ainvoke({"query": query or f"{PAY}와 {CACHE} 상태를 각각 확인하고 필요한 대응 매뉴얼을 알려주세요.",
            "events": [], "cost_usd": 0, "history": []})

    async def test_partial_model_plan_investigates_both_with_separate_runbooks(self):
        deps = self.deps()
        result = await self.run_graph(deps)
        self.assertEqual([c.args[1]["system_name"] for c in deps.mcp.call_tool.call_args_list], [PAY, CACHE])
        books = {c["system_name"]: c["data"] for c in result["collected"] if c["source"] == "runbook"}
        self.assertIn("CPU 매뉴얼", books[PAY]); self.assertIn("메모리 매뉴얼", books[CACHE])
        self.assertIn(PAY, result["final"]); self.assertIn(CACHE, result["final"])
        self.assertIn("CPU 매뉴얼", result["final"]); self.assertIn("메모리 매뉴얼", result["final"])
        self.assertEqual(result["replan_rounds"], 1)
        self.assertEqual(result["events"][-1]["type"], "Finalize")

    async def test_same_incident_keeps_system_associations(self):
        result = await self.run_graph(self.deps(same_type=True))
        books = [c for c in result["collected"] if c["source"] == "runbook"]
        self.assertEqual({c["system_name"] for c in books}, {PAY, CACHE})
        self.assertTrue(all("CPU 매뉴얼" in c["data"] for c in books))

    async def test_unrequested_model_target_not_called(self):
        deps = self.deps(targets=(PAY, "auth-service"))
        await self.run_graph(deps)
        self.assertEqual({c.args[1]["system_name"] for c in deps.mcp.call_tool.call_args_list}, {PAY, CACHE})

    async def test_single_system_remains_single(self):
        deps = self.deps()
        result = await self.run_graph(deps, PAY + " CPU 급증")
        self.assertEqual(deps.mcp.call_tool.await_count, 1)
        self.assertEqual(len([c for c in result["collected"] if c["source"] == "runbook"]), 1)

    async def test_persistent_answer_omission_gets_grounded_supplement(self):
        deps = self.deps(omit_answer=True)
        result = await self.run_graph(deps)
        self.assertEqual(deps.quality_llm.write.await_count, 3)
        self.assertIn("[누락 대상 근거 보충: cache-service]", result["final"])
        self.assertIn("메모리 매뉴얼", result["final"])
        self.assertIn("AnswerCoverageSupplemented", [e["type"] for e in result["events"]])

    async def test_existing_one_runbook_does_not_cover_other_system(self):
        state = {"registered_targets": [PAY, CACHE], "tasks": [], "events": [], "collected": [
            {"source": "runbook", "system_name": PAY, "data": "CPU 매뉴얼"},
            {"source": "monitoring", "data": {"system_name": CACHE, "status": "degraded", "alerts": ["memory high"]}}]}
        result = await replan_check_node(state, self.deps())
        self.assertEqual(result["tasks"][0]["system_name"], CACHE)
        self.assertEqual(result["tasks"][0]["target"], "memory-leak")

    async def test_missing_monitoring_is_not_claimed_sufficient(self):
        with self.assertRaisesRegex(RuntimeError, "모니터링 수집 누락"):
            await replan_check_node({"registered_targets": [CACHE], "tasks": [], "events": [], "collected": []}, self.deps())

    async def test_stream_keeps_both_system_runbook_events(self):
        chunks = [x async for x in build_graph(self.deps()).astream({"query": f"{PAY}와 {CACHE} 장애", "events": [], "cost_usd": 0}, stream_mode=["updates", "custom"])]
        events = [e for mode, payload in chunks if mode == "updates" for delta in payload.values() for e in delta.get("events", [])]
        books = {e["system_name"] for e in events if e["type"] == "TaskCompleted" and e["source"] == "runbook"}
        self.assertEqual(books, {PAY, CACHE})


if __name__ == "__main__": unittest.main()
