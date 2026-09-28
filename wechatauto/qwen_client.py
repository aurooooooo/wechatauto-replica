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


from wechatauto.logger import wxlog

PROMPT_FILE = Path(__file__).with_name("prompts.ini")


class QwenBillingError(RuntimeError):
    """千问账户余额不足或欠费。"""


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

    @staticmethod
    def _log(level: str, message: str) -> None:
        try:
            getattr(wxlog, level)(message)
        except Exception:
            # 日志异常不能覆盖模型请求本身的错误。
            pass

    @staticmethod
    def _log_payload(payload):
        """记录请求结构，但不把语音 base64 写入日志。"""
        if not isinstance(payload, dict):
            return payload
        result = dict(payload)
        messages = []
        for message in payload.get("messages", []):
            if not isinstance(message, dict):
                messages.append(message)
                continue
            logged_message = dict(message)
            content = message.get("content")
            if isinstance(content, list):
                logged_content = []
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "input_audio"
                        and isinstance(part.get("input_audio"), dict)
                    ):
                        audio = part["input_audio"]
                        logged_content.append({
                            "type": "input_audio",
                            "input_audio": {
                                "data": "<base64 omitted; chars=%d>"
                                % len(str(audio.get("data") or "")),
                            },
                        })
                    else:
                        logged_content.append(part)
                logged_message["content"] = logged_content
            messages.append(logged_message)
        if "messages" in payload:
            result["messages"] = messages
        return result

    @staticmethod
    def _normalize_json_keys(value):
        """修复模型偶发在 JSON 字段名中插入空格的问题。"""
        if isinstance(value, dict):
            return {
                re.sub(r"\s+", "", key): QwenClient._normalize_json_keys(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [QwenClient._normalize_json_keys(item) for item in value]
        return value

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
        self._log(
            "debug",
            "QWEN_REQUEST method=%s url=%s payload=%s"
            % (
                method,
                url,
                json.dumps(
                    self._log_payload(payload),
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                    self._log(
                        "debug",
                        "QWEN_RESPONSE attempt=%d url=%s body=%s"
                        % (
                            attempt + 1,
                            url,
                            json.dumps(result, ensure_ascii=False, default=str),
                        ),
                    )
                    if self._is_billing_error(result):
                        raise QwenBillingError("千问账户余额不足或已欠费")
                    return result
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:2000]
                self._log(
                    "error",
                    "QWEN_HTTP_ERROR attempt=%d url=%s status=%s body=%r"
                    % (attempt + 1, url, exc.code, body),
                )
                if exc.code == 402 or self._is_billing_error(body):
                    raise QwenBillingError("千问账户余额不足或已欠费") from exc
                if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                    continue
                raise RuntimeError("千问接口返回 HTTP %s：%s" % (exc.code, body)) from exc
            except (TimeoutError, socket.timeout, URLError) as exc:
                self._log(
                    "error",
                    "QWEN_NETWORK_ERROR attempt=%d url=%s error=%r"
                    % (attempt + 1, url, exc),
                )
                if attempt == 0:
                    continue
                raise RuntimeError("千问接口请求失败（已重试1次）：%s" % exc) from exc
        raise RuntimeError("千问接口请求失败")

    @staticmethod
    def _is_billing_error(value) -> bool:
        if isinstance(value, dict):
            parts = []
            for key in ("code", "message", "type", "status"):
                if value.get(key) is not None:
                    parts.append(str(value[key]))
            if isinstance(value.get("error"), dict):
                parts.append(QwenClient._error_text(value["error"]))
            value = " ".join(parts)
        text = str(value).casefold().replace("-", "_")
        markers = (
            "arrearage", "accountoverdue", "paymentrequired",
            "insufficientbalance", "insufficient_balance", "insufficient balance",
            "insufficientquota", "insufficient_quota", "insufficient quota",
            "quota_exceeded", "quota exceeded", "余额不足", "余额不够",
            "账户欠费", "账号欠费", "欠费", "请充值", "需要充值",
            "please recharge", "need recharge",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _error_text(value) -> str:
        if isinstance(value, dict):
            return " ".join(
                str(value[key]) for key in ("code", "message", "type", "status")
                if value.get(key) is not None
            )
        return str(value)

    def chat(self, prompt: str) -> str:
        self._log("info", "QWEN_CHAT_INPUT prompt=%r" % prompt)
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
        self._log("info", "QWEN_CHAT_OUTPUT content=%r" % content.strip())
        return content.strip()

    def interpret(self, text: str, now_iso: str) -> dict:
        """将触发消息解析为聊天或待办命令。"""
        instruction = render_prompt("todo_interpret", now_iso=now_iso, text=text)
        self._log(
            "info",
            "QWEN_INTERPRET_INPUT now=%s user_text=%r prompt=%r"
            % (now_iso, text, instruction),
        )
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
            result = self._normalize_json_keys(json.loads(content))
        except json.JSONDecodeError as exc:
            raise RuntimeError("千问意图解析返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("千问意图解析未返回 JSON 对象")
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
        self._log(
            "info",
            "QWEN_INTERPRET_OUTPUT raw_content=%r parsed_command=%s"
            % (
                content,
                json.dumps(result, ensure_ascii=False, default=str),
            ),
        )
        return result

    def transcribe_file(self, file_path: str, mime_type: Optional[str] = None) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError("音频文件不存在：%s" % path)
        max_bytes = int(os.environ.get("QWEN_ASR_MAX_BYTES", str(10 * 1024 * 1024)))
        if path.stat().st_size > max_bytes:
            raise ValueError("音频文件超过 qwen3-asr-flash 的 10 MB 限制")
        mime = mime_type or mimetypes.guess_type(str(path))[0] or "audio/wav"
        self._log(
            "info",
            "QWEN_ASR_INPUT file=%s mime=%s bytes=%d"
            % (path, mime, path.stat().st_size),
        )
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
        self._log("info", "QWEN_ASR_OUTPUT transcript=%r" % content.strip())
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
