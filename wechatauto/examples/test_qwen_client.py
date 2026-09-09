"""千问兼容接口请求格式的无网络测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
