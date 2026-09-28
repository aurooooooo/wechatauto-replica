"""千问兼容接口请求格式的无网络测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechatauto.qwen_client import (
    PROMPT_FILE, QwenBillingError, QwenClient, render_prompt,
)


class _FakeClient(QwenClient):
    def __init__(self):
        super().__init__("test-key")
        self.request = None

    def _json_request(self, url, method="GET", payload=None):
        self.request = (url, method, payload)
        return {"choices": [{"message": {"content": "识别结果"}}]}


class QwenClientTest(unittest.TestCase):
    def test_prompts_are_loaded_from_central_file(self):
        self.assertTrue(PROMPT_FILE.is_file())
        rendered = render_prompt(
            "todo_interpret", now_iso="2026-09-12T10:00:00+08:00", text="测试消息",
        )
        self.assertIn("当前北京时间：2026-09-12T10:00:00+08:00", rendered)
        self.assertIn("用户消息：测试消息", rendered)

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

    def test_interpret_accepts_help_intent(self):
        client = _FakeClient()
        client._json_request = lambda *args, **kwargs: {
            "choices": [{"message": {"content": '{"intent":"help"}'}}],
        }
        self.assertEqual(
            client.interpret("你能做什么", "2026-09-09T10:00:00+08:00")["intent"],
            "help",
        )

    def test_interpret_normalizes_single_semantic_todo(self):
        client = _FakeClient()
        client._json_request = lambda *args, **kwargs: {
            "choices": [{"message": {"content": (
                '{"intent":"create","title":"开会",'
                '"date_text":"明天","time_text":"下午三点",'
                '"remind_text":"提前30分钟",'
                '"date_semantic":{"kind":"relative_days","value":1},'
                '"time_semantic":{"kind":"clock","hour":3,"minute":0,'
                '"day_period":"afternoon","is_24_hour":false,"near_future":false},'
                '"reminder_semantic":{"kind":"before_event","value":30,'
                '"unit":"minutes"}}'
            )}}],
        }
        result = client.interpret("今晚八点开会", "2026-09-09T10:00:00+08:00")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "开会")
        self.assertEqual(
            result["items"][0]["date_semantic"],
            {"kind": "relative_days", "value": 1},
        )

    def test_interpret_prompt_requires_structured_time_semantics(self):
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
        self.assertIn("date_semantic", instruction)
        self.assertIn("time_semantic", instruction)
        self.assertIn("reminder_semantic", instruction)
        self.assertIn("N个月后", instruction)
        self.assertIn("等会儿三点半", instruction)
        self.assertIn("十一点半", instruction)
        self.assertIn("最近未来11:30/23:30", instruction)
        self.assertIn("下午两点半", instruction)
        self.assertIn("按当前北京时间选择最近的未来时间", instruction)
        self.assertIn("before_event(value=30,unit=minutes)", instruction)
        self.assertIn("三个semantic字段始终为对象", instruction)
        self.assertIn("删除编号12的待办", instruction)
        self.assertIn("提醒李工去", instruction)
        self.assertIn("不返回event_at/remind_at", instruction)
        self.assertNotIn("create_failed", instruction)

    def test_help_prompt_is_fixed_content(self):
        help_text = render_prompt("todo_help")
        self.assertIn("我能帮你做这些事", help_text)
        self.assertIn("删除编号12的待办", help_text)
        self.assertIn("提前30分钟", help_text)

    def test_billing_errors_are_recognized(self):
        self.assertTrue(QwenClient._is_billing_error({"code": "Arrearage"}))
        self.assertTrue(QwenClient._is_billing_error("余额不足，请充值"))
        self.assertFalse(QwenClient._is_billing_error("请求超时"))
        self.assertTrue(issubclass(QwenBillingError, RuntimeError))

    def test_billing_prompt_contains_recharge_url(self):
        message = render_prompt("qwen_billing_error")
        self.assertIn("https://platform.qianwenai.com/home", message)

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
