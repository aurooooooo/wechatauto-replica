# -*- coding: utf-8 -*-
"""按联系人用户名或群名发送一条微信文本消息。

用法：
    python send_text.py t13404090311 "你好，这是测试消息"
    python send_text.py "项目群" "请处理这个事项" --at "张三"
    python send_text.py

不传参数时默认向 testUser 发送“你好”。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from typing import Optional, Tuple

from wechatauto import WeChatDB
from wechatauto.uia_driver import WeChatUIA

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


DEFAULT_TARGET = "testUser"
DEFAULT_TEXT = "你好"
DEFAULT_DB_DIR = os.environ.get("WECHAT_DB_DIR", r"D:\xwechat_files")


def operation_log(step: str, message: str) -> None:
    """操作日志写入 stderr，避免影响 stdout 的最终 JSON。"""
    now = time.strftime("%H:%M:%S")
    print("[%s] [%s] %s" % (now, step, message), file=sys.stderr, flush=True)


def resolve_target(db: WeChatDB, target: str) -> Tuple[str, str]:
    """返回适合微信搜索的显示名和数据库 username。"""
    target = target.strip()
    if not target:
        raise ValueError("目标不能为空")

    hits = db.search_contact(target)
    exact = [
        hit for hit in hits
        if target in (hit.get("username"), hit.get("nick_name"), hit.get("remark"))
    ]
    candidates = exact or hits
    if not candidates:
        raise LookupError("未找到联系人或群：%s" % target)
    if len(candidates) > 1:
        choices = ", ".join(
            "%s(%s)" % (
                hit.get("remark") or hit.get("nick_name") or hit.get("username"),
                hit.get("username"),
            )
            for hit in candidates[:5]
        )
        raise LookupError("目标不唯一，请使用更精确的群名或用户名：%s" % choices)

    hit = candidates[0]
    display_name = hit.get("remark") or hit.get("nick_name") or hit["username"]
    return display_name, hit["username"]


def resolve_group_member(
    db: WeChatDB,
    group_id: str,
    member: Optional[str] = None,
    member_id: Optional[str] = None,
) -> Tuple[str, str]:
    """在指定群内唯一解析成员，返回当前可见名称和稳定 username/wxid。"""
    def key(value: Optional[str]) -> str:
        return "".join(
            char.casefold() for char in str(value or "").strip().lstrip("@").strip()
            if not char.isspace() and not unicodedata.category(char).startswith("P")
        )

    members = db.get_group_members(group_id)
    if member_id:
        exact_id = [item for item in members if item.get("username") == member_id]
        if len(exact_id) != 1:
            raise LookupError("提醒对象已不在当前群：%s" % member_id)
        hit = exact_id[0]
    else:
        query = (member or "").strip()
        if not query:
            raise ValueError("@成员不能为空")
        normalized = key(query)
        exact = [
            item for item in members
            if normalized in {
                key(item.get("username")), key(item.get("nick_name")),
                key(item.get("remark")),
            }
        ]
        if not exact:
            raise LookupError("当前群内未找到成员：%s" % query)
        if len(exact) != 1:
            choices = "、".join(
                "%s(%s)" % (
                    item.get("remark") or item.get("nick_name") or item["username"],
                    item["username"],
                ) for item in exact[:5]
            )
            raise LookupError("当前群内成员名称不唯一：%s；候选：%s" % (query, choices))
        hit = exact[0]
    aliases = [hit.get("remark"), hit.get("nick_name"), hit.get("username")]
    matched_alias = next(
        (alias for alias in aliases if member and key(alias) == key(member)), None,
    )
    return matched_alias or next(alias for alias in aliases if alias), hit["username"]


def open_chat_once(uia: WeChatUIA, display_name: str) -> None:
    """通过 UIA 精确选择一次搜索结果，不进入 OCR 侧栏扫描。"""
    if not uia.ensure_window():
        raise RuntimeError("微信 UIA 窗口不可用")

    current = uia.current_chat()
    operation_log("UIA", "当前会话=%r" % current)
    if current == display_name and uia._chat_input() is not None:
        operation_log("OPEN_CHAT", "目标会话已打开，无需重新搜索")
        return

    search = uia._search_box(uia._win)
    if search is None:
        raise RuntimeError("未找到微信搜索框")

    operation_log("OPEN_CHAT", "在微信搜索框查询：%s" % display_name)
    uia._paste_into(search, display_name, clear=True)
    results = uia._collect_results(display_name)
    exact = [item for item in results if item["name"] == display_name]
    candidates = exact or results
    if not candidates:
        raise RuntimeError("微信搜索未找到目标：%s" % display_name)
    if len(candidates) > 1:
        choices = ", ".join(
            "%s[%s]" % (item["name"], item.get("section") or "未知分类")
            for item in candidates[:5]
        )
        raise RuntimeError("微信搜索结果不唯一：%s" % choices)

    chosen = candidates[0]
    operation_log(
        "OPEN_CHAT",
        "点击搜索结果：%s[%s]" % (
            chosen["name"], chosen.get("section") or "未知分类",
        ),
    )
    chosen["cell"].Click()
    time.sleep(0.8)

    input_control = uia._chat_input()
    operation_log(
        "UIA",
        "点击后当前会话=%r，输入框=%s"
        % (uia.current_chat(), "已找到" if input_control is not None else "未找到"),
    )
    if input_control is None:
        raise RuntimeError("目标会话已点击，但未找到消息输入框")


def send_text_with_mention(uia: WeChatUIA, member: str, text: str) -> None:
    """在当前群聊通过微信 MentionPopover 选择成员并发送文本。"""
    member = member.strip()
    if not member:
        raise ValueError("@成员不能为空")

    input_control = uia._chat_input()
    if input_control is None:
        raise RuntimeError("未找到消息输入框")

    operation_log("AT", "触发群成员选择：%s" % member)
    input_control.Click()
    input_control.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
    input_control.SendKeys("@" + member.replace(" ", ""), waitTime=0.05)

    popover = uia._win.WindowControl(
        ClassName="mmui::XPopover",
        Name="Weixin",
        AutomationId="MentionPopover",
    )
    if not popover.Exists(2.0, 0.2):
        input_control.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        raise RuntimeError("微信未弹出群成员选择窗口")

    members = list(popover.ListControl().GetChildren())
    if len(members) == 1:
        chosen = members[0]
    else:
        expected = member.replace(" ", "")
        exact = [item for item in members
                 if (item.Name or "").replace(" ", "") == expected]
        if len(exact) != 1:
            popover.SendKeys("{ESC}")
            input_control.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            choices = "、".join((item.Name or "未知成员") for item in members[:5])
            raise RuntimeError("@成员不唯一或不存在：%s；候选：%s" % (member, choices or "无"))
        chosen = exact[0]

    operation_log("AT", "选择群成员：%s" % (chosen.Name or member))
    chosen.Click()
    time.sleep(0.3)
    uia._clip_set(text)
    input_control.SendKeys("{Ctrl}v", waitTime=0.05)
    time.sleep(0.2)
    input_control.SendKeys("{Enter}", waitTime=0.05)


def send_text(
    target: str = DEFAULT_TARGET,
    text: str = DEFAULT_TEXT,
    db_dir: Optional[str] = DEFAULT_DB_DIR,
    at: Optional[str] = None,
    at_user_id: Optional[str] = None,
) -> dict:
    """查询目标并发送文本；可直接供 function call 或 MCP 包装调用。"""
    if not isinstance(text, str) or not text:
        raise ValueError("消息内容不能为空")

    operation_log("START", "准备发送文本，目标=%s，字符数=%d" % (target, len(text)))
    operation_log("DATABASE", "读取微信联系人数据库：%s" % db_dir)
    db = WeChatDB(db_dir=db_dir)
    display_name, username = resolve_target(db, target)
    operation_log(
        "RESOLVE",
        "目标已解析：%s -> %s (%s)" % (target, display_name, username),
    )
    if (at or at_user_id) and not username.endswith("@chatroom"):
        raise ValueError("--at 只能用于群聊，当前目标不是群聊：%s" % display_name)

    resolved_at = at
    resolved_at_user_id = at_user_id
    if at or at_user_id:
        resolved_at, resolved_at_user_id = resolve_group_member(
            db, username, member=at, member_id=at_user_id,
        )
        operation_log(
            "AT_RESOLVE", "群成员已解析：%s -> %s (%s)" % (
                at or at_user_id, resolved_at, resolved_at_user_id,
            ),
        )

    operation_log("WINDOW", "连接微信 UIA 窗口")
    uia = WeChatUIA()
    open_chat_once(uia, display_name)

    if resolved_at:
        send_text_with_mention(uia, resolved_at, text)
    else:
        operation_log("SEND", "向当前输入框粘贴文本并按回车")
        if not uia.send_text(text):
            raise RuntimeError("输入或发送文本失败")
    operation_log("RESULT", "成功：消息已发送")
    return {
        "ok": True,
        "status": "成功",
        "message": "消息已发送",
        "target": target,
        "resolved_name": display_name,
        "username": username,
        "content": text,
        "at": resolved_at,
        "at_user_id": resolved_at_user_id,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="发送一条微信文本消息")
    parser.add_argument("target", nargs="?", default=DEFAULT_TARGET,
                        help="联系人用户名、昵称、备注或群名（默认：testUser）")
    parser.add_argument("text", nargs="?", default=DEFAULT_TEXT,
                        help="文本内容（默认：你好）")
    parser.add_argument("--at", metavar="MEMBER",
                        help="在群聊中 @ 指定成员（使用群内显示名称）")
    parser.add_argument("--db-dir", default=DEFAULT_DB_DIR,
                        help="微信数据目录（默认读取 WECHAT_DB_DIR 或 D:\\xwechat_files）")
    args = parser.parse_args(argv)

    try:
        result = send_text(args.target, args.text, args.db_dir, args.at)
    except Exception as exc:
        operation_log("ERROR", str(exc))
        result = {"ok": False, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
