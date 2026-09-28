"""待办命令处理的无外部服务测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from wechatauto.ai_worker import (
    AIWorker, SHANGHAI, _format_reminder, _parse_datetime,
)
from wechatauto.send_text import resolve_group_member
from wechatauto.time_resolver import TimeResolver


def _clock(hour, minute=0, period=None):
    return {
        "kind": "clock", "hour": hour, "minute": minute,
        "day_period": period, "is_24_hour": False,
    }


def _todo(title, date_semantic, time_semantic, reminder_semantic=None, target=None):
    return {
        "title": title,
        "date_semantic": date_semantic,
        "time_semantic": time_semantic,
        "reminder_semantic": reminder_semantic or {"kind": "at_event"},
        "target_text": target,
    }


class _Storage:
    def __init__(self):
        self.owner = None
        self.created_items = None
        self.create_result = None
        self.clarification = None
        self.delete_args = None

    def create_todos(self, message, items):
        self.owner = message["sender_id"]
        self.created_items = items
        if self.create_result is not None:
            return self.create_result
        return {
            "created": [{
                "id": 12 + index, "title": item["title"],
                "event_at": item["event_at"],
                "event_all_day": item["event_all_day"],
                "remind_at": item["remind_at"],
                "session_type": message["session_type"],
                "origin_session_name": message["session_name"],
                "reminder_target_id": item["reminder_target_id"],
                "reminder_target_name": item["reminder_target_name"],
            } for index, item in enumerate(items)],
            "updated": [], "conflicts": [],
        }

    def list_todos(self, account_id, creator_id, *args):
        self.owner = creator_id
        return []

    def prepare_todo_delete(self, account_id, creator_id, session_id, *args):
        self.delete_args = args
        todo_id = args[-1]
        if todo_id != 12:
            return []
        return [{
            "id": 12, "title": "和客户甲开会",
            "event_at": _parse_datetime("2026-09-11T19:30:00+08:00"),
            "event_all_day": False,
            "remind_at": _parse_datetime("2026-09-11T19:10:00+08:00"),
        }]

    def confirm_todo_replace(self, account_id, creator_id, session_id):
        return {"status": "replaced", "todos": [{
            "id": 14, "title": "打游戏",
            "event_at": _parse_datetime("2026-09-09T22:00:00+08:00"),
            "event_all_day": False,
            "remind_at": _parse_datetime("2026-09-09T21:55:00+08:00"),
        }]}

    def cancel_todo_replace(self, account_id, creator_id, session_id):
        return True

    def save_todo_clarification(self, message, context, question):
        self.clarification = {"context": context, "question": question}


class _WeChatDB:
    def get_group_members(self, group_id):
        assert group_id == "room@chatroom"
        return [
            {"username": "user_wxid", "nick_name": "张三", "remark": ""},
            {"username": "worker_wxid", "nick_name": "恸。", "remark": "李工"},
        ]

class TodoFlowTest(unittest.TestCase):
    def setUp(self):
        self.worker = AIWorker.__new__(AIWorker)
        self.worker.account_id = "self_wxid"
        self.worker.storage = _Storage()
        self.worker.db_dir = r"D:\xwechat_files"
        self.worker.log = lambda *args: None
        self.worker.wechat_db = _WeChatDB()
        self.worker.time_resolver = TimeResolver()
        self.now = _parse_datetime("2026-09-10T14:00:00+08:00")
        self.row = {
            "id": 1, "account_id": "self_wxid", "sender_id": "user_wxid",
            "sender_name": "张三", "session_id": "room@chatroom",
            "session_name": "项目群", "session_type": "group",
        }

    def test_help_returns_fixed_content(self):
        reply = self.worker._handle_command(
            self.row, {"intent": "help"}, "",
        )
        self.assertIn("我能帮你做这些事", reply)
        self.assertIn("同一个提醒对象在同一时间", reply)
        self.assertIn("普通问题可以直接询问我", reply)

    def test_create_returns_structured_overview_for_creator(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "和客户甲开会", {"kind": "relative_days", "value": 1},
                _clock(7, 30, "evening"),
                {"kind": "before_event", "value": 20, "unit": "minutes"},
            )],
        }, "", self.now)
        self.assertEqual(self.worker.storage.owner, "user_wxid")
        self.assertEqual(reply, """✅ 待办创建成功
