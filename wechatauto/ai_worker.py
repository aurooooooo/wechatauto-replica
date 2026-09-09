# -*- coding: utf-8 -*-
"""语音转写和微信自动回复后台任务。"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from wechatauto.qwen_client import QwenClient, reply_prompt, reply_trigger
from wechatauto.send_text import send_text


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("无法解析时间：%s" % text) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _format_time(value, all_day: bool = False) -> str:
    value = value.astimezone(SHANGHAI)
    return value.strftime("%Y-%m-%d" if all_day else "%Y-%m-%d %H:%M")


def _format_todo(todo: dict) -> str:
    event = _format_time(todo["event_at"], bool(todo.get("event_all_day")))
    reminder = (
        _format_time(todo["remind_at"]) if todo.get("remind_at") else "未设置"
    )
    return "#%s｜%s｜时间：%s｜提醒：%s" % (
        todo["id"], todo["title"], event, reminder,
    )


class AIWorker:
    def __init__(
        self,
        storage,
        account_id: str,
        db_dir: str,
        log: Callable[[str, str], None],
        client: Optional[QwenClient] = None,
    ):
        self.storage = storage
        self.account_id = account_id
        self.db_dir = db_dir
        self.log = log
        self.client = client or QwenClient.from_env()
        self.tmp_dir = Path(os.environ.get(
            "WECHAT_ASR_TMP",
            os.path.join(tempfile.gettempdir(), "wechatauto_asr"),
        )).resolve()
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="wechat-ai-worker", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _transcribe_one(self) -> bool:
        row = self.storage.claim_pending_asr(self.account_id)
        if not row:
            return False
        path = self.tmp_dir / ("%s.wav" % row["id"])
        try:
            self.storage.download_media(row["object_key"], str(path))
            transcript = self.client.transcribe_file(str(path), "audio/wav")
            incoming = row.get("sender_id") != self.account_id
            triggered = (
                incoming and row["session_type"] == "private"
                and reply_trigger("private", transcript)
            )
            self.storage.mark_asr_ready(row["id"], transcript, triggered)
            self.log(
                "ASR", "%s | %s | 转写完成，字符数=%d" % (
                    row["session_name"], row["sender_name"], len(transcript),
                ),
            )
        except Exception as exc:
            self.storage.mark_asr_failure(row["id"], str(exc))
            self.log(
                "ASR_ERROR", "%s/%s：%s" % (
                    row["session_id"], row["local_id"], exc,
                ),
            )
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return True

    def _handle_command(self, row: dict, command: dict, prompt: str) -> str:
        intent = command["intent"]
        if intent == "chat":
            return command.get("reply") or self.client.chat(prompt)
        if not row.get("sender_id"):
            return "无法识别消息发送者，不能操作待办，请稍后重试。"

        if intent == "create":
            title = str(command.get("title") or "").strip()
            event_at = _parse_datetime(command.get("event_at"))
            if not title or event_at is None:
                return "未能识别完整的待办事项和时间，请补充后重新发送。"
            remind_at = _parse_datetime(command.get("remind_at"))
            todo = self.storage.create_todo(
                row, title, event_at, bool(command.get("event_all_day")), remind_at,
            )
            return "✅ 待办已添加\n" + _format_todo(todo)

        start_at = _parse_datetime(command.get("range_start"))
        end_at = _parse_datetime(command.get("range_end"))
        keyword = str(command.get("keyword") or "").strip() or None
        if intent == "list":
            todos = self.storage.list_todos(
                self.account_id, row["sender_id"], start_at, end_at, keyword,
            )
            if not todos:
                return "📋 该时间段没有待办事项。"
            return "📋 待办事项（%d）\n%s" % (
                len(todos), "\n".join(_format_todo(todo) for todo in todos),
            )

        if intent == "delete":
            todos = self.storage.prepare_todo_delete(
                self.account_id, row["sender_id"], row["session_id"],
                start_at, end_at, keyword,
            )
            if not todos:
                return "没有找到可删除的待办事项。"
            marker = "@robot" if row["session_type"] == "group" else "robot"
            return "⚠️ 请确认删除\n%s\n回复“%s 确认删除 #编号”。" % (
                "\n".join(_format_todo(todo) for todo in todos), marker,
            )

        if intent == "confirm_delete":
            raw_id = command.get("todo_id")
            try:
                todo_id = int(str(raw_id).lstrip("#")) if raw_id is not None else None
            except (TypeError, ValueError):
                todo_id = None
            result = self.storage.confirm_todo_delete(
                self.account_id, row["sender_id"], row["session_id"], todo_id,
            )
            if result["status"] == "deleted":
                return "✅ 待办已删除\n" + _format_todo(result["todo"])
            if result["status"] in ("choose", "invalid"):
                ids = "、".join("#%s" % value for value in result["candidate_ids"])
                return "请从待删除候选中指定正确编号：%s" % ids
            return "没有待确认的删除请求，可能已超过15分钟，请重新发起删除。"

        if intent == "cancel_delete":
            cancelled = self.storage.cancel_todo_delete(
                self.account_id, row["sender_id"], row["session_id"],
            )
            return "已取消删除操作。" if cancelled else "当前没有待确认的删除操作。"

        raise RuntimeError("未支持的待办意图：%s" % intent)

    def _reply_one(self) -> bool:
        row = self.storage.claim_pending_reply(self.account_id)
        if not row:
            return False
        source = row.get("transcript") or row.get("content") or ""
        try:
            prompt = reply_prompt(row["session_type"], source)
            command = self.client.interpret(
                prompt, datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            )
            reply = self._handle_command(row, command, prompt)
            self.storage.mark_reply_ready(row["id"], reply)
            if not self.storage.claim_ready_reply(row["id"]):
                return True
            send_text(row["session_id"], reply, self.db_dir)
            self.storage.mark_reply_sent(row["id"])
            self.log(
                "AUTO_REPLY", "%s | %s | 已发送，字符数=%d" % (
                    row["session_name"], row["sender_name"], len(reply),
                ),
            )
        except Exception as exc:
            self.storage.mark_reply_failure(row["id"], str(exc))
            self.log(
                "AUTO_REPLY_ERROR", "%s/%s：%s" % (
                    row["session_id"], row["local_id"], exc,
                ),
            )
        return True

    def _remind_one(self) -> bool:
        todo = self.storage.claim_due_reminder(self.account_id)
        if not todo:
            return False
        text = "⏰ 待办提醒\n" + _format_todo(todo)
        try:
            send_text(
                todo["origin_session_id"], text, self.db_dir,
                at=todo["creator_name"] if todo["session_type"] == "group" else None,
            )
            self.storage.mark_reminder_sent(todo["id"])
            self.log("REMINDER", "%s | #%s 已发送" % (
                todo["origin_session_name"], todo["id"],
            ))
        except Exception as exc:
            self.storage.mark_reminder_failure(todo["id"], str(exc))
            self.log("REMINDER_ERROR", "#%s：%s" % (todo["id"], exc))
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self._remind_one()
                did_work = self._transcribe_one() or did_work
                did_work = self._remind_one() or did_work
                did_work = self._reply_one() or did_work
                if not did_work:
                    self._stop.wait(1)
            except Exception as exc:
                self.log("AI_WORKER_ERROR", str(exc))
                self._stop.wait(3)
