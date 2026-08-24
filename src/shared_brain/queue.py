"""Durable client-side retry queue for offline writes.

Stores the full business intent (HTTP shape + payload) and tracks retry state:
- attempts / last_error for diagnostics
- status: pending -> failed (permanent) once max_attempts is exceeded or the
  error is classified as non-retryable
- next_retry_at: exponential backoff so a dead server is not hammered
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class OfflineQueue:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_ops(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  op_key TEXT NOT NULL UNIQUE,
                  method TEXT NOT NULL,
                  path TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  status TEXT NOT NULL DEFAULT 'pending',
                  next_retry_at TEXT
                )
                """
            )
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(pending_ops)").fetchall()}
            if "status" not in existing:
                conn.execute("ALTER TABLE pending_ops ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
            if "next_retry_at" not in existing:
                conn.execute("ALTER TABLE pending_ops ADD COLUMN next_retry_at TEXT")
        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any],
        op_key: Optional[str] = None,
        error: Optional[str] = None,
    ) -> str:
        key = op_key or str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO pending_ops(op_key, method, path, payload_json, created_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, method.upper(), path, json.dumps(payload, ensure_ascii=False), _now(), error),
            )
        return key

    def list(self, limit: int = 100, due_only: bool = False, include_payload: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM pending_ops"
        params: List[Any] = []
        if due_only:
            sql += " WHERE status != 'failed' AND (next_retry_at IS NULL OR next_retry_at <= ?)"
            params.append(_now())
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            items = []
            for row in rows:
                item = {
                    "op_key": row["op_key"],
                    "method": row["method"],
                    "path": row["path"],
                    "attempts": int(row["attempts"]),
                    "last_error": row["last_error"],
                    "status": row["status"],
                    "next_retry_at": row["next_retry_at"],
                    "created_at": row["created_at"],
                }
                if include_payload:
                    item["payload"] = json.loads(row["payload_json"])
                items.append(item)
            return items

    def mark_failed(
        self,
        op_key: str,
        error: str,
        retryable: bool = True,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """Record a failed attempt; permanent failure once exhausted or non-retryable."""
        with self.connect() as conn:
            row = conn.execute("SELECT attempts FROM pending_ops WHERE op_key = ?", (op_key,)).fetchone()
            attempts = int(row["attempts"]) + 1 if row is not None else 1
            if attempts >= max_attempts or not retryable:
                conn.execute(
                    "UPDATE pending_ops SET attempts = ?, last_error = ?, status = 'failed', next_retry_at = NULL "
                    "WHERE op_key = ?",
                    (attempts, error[:2000], op_key),
                )
            else:
                delay = min(2 ** attempts, MAX_BACKOFF_SECONDS)
                next_retry = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat(timespec="microseconds").replace("+00:00", "Z")
                conn.execute(
                    "UPDATE pending_ops SET attempts = ?, last_error = ?, status = 'pending', next_retry_at = ? "
                    "WHERE op_key = ?",
                    (attempts, error[:2000], next_retry, op_key),
                )

    def retry(self, op_key: str) -> bool:
        """Reset a (possibly permanently failed) task for immediate retry."""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE pending_ops SET status = 'pending', next_retry_at = NULL, last_error = NULL "
                "WHERE op_key = ?",
                (op_key,),
            )
            return cursor.rowcount > 0

    def remove(self, op_key: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM pending_ops WHERE op_key = ?", (op_key,))
            return cursor.rowcount > 0

    def count(self, status: Optional[str] = None) -> int:
        with self.connect() as conn:
            if status:
                return int(
                    conn.execute("SELECT count(*) FROM pending_ops WHERE status = ?", (status,)).fetchone()[0]
                )
            return int(conn.execute("SELECT count(*) FROM pending_ops").fetchone()[0])
