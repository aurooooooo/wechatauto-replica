"""千问兼容接口请求格式的无网络测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechatauto.qwen_client import QwenClient


class _FakeClient(QwenClient):
    def __init__(self):
        super().__init__("test-key")
        self.request = None

    def _json_request(self, url, method="GET", payload=None):
        self.request = (url, method, payload)
        return {"choices": [{"message": {"content": "识别结果"}}]}


class QwenClientTest(unittest.TestCase):
    def test_transcribe_uses_base64_data_uri(self):
        client = _FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            path.write_bytes(b"RIFFtest")
            result = client.transcribe_file(str(path), "audio/wav")

        self.assertEqual(result, "识别结果")
        url, method, payload = client.request
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(method, "POST")
        self.assertEqual(payload["model"], "qwen3-asr-flash")
        data = payload["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(data.startswith("data:audio/wav;base64,"))

    def test_interpret_requires_known_json_intent(self):
        client = _FakeClient()
        client._json_request = lambda *args, **kwargs: {
            "choices": [{"message": {"content": '{"intent":"list"}'}}],
        }
        self.assertEqual(
            client.interpret("今天有什么待办", "2026-09-09T10:00:00+08:00")["intent"],
            "list",
        )

    def test_interpret_normalizes_single_semantic_todo(self):
        client = _FakeClient()
        client._json_request = lambda *args, **kwargs: {
            "choices": [{"message": {"content": (
                '{"intent":"create","title":"开会",'
                '"date_text":"明天","time_text":"下午三点",'
                '"remind_text":"提前30分钟"}'
            )}}],
        }
        result = client.interpret("今晚八点开会", "2026-09-09T10:00:00+08:00")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "开会")

    def test_interpret_prompt_requires_raw_time_semantics(self):
        client = _FakeClient()
        captured = {}

        def fake_request(*args, **kwargs):
            captured.update(kwargs["payload"])
            return {"choices": [{"message": {"content": '{"intent":"chat"}'}}]}

        client._json_request = fake_request
        client.interpret("等会儿三点半提醒我", "2026-09-10T14:00:00+08:00")
        instruction = captured["messages"][0]["content"]
        self.assertIn("date_text", instruction)
        self.assertIn("time_text", instruction)
        self.assertIn("remind_text", instruction)
        self.assertIn("target_text", instruction)
        self.assertIn("提醒李工去铺线", instruction)
        self.assertIn("不得返回 event_at、remind_at", instruction)
        self.assertNotIn("create_failed", instruction)

    def test_json_request_retries_one_timeout(self):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        client = QwenClient("test-key")
        with patch("wechatauto.qwen_client.urlopen", side_effect=[TimeoutError(), _Response()]) as mocked:
            self.assertTrue(client._json_request("https://example.test")["ok"])
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
