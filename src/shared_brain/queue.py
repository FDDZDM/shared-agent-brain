"""Durable client-side retry queue for offline writes."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


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
                  last_error TEXT
                )
                """
            )
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

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM pending_ops ORDER BY id LIMIT ?", (limit,)).fetchall()
            return [
                {
                    **dict(row),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]

    def mark_failed(self, op_key: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pending_ops SET attempts = attempts + 1, last_error = ? WHERE op_key = ?",
                (error[:2000], op_key),
            )

    def remove(self, op_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM pending_ops WHERE op_key = ?", (op_key,))

    def count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT count(*) FROM pending_ops").fetchone()[0])
