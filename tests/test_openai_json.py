import json
import unittest
from unittest.mock import Mock, patch
from agent.openai_llm import OpenAIQualityLLM, OpenAIFastLLM


class OpenAIOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_quality_request_enables_json_and_preserves_contract(self):
        content = json.dumps({"systems": []})
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 10}}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-placeholder"}):
            model = OpenAIQualityLLM("gpt-4o", usd_per_1k_tokens=0)
        with patch("agent.openai_llm.httpx.post", return_value=response) as post:
            result, usage = await model.write("장애 조사", [], 1, None)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["model"], "gpt-4o")
        self.assertEqual(json.loads(result), {"systems": []})
        self.assertEqual(usage["tokens"], 10)
        self.assertIn("JSON", payload["messages"][0]["content"])

    async def test_plain_chat_does_not_enable_json(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "안녕하세요"}}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-placeholder"}):
            model = OpenAIFastLLM("gpt-4o-mini", usd_per_1k_tokens=0)
        with patch("agent.openai_llm.httpx.post", return_value=response) as post:
            text, _ = await model.chat("안녕", [])
        self.assertEqual(text, "안녕하세요")
        self.assertNotIn("response_format", post.call_args.kwargs["json"])
