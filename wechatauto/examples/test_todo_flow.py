"""待办命令处理的无外部服务测试。"""

from __future__ import annotations

import unittest

from wechatauto.ai_worker import AIWorker, SHANGHAI, _parse_datetime


class _Storage:
    def __init__(self):
        self.owner = None

    def create_todo(self, message, title, event_at, event_all_day, remind_at):
        self.owner = message["sender_id"]
        return {
            "id": 12, "title": title, "event_at": event_at,
            "event_all_day": event_all_day, "remind_at": remind_at,
        }

    def list_todos(self, account_id, creator_id, *args):
        self.owner = creator_id
        return []


class TodoFlowTest(unittest.TestCase):
    def setUp(self):
        self.worker = AIWorker.__new__(AIWorker)
        self.worker.account_id = "self_wxid"
        self.worker.storage = _Storage()
        self.row = {
            "id": 1, "account_id": "self_wxid", "sender_id": "user_wxid",
            "sender_name": "张三", "session_id": "room@chatroom",
            "session_name": "项目群", "session_type": "group",
        }

    def test_create_returns_structured_overview_for_creator(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "title": "和客户甲开会",
            "event_at": "2026-09-11T19:30:00+08:00",
            "event_all_day": False,
            "remind_at": "2026-09-11T19:10:00+08:00",
        }, "")
        self.assertEqual(self.worker.storage.owner, "user_wxid")
        self.assertIn("#12", reply)
        self.assertIn("2026-09-11 19:10", reply)

    def test_list_is_scoped_to_sender(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "list", "range_start": "2026-09-09",
            "range_end": "2026-09-10", "keyword": None,
        }, "")
        self.assertEqual(self.worker.storage.owner, "user_wxid")
        self.assertIn("没有待办", reply)

    def test_naive_model_time_is_beijing_time(self):
        parsed = _parse_datetime("2026-09-11T19:30:00")
        self.assertEqual(parsed.tzinfo, SHANGHAI)


if __name__ == "__main__":
    unittest.main()
