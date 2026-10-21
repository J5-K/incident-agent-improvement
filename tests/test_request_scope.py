import unittest
from unittest.mock import AsyncMock
from agent.runbook_search import RunbookSearch, request_scope


class RequestScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_compound_searches_symptoms_independently(self):
        retriever = RunbookSearch()
        retriever.search = AsyncMock(side_effect=[
            [{"incident_type": "cpu-spike", "score": .7}],
            [{"incident_type": "memory-leak", "score": .6}],
        ])
        result = await retriever.search_request("payments-service CPU와 메모리 문제", "payments-service", ["payments-service"], {"alerts": ["responding slow"]})
        self.assertEqual({h["incident_type"] for h in result["hits"]}, {"cpu-spike", "memory-leak"})
        self.assertEqual(result["missing"], [])
        self.assertEqual(retriever.search.await_count, 2)

    async def test_tls_exclusions_do_not_search_alerts(self):
        retriever = RunbookSearch()
        retriever.search = AsyncMock()
        result = await retriever.search_request("payments-service TLS 인증서 만료 조사. CPU·메모리·응답 지연 대응은 제외해주세요.", "payments-service", ["payments-service"], {"alerts": ["CPU 91%", "responding slow"]})
        self.assertEqual(result["hits"], [])
        self.assertTrue(result["missing"])
        retriever.search.assert_not_awaited()
        self.assertEqual(set(result["scope"]["excluded"]), {"cpu-spike", "memory-leak", "high-latency"})

    async def test_missing_symptom_is_not_replaced(self):
        retriever = RunbookSearch()
        retriever.search = AsyncMock(return_value=[{"incident_type": "high-latency", "score": .8}])
        result = await retriever.search_request("payments-service 메모리 문제", "payments-service", ["payments-service"], {})
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["missing"], ["memory-leak"])

    async def test_excluded_symptom_filtered_in_generic_search(self):
        retriever = RunbookSearch()
        retriever.search = AsyncMock(return_value=[{"incident_type": "cpu-spike", "score": .8}])
        result = await retriever.search_request("payments-service 상태 조사. CPU는 제외", "payments-service", ["payments-service"], {"alerts": ["CPU 91%"]})
        self.assertEqual(result["hits"], [])
        self.assertNotIn("CPU 91%", retriever.search.call_args.args[0])

    async def test_search_failure_propagates_without_fallback(self):
        retriever = RunbookSearch()
        retriever.search = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            await retriever.search_request("payments-service CPU 문제", "payments-service", ["payments-service"], {})


class SharedPredicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_relational_timeout_reaches_both_systems(self):
        from agent.runbook_search import scoped_query
        q = "gateway-service에서 payments-service로 가는 요청이 타임아웃 나고 있어요."
        targets = ["gateway-service", "payments-service"]
        for name in targets:
            self.assertIn("타임아웃", scoped_query(q, name, targets))

    async def test_generic_status_uses_each_system_alerts_separately(self):
        q = "payments-service와 cache-service 상태를 각각 확인하고 필요한 대응 매뉴얼을 알려주세요."
        targets = ["payments-service", "cache-service"]
        for name, alert, kind in [(targets[0], "CPU 91%", "cpu-spike"), (targets[1], "heap near limit", "memory-leak")]:
            retriever = RunbookSearch()
            retriever.search = AsyncMock(return_value=[{"incident_type": kind, "score": .7}])
            result = await retriever.search_request(q, name, targets, {"alerts": [alert]})
            self.assertEqual(result["scope"]["included"], [kind])
            self.assertEqual(result["hits"][0]["incident_type"], kind)

    async def test_distinct_symptoms_stay_separate(self):
        from agent.runbook_search import scoped_query
        q = "payments-service CPU가 급증하고 cache-service 메모리가 늘어요"
        targets = ["payments-service", "cache-service"]
        self.assertNotIn("메모리", scoped_query(q, targets[0], targets))
        self.assertNotIn("CPU", scoped_query(q, targets[1], targets))