【待办】和客户甲开会
【时间】2026-09-11 19:30
【对象】张三
【提醒】2026-09-11 19:10
【编号】#12""")

    def test_plain_name_cannot_create_todo_for_another_member(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "去工地铺线", {"kind": "relative_days", "value": 2},
                _clock(9, period="morning"), target="李工",
            )],
        }, "", self.now)
        self.assertIn("必须在当前消息中真实 @该成员", reply)
        self.assertIsNone(self.worker.storage.owner)

    def test_unknown_group_member_is_rejected_before_database_write(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "去工地铺线", {"kind": "relative_days", "value": 2},
                _clock(9, period="morning"), target="不存在的人",
            )],
        }, "", self.now)
        self.assertIn("必须在当前消息中真实 @该成员", reply)
        self.assertIsNone(self.worker.storage.owner)

    def test_self_pronoun_always_targets_creator(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "下班", {"kind": "today"}, _clock(5, 45, "afternoon"),
                target="我",
            )],
        }, "", self.now)
        self.assertIn("【对象】张三", reply)
        self.assertEqual(
            self.worker.storage.created_items[0]["reminder_target_id"], "user_wxid",
        )

    def test_plain_member_name_ignores_trailing_punctuation(self):
        name, member_id = resolve_group_member(
            self.worker.wechat_db, "room@chatroom", member="恸",
        )
        self.assertEqual((name, member_id), ("恸。", "worker_wxid"))

    def test_real_mention_wxid_has_priority_over_model_name(self):
        self.row["metadata"] = {
            "mentioned_user_ids": ["self_wxid", "worker_wxid"],
        }
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "下班", {"kind": "today"}, _clock(5, 45, "afternoon"),
                target="恸",
            )],
        }, "", self.now)
        self.assertIn("【对象】恸。", reply)
        self.assertEqual(
            self.worker.storage.created_items[0]["reminder_target_id"], "worker_wxid",
        )

    def test_due_reminder_uses_stored_member_wxid(self):
        todo = {
            "id": 12, "title": "去工地铺线", "session_type": "group",
            "origin_session_id": "room@chatroom", "origin_session_name": "项目群",
            "creator_name": "张三", "reminder_target_name": "李工",
            "reminder_target_id": "worker_wxid",
            "event_at": _parse_datetime("2026-09-12T09:00:00+08:00"),
            "event_all_day": False,
        }
        self.worker.storage.claim_due_reminder = lambda account_id: todo
        self.worker.storage.mark_reminder_sent = lambda todo_id: None
        self.worker.storage.mark_reminder_failure = lambda todo_id, error: None
        with patch("wechatauto.ai_worker.send_text") as mocked:
            self.assertTrue(self.worker._remind_one())
        self.assertEqual(mocked.call_args.kwargs["at_user_id"], "worker_wxid")
        self.assertEqual(mocked.call_args.kwargs["at"], "李工")

    def test_stored_wxid_resolves_current_member_name_after_rename(self):
        current_name, member_id = resolve_group_member(
            self.worker.wechat_db, "room@chatroom", member="旧名称",
            member_id="worker_wxid",
        )
        self.assertEqual((current_name, member_id), ("李工", "worker_wxid"))

    def test_create_multiple_todos(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [
                _todo(
                    "看剧", {"kind": "today"}, _clock(8, period="evening"),
                    {"kind": "before_event", "value": 10, "unit": "minutes"},
                ),
                _todo(
                    "打游戏", {"kind": "today"}, _clock(10, period="evening"),
                    {"kind": "before_event", "value": 5, "unit": "minutes"},
                ),
            ],
        }, "", self.now)
        self.assertIn("待办创建成功（2项）", reply)
        self.assertIn("【待办】看剧", reply)
        self.assertIn("【待办】打游戏", reply)

    def test_create_defaults_reminder_to_event_time(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "出发去看办公室", {"kind": "today"},
                _clock(2, 30, "afternoon"),
            )],
        }, "", self.now)
        self.assertIn("【提醒】2026-09-10 14:30", reply)

    def test_bare_clock_uses_nearest_future_time(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "吃饭", {"kind": "none"}, _clock(11, 30),
            )],
        }, "", self.now)
        self.assertIn("【时间】2026-09-10 23:30", reply)
        self.assertIn("【编号】#12", reply)
        self.assertIsNone(self.worker.storage.clarification)

    def test_chat_cannot_claim_todo_was_created(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "chat", "reply": "好的，已记录您今天下午15:30的答辩提醒。",
        }, "")
        self.assertIn("待办创建失败", reply)

    def test_conflict_warns_and_keeps_new_todo(self):
        event_at = _parse_datetime("2026-09-10T22:00:00+08:00")
        self.worker.storage.create_result = {
            "created": [{
                "id": 4, "title": "打游戏", "event_at": event_at,
                "event_all_day": False, "remind_at": event_at,
                "session_type": "group", "reminder_target_name": "张三",
            }],
            "updated": [],
            "conflicts": [{
                "existing": [{
                    "id": 3, "title": "开会",
                    "reminder_target_name": "张三",
                }],
                "new": {
                    "title": "打游戏", "event_at": event_at,
                    "event_all_day": False, "reminder_target_name": "张三",
                },
            }],
        }
        reply = self.worker._handle_command(self.row, {
            "intent": "create", "items": [_todo(
                "打游戏", {"kind": "today"}, _clock(10, period="evening"),
            )],
        }, "", self.now)
        self.assertIn("待办创建成功", reply)
        self.assertIn("同一提醒对象同一时间已有其他待办", reply)
        self.assertIn("删除编号#3的待办", reply)

    def test_delete_by_number_prepares_exact_candidate(self):
        reply = self.worker._handle_command(self.row, {
            "intent": "delete", "todo_id": 12,
            "range_start": None, "range_end": None, "keyword": None,
        }, "")
        self.assertIn("请确认删除", reply)
        self.assertIn("【编号】#12", reply)
        self.assertEqual(self.worker.storage.delete_args[-1], 12)

    def test_confirm_replace(self):
        reply = self.worker._handle_command(
            self.row, {"intent": "confirm_replace"}, "",
        )
        self.assertIn("待办替换成功", reply)
        self.assertIn("【待办】打游戏", reply)

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

    def test_reminder_uses_relative_today_template(self):
        now = _parse_datetime("2026-09-09T17:00:00+08:00")
        todo = {
            "id": 3, "title": "开会", "creator_name": "张三",
            "event_at": _parse_datetime("2026-09-09T17:05:00+08:00"),
            "event_all_day": False,
        }
        self.assertEqual(_format_reminder(todo, now), """【待办】开会
【时间】今天 17:05
【发起人】张三""")


if __name__ == "__main__":
    unittest.main()
