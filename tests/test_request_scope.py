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
