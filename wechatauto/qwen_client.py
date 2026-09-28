# -*- coding: utf-8 -*-
"""千问文本生成与录音文件转写的最小 HTTP 客户端。"""

from __future__ import annotations

import base64
from configparser import ConfigParser
from functools import lru_cache
import json
import os
import mimetypes
import re
import socket
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROMPT_FILE = Path(__file__).with_name("prompts.ini")


@lru_cache(maxsize=1)
def _prompt_config() -> ConfigParser:
    config = ConfigParser(interpolation=None)
    if not config.read(PROMPT_FILE, encoding="utf-8"):
        raise RuntimeError("Prompt 文件不存在：%s" % PROMPT_FILE)
    return config


def render_prompt(name: str, **values) -> str:
    try:
        template = _prompt_config().get(name, "template").strip()
    except Exception as exc:
        raise RuntimeError("Prompt 配置缺少有效段落：%s" % name) from exc
    for key, value in values.items():
        template = template.replace("{%s}" % key, str(value))
    return template

class QwenClient:
    def __init__(
        self,
        api_key: str,
        chat_model: str = "qwen3.8-flash",
        asr_model: str = "qwen3-asr-flash",
        compatible_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout: int = 120,
    ):
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY 不能为空")
        self.api_key = api_key
        self.chat_model = chat_model
        self.asr_model = asr_model
        self.compatible_base_url = compatible_base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "QwenClient":
        return cls(
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            chat_model=os.environ.get("QWEN_CHAT_MODEL", "qwen3.8-flash"),
            asr_model=os.environ.get("QWEN_ASR_MODEL", "qwen3-asr-flash"),
            compatible_base_url=os.environ.get(
                "DASHSCOPE_COMPATIBLE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            timeout=int(os.environ.get("QWEN_HTTP_TIMEOUT", "120")),
        )

    def _json_request(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[dict] = None,
    ) -> dict:
        headers = {"Authorization": "Bearer " + self.api_key}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                    continue
                body = exc.read().decode("utf-8", errors="replace")[:2000]
                raise RuntimeError("千问接口返回 HTTP %s：%s" % (exc.code, body)) from exc
            except (TimeoutError, socket.timeout, URLError) as exc:
                if attempt == 0:
                    continue
                raise RuntimeError("千问接口请求失败（已重试1次）：%s" % exc) from exc
        raise RuntimeError("千问接口请求失败")

    def chat(self, prompt: str) -> str:
        response = self._json_request(
            self.compatible_base_url + "/chat/completions",
            method="POST",
            payload={
                "model": self.chat_model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("千问文本回复缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("千问返回了空文本")
        return content.strip()

    def interpret(self, text: str, now_iso: str) -> dict:
        """将触发消息解析为聊天或待办命令。"""
        instruction = render_prompt("todo_interpret", now_iso=now_iso, text=text)
        response = self._json_request(
            self.compatible_base_url + "/chat/completions",
            method="POST",
            payload={
                "model": self.chat_model,
                "messages": [{"role": "user", "content": instruction}],
                "response_format": {"type": "json_object"},
            },
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("千问意图解析响应格式错误") from exc
        if not isinstance(content, str):
            raise RuntimeError("千问意图解析未返回文本 JSON")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("千问意图解析返回了无效 JSON") from exc
        if result.get("intent") not in {
            "help", "create", "list", "delete", "confirm_delete", "cancel_delete",
            "confirm_replace", "cancel_replace", "chat",
        }:
            raise RuntimeError("千问返回了未知意图")
        if result["intent"] == "create":
            items = result.get("items")
            if not isinstance(items, list):
                items = [{key: result.get(key) for key in (
                    "title", "date_text", "time_text", "remind_text", "target_text",
                    "date_semantic", "time_semantic", "reminder_semantic",
                )}]
            result["items"] = items
        return result

    def transcribe_file(self, file_path: str, mime_type: Optional[str] = None) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError("音频文件不存在：%s" % path)
        max_bytes = int(os.environ.get("QWEN_ASR_MAX_BYTES", str(10 * 1024 * 1024)))
        if path.stat().st_size > max_bytes:
            raise ValueError("音频文件超过 qwen3-asr-flash 的 10 MB 限制")
        mime = mime_type or mimetypes.guess_type(str(path))[0] or "audio/wav"
        data_uri = "data:%s;base64,%s" % (
            mime, base64.b64encode(path.read_bytes()).decode("ascii"),
        )
        response = self._json_request(
            self.compatible_base_url + "/chat/completions",
            method="POST",
            payload={
                "model": self.asr_model,
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "input_audio",
                        "input_audio": {"data": data_uri},
                    }],
                }],
                "asr_options": {"enable_itn": False},
            },
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("语音转写响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("语音转写结果为空")
        return content.strip()


def reply_trigger(
    session_type: str,
    content: str,
    source: str = "",
    account_id: str = "",
) -> bool:
    if session_type != "group":
        return "robot" in (content or "").casefold()
    return account_id in mentioned_user_ids(source)


def mentioned_user_ids(source: str) -> list[str]:
    """从微信 msgsource 中提取真实 @ 的用户 ID。"""
    try:
        root = ET.fromstring((source or "").strip())
    except (ET.ParseError, ValueError):
        return []
    targets = root.findtext("atuserlist") or ""
    return list(dict.fromkeys(
        item.strip() for item in re.split(r"[,;\s]+", targets) if item.strip()
    ))


def reply_prompt(session_type: str, content: str) -> str:
    marker = "@robot" if session_type == "group" else "robot"
    lowered = content.casefold()
    position = lowered.find(marker)
    if position >= 0:
        content = content[:position] + content[position + len(marker):]
    return content.strip(" \t\r\n\u2005") or "请回复我。"
