"""SQLite persistence for versioned memories and durable idempotency."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .errors import ConflictError, NotFoundError, ValidationError
from .security import content_hash, hash_token, verify_token


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  token_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('global', 'user', 'project')),
  kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'decision', 'pitfall')),
  project_key TEXT,
  current_version INTEGER NOT NULL,
  deleted_at TEXT,
  deleted_by_agent TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((scope = 'project' AND project_key IS NOT NULL) OR scope != 'project')
);

CREATE TABLE IF NOT EXISTS memory_versions (
  rowid INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'decision', 'pitfall')),
  title TEXT NOT NULL,
  content_text TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  source_session_id TEXT,
  trust_level INTEGER NOT NULL DEFAULT 0 CHECK (trust_level BETWEEN 0 AND 3),
  content_hash TEXT NOT NULL,
  supersedes_id TEXT REFERENCES memory_versions(id),
  created_at TEXT NOT NULL,
  UNIQUE(memory_id, version)
);

CREATE INDEX IF NOT EXISTS idx_memories_scope_project
  ON memories(scope, project_key, deleted_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_versions_memory_version
  ON memory_versions(memory_id, version);
CREATE INDEX IF NOT EXISTS idx_memory_versions_hash
  ON memory_versions(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_versions_fts USING fts5(
  title,
  content_text,
  content='memory_versions',
  content_rowid='rowid',
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS memory_versions_ai AFTER INSERT ON memory_versions BEGIN
  INSERT INTO memory_versions_fts(rowid, title, content_text)
  VALUES (new.rowid, new.title, new.content_text);
END;

CREATE TRIGGER IF NOT EXISTS memory_versions_ad AFTER DELETE ON memory_versions BEGIN
  INSERT INTO memory_versions_fts(memory_versions_fts, rowid, title, content_text)
  VALUES ('delete', old.rowid, old.title, old.content_text);
END;

CREATE TRIGGER IF NOT EXISTS memory_versions_au AFTER UPDATE ON memory_versions BEGIN
  INSERT INTO memory_versions_fts(memory_versions_fts, rowid, title, content_text)
  VALUES ('delete', old.rowid, old.title, old.content_text);
  INSERT INTO memory_versions_fts(rowid, title, content_text)
  VALUES (new.rowid, new.title, new.content_text);
END;

CREATE TABLE IF NOT EXISTS applied_ops (
  op_key TEXT PRIMARY KEY,
  request_hash TEXT NOT NULL,
  response_status INTEGER NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_change_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete')),
  source_agent TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_change_log_memory
  ON memory_change_log(memory_id, seq);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class BrainStore:
    """Small transactional store; each operation owns one SQLite connection."""

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self, token: str) -> None:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            current = conn.execute("SELECT token_hash FROM users WHERE id = 1").fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO users(id, token_hash, created_at) VALUES (1, ?, ?)",
                    (hash_token(token), now_iso()),
                )
            elif not verify_token(token, str(current["token_hash"])):
                raise RuntimeError(
                    "BRAIN_TOKEN does not match the token used to initialize this database"
                )
        os.chmod(path, 0o600)

    def token_hash(self) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT token_hash FROM users WHERE id = 1").fetchone()
            if row is None:
                raise RuntimeError("Shared Brain has not been initialized")
            return str(row["token_hash"])

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _idempotent(
        self,
        op_key: str,
        req_hash: str,
        operation: Callable[[sqlite3.Connection], Tuple[int, Dict[str, Any]]],
    ) -> Tuple[int, Dict[str, Any]]:
        with self.write_transaction() as conn:
            previous = conn.execute(
                "SELECT request_hash, response_status, result_json FROM applied_ops WHERE op_key = ?",
                (op_key,),
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != req_hash:
                    raise ConflictError("Idempotency-Key was already used for a different request")
                return int(previous["response_status"]), json.loads(previous["result_json"])
            status, result = operation(conn)
            conn.execute(
                "INSERT INTO applied_ops(op_key, request_hash, response_status, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (op_key, req_hash, status, json.dumps(result, ensure_ascii=False), now_iso()),
            )
            return status, result

    @staticmethod
    def _validate_scope(scope: str, project_key: Optional[str]) -> None:
        if scope == "project" and not (project_key or "").strip():
            raise ValidationError("project_key is required when scope=project")

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["memory_id"],
            "scope": row["scope"],
            "kind": row["kind"],
            "project_key": row["project_key"],
            "current_version": row["current_version"],
            "title": row["title"],
            "content_text": row["content_text"],
            "source_agent": row["source_agent"],
            "source_session_id": row["source_session_id"],
            "trust_level": row["trust_level"],
            "content_hash": row["content_hash"],
            "version_id": row["version_id"],
            "deleted_at": row["deleted_at"],
            "deleted_by_agent": row["deleted_by_agent"],
            "created_at": row["memory_created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _current_select() -> str:
        return """
          SELECT m.id AS memory_id, m.scope, v.kind, m.project_key,
                 m.current_version, m.deleted_at, m.deleted_by_agent, m.created_at AS memory_created_at,
                 m.updated_at, v.id AS version_id, v.title, v.content_text,
                 v.source_agent, v.source_session_id, v.trust_level, v.content_hash
            FROM memories m
            JOIN memory_versions v
              ON v.memory_id = m.id AND v.version = m.current_version
        """

    def create_memory(
        self, payload: Dict[str, Any], op_key: str, req_hash: str
    ) -> Tuple[int, Dict[str, Any]]:
        self._validate_scope(payload["scope"], payload.get("project_key"))

        def operation(conn: sqlite3.Connection) -> Tuple[int, Dict[str, Any]]:
            timestamp = now_iso()
            digest = content_hash(payload["title"], payload["content_text"])
            duplicate = conn.execute(
                self._current_select()
                + " WHERE m.deleted_at IS NULL AND m.scope = ? AND m.kind = ? "
                + " AND COALESCE(m.project_key, '') = COALESCE(?, '') AND v.content_hash = ? LIMIT 1",
                (payload["scope"], payload["kind"], payload.get("project_key"), digest),
            ).fetchone()
            if duplicate is not None:
                result = self._row_to_memory(duplicate)
                result["deduplicated"] = True
                return 200, result

            memory_id = str(uuid.uuid4())
            version_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO memories(id, scope, kind, project_key, current_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (
                    memory_id,
                    payload["scope"],
                    payload["kind"],
                    payload.get("project_key"),
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                "INSERT INTO memory_versions(id, memory_id, version, kind, title, content_text, source_agent, "
                "source_session_id, trust_level, content_hash, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    memory_id,
                    payload["kind"],
                    payload["title"].strip(),
                    payload["content_text"].strip(),
                    payload["source_agent"],
                    payload.get("source_session_id"),
                    payload.get("trust_level", 0),
                    digest,
                    timestamp,
                ),
            )
            conn.execute(
                "INSERT INTO memory_change_log(memory_id, action, source_agent, changed_at) VALUES (?, 'create', ?, ?)",
                (memory_id, payload["source_agent"], timestamp),
            )
            row = conn.execute(self._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
            result = self._row_to_memory(row)
            result["deduplicated"] = False
            return 201, result

        return self._idempotent(op_key, req_hash, operation)

    def get_memory(self, memory_id: str, include_deleted: bool = False) -> Dict[str, Any]:
        query = self._current_select() + " WHERE m.id = ?"
        if not include_deleted:
            query += " AND m.deleted_at IS NULL"
        with self.connect() as conn:
            row = conn.execute(query, (memory_id,)).fetchone()
            if row is None:
                raise NotFoundError("memory not found")
            return self._row_to_memory(row)

    def list_versions(self, memory_id: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if exists is None:
                raise NotFoundError("memory not found")
            rows = conn.execute(
                "SELECT id AS version_id, memory_id, version, kind, title, content_text, source_agent, "
                "source_session_id, trust_level, content_hash, supersedes_id, created_at "
                "FROM memory_versions WHERE memory_id = ? ORDER BY version DESC",
                (memory_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_memory(
        self, memory_id: str, payload: Dict[str, Any], op_key: str, req_hash: str
    ) -> Tuple[int, Dict[str, Any]]:
        def operation(conn: sqlite3.Connection) -> Tuple[int, Dict[str, Any]]:
            current = conn.execute(self._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
            if current is None or current["deleted_at"] is not None:
                raise NotFoundError("memory not found")
            if int(current["current_version"]) != int(payload["expected_version"]):
                raise ConflictError(
                    f"expected version {payload['expected_version']}, current version is {current['current_version']}"
                )
            timestamp = now_iso()
            new_version = int(current["current_version"]) + 1
            title = (payload.get("title") or current["title"]).strip()
            content_text = (payload.get("content_text") or current["content_text"]).strip()
            kind = payload.get("kind") or current["kind"]
            trust_level = payload.get("trust_level")
            if trust_level is None:
                trust_level = current["trust_level"]
            version_id = str(uuid.uuid4())
            digest = content_hash(title, content_text)
            conn.execute(
                "INSERT INTO memory_versions(id, memory_id, version, kind, title, content_text, source_agent, "
                "source_session_id, trust_level, content_hash, supersedes_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    memory_id,
                    new_version,
                    kind,
                    title,
                    content_text,
                    payload["source_agent"],
                    payload.get("source_session_id"),
                    trust_level,
                    digest,
                    current["version_id"],
                    timestamp,
                ),
            )
            conn.execute(
                "UPDATE memories SET kind = ?, current_version = ?, updated_at = ? WHERE id = ?",
                (kind, new_version, timestamp, memory_id),
            )
            conn.execute(
                "INSERT INTO memory_change_log(memory_id, action, source_agent, changed_at) VALUES (?, 'update', ?, ?)",
                (memory_id, payload["source_agent"], timestamp),
            )
            row = conn.execute(self._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
            return 200, self._row_to_memory(row)

        return self._idempotent(op_key, req_hash, operation)

    def delete_memory(
        self, memory_id: str, payload: Dict[str, Any], op_key: str, req_hash: str
    ) -> Tuple[int, Dict[str, Any]]:
        def operation(conn: sqlite3.Connection) -> Tuple[int, Dict[str, Any]]:
            current = conn.execute(self._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
            if current is None:
                raise NotFoundError("memory not found")
            if current["deleted_at"] is not None:
                return 200, self._row_to_memory(current)
            if int(current["current_version"]) != int(payload["expected_version"]):
                raise ConflictError(
                    f"expected version {payload['expected_version']}, current version is {current['current_version']}"
                )
            timestamp = now_iso()
            conn.execute(
                "UPDATE memories SET deleted_at = ?, deleted_by_agent = ?, updated_at = ? WHERE id = ?",
                (timestamp, payload["source_agent"], timestamp, memory_id),
            )
            conn.execute(
                "INSERT INTO memory_change_log(memory_id, action, source_agent, changed_at) VALUES (?, 'delete', ?, ?)",
                (memory_id, payload["source_agent"], timestamp),
            )
            row = conn.execute(self._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
            return 200, self._row_to_memory(row)

        return self._idempotent(op_key, req_hash, operation)

    def search_memories(
        self,
        query: str,
        scope: Optional[str] = None,
        project_key: Optional[str] = None,
        kind: Optional[str] = None,
        source_agent: Optional[str] = None,
        min_trust_level: int = 0,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        filters = ["m.deleted_at IS NULL", "v.trust_level >= ?"]
        params: List[Any] = [min_trust_level]
        if scope:
            filters.append("m.scope = ?")
            params.append(scope)
        if project_key:
            filters.append("(m.scope != 'project' OR m.project_key = ?)")
            params.append(project_key)
        else:
            filters.append("m.scope != 'project'")
        if kind:
            filters.append("v.kind = ?")
            params.append(kind)
        if source_agent:
            filters.append("v.source_agent = ?")
            params.append(source_agent)
        where = " AND ".join(filters)

        with self.connect() as conn:
            if len(text) < 3:
                escaped_text = text.replace("%", "\\%").replace("_", "\\_")
                like = "%" + escaped_text + "%"
                sql = (
                    self._current_select()
                    + f" WHERE {where} AND (v.title LIKE ? ESCAPE '\\' OR v.content_text LIKE ? ESCAPE '\\') "
                    + "ORDER BY m.updated_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, (*params, like, like, limit)).fetchall()
            else:
                phrase = '"' + text.replace('"', '""') + '"'
                sql = (
                    self._current_select()
                    + " JOIN memory_versions_fts fts ON fts.rowid = v.rowid "
                    + f" WHERE {where} AND memory_versions_fts MATCH ? "
                    + "ORDER BY bm25(memory_versions_fts), m.updated_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, (*params, phrase, limit)).fetchall()
            return [self._row_to_memory(row) for row in rows]

    def changes_since(
        self, cursor: int, project_key: Optional[str], limit: int = 100
    ) -> List[Dict[str, Any]]:
        filters = ["c.seq > ?"]
        params: List[Any] = [cursor]
        if project_key:
            filters.append("(base.scope != 'project' OR base.project_key = ?)")
            params.append(project_key)
        else:
            filters.append("base.scope != 'project'")
        sql = (
            "SELECT base.*, c.seq, c.action, c.source_agent AS change_source_agent FROM ("
            + self._current_select()
            + ") base JOIN memory_change_log c ON c.memory_id = base.memory_id "
            + " WHERE "
            + " AND ".join(filters)
            + " ORDER BY c.seq LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
            result = []
            for row in rows:
                item = self._row_to_memory(row)
                item["change_seq"] = row["seq"]
                item["change_action"] = row["action"]
                item["change_source_agent"] = row["change_source_agent"]
                result.append(item)
            return result
