import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import server


class StreamErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_error_sends_terminal_event_without_raw_exception(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("private internal error")
            yield
        deps = SimpleNamespace(version="v2", session_store=Mock())
        deps.session_store.load.return_value = []
        with patch.dict(server.app.state._state, {"deps": deps, "graph": SimpleNamespace(astream=broken)}):
            response = await server.chat_stream("질문", "test")
            with self.assertLogs(level="ERROR"):
                body = "".join([chunk async for chunk in response.body_iterator])
        self.assertIn("StreamError", body)
        self.assertIn("[DONE]", body)
        self.assertNotIn("private internal error", body)
        deps.session_store.append.assert_not_called()
