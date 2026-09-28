# -*- coding: utf-8 -*-
"""监听全部或指定微信会话，并归档文字、图片和语音。

用法：
    python listen_messages.py --all
    python listen_messages.py testUser "项目讨论群"
    python listen_messages.py --all --log-only
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, Iterable, Tuple

from wechatauto.ai_worker import AIWorker
from wechatauto.db import Listener, WeChatDB
from wechatauto.qwen_client import mentioned_user_ids, reply_trigger
from wechatauto.storage import ArchiveStorage, MediaArchiveWorker
from wechatauto.logger import wxlog

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


DEFAULT_DB_DIR = os.environ.get("WECHAT_DB_DIR", r"D:\xwechat_files")


def operation_log(step: str, message: str) -> None:
    wxlog.info("[%s] %s", step, message)


class DisplayNames:
    """按 username 缓存备注优先的显示名，避免每条消息重复查库。"""

    def __init__(self, db: WeChatDB):
        self.db = db
        self._cache: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, username: str) -> str:
        if not username:
            return "未知用户"
        with self._lock:
            cached = self._cache.get(username)
            if cached:
                return cached
            display = self.db.get_nickname(username) or username
            self._cache[username] = display
            return display


def resolve_target(
    db: WeChatDB,
    session_ids: Iterable[str],
    target: str,
) -> Tuple[str, str]:
    """将群名、备注、昵称或 username 解析为稳定 session ID。"""
    target = target.strip()
    if not target:
        raise ValueError("监听目标不能为空")

    known = set(session_ids)
    if target in known:
        return target, db.get_nickname(target)

    hits = [hit for hit in db.search_contact(target) if hit["username"] in known]
    exact = [
        hit for hit in hits
        if target in (hit.get("username"), hit.get("nick_name"), hit.get("remark"))
    ]
    candidates = exact or hits
    if not candidates:
        raise LookupError("未找到会话：%s" % target)
    if len(candidates) > 1:
        choices = ", ".join(
            "%s(%s)" % (
                hit.get("remark") or hit.get("nick_name") or hit["username"],
                hit["username"],
            )
            for hit in candidates[:5]
        )
        raise LookupError("目标不唯一，请使用更精确的名称或 username：%s" % choices)

    hit = candidates[0]
    display = hit.get("remark") or hit.get("nick_name") or hit["username"]
    return hit["username"], display


def message_context(message: dict, names: DisplayNames) -> dict:
    """得到可展示、可持久化的会话与发送者信息。"""
    session_id = message["username"]
    chat_name = names.get(session_id)
    content = str(message.get("content") or "")
    group_sender = None
    if session_id.endswith("@chatroom"):
        prefix = re.match(r"^(wxid_[0-9A-Za-z_-]+):\s*", content)
        if prefix:
            group_sender = prefix.group(1)
            content = content[prefix.end():]
        else:
            group_sender = message.get("sender_username") or None
            if not group_sender:
                xml_sender = re.search(r'fromusername=["\']([^"\']+)', content)
                if xml_sender:
                    group_sender = xml_sender.group(1)

    if message.get("sender_id") == 2:
        sender_id = None
        sender_name = "我"
    elif session_id.endswith("@chatroom"):
        sender_id = group_sender
        sender_name = names.get(group_sender) if group_sender else "未知群成员"
    else:
        # 私聊中非本人的发送者就是当前会话联系人；real_sender_id 不是
        # 可跨会话使用的全局联系人 ID，不能据此映射好友备注。
        sender_id = session_id
        sender_name = chat_name
    return {
        "session_id": session_id,
        "session_name": chat_name,
        "session_type": "group" if session_id.endswith("@chatroom") else "private",
        "sender_id": sender_id,
        "sender_name": sender_name,
        "content": content,
    }


def parse_quote_message(content: str):
    """解析 appmsg/type=57，仅返回引用回复需要归档的文本和元数据。"""
    try:
        root = ET.fromstring((content or "").strip())
    except (ET.ParseError, ValueError):
        return None
    appmsg = root.find("appmsg")
    if appmsg is None or (appmsg.findtext("type") or "").strip() != "57":
        return None
    refer = appmsg.find("refermsg")
    if refer is None:
        return None

    title = (appmsg.findtext("title") or "").strip()
    quoted_content = (refer.findtext("content") or "").strip()
    quoted_name = (refer.findtext("displayname") or "").strip()
    quote_line = "[引用%s：%s]" % (
        (" " + quoted_name) if quoted_name else "", quoted_content,
    )
    return {
        "content": "\n".join(part for part in (title, quote_line) if part),
        "trigger_content": title,
        "metadata": {
            "quoted_type": (refer.findtext("type") or "").strip() or None,
            "quoted_server_id": (refer.findtext("svrid") or "").strip() or None,
            "quoted_sender_id": (refer.findtext("fromusr") or "").strip() or None,
            "quoted_session_id": (refer.findtext("chatusr") or "").strip() or None,
            "quoted_sender_name": quoted_name or None,
            "quoted_content": quoted_content,
            "quoted_create_time": (refer.findtext("createtime") or "").strip() or None,
        },
    }


def make_log_callback(names: DisplayNames):
    def on_message(message: dict, listener: Listener) -> None:
        del listener
        context = message_context(message, names)
        timestamp = time.strftime(
            "%m-%d %H:%M:%S", time.localtime(message["create_time"])
        )
        quote = parse_quote_message(context["content"])
        content = (quote["content"] if quote else context["content"])
        content = content.replace("\r", "").replace("\n", "\\n")
        operation_log(
            "WECHAT_MESSAGE",
            "%s | %s | %s | %s | %s"
            % (
                timestamp, context["session_name"], context["sender_name"],
                "引用文本" if quote else message.get("type"), content,
            ),
        )

    return on_message


STORED_TYPES = {"文本": "text", "图片": "image", "语音": "voice"}
TYPE_CODES = {"文本": 1, "图片": 3, "语音": 34}


def make_storage_callback(
    names: DisplayNames,
    storage: ArchiveStorage,
    account_id: str,
):
    """数据库短暂不可用时阻塞本会话重试，其他会话继续处理。"""
    def on_message(message: dict, listener: Listener) -> None:
        attempt = 0
        while True:
            try:
                context = message_context(message, names)
                quote = (
                    parse_quote_message(context["content"])
                    if message.get("type") == "文件/链接/卡片" else None
                )
                message_type = "text" if quote else STORED_TYPES.get(message.get("type"))
                if message_type is None:
                    # 动画表情(47)及其他类型只推进水位，不写消息表。
                    storage.advance_offset(
                        account_id, context["session_id"], message["sort_seq"],
                    )
                    return

                raw = None
                if message_type in ("image", "voice") or quote:
                    raw = names.db.get_message_row(
                        context["session_id"], message["local_id"],
                    )
                incoming = message.get("sender_id") != 2
                trigger_content = quote["trigger_content"] if quote else context["content"]
                explicit_trigger = (
                    message_type == "text" and incoming
                    and reply_trigger(
                        context["session_type"], trigger_content,
                        message.get("source") or "", account_id,
                    )
                )
                should_reply = explicit_trigger
                metadata = {
                    "wechat_type": message.get("type"),
                    "type_code": raw.get("local_type") if raw
                    else message.get("type_code") or TYPE_CODES.get(message.get("type")),
                }
                if context["session_type"] == "group":
                    metadata["mentioned_user_ids"] = mentioned_user_ids(
                        message.get("source") or "",
                    )
                    metadata["real_mention"] = explicit_trigger
                if quote:
                    metadata["quote"] = quote["metadata"]
                storage.save_message({
                    "account_id": account_id,
                    "session_id": context["session_id"],
                    "local_id": message["local_id"],
                    "sort_seq": message["sort_seq"],
                    "server_id": raw.get("server_id") if raw else None,
                    "session_name": context["session_name"],
                    "session_type": context["session_type"],
                    "sender_id": account_id if message.get("sender_id") == 2
                    else context["sender_id"],
                    "sender_name": context["sender_name"],
                    "message_type": message_type,
                    "content": (
                        quote["content"] if quote else context["content"]
                    ) if message_type == "text" else None,
                    "sent_at": datetime.fromtimestamp(
                        message["create_time"], tz=timezone.utc,
                    ),
                    "media_status": "pending" if message_type != "text" else None,
                    "asr_status": "waiting_media" if message_type == "voice" else None,
                    "reply_status": "pending" if should_reply else None,
                    "metadata": metadata,
                })
                operation_log(
                    "STORE",
                    "%s | %s | %s" % (
                        context["session_name"], context["sender_name"],
                        message_type,
                    ),
                )
                return
            except Exception as exc:
                attempt += 1
                delay = min(30, 2 ** min(attempt, 5))
                operation_log(
                    "STORE_RETRY",
                    "%s/%s 写入失败，第 %d 次重试：%s" % (
                        message.get("username"), message.get("local_id"),
                        attempt, exc,
                    ),
                )
                if listener._stop.wait(delay):
                    return

    return on_message


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="监听微信会话的新消息")
    parser.add_argument("targets", nargs="*", help="群名、备注、昵称或 username")
    parser.add_argument("--all", action="store_true", help="监听全部会话并自动发现新会话")
    parser.add_argument("--log-only", action="store_true",
                        help="仅打印消息，不连接 PostgreSQL/MinIO")
    parser.add_argument("--no-ai", action="store_true",
                        help="只归档，不进行语音转写和自动回复")
    parser.add_argument("--db-dir", default=DEFAULT_DB_DIR,
                        help="微信数据目录（默认读取 WECHAT_DB_DIR 或 D:\\xwechat_files）")
    args = parser.parse_args(argv)

    if args.all and args.targets:
        parser.error("--all 不能与指定会话同时使用")
    if not args.all and not args.targets:
        parser.error("请传入监听目标，或使用 --all")

    operation_log("DATABASE", "读取微信数据库：%s" % args.db_dir)
    db = WeChatDB(db_dir=args.db_dir)
    account = db.get_self_info()
    operation_log("ACCOUNT", account.get("remark") or account.get("nick_name")
                  or account.get("username") or "未知账号")

    sessions = db.get_sessions(limit=500)
    session_ids = [session["username"] for session in sessions]
    account_id = account.get("username") or getattr(db, "wxid", None)
    if not account_id:
        raise RuntimeError("无法取得当前微信账号 ID")

    names = DisplayNames(db)
    storage = None
    media_worker = None
    ai_worker = None
    offsets = None
    if args.log_only:
        callback = make_log_callback(names)
        operation_log("MODE", "仅输出日志，不写 PostgreSQL/MinIO")
    else:
        operation_log("STORAGE", "连接 PostgreSQL 和 MinIO")
        storage = ArchiveStorage.from_env()
        for attempt in range(1, 11):
            try:
                storage.initialize()
                break
            except Exception as exc:
                if attempt == 10:
                    raise
                operation_log(
                    "STORAGE_RETRY", "存储尚未就绪，第 %d 次重试：%s" % (attempt, exc),
                )
                time.sleep(2)
        offsets = storage.load_offsets(account_id)
        callback = make_storage_callback(names, storage, account_id)
        media_worker = MediaArchiveWorker(db, storage, account_id, operation_log)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if args.no_ai:
            operation_log("AI", "已通过 --no-ai 禁用语音转写和自动回复")
        elif not api_key or api_key.startswith("replace-this-"):
            operation_log("AI_DISABLED", "未配置 DASHSCOPE_API_KEY，仅执行消息归档")
        else:
            ai_worker = AIWorker(
                storage, account_id, args.db_dir, operation_log,
            )
            operation_log(
                "AI", "已启用语音转写和 robot 触发回复",
            )
        operation_log("STORAGE", "存储已就绪，恢复 %d 个会话水位" % len(offsets))

    listener = Listener(db, interval=1.0, watermark=offsets)

    if args.all:
        operation_log("REGISTER", "正在注册全部会话，共 %d 个" % len(sessions))
        listener.add_all(callback, discover=True)
        operation_log("LISTEN", "已监听全部会话，并自动发现新会话")
    else:
        resolved = []
        for target in args.targets:
            session_id, display_name = resolve_target(db, session_ids, target)
            if session_id not in resolved:
                resolved.append(session_id)
                listener.add_listener(session_id, callback)
                operation_log("LISTEN", "已监听：%s" % display_name)

    operation_log("START", "开始监听，按 Ctrl+C 停止")
    if media_worker:
        media_worker.start()
    if ai_worker:
        ai_worker.start()
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        operation_log("STOP", "收到 Ctrl+C，正在停止")
    finally:
        listener.stop()
        if media_worker:
            media_worker.stop()
        if ai_worker:
            ai_worker.stop()
    operation_log("STOP", "监听已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
