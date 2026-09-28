# -*- coding: utf-8 -*-
"""语音转写和微信自动回复后台任务。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import traceback
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from wechatauto.qwen_client import (
    QwenBillingError, QwenClient, render_prompt, reply_prompt, reply_trigger,
)
from wechatauto import WeChatDB
from wechatauto.send_text import resolve_group_member, send_text
from wechatauto.time_resolver import TimeResolver


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


def _list_date_range(text: str, now: datetime):
    """为明确的中文日期查询生成当天起止范围，避免依赖模型补全。"""
    matches = re.findall(r"大后天|后天|明天|今天", text or "")
    if len(matches) != 1:
        return None
    day_offset = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[matches[0]]
    target_date = now.date() + timedelta(days=day_offset)
    start = datetime.combine(target_date, time.min, SHANGHAI)
    return start, start + timedelta(days=1)


def _format_todo(todo: dict) -> str:
    event = _format_time(todo["event_at"], bool(todo.get("event_all_day")))
    reminder = (
        _format_time(todo["remind_at"]) if todo.get("remind_at") else "未设置"
    )
    lines = ["【待办】%s" % todo["title"], "【时间】%s" % event]
    if todo.get("session_type") == "group" and todo.get("reminder_target_name"):
        lines.append("【对象】%s" % todo["reminder_target_name"])
        target_id = todo.get("reminder_target_id")
        creator_id = todo.get("creator_id")
        target_name = todo.get("reminder_target_name")
        creator_name = todo.get("creator_name") or creator_id
        other_member = (
            target_id and creator_id and target_id != creator_id
        ) or (
            not target_id and not creator_id and creator_name
            and target_name != creator_name
        )
        if other_member and creator_name:
            lines.append("【创建人】%s" % creator_name)
    lines.extend(("【提醒】%s" % reminder, "【编号】#%s" % todo["id"]))
    return "\n".join(lines)


def _format_create_result(result: dict, session_type: str) -> str:
    sections = []
    created = result["created"]
    if created:
        heading = "✅ 待办创建成功" + ("（%d项）" % len(created) if len(created) > 1 else "")
        sections.append(heading + "\n" + "\n——\n".join(
            _format_todo(todo) for todo in created
        ))
    updated = result["updated"]
    if updated:
        sections.append("✅ 相同事项已按最新信息更新\n" + "\n——\n".join(
            _format_todo(todo) for todo in updated
        ))
    if result["conflicts"]:
        details = []
        for conflict in result["conflicts"]:
            new = conflict["new"]
            new_time = _format_time(new["event_at"], bool(new.get("event_all_day")))
            existing_todos = conflict["existing"]
            existing = "、".join(
                "#%s %s" % (todo["id"], todo["title"])
                for todo in existing_todos
            )
            target_name = new.get("reminder_target_name") or (
                existing_todos[0].get("reminder_target_name")
                if existing_todos else None
            )
            target_line = "\n【对象】%s" % target_name if target_name else ""
            details.append("【时间】%s%s\n【已有】%s\n【新待办】%s" % (
                new_time, target_line, existing, new["title"],
            ))
        example_id = result["conflicts"][0]["existing"][0]["id"]
        instruction = (
            "如需删除已有事项，请重新真实 @robot 发送“删除编号#%s的待办”；"
            "系统会先要求确认。"
            % example_id
            if session_type == "group"
            else "如需删除已有事项，请重新发送“robot 删除编号#%s的待办”；"
            "系统会先要求确认。"
            % example_id
        )
        sections.append(
            "⚠️ 同一提醒对象同一时间已有其他待办，新待办已保留\n"
            + "\n——\n".join(details) + "\n【操作】" + instruction
        )
    return "\n".join(sections)


def _format_reminder(todo: dict, now=None) -> str:
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    event = todo["event_at"].astimezone(SHANGHAI)
    if event.date() == now.date():
        deadline = "今天" if todo.get("event_all_day") else event.strftime("今天 %H:%M")
    elif event.date() == now.date() + timedelta(days=1):
        deadline = "明天" if todo.get("event_all_day") else event.strftime("明天 %H:%M")
    else:
        deadline = _format_time(event, bool(todo.get("event_all_day")))
    creator = todo.get("creator_name") or todo.get("creator_id") or "未知用户"
    return "【待办】%s\n【时间】%s\n【发起人】%s" % (
        todo["title"], deadline, creator,
    )


def _format_create_failure(session_type: str, reason: str = "时间信息不够明确") -> str:
    example = (
        "真实 @robot 今天下午15:30去答辩，到点提醒我"
        if session_type == "group"
        else "robot 今天下午15:30去答辩，到点提醒我"
    )
    return (
        "❌ 待办创建失败\n【原因】%s\n"
        "【规则】请重新触发机器人，并写明具体时间和事项；"
        "未明确日期时按最近的未来时间处理；未写提前量时默认在任务时间提醒；"
        "只说“提前提醒”时默认提前30分钟。\n【示例】%s"
    ) % (reason, example)


def _claims_todo_created(text: str) -> bool:
    return bool(re.search(
        r"待办.{0,8}(?:创建|添加).{0,4}成功|已(?:经)?(?:为您)?(?:记录|设置).*提醒",
        text,
    ))


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
        self.wechat_db = WeChatDB(db_dir=db_dir)
        self.time_resolver = TimeResolver()
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

    def _send_qwen_billing_notice(self, session_id: str) -> None:
        try:
            send_text(
                session_id, render_prompt("qwen_billing_error"), self.db_dir,
            )
            self.log("QWEN_BILLING", "%s | 已发送充值提示" % session_id)
        except Exception as exc:
            self.log("QWEN_BILLING_NOTICE_ERROR", "%s：%s" % (session_id, exc))

    def _transcribe_one(self) -> bool:
        row = self.storage.claim_pending_asr(self.account_id)
        if not row:
            return False
        path = self.tmp_dir / ("%s.wav" % row["id"])
        incoming = row.get("sender_id") != self.account_id
        try:
            self.storage.download_media(row["object_key"], str(path))
            transcript = self.client.transcribe_file(str(path), "audio/wav")
            triggered = (
                incoming and row["session_type"] == "private"
                and reply_trigger("private", transcript)
            )
            self.storage.mark_asr_ready(row["id"], transcript, triggered)
            self.log(
                "ASR", "%s | %s | 转写完成，字符数=%d，内容=%r" % (
                    row["session_name"], row["sender_name"],
                    len(transcript), transcript,
                ),
            )
        except QwenBillingError as exc:
            self.storage.mark_asr_failure(row["id"], str(exc))
            if incoming:
                self._send_qwen_billing_notice(row["session_id"])
            self.log(
                "ASR_ERROR", "%s/%s：%s" % (
                    row["session_id"], row["local_id"], exc,
                ),
            )
        except Exception as exc:
            self.storage.mark_asr_failure(row["id"], str(exc))
            self.log(
                "ASR_ERROR", "%s/%s：%s\n%s" % (
                    row["session_id"], row["local_id"], exc,
                    traceback.format_exc(),
                ),
            )
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return True

    def _handle_command(
        self, row: dict, command: dict, prompt: str, now: datetime | None = None,
    ) -> str:
        intent = command["intent"]
        if intent == "help":
            return render_prompt("todo_help")
        if intent == "chat":
            reply = command.get("reply") or self.client.chat(prompt)
            return _format_create_failure(row["session_type"]) if _claims_todo_created(reply) else reply
        if not row.get("sender_id"):
            return "无法识别消息发送者，不能操作待办，请稍后重试。"

        if intent == "create":
            items = []
            raw_items = command.get("items") or []
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            mentioned_targets = [
                value for value in metadata.get("mentioned_user_ids", [])
                if value and value != self.account_id
            ] if row["session_type"] == "group" else []
            if len(mentioned_targets) > 1:
                return _format_create_failure(
                    row["session_type"], "一次待办只能指定一个被提醒人",
                )
            for raw in raw_items:
                if not isinstance(raw, dict):
                    return "未能识别完整的待办事项和时间，请补充后重新发送。"
                self.log(
                    "TODO_ITEM_INPUT",
                    "%s/%s 原始待办=%s" % (
                        row["session_id"], row.get("local_id"),
                        json.dumps(raw, ensure_ascii=False, default=str),
                    ),
                )
                title = str(raw.get("title") or "").strip()
                if not title:
                    return "未能识别完整的待办事项和时间，请补充后重新发送。"
                resolution = self.time_resolver.resolve(
                    raw, now or datetime.now(SHANGHAI),
                )
                if resolution["status"] == "failed":
                    self.log(
                        "TODO_TIME_ERROR",
                        "%s/%s 原始待办=%s 解析结果=%s" % (
                            row["session_id"], row.get("local_id"),
                            json.dumps(raw, ensure_ascii=False, default=str),
                            json.dumps(resolution, ensure_ascii=False, default=str),
                        ),
                    )
                    return _format_create_failure(
                        row["session_type"], resolution["reason"],
                    )
                items.append({
                    "title": title,
                    "event_at": _parse_datetime(resolution["event_at"]),
                    "event_all_day": bool(resolution.get("event_all_day")),
                    "remind_at": _parse_datetime(resolution.get("remind_at")),
                })
                target_text = str(raw.get("target_text") or "").strip()
                self_target = target_text in {"我", "我自己", "自己", "本人"}
                if target_text and not self_target and not mentioned_targets:
                    reason = (
                        "提醒其他成员时，必须在当前消息中真实 @该成员"
                        if row["session_type"] == "group"
                        else "私聊暂不支持指定其他提醒对象"
                    )
                    return _format_create_failure(
                        row["session_type"], reason,
                    )
                if mentioned_targets:
                    try:
                        target_name, target_id = resolve_group_member(
                            self.wechat_db, row["session_id"],
                            member=None if self_target else (target_text or None),
                            member_id=mentioned_targets[0],
                        )
                    except (LookupError, ValueError) as exc:
                        return _format_create_failure(row["session_type"], str(exc))
                else:
                    target_id, target_name = row["sender_id"], row["sender_name"]
                items[-1].update({
                    "reminder_target_id": target_id,
                    "reminder_target_name": target_name,
                })
            if not items:
                return "未能识别完整的待办事项和时间，请补充后重新发送。"
            result = self.storage.create_todos(row, items)
            return _format_create_result(result, row["session_type"])

        if intent == "confirm_replace":
            result = self.storage.confirm_todo_replace(
                self.account_id, row["sender_id"], row["session_id"],
            )
            if result["status"] == "replaced":
                return "✅ 待办替换成功\n" + "\n——\n".join(
                    _format_todo(todo) for todo in result["todos"]
                )
            return "没有待确认的替换请求，可能已超过15分钟，请重新创建待办。"

        if intent == "cancel_replace":
            cancelled = self.storage.cancel_todo_replace(
                self.account_id, row["sender_id"], row["session_id"],
            )
            return "已取消替换，原待办保持不变。" if cancelled else "当前没有待确认的替换操作。"

        start_at = _parse_datetime(command.get("range_start"))
        end_at = _parse_datetime(command.get("range_end"))
        keyword = str(command.get("keyword") or "").strip() or None
        if intent == "list":
            query_now = now or datetime.now(SHANGHAI)
            if query_now.tzinfo is None:
                query_now = query_now.replace(tzinfo=SHANGHAI)
            else:
                query_now = query_now.astimezone(SHANGHAI)
            explicit_range = _list_date_range(prompt, query_now)
            if explicit_range is not None:
                start_at, end_at = explicit_range
            todos = self.storage.list_todos(
                self.account_id, row["sender_id"], start_at, end_at, keyword,
                not_before=query_now,
            )
            if not todos:
                return "📋 该时间段没有待办事项。"
            return "📋 待办事项（%d）\n%s" % (
                len(todos), "\n——\n".join(_format_todo(todo) for todo in todos),
            )

        if intent == "delete":
            try:
                todo_id = (
                    int(str(command.get("todo_id")).lstrip("#"))
                    if command.get("todo_id") is not None else None
                )
            except (TypeError, ValueError):
                return "无法识别待办编号，请使用类似“删除编号12的待办”。"
            todos = self.storage.prepare_todo_delete(
                self.account_id, row["sender_id"], row["session_id"],
                start_at, end_at, keyword, todo_id,
            )
            if not todos:
                return "没有找到可删除的待办事项。"
            marker = "@robot" if row["session_type"] == "group" else "robot"
            return "⚠️ 请确认删除\n%s\n【操作】回复“%s 确认删除 #编号”。" % (
                "\n——\n".join(_format_todo(todo) for todo in todos), marker,
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
                return "✅ 待办删除成功\n" + _format_todo(result["todo"])
            if result["status"] in ("choose", "invalid"):
                ids = "、".join("#%s" % value for value in result["candidate_ids"])
                return "【可选编号】%s\n【操作】请指定正确编号。" % ids
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
            confirmation = re.sub(r"@\S+", "", prompt).strip(" \t\r\n\u2005，。！!")
            pending_replace = row.get("sender_id") and self.storage.has_pending_todo_replace(
                self.account_id, row["sender_id"], row["session_id"],
            )
            now = datetime.now(SHANGHAI)
            if pending_replace and confirmation in {"是", "确认", "替换", "确认替换"}:
                command = {"intent": "confirm_replace"}
            elif pending_replace and confirmation in {"否", "不", "取消", "不替换"}:
                command = {"intent": "cancel_replace"}
            else:
                command = self.client.interpret(
                    prompt, now.isoformat(timespec="seconds"),
                )
            self.log(
                "AI_COMMAND",
                "%s/%s 输入=%r 命令=%s" % (
                    row["session_id"], row.get("local_id"), source,
                    json.dumps(command, ensure_ascii=False, default=str),
                ),
            )
            reply = self._handle_command(row, command, prompt, now)
            self.log(
                "AI_REPLY_RESULT",
                "%s/%s 回复=%r" % (
                    row["session_id"], row.get("local_id"), reply,
                ),
            )
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
        except QwenBillingError as exc:
            self.storage.mark_reply_failure(row["id"], str(exc))
            self._send_qwen_billing_notice(row["session_id"])
            self.log(
                "AUTO_REPLY_ERROR", "%s/%s：%s" % (
                    row["session_id"], row["local_id"], exc,
                ),
            )
        except Exception as exc:
            self.storage.mark_reply_failure(row["id"], str(exc))
            self.log(
                "AUTO_REPLY_ERROR", "%s/%s：%s\n%s" % (
                    row["session_id"], row["local_id"], exc,
                    traceback.format_exc(),
                ),
            )
        return True

    def _remind_one(self) -> bool:
        todo = self.storage.claim_due_reminder(self.account_id)
        if not todo:
            return False
        is_group = todo["session_type"] == "group"
        text = ("\n" if is_group else "") + _format_reminder(todo)
        try:
            send_text(
                todo["origin_session_id"], text, self.db_dir,
                at=todo.get("reminder_target_name") if is_group else None,
                at_user_id=todo.get("reminder_target_id") if is_group else None,
            )
            self.storage.mark_reminder_sent(todo["id"])
            self.log("REMINDER", "%s | #%s 已发送" % (
                todo["origin_session_name"], todo["id"],
            ))
        except Exception as exc:
            self.storage.mark_reminder_failure(todo["id"], str(exc))
            self.log(
                "REMINDER_ERROR",
                "#%s：%s\n%s" % (todo["id"], exc, traceback.format_exc()),
            )
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
                self.log(
                    "AI_WORKER_ERROR",
                    "%s\n%s" % (exc, traceback.format_exc()),
                )
                self._stop.wait(3)
