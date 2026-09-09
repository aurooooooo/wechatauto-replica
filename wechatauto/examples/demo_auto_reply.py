# -*- coding: utf-8 -*-
"""监听指定联系人并自动回复固定文本。

用法：
    python -u demo_auto_reply.py 懒猪儿 "收到，我稍后回复你。"
"""
from __future__ import annotations

import queue
import sys

from wechatauto.db import Listener, WeChatDB
from wechatauto.guia import WeChatGUI

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(
            encoding="utf-8", errors="replace",
            line_buffering=True, write_through=True,
        )


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "懒猪儿"
    reply_text = sys.argv[2] if len(sys.argv) > 2 else "收到，我稍后回复你。"

    db = WeChatDB(db_dir=r"D:\xwechat_files")
    hits = db.search_contact(target)
    username = hits[0]["username"] if hits else target
    pending: queue.Queue[dict] = queue.Queue()
    gui = WeChatGUI()

    def on_message(msg: dict, _: Listener) -> None:
        if msg["sender_id"] != 2 and msg["type"] == "文本":
            pending.put(msg)

    listener = Listener(db, interval=1.0)
    listener.add_listener(username, on_message)
    listener.start()
    print(f"监听：{target} → {username}")
    print(f"自动回复：{reply_text}")
    print("运行中（Ctrl+C 停止）...")

    try:
        while True:
            msg = pending.get()
            print(f"收到：{msg['content']}")
            result = gui.send_msg(reply_text, who=target, verify=False)
            print(f"回复：{result['status']} - {result['message']}")
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        print("已停止。")


if __name__ == "__main__":
    main()
