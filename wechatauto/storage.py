# -*- coding: utf-8 -*-
"""微信消息的 PostgreSQL 元数据存储与 MinIO 媒体归档。"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import threading
import wave
from datetime import timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import quote_plus

from wechatauto.media import MediaDownloader


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS wechat_messages (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        local_id BIGINT NOT NULL,
        sort_seq BIGINT NOT NULL,
        server_id BIGINT,
        session_name TEXT NOT NULL,
        session_type TEXT NOT NULL CHECK (session_type IN ('private', 'group')),
        sender_id TEXT,
        sender_name TEXT NOT NULL,
        message_type TEXT NOT NULL CHECK (message_type IN ('text', 'image', 'voice')),
        content TEXT,
        sent_at TIMESTAMPTZ NOT NULL,
        object_key TEXT,
        media_status TEXT CHECK (media_status IS NULL OR media_status IN ('pending', 'ready', 'failed')),
        media_mime TEXT,
        media_size BIGINT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        next_retry_at TIMESTAMPTZ,
        last_error TEXT,
        transcript TEXT,
        asr_status TEXT CHECK (asr_status IS NULL OR asr_status IN
            ('waiting_media', 'pending', 'processing', 'ready', 'failed')),
        asr_error TEXT,
        reply_status TEXT CHECK (reply_status IS NULL OR reply_status IN
            ('pending', 'generating', 'ready', 'sending', 'sent', 'failed')),
        reply_text TEXT,
        reply_error TEXT,
        replied_at TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (account_id, session_id, local_id),
        UNIQUE (account_id, session_id, sort_seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_messages_session_time
    ON wechat_messages (account_id, session_id, sent_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_messages_media_pending
    ON wechat_messages (account_id, next_retry_at)
    WHERE media_status = 'pending'
    """,
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS transcript TEXT",
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS asr_status TEXT",
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS asr_error TEXT",
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS reply_status TEXT",
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS reply_text TEXT",
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS reply_error TEXT",
    "ALTER TABLE wechat_messages ADD COLUMN IF NOT EXISTS replied_at TIMESTAMPTZ",
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_messages_asr_pending
    ON wechat_messages (account_id, sent_at)
    WHERE asr_status = 'pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_messages_reply_pending
    ON wechat_messages (account_id, sent_at)
    WHERE reply_status IN ('pending', 'ready')
    """,
    """
    CREATE TABLE IF NOT EXISTS wechat_todos (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        creator_id TEXT NOT NULL,
        creator_name TEXT NOT NULL,
        origin_session_id TEXT NOT NULL,
        origin_session_name TEXT NOT NULL,
        session_type TEXT NOT NULL CHECK (session_type IN ('private', 'group')),
        reminder_target_id TEXT,
        reminder_target_name TEXT,
        title TEXT NOT NULL,
        event_at TIMESTAMPTZ NOT NULL,
        event_all_day BOOLEAN NOT NULL DEFAULT FALSE,
        remind_at TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'deleted')),
        reminder_status TEXT CHECK (reminder_status IS NULL OR reminder_status IN
            ('pending', 'sending', 'sent', 'failed')),
        reminder_error TEXT,
        reminded_at TIMESTAMPTZ,
        source_message_id BIGINT NOT NULL REFERENCES wechat_messages(id),
        source_item_index INTEGER NOT NULL DEFAULT 0,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_todos_owner_time
    ON wechat_todos (account_id, creator_id, event_at)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_todos_reminder_pending
    ON wechat_todos (account_id, remind_at)
    WHERE status = 'active' AND reminder_status = 'pending'
    """,
    "ALTER TABLE wechat_todos ADD COLUMN IF NOT EXISTS source_item_index INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wechat_todos ADD COLUMN IF NOT EXISTS reminder_target_id TEXT",
    "ALTER TABLE wechat_todos ADD COLUMN IF NOT EXISTS reminder_target_name TEXT",
    """
    UPDATE wechat_todos SET reminder_target_id=creator_id,
        reminder_target_name=creator_name
    WHERE reminder_target_id IS NULL OR reminder_target_name IS NULL
    """,
    "ALTER TABLE wechat_todos DROP CONSTRAINT IF EXISTS wechat_todos_source_message_id_key",
    """
    UPDATE wechat_todos SET remind_at=event_at, reminder_status='pending',
        updated_at=NOW()
    WHERE status='active' AND event_all_day=FALSE AND remind_at IS NULL
      AND event_at >= NOW()
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_wechat_todos_source_item
    ON wechat_todos (source_message_id, source_item_index)
    """,
    """
    CREATE TABLE IF NOT EXISTS wechat_todo_delete_requests (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        creator_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        candidate_ids BIGINT[] NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_todo_delete_pending
    ON wechat_todo_delete_requests (account_id, creator_id, session_id, created_at DESC)
    WHERE status = 'pending'
    """,
    """
    CREATE TABLE IF NOT EXISTS wechat_todo_replace_requests (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        creator_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        source_message_id BIGINT NOT NULL REFERENCES wechat_messages(id),
        replacements JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_todo_replace_pending
    ON wechat_todo_replace_requests (account_id, creator_id, session_id, created_at DESC)
    WHERE status = 'pending'
    """,
    """
    CREATE TABLE IF NOT EXISTS wechat_todo_clarifications (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        creator_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        context TEXT NOT NULL,
        question TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wechat_todo_clarification_pending
    ON wechat_todo_clarifications (account_id, creator_id, session_id, created_at DESC)
    WHERE status = 'pending'
    """,
    """
    CREATE TABLE IF NOT EXISTS wechat_ingestion_offsets (
        account_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        sort_seq BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (account_id, session_id)
    )
    """,
)


class ArchiveStorage:
    """每次操作使用独立连接，适配 Listener 的跨会话工作线程。"""

    def __init__(
        self,
        postgres_dsn: str,
        minio_endpoint: str,
        minio_access_key: str,
        minio_secret_key: str,
        minio_bucket: str = "wechat-media",
        minio_secure: bool = False,
    ):
        try:
            import psycopg
            from psycopg.rows import dict_row
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError(
                '缺少存储依赖，请执行：pip install -e ".[storage]"'
            ) from exc

        self.postgres_dsn = postgres_dsn
        self.bucket = minio_bucket
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._minio = Minio(
            minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            secure=minio_secure,
        )

    @classmethod
    def from_env(cls) -> "ArchiveStorage":
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            password = os.environ.get("POSTGRES_PASSWORD")
            if not password:
                raise RuntimeError("未配置 POSTGRES_DSN 或 POSTGRES_PASSWORD")
            user = quote_plus(os.environ.get("POSTGRES_USER", "wechat"))
            password = quote_plus(password)
            host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
            port = os.environ.get("POSTGRES_PORT", "5432")
            database = quote_plus(os.environ.get("POSTGRES_DB", "wechat_archive"))
            dsn = "postgresql://%s:%s@%s:%s/%s" % (
                user, password, host, port, database,
            )

        access_key = os.environ.get("MINIO_ACCESS_KEY") or os.environ.get("MINIO_ROOT_USER")
        secret_key = os.environ.get("MINIO_SECRET_KEY") or os.environ.get("MINIO_ROOT_PASSWORD")
        if not access_key or not secret_key:
            raise RuntimeError("未配置 MinIO 访问账号或密码")
        return cls(
            postgres_dsn=dsn,
            minio_endpoint=os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000"),
            minio_access_key=access_key,
            minio_secret_key=secret_key,
            minio_bucket=os.environ.get("MINIO_BUCKET", "wechat-media"),
            minio_secure=os.environ.get("MINIO_SECURE", "false").lower()
            in ("1", "true", "yes"),
        )

    def _connect(self):
        return self._psycopg.connect(self.postgres_dsn)

    def initialize(self) -> None:
        with self._connect() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "UPDATE wechat_messages SET asr_status='pending' "
                "WHERE asr_status='processing'"
            )
            conn.execute(
                "UPDATE wechat_messages SET reply_status='pending' "
                "WHERE reply_status='generating'"
            )
            # 旧版本仅按正文“@robot”入队；没有真实 @ 证据的群消息必须作废。
            conn.execute(
                "UPDATE wechat_messages SET reply_status=NULL "
                "WHERE session_type='group' "
                "AND reply_status IN ('pending', 'generating', 'ready') "
                "AND COALESCE(metadata->>'real_mention', 'false') <> 'true'"
            )
        if not self._minio.bucket_exists(self.bucket):
            self._minio.make_bucket(self.bucket)

    def load_offsets(self, account_id: str) -> Dict[str, int]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    "SELECT session_id, sort_seq FROM wechat_ingestion_offsets "
                    "WHERE account_id=%s",
                    (account_id,),
                )
                return {row["session_id"]: row["sort_seq"] for row in cur.fetchall()}

    @staticmethod
    def _advance_offset(cur, account_id: str, session_id: str, sort_seq: int) -> None:
        cur.execute(
            """
            INSERT INTO wechat_ingestion_offsets (account_id, session_id, sort_seq)
            VALUES (%s, %s, %s)
            ON CONFLICT (account_id, session_id) DO UPDATE SET
                sort_seq = GREATEST(wechat_ingestion_offsets.sort_seq, EXCLUDED.sort_seq),
                updated_at = NOW()
            """,
            (account_id, session_id, sort_seq),
        )

    def advance_offset(self, account_id: str, session_id: str, sort_seq: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._advance_offset(cur, account_id, session_id, sort_seq)

    def save_message(self, message: dict) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wechat_messages (
                        account_id, session_id, local_id, sort_seq, server_id,
                        session_name, session_type, sender_id, sender_name,
                        message_type, content, sent_at, media_status, asr_status,
                        reply_status, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s::jsonb
                    )
                    ON CONFLICT (account_id, session_id, local_id) DO UPDATE SET
                        session_name = EXCLUDED.session_name,
                        sender_id = EXCLUDED.sender_id,
                        sender_name = EXCLUDED.sender_name,
                        content = EXCLUDED.content,
                        metadata = wechat_messages.metadata || EXCLUDED.metadata,
                        updated_at = NOW()
                    """,
                    (
                        message["account_id"], message["session_id"],
                        message["local_id"], message["sort_seq"],
                        message.get("server_id"), message["session_name"],
                        message["session_type"], message.get("sender_id"),
                        message["sender_name"], message["message_type"],
                        message.get("content"), message["sent_at"],
                        message.get("media_status"),
                        message.get("asr_status"), message.get("reply_status"),
                        json.dumps(message.get("metadata") or {}, ensure_ascii=False),
                    ),
                )
                self._advance_offset(
                    cur, message["account_id"], message["session_id"],
                    message["sort_seq"],
                )

    def fetch_pending_media(self, account_id: str, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    SELECT account_id, session_id, local_id, message_type,
                           sent_at, retry_count
                    FROM wechat_messages
                    WHERE account_id=%s AND media_status='pending'
                      AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                    ORDER BY sent_at ASC
                    LIMIT %s
                    """,
                    (account_id, limit),
                )
                return list(cur.fetchall())

    @staticmethod
    def _safe_part(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z@._-]+", "_", value).strip("._") or "unknown"

    def upload_media(self, row: dict, path: str) -> tuple:
        sent_at = row["sent_at"].astimezone(timezone.utc)
        extension = Path(path).suffix.lower() or ".bin"
        object_key = "%s/%s/%s/%s%s" % (
            self._safe_part(row["account_id"]),
            self._safe_part(row["session_id"]),
            sent_at.strftime("%Y/%m"),
            row["local_id"],
            extension,
        )
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._minio.fput_object(self.bucket, object_key, path, content_type=mime)
        return object_key, mime, os.path.getsize(path)

    def download_media(self, object_key: str, path: str) -> str:
        self._minio.fget_object(self.bucket, object_key, path)
        return path

    def mark_media_ready(
        self, row: dict, object_key: str, mime: str, size: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET object_key=%s, media_mime=%s,
                    media_size=%s, media_status='ready', next_retry_at=NULL,
                    last_error=NULL,
                    asr_status=CASE WHEN message_type='voice'
                        THEN 'pending' ELSE asr_status END,
                    updated_at=NOW()
                WHERE account_id=%s AND session_id=%s AND local_id=%s
                """,
                (
                    object_key, mime, size, row["account_id"],
                    row["session_id"], row["local_id"],
                ),
            )

    def mark_media_failure(
        self, row: dict, error: str, max_retries: int,
    ) -> tuple:
        retry_count = int(row["retry_count"]) + 1
        status = "failed" if retry_count >= max_retries else "pending"
        delay = min(300, 2 ** min(retry_count, 8))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET retry_count=%s, media_status=%s,
                    next_retry_at=CASE WHEN %s='pending'
                        THEN NOW() + (%s * INTERVAL '1 second') ELSE NULL END,
                    last_error=%s, updated_at=NOW()
                WHERE account_id=%s AND session_id=%s AND local_id=%s
                """,
                (
                    retry_count, status, status, delay, error[:2000],
                    row["account_id"], row["session_id"], row["local_id"],
                ),
            )
        return status, retry_count

    def claim_pending_asr(self, account_id: str) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    WITH candidate AS (
                        SELECT id FROM wechat_messages
                        WHERE account_id=%s AND asr_status='pending'
                        ORDER BY sent_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE wechat_messages AS message
                    SET asr_status='processing', asr_error=NULL, updated_at=NOW()
                    FROM candidate WHERE message.id=candidate.id
                    RETURNING message.*
                    """,
                    (account_id,),
                )
                return cur.fetchone()

    def mark_asr_ready(self, message_id: int, transcript: str, trigger: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET transcript=%s, asr_status='ready',
                    asr_error=NULL,
                    reply_status=CASE WHEN %s THEN 'pending' ELSE reply_status END,
                    updated_at=NOW()
                WHERE id=%s
                """,
                (transcript, trigger, message_id),
            )

    def mark_asr_failure(self, message_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET asr_status='failed', asr_error=%s,
                    updated_at=NOW() WHERE id=%s
                """,
                (error[:2000], message_id),
            )

    def claim_pending_reply(self, account_id: str) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    WITH candidate AS (
                        SELECT id FROM wechat_messages
                        WHERE account_id=%s AND reply_status='pending'
                          AND (session_type='private'
                            OR metadata->>'real_mention'='true')
                        ORDER BY sent_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE wechat_messages AS message
                    SET reply_status='generating', reply_error=NULL, updated_at=NOW()
                    FROM candidate WHERE message.id=candidate.id
                    RETURNING message.*
                    """,
                    (account_id,),
                )
                return cur.fetchone()

    def mark_reply_ready(self, message_id: int, reply_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET reply_text=%s, reply_status='ready',
                    reply_error=NULL, updated_at=NOW() WHERE id=%s
                """,
                (reply_text, message_id),
            )

    def claim_ready_reply(self, message_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE wechat_messages SET reply_status='sending', updated_at=NOW()
                WHERE id=%s AND reply_status='ready'
                """,
                (message_id,),
            )
            return result.rowcount == 1

    def mark_reply_sent(self, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET reply_status='sent', replied_at=NOW(),
                    reply_error=NULL, updated_at=NOW() WHERE id=%s
                """,
                (message_id,),
            )

    def mark_reply_failure(self, message_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_messages SET reply_status='failed', reply_error=%s,
                    updated_at=NOW() WHERE id=%s
                """,
                (error[:2000], message_id),
            )

    @staticmethod
    def _normalized_title(title: str) -> str:
        return re.sub(r"[\W_]+", "", title.casefold(), flags=re.UNICODE)

    @staticmethod
    def _replacement_json(item: dict, index: int, old_todos: List[dict]) -> dict:
        return {
            "old_todo_ids": [todo["id"] for todo in old_todos],
            "item_index": index,
            "title": item["title"],
            "event_at": item["event_at"].isoformat(),
            "event_all_day": bool(item.get("event_all_day")),
            "remind_at": item["remind_at"].isoformat() if item.get("remind_at") else None,
            "reminder_target_id": item.get("reminder_target_id"),
            "reminder_target_name": item.get("reminder_target_name"),
        }

    @staticmethod
    def _insert_todo(cur, message: dict, item: dict, index: int) -> dict:
        target_id = item.get("reminder_target_id") or message["sender_id"]
        target_name = item.get("reminder_target_name") or message["sender_name"]
        cur.execute(
            """
            INSERT INTO wechat_todos (
                account_id, creator_id, creator_name, origin_session_id,
                origin_session_name, session_type, title, event_at,
                reminder_target_id, reminder_target_name, event_all_day,
                remind_at, reminder_status,
                source_message_id, source_item_index
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
            ON CONFLICT (source_message_id, source_item_index) DO UPDATE SET
                title=EXCLUDED.title, event_at=EXCLUDED.event_at,
                event_all_day=EXCLUDED.event_all_day,
                remind_at=EXCLUDED.remind_at,
                reminder_target_id=EXCLUDED.reminder_target_id,
                reminder_target_name=EXCLUDED.reminder_target_name,
                reminder_status=EXCLUDED.reminder_status,
                updated_at=NOW()
            RETURNING *
            """,
            (
                message["account_id"], message["sender_id"],
                message["sender_name"], message["session_id"],
                message["session_name"], message["session_type"],
                item["title"], item["event_at"],
                target_id, target_name,
                item["event_all_day"], item.get("remind_at"),
                "pending" if item.get("remind_at") is not None else None,
                message["id"], index,
            ),
        )
        return cur.fetchone()

    def create_todos(self, message: dict, items: List[dict]) -> dict:
        """批量创建；同时间同标题更新，不同标题等待用户确认替换。"""
        created, updated, conflicts, replacements = [], [], [], []
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    UPDATE wechat_todo_replace_requests SET status='cancelled'
                    WHERE account_id=%s AND creator_id=%s AND session_id=%s
                      AND status='pending'
                    """,
                    (message["account_id"], message["sender_id"], message["session_id"]),
                )
                for index, item in enumerate(items):
                    cur.execute(
                        """
                        SELECT * FROM wechat_todos
                        WHERE account_id=%s AND creator_id=%s AND event_at=%s
                          AND status='active'
                        ORDER BY id
                        FOR UPDATE
                        """,
                        (message["account_id"], message["sender_id"], item["event_at"]),
                    )
                    existing = list(cur.fetchall())
                    same = [todo for todo in existing if self._normalized_title(todo["title"])
                            == self._normalized_title(item["title"])]
                    if len(existing) == 1 and same:
                        cur.execute(
                            """
                            UPDATE wechat_todos SET title=%s, event_all_day=%s,
                                remind_at=%s,
                                reminder_status=%s,
                                reminder_error=NULL, reminded_at=NULL,
                                origin_session_id=%s, origin_session_name=%s,
                                session_type=%s, creator_name=%s,
                                reminder_target_id=%s, reminder_target_name=%s,
                                source_message_id=%s, source_item_index=%s,
                                updated_at=NOW()
                            WHERE id=%s RETURNING *
                            """,
                            (
                                item["title"], item["event_all_day"], item.get("remind_at"),
                                "pending" if item.get("remind_at") is not None else None,
                                message["session_id"],
                                message["session_name"], message["session_type"],
                                message["sender_name"],
                                item.get("reminder_target_id") or message["sender_id"],
                                item.get("reminder_target_name") or message["sender_name"],
                                message["id"], index,
                                same[0]["id"],
                            ),
                        )
                        updated.append(cur.fetchone())
                    elif existing:
                        conflicts.append({"existing": existing, "new": item})
                        replacements.append(self._replacement_json(item, index, existing))
                    else:
                        created.append(self._insert_todo(cur, message, item, index))
                if replacements:
                    cur.execute(
                        """
                        INSERT INTO wechat_todo_replace_requests (
                            account_id, creator_id, session_id, source_message_id,
                            replacements, expires_at
                        ) VALUES (%s, %s, %s, %s, %s::jsonb,
                            NOW() + INTERVAL '15 minutes')
                        """,
                        (
                            message["account_id"], message["sender_id"],
                            message["session_id"], message["id"],
                            json.dumps(replacements, ensure_ascii=False),
                        ),
                    )
        return {"created": created, "updated": updated, "conflicts": conflicts}

    def has_pending_todo_replace(
        self, account_id: str, creator_id: str, session_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM wechat_todo_replace_requests
                WHERE account_id=%s AND creator_id=%s AND session_id=%s
                  AND status='pending' AND expires_at > NOW()
                LIMIT 1
                """,
                (account_id, creator_id, session_id),
            ).fetchone()
            return row is not None

    def confirm_todo_replace(
        self, account_id: str, creator_id: str, session_id: str,
    ) -> dict:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """UPDATE wechat_todo_replace_requests SET status='expired'
                       WHERE status='pending' AND expires_at <= NOW()"""
                )
                cur.execute(
                    """
                    SELECT request.*, message.sender_name, message.session_name,
                           message.session_type
                    FROM wechat_todo_replace_requests AS request
                    JOIN wechat_messages AS message ON message.id=request.source_message_id
                    WHERE request.account_id=%s AND request.creator_id=%s
                      AND request.session_id=%s AND request.status='pending'
                      AND request.expires_at > NOW()
                    ORDER BY request.created_at DESC LIMIT 1 FOR UPDATE
                    """,
                    (account_id, creator_id, session_id),
                )
                request = cur.fetchone()
                if not request:
                    return {"status": "missing", "todos": []}
                message = {
                    "id": request["source_message_id"], "account_id": account_id,
                    "sender_id": creator_id, "sender_name": request["sender_name"],
                    "session_id": session_id, "session_name": request["session_name"],
                    "session_type": request["session_type"],
                }
                todos = []
                for replacement in request["replacements"]:
                    cur.execute(
                        """
                        UPDATE wechat_todos SET status='deleted', deleted_at=NOW(),
                            reminder_status=NULL, updated_at=NOW()
                        WHERE id=ANY(%s) AND account_id=%s AND creator_id=%s
                          AND status='active'
                        """,
                        (replacement["old_todo_ids"], account_id, creator_id),
                    )
                    item = {
                        "title": replacement["title"],
                        "event_at": replacement["event_at"],
                        "event_all_day": replacement["event_all_day"],
                        "remind_at": replacement.get("remind_at"),
                        "reminder_target_id": replacement.get("reminder_target_id"),
                        "reminder_target_name": replacement.get("reminder_target_name"),
                    }
                    todos.append(self._insert_todo(
                        cur, message, item, replacement["item_index"],
                    ))
                cur.execute(
                    "UPDATE wechat_todo_replace_requests SET status='confirmed' WHERE id=%s",
                    (request["id"],),
                )
                return {"status": "replaced", "todos": todos}

    def cancel_todo_replace(
        self, account_id: str, creator_id: str, session_id: str,
    ) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE wechat_todo_replace_requests SET status='cancelled'
                WHERE account_id=%s AND creator_id=%s AND session_id=%s
                  AND status='pending'
                """,
                (account_id, creator_id, session_id),
            )
            return result.rowcount > 0

    def save_todo_clarification(
        self, message: dict, context: str, question: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_todo_clarifications SET status='cancelled'
                WHERE account_id=%s AND creator_id=%s AND session_id=%s
                  AND status='pending'
                """,
                (message["account_id"], message["sender_id"], message["session_id"]),
            )
            conn.execute(
                """
                INSERT INTO wechat_todo_clarifications (
                    account_id, creator_id, session_id, context, question, expires_at
                ) VALUES (%s, %s, %s, %s, %s, NOW() + INTERVAL '15 minutes')
                """,
                (
                    message["account_id"], message["sender_id"],
                    message["session_id"], context, question,
                ),
            )

    def get_pending_todo_clarification(
        self, account_id: str, creator_id: str, session_id: str,
    ) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    UPDATE wechat_todo_clarifications SET status='expired'
                    WHERE status='pending' AND expires_at <= NOW()
                    """
                )
                cur.execute(
                    """
                    SELECT * FROM wechat_todo_clarifications
                    WHERE account_id=%s AND creator_id=%s AND session_id=%s
                      AND status='pending' AND expires_at > NOW()
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (account_id, creator_id, session_id),
                )
                return cur.fetchone()

    def clear_todo_clarification(self, clarification_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_todo_clarifications SET status='confirmed'
                WHERE id=%s AND status='pending'
                """,
                (clarification_id,),
            )

    def cancel_todo_clarification(
        self, account_id: str, creator_id: str, session_id: str,
    ) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE wechat_todo_clarifications SET status='cancelled'
                WHERE account_id=%s AND creator_id=%s AND session_id=%s
                  AND status='pending'
                """,
                (account_id, creator_id, session_id),
            )
            return result.rowcount > 0

    @staticmethod
    def _todo_where(start_at, end_at, keyword):
        clauses = ["account_id=%s", "creator_id=%s", "status='active'"]
        values = []
        if start_at is not None:
            clauses.append("event_at >= %s")
            values.append(start_at)
        if end_at is not None:
            clauses.append("event_at < %s")
            values.append(end_at)
        if keyword:
            clauses.append("title ILIKE %s")
            values.append("%%%s%%" % keyword)
        return " AND ".join(clauses), values

    def list_todos(
        self,
        account_id: str,
        creator_id: str,
        start_at=None,
        end_at=None,
        keyword: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        where, values = self._todo_where(start_at, end_at, keyword)
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    "SELECT * FROM wechat_todos WHERE " + where
                    + " ORDER BY event_at ASC LIMIT %s",
                    [account_id, creator_id] + values + [limit],
                )
                return list(cur.fetchall())

    def prepare_todo_delete(
        self,
        account_id: str,
        creator_id: str,
        session_id: str,
        start_at=None,
        end_at=None,
        keyword: Optional[str] = None,
    ) -> List[dict]:
        candidates = self.list_todos(
            account_id, creator_id, start_at, end_at, keyword, limit=20,
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_todo_delete_requests SET status='cancelled'
                WHERE account_id=%s AND creator_id=%s AND session_id=%s
                  AND status='pending'
                """,
                (account_id, creator_id, session_id),
            )
            if candidates:
                conn.execute(
                    """
                    INSERT INTO wechat_todo_delete_requests (
                        account_id, creator_id, session_id, candidate_ids,
                        expires_at
                    ) VALUES (%s, %s, %s, %s, NOW() + INTERVAL '15 minutes')
                    """,
                    (
                        account_id, creator_id, session_id,
                        [todo["id"] for todo in candidates],
                    ),
                )
        return candidates

    def confirm_todo_delete(
        self,
        account_id: str,
        creator_id: str,
        session_id: str,
        todo_id: Optional[int] = None,
    ) -> dict:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    UPDATE wechat_todo_delete_requests SET status='expired'
                    WHERE status='pending' AND expires_at <= NOW()
                    """
                )
                cur.execute(
                    """
                    SELECT * FROM wechat_todo_delete_requests
                    WHERE account_id=%s AND creator_id=%s AND session_id=%s
                      AND status='pending' AND expires_at > NOW()
                    ORDER BY created_at DESC LIMIT 1 FOR UPDATE
                    """,
                    (account_id, creator_id, session_id),
                )
                request = cur.fetchone()
                if not request:
                    return {"status": "missing"}
                ids = list(request["candidate_ids"])
                if todo_id is None and len(ids) != 1:
                    return {"status": "choose", "candidate_ids": ids}
                selected = todo_id if todo_id is not None else ids[0]
                if selected not in ids:
                    return {"status": "invalid", "candidate_ids": ids}
                cur.execute(
                    """
                    UPDATE wechat_todos SET status='deleted', deleted_at=NOW(),
                        updated_at=NOW(), reminder_status=NULL
                    WHERE id=%s AND account_id=%s AND creator_id=%s
                      AND status='active' RETURNING *
                    """,
                    (selected, account_id, creator_id),
                )
                todo = cur.fetchone()
                cur.execute(
                    "UPDATE wechat_todo_delete_requests SET status='confirmed' WHERE id=%s",
                    (request["id"],),
                )
                return {"status": "deleted" if todo else "missing", "todo": todo}

    def cancel_todo_delete(
        self, account_id: str, creator_id: str, session_id: str,
    ) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE wechat_todo_delete_requests SET status='cancelled'
                WHERE account_id=%s AND creator_id=%s AND session_id=%s
                  AND status='pending'
                """,
                (account_id, creator_id, session_id),
            )
            return result.rowcount > 0

    def claim_due_reminder(self, account_id: str) -> Optional[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row) as cur:
                cur.execute(
                    """
                    WITH candidate AS (
                        SELECT id FROM wechat_todos
                        WHERE account_id=%s AND status='active'
                          AND reminder_status='pending' AND remind_at <= NOW()
                        ORDER BY remind_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE wechat_todos AS todo
                    SET reminder_status='sending', updated_at=NOW()
                    FROM candidate WHERE todo.id=candidate.id
                    RETURNING todo.*
                    """,
                    (account_id,),
                )
                return cur.fetchone()

    def mark_reminder_sent(self, todo_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_todos SET reminder_status='sent', reminded_at=NOW(),
                    reminder_error=NULL, updated_at=NOW() WHERE id=%s
                """,
                (todo_id,),
            )

    def mark_reminder_failure(self, todo_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE wechat_todos SET reminder_status='failed',
                    reminder_error=%s, updated_at=NOW() WHERE id=%s
                """,
                (error[:2000], todo_id),
            )


