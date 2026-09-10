# -*- coding: utf-8 -*-
"""初始化客户/项目多维表格并可执行关联记录冒烟测试。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from wechatauto.feishu_base import FeishuBaseClient


def _load_env() -> None:
    path = Path(__file__).resolve().parents[1] / "deploy" / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="初始化飞书客户和项目表")
    parser.add_argument("--smoke", action="store_true", help="创建并清理关联测试记录")
    args = parser.parse_args(argv)
    _load_env()
    client = FeishuBaseClient.from_env()
    tables = client.ensure_business_tables()
    if args.smoke:
        client.smoke_test(tables)
    print(json.dumps({"ok": True, "smoke": args.smoke, **tables}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
