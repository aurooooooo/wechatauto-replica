"""聊天归档关键规则的无外部服务回归检查。"""

from __future__ import annotations

import threading
import unittest

from wechatauto.db import Listener
from wechatauto.listen_messages import make_storage_callback, parse_quote_message
from wechatauto.qwen_client import mentioned_user_ids, reply_prompt, reply_trigger


class _Names:
    def __init__(self):
        class DB:
            @staticmethod
            def get_message_row(session_id, local_id):
                return {
                    "local_type": 49 if local_id == 5 else (3 if local_id == 2 else 34),
                    "server_id": 1000 + local_id,
                }

        self.db = DB()

    @staticmethod
    def get(username):
        return {"friend": "好友备注"}.get(username, username)


class _Storage:
    def __init__(self):
        self.messages = []
        self.offsets = []

    def save_message(self, message):
        self.messages.append(message)

    def advance_offset(self, account_id, session_id, sort_seq):
        self.offsets.append((account_id, session_id, sort_seq))

class _CallbackListener:
    def __init__(self):
        self._stop = threading.Event()


class ArchiveFlowTest(unittest.TestCase):
    def test_text_emoji_is_stored_and_animated_sticker_is_skipped(self):
        storage = _Storage()
        callback = make_storage_callback(_Names(), storage, "self_wxid")
        listener = _CallbackListener()

        callback({
            "username": "friend", "local_id": 1, "sort_seq": 10,
            "sender_id": 3, "create_time": 1, "type": "文本",
            "content": "收到😀",
        }, listener)
        callback({
            "username": "friend", "local_id": 2, "sort_seq": 11,
            "sender_id": 3, "create_time": 2, "type": "动画表情",
            "content": "[动画表情]",
        }, listener)

        self.assertEqual([m["content"] for m in storage.messages], ["收到😀"])
        self.assertEqual(storage.offsets, [("self_wxid", "friend", 11)])

    def test_discovered_session_starts_before_its_first_message(self):
        class DB:
            @staticmethod
            def get_sessions(limit=500):
                return [{"username": "new@chatroom"}]

            @staticmethod
            def get_new_messages(user, since_seq=0):
                return []

        listener = Listener(DB())
        listener._all_callback = lambda message, owner: None
        listener._discover_new = True
        listener._poll_once()
        self.assertEqual(listener.watermark["new@chatroom"], 0)

    def test_image_and_voice_enter_pending_queue(self):
        storage = _Storage()
        callback = make_storage_callback(_Names(), storage, "self_wxid")
        listener = _CallbackListener()

        for local_id, message_type in ((2, "图片"), (3, "语音")):
            callback({
                "username": "friend", "local_id": local_id,
                "sort_seq": 10 + local_id, "sender_id": 3,
                "create_time": local_id, "type": message_type,
                "content": "[%s]" % message_type,
            }, listener)

        self.assertEqual(
            [(m["message_type"], m["media_status"]) for m in storage.messages],
            [("image", "pending"), ("voice", "pending")],
        )
        self.assertTrue(all(m["content"] is None for m in storage.messages))

    def test_only_incoming_trigger_messages_enter_reply_queue(self):
        storage = _Storage()
        callback = make_storage_callback(_Names(), storage, "self_wxid")
        listener = _CallbackListener()
        messages = (
            ("friend", 1, 3, "robot 帮我总结", "", "pending"),
            ("room@chatroom", 2, 3, "wxid_a: @robot 帮我总结",
             "<msgsource><atuserlist>self_wxid</atuserlist></msgsource>", "pending"),
            ("room@chatroom", 3, 3, "wxid_a: @robot 只是普通文本", "", None),
            ("room@chatroom", 4, 3, "wxid_a: @其他人",
             "<msgsource><atuserlist>wxid_other</atuserlist></msgsource>", None),
            ("friend", 7, 2, "robot 这是我发的", "", None),
        )
        for username, local_id, sender_id, content, source, _ in messages:
            callback({
                "username": username, "local_id": local_id,
                "sort_seq": 20 + local_id, "sender_id": sender_id,
                "create_time": local_id, "type": "文本", "content": content,
                "source": source,
            }, listener)

        self.assertEqual(
            [message.get("reply_status") for message in storage.messages],
            [expected for *_, expected in messages],
        )

    def test_trigger_and_prompt_rules(self):
        self.assertTrue(reply_trigger("private", "请 robot 回答"))
        self.assertTrue(reply_trigger(
            "group", "@Robot 请回答",
            "<msgsource><atuserlist>self_wxid</atuserlist></msgsource>",
            "self_wxid",
        ))
        self.assertFalse(reply_trigger("group", "@robot 只是普通文本", "", "self_wxid"))
        self.assertFalse(reply_trigger(
            "group", "@robot 请回答",
            "<msgsource><atuserlist>wxid_other</atuserlist></msgsource>",
            "self_wxid",
        ))
        self.assertEqual(reply_prompt("group", "@robot  请回答"), "请回答")
        self.assertEqual(
            mentioned_user_ids(
                "<msgsource><atuserlist>self_wxid,wxid_other</atuserlist></msgsource>"
            ),
            ["self_wxid", "wxid_other"],
        )

    def test_real_mention_ids_are_persisted(self):
        storage = _Storage()
        callback = make_storage_callback(_Names(), storage, "self_wxid")
        callback({
            "username": "room@chatroom", "local_id": 8, "sort_seq": 28,
            "sender_id": 3, "create_time": 8, "type": "文本",
            "content": "wxid_a: @robot @李工 明天提醒",
            "source": (
                "<msgsource><atuserlist>self_wxid,wxid_worker"
                "</atuserlist></msgsource>"
            ),
        }, _CallbackListener())
        self.assertEqual(
            storage.messages[0]["metadata"]["mentioned_user_ids"],
            ["self_wxid", "wxid_worker"],
        )

    def test_quote_appmsg_is_stored_as_text_but_other_cards_are_skipped(self):
        storage = _Storage()
        callback = make_storage_callback(_Names(), storage, "self_wxid")
        listener = _CallbackListener()
        quote_xml = """<?xml version="1.0"?>
        <msg><appmsg><title>当前回复</title><type>57</type><refermsg>
        <type>1</type><svrid>123</svrid><fromusr>wxid_author</fromusr>
        <chatusr>friend</chatusr><displayname>原作者</displayname>
        <content>被引用的内容</content><createtime>100</createtime>
        </refermsg></appmsg><fromusername>friend</fromusername></msg>"""
        callback({
            "username": "friend", "local_id": 5, "sort_seq": 30,
            "sender_id": 3, "create_time": 5, "type": "文件/链接/卡片",
            "content": quote_xml,
        }, listener)
        callback({
            "username": "friend", "local_id": 6, "sort_seq": 31,
            "sender_id": 3, "create_time": 6, "type": "文件/链接/卡片",
            "content": "<msg><appmsg><title>普通链接</title><type>5</type></appmsg></msg>",
        }, listener)

        self.assertEqual(len(storage.messages), 1)
        saved = storage.messages[0]
        self.assertEqual(saved["message_type"], "text")
        self.assertEqual(saved["content"], "当前回复\n[引用 原作者：被引用的内容]")
        self.assertEqual(saved["metadata"]["quote"]["quoted_server_id"], "123")
        self.assertEqual(storage.offsets, [("self_wxid", "friend", 31)])
        self.assertIsNotNone(parse_quote_message(quote_xml))


if __name__ == "__main__":
    unittest.main()