def silk_to_wav(silk_path: str, sample_rate: int = 24000) -> str:
    """将微信 SILK 解码为单声道 16-bit PCM WAV。"""
    try:
        import pysilk
    except ImportError as exc:
        raise RuntimeError('缺少语音转换依赖，请执行：pip install -e ".[storage,ai]"') from exc

    source = Path(silk_path)
    pcm_path = source.with_suffix(".pcm")
    wav_path = source.with_suffix(".wav")
    try:
        with source.open("rb") as silk, pcm_path.open("wb") as pcm:
            pysilk.decode(silk, pcm, sample_rate)
        with pcm_path.open("rb") as pcm, wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.read())
        return str(wav_path)
    finally:
        try:
            pcm_path.unlink()
        except OSError:
            pass


class MediaArchiveWorker:
    """将 pending 图片/语音从微信本地库上传到 MinIO。"""

    def __init__(
        self,
        db,
        storage: ArchiveStorage,
        account_id: str,
        log: Callable[[str, str], None],
    ):
        save_dir = os.environ.get(
            "WECHAT_MEDIA_TMP",
            os.path.join(tempfile.gettempdir(), "wechatauto_archive"),
        )
        self.downloader = MediaDownloader(db, save_dir=save_dir)
        self.storage = storage
        self.account_id = account_id
        self.log = log
        self.save_dir = Path(save_dir).resolve()
        self.max_retries = int(os.environ.get("MEDIA_MAX_RETRIES", "20"))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="wechat-media-archive", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _download(self, row: dict) -> Optional[str]:
        if row["message_type"] == "image":
            return self.downloader.download_image(
                row["session_id"], row["local_id"], str(self.save_dir),
            )
        if row["message_type"] == "voice":
            return self.downloader.download_voice(
                row["session_id"], row["local_id"], str(self.save_dir),
            )
        return None

    def _remove_temp(self, path: Optional[str]) -> None:
        if not path:
            return
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(self.save_dir)
        except ValueError:
            return
        try:
            candidate.unlink()
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self.storage.fetch_pending_media(self.account_id)
                if not rows:
                    self._stop.wait(2)
                    continue
                for row in rows:
                    if self._stop.is_set():
                        return
                    path = None
                    upload_path = None
                    try:
                        path = self._download(row)
                        if not path:
                            raise FileNotFoundError("微信本地媒体尚未落地")
                        upload_path = (
                            silk_to_wav(path) if row["message_type"] == "voice" else path
                        )
                        object_key, mime, size = self.storage.upload_media(
                            row, upload_path,
                        )
                        self.storage.mark_media_ready(row, object_key, mime, size)
                        self.log(
                            "MEDIA", "%s/%s 已上传：%s" % (
                                row["session_id"], row["local_id"], object_key,
                            ),
                        )
                    except Exception as exc:
                        status, count = self.storage.mark_media_failure(
                            row, str(exc), self.max_retries,
                        )
                        self.log(
                            "MEDIA_RETRY",
                            "%s/%s 第 %d 次失败，状态=%s：%s" % (
                                row["session_id"], row["local_id"],
                                count, status, exc,
                            ),
                        )
                    finally:
                        if upload_path and upload_path != path:
                            self._remove_temp(upload_path)
                        self._remove_temp(path)
            except Exception as exc:
                self.log("MEDIA_ERROR", str(exc))
                self._stop.wait(5)
