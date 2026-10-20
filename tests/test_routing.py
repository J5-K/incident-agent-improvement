import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from agent.routing import should_promote_incident
from agent.graph import route_node


class RoutingRulesTests(unittest.TestCase):
    def test_daily_symptoms(self):
        for text in ["프로세서가 계속 바쁘고 처리가 밀려요", "응답이 느려요", "메모리를 더 먹고 있어요", "접속이 안 돼요", "디스크가 가득 찼어요", "상태를 확인해주세요"]:
            with self.subTest(text=text):
                self.assertTrue(should_promote_incident("payments-service " + text, ["payments-service"]))

    def test_negative_controls(self):
        for text in ["안녕", "payments-service 이름 멋지네", "payments-service 타임아웃이 무슨 뜻이야?", "payments-service 메모리 증가 개념 설명"]:
            with self.subTest(text=text):
                self.assertFalse(should_promote_incident(text, ["payments-service"]))
        self.assertFalse(should_promote_incident("응답이 느려요", []))


class RoutingNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_misclassification_promoted_and_recorded(self):
        fast = Mock()
        fast.route.return_value = ({"route": "chat", "needs_analysis": False}, {"usd": 0.1})
        result = await route_node({"query": "payments-service 처리가 밀려요", "registered_targets": ["payments-service"], "cost_usd": 0.2}, SimpleNamespace(fast_llm=fast))
        self.assertEqual(result["route"], "incident")
        self.assertEqual(result["events"][0]["original_route"], "chat")
        self.assertEqual(result["cost_usd"], 0.3)

    async def test_model_incident_is_preserved(self):
        fast = Mock()
        fast.route.return_value = ({"route": "incident", "needs_analysis": True}, {"usd": 0})
        result = await route_node({"query": "장애", "registered_targets": []}, SimpleNamespace(fast_llm=fast))
        self.assertEqual(result["route"], "incident")
        self.assertTrue(result["needs_analysis"])
