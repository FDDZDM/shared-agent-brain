"""SQLite persistence for versioned memories and durable idempotency."""

from __future__ import annotations

import base64
import json
import os
import re
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

-- 会话目录：各客户端上报自己的会话元数据，供跨 agent 的选择器（remember/update/forget）使用。
-- agent_id = 客户端 init 时配置的身份名；synced_at = 会话内容已提炼入库的时间戳（NULL = 未上传）。
CREATE TABLE IF NOT EXISTS sessions (
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  title TEXT,
  updated_at TEXT NOT NULL,
  synced_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent_updated
  ON sessions(agent_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_synced
  ON sessions(agent_id, synced_at);

-- schema 版本跟踪：现有表结构视为 v1，后续变更走 MIGRATIONS。
CREATE TABLE IF NOT EXISTS schema_version (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# CJK 连续块或字母数字/下划线标识符；trigram 需要至少 3 字符才能建立索引。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")


def _encode_cursor(*parts: str) -> str:
    """Encode a validated keyset tuple as opaque URL-safe base64."""
    raw = "\n".join(parts).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str, expected_parts: int = 2) -> Tuple[str, ...]:
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("invalid cursor") from exc
    parts = tuple(raw.split("\n"))
    if len(parts) != expected_parts or any(not part for part in parts):
        raise ValidationError("invalid cursor")
    return parts

# 会话目录隔离迁移（v2）：sessions 增加 project_key/device_id 并改为
# (project_key, agent_id, device_id, session_id) 复合主键。旧行保留进
# 显式 'legacy' 项目，隔离默认生效，管理员可用 project_key=legacy 查询。
_SESSIONS_V2 = """
CREATE TABLE sessions (
  project_key TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL,
  device_id TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL,
  title TEXT,
  updated_at TEXT NOT NULL,
  synced_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (project_key, agent_id, device_id, session_id)
);
CREATE INDEX idx_sessions_scope ON sessions(project_key, agent_id, updated_at);
CREATE INDEX idx_sessions_synced ON sessions(project_key, agent_id, synced_at);
"""


def _migrate_sessions_project_isolation(conn: sqlite3.Connection) -> None:
    legacy_rows = conn.execute(
        "SELECT agent_id, session_id, title, updated_at, synced_at, created_at FROM sessions"
    ).fetchall()
    conn.execute("DROP TABLE IF EXISTS sessions")
    conn.executescript(_SESSIONS_V2)
    for row in legacy_rows:
        conn.execute(
            "INSERT INTO sessions(project_key, agent_id, device_id, session_id, title, updated_at, synced_at, created_at) "
            "VALUES ('legacy', ?, '', ?, ?, ?, ?, ?)",
            (row["agent_id"], row["session_id"], row["title"], row["updated_at"], row["synced_at"], row["created_at"]),
        )


def _migrate_sessions_sync_state(conn: sqlite3.Connection) -> None:
    """v3：sessions 增加 revision/hash 同步状态列。

    content_revision 由服务端在内容指纹变化时自增；synced_revision 记录
    最近一次成功同步对应的版本；两者不等即「同步后有更新」。
    """
    columns = [
        ("content_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("synced_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("content_hash", "TEXT"),
        ("synced_memory_id", "TEXT"),
        ("last_sync_error", "TEXT"),
    ]
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for name, definition in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")


MIGRATIONS = [
    {
        "version": 2,
        "name": "sessions_project_isolation",
        "apply": _migrate_sessions_project_isolation,
    },
    {
        "version": 3,
        "name": "sessions_sync_state",
        "apply": _migrate_sessions_sync_state,
    },
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    current = int(row["version"]) if row is not None else 0
    for migration in MIGRATIONS:
        if migration["version"] <= current:
            continue
        migration["apply"](conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(id, version, applied_at) VALUES (1, ?, ?)",
            (migration["version"], now_iso()),
        )


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
            _apply_migrations(conn)
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

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            return int(row["version"]) if row is not None else 0

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

    @staticmethod
    def _insert_memory_op(
        conn: sqlite3.Connection,
        payload: Dict[str, Any],
        timestamp: str,
        *,
        deduplicate: bool = True,
    ) -> Tuple[int, Dict[str, Any]]:
        """Insert (or dedupe-return) one memory inside an open transaction.

        Shared by create_memory and the compound session-sync operation so the
        memory write and the session sync marker commit atomically.
        """
        digest = content_hash(payload["title"], payload["content_text"])
        if deduplicate:
            duplicate = conn.execute(
                BrainStore._current_select()
                + " WHERE m.deleted_at IS NULL AND m.scope = ? AND m.kind = ? "
                + " AND COALESCE(m.project_key, '') = COALESCE(?, '') AND v.content_hash = ? LIMIT 1",
                (payload["scope"], payload["kind"], payload.get("project_key"), digest),
            ).fetchone()
            if duplicate is not None:
                result = BrainStore._row_to_memory(duplicate)
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
        row = conn.execute(BrainStore._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
        result = BrainStore._row_to_memory(row)
        result["deduplicated"] = False
        return 201, result

    def create_memory(
        self, payload: Dict[str, Any], op_key: str, req_hash: str
    ) -> Tuple[int, Dict[str, Any]]:
        self._validate_scope(payload["scope"], payload.get("project_key"))

        def operation(conn: sqlite3.Connection) -> Tuple[int, Dict[str, Any]]:
            return BrainStore._insert_memory_op(conn, payload, now_iso())

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
            return BrainStore._append_memory_version_op(
                conn,
                memory_id,
                payload,
                now_iso(),
                expected_version=int(payload["expected_version"]),
            )

        return self._idempotent(op_key, req_hash, operation)

    @staticmethod
    def _append_memory_version_op(
        conn: sqlite3.Connection,
        memory_id: str,
        payload: Dict[str, Any],
        timestamp: str,
        expected_version: Optional[int] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Append one version inside an existing transaction.

        Explicit updates pass ``expected_version`` for optimistic locking.
        Compound session sync already serializes on the write transaction and
        follows the session's durable ``synced_memory_id``, so it can append to
        the current version atomically without a client-side race window.
        """
        current = conn.execute(BrainStore._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
        if current is None or current["deleted_at"] is not None:
            raise NotFoundError("memory not found")
        if expected_version is not None and int(current["current_version"]) != expected_version:
            raise ConflictError(
                f"expected version {expected_version}, current version is {current['current_version']}"
            )
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
        row = conn.execute(BrainStore._current_select() + " WHERE m.id = ?", (memory_id,)).fetchone()
        return 200, BrainStore._row_to_memory(row)

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
            # A deleted session-derived memory must make its source session
            # selectable again. Keeping a tombstoned link marked as synced
            # strands the conversation with no visible way to recreate it.
            conn.execute(
                "UPDATE sessions SET synced_at = NULL, synced_revision = 0, "
                "synced_memory_id = NULL, last_sync_error = NULL "
                "WHERE synced_memory_id = ?",
                (memory_id,),
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
        """Multi-strategy recall.

        The full user sentence is never used as one exact FTS phrase:
        1. token OR query over FTS5 trigram (bm25 ranked)  -> or_terms
        2. the full sentence as a phrase                   -> phrase
        3. LIKE substring fallback                          -> like / like_fallback

        Every item carries score / matched_terms / match_strategy so clients
        can explain why a memory was recalled.
        """
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

        def _decorate(row: sqlite3.Row, score, terms, strategy) -> Dict[str, Any]:
            item = self._row_to_memory(row)
            item["score"] = score
            item["match_strategy"] = strategy
            haystack = f"{item['title']} {item['content_text']}".casefold()
            hits = [term for term in terms if term.casefold() in haystack]
            item["matched_terms"] = hits or list(terms)
            return item

        terms = self._search_terms(text)
        with self.connect() as conn:
            # 1~2 字或无可分词：LIKE 子串兜底（trigram 需要至少 3 字符）。
            if len(text) < 3 or not terms:
                escaped_text = text.replace("%", "\\%").replace("_", "\\_")
                like = "%" + escaped_text + "%"
                sql = (
                    self._current_select()
                    + f" WHERE {where} AND (v.title LIKE ? ESCAPE '\\' OR v.content_text LIKE ? ESCAPE '\\') "
                    + "ORDER BY m.updated_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, (*params, like, like, limit)).fetchall()
                return [
                    _decorate(row, None, [text], "like")
                    for row in rows
                ]

            fts_select = self._current_select().replace(
                "FROM memories m",
                ", bm25(memory_versions_fts) AS _score FROM memories m",
                1,
            )
            fts_sql = (
                fts_select
                + " JOIN memory_versions_fts fts ON fts.rowid = v.rowid "
                + f" WHERE {where} AND memory_versions_fts MATCH ? "
                + "ORDER BY _score, m.updated_at DESC LIMIT ?"
            )
            like_sql = (
                self._current_select()
                + f" WHERE {where} AND (v.title LIKE ? ESCAPE '\\' OR v.content_text LIKE ? ESCAPE '\\') "
                + "ORDER BY m.updated_at DESC LIMIT ?"
            )

            def _run_match(match_expr: str) -> List[Dict[str, Any]]:
                rows = conn.execute(fts_sql, (*params, match_expr, limit)).fetchall()
                return [_decorate(row, row["_score"], terms, "or_terms") for row in rows]

            # 1) 词 OR 召回（bm25 排序）。
            or_expr = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            items = _run_match(or_expr)
            if items:
                return items

            # 2) 整句短语降级。
            phrase = '"' + text.replace('"', '""') + '"'
            rows = conn.execute(fts_sql, (*params, phrase, limit)).fetchall()
            if rows:
                return [
                    _decorate(row, row["_score"], terms, "phrase")
                    for row in rows
                ]

            # 3) LIKE 子串兜底。
            escaped_text = text.replace("%", "\\%").replace("_", "\\_")
            like = "%" + escaped_text + "%"
            rows = conn.execute(like_sql, (*params, like, like, limit)).fetchall()
            return [
                _decorate(row, None, terms, "like_fallback")
                for row in rows
            ]

    @staticmethod
    def _search_terms(text: str) -> List[str]:
        """Split a query into trigram-usable terms.

        Latin identifiers are kept whole; CJK has no word boundaries, so a
        long CJK run is expanded into sliding 3-grams (each a real trigram
        present in the query) so "数据库" inside a long sentence still hits.
        """
        terms: List[str] = []
        for match in _CJK_RUN.finditer(text):
            token = match.group(0)
            if len(token) < 3:
                continue
            if re.search(r"[\u4e00-\u9fff]", token):
                terms.extend(token[index : index + 3] for index in range(len(token) - 2))
            else:
                terms.append(token)
        return terms

    def list_recent_memories(
        self,
        scope: Optional[str] = None,
        project_key: Optional[str] = None,
        source_agent: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """最近记忆列表（update/forget/search 无参选择器的数据源）。

        Keyset 分页：cursor 由上一页最后一项的 (updated_at, id) 编码；
        返回 {items, next_cursor, has_more}。
        """
        filters = ["m.deleted_at IS NULL"]
        params: List[Any] = []
        if scope:
            filters.append("m.scope = ?")
            params.append(scope)
        if project_key:
            filters.append("(m.scope != 'project' OR m.project_key = ?)")
            params.append(project_key)
        else:
            filters.append("m.scope != 'project'")
        if source_agent:
            filters.append("v.source_agent = ?")
            params.append(source_agent)
        if cursor:
            cursor_updated_at, cursor_id = _decode_cursor(cursor)
            filters.append("(m.updated_at < ? OR (m.updated_at = ? AND m.id < ?))")
            params += [cursor_updated_at, cursor_updated_at, cursor_id]
        sql = (
            self._current_select()
            + " WHERE " + " AND ".join(filters)
            + " ORDER BY m.updated_at DESC, m.id DESC LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, (*params, limit + 1)).fetchall()
            has_more = len(rows) > limit
            items = [self._row_to_memory(row) for row in rows[:limit]]
            next_cursor = None
            if has_more and items:
                last = items[-1]
                next_cursor = _encode_cursor(last["updated_at"], last["id"])
            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def changes_since(
        self, cursor: int, project_key: Optional[str], limit: int = 100
    ) -> Dict[str, Any]:
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
            rows = conn.execute(sql, (*params, limit + 1)).fetchall()
            has_more = len(rows) > limit
            result = []
            for row in rows[:limit]:
                item = self._row_to_memory(row)
                item["change_seq"] = row["seq"]
                item["change_action"] = row["action"]
                item["change_source_agent"] = row["change_source_agent"]
                result.append(item)
            next_cursor = result[-1]["change_seq"] if result else cursor
            return {
                "items": result,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }

    # -- 会话目录 -------------------------------------------------------------

    def upsert_session(
        self,
        agent_id: str,
        session_id: str,
        title: Optional[str],
        updated_at: str,
        project_key: str = "",
        device_id: str = "",
        content_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """幂等上报会话元数据。synced_at 保留（客户端上报不得覆盖上传状态）。

        content_hash 提供时由服务端检测内容指纹变化并自增 content_revision，
        客户端无需自己维护版本号；同步状态由 synced_revision 与
        content_revision 的关系推导（never_synced / synced / changed / failed）。
        """
        agent_id = agent_id.strip()[:128]
        session_id = session_id.strip()[:255]
        project_key = project_key.strip()[:255]
        device_id = device_id.strip()[:128]
        if not agent_id or not session_id:
            raise ValidationError("agent_id and session_id are required")
        with self.write_transaction() as conn:
            existing = conn.execute(
                "SELECT content_hash, updated_at, synced_at, created_at FROM sessions "
                "WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                (project_key, agent_id, device_id, session_id),
            ).fetchone()
            # Session reports can arrive out of order after reconnect. ISO-8601
            # UTC timestamps sort chronologically, so an older report must not
            # roll the directory back or manufacture a false new revision.
            synthetic_sync_row = (
                existing is not None
                and existing["synced_at"] is not None
                and existing["created_at"] == existing["updated_at"] == existing["synced_at"]
            )
            if existing is not None and not synthetic_sync_row and updated_at < existing["updated_at"]:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                    (project_key, agent_id, device_id, session_id),
                ).fetchone()
                return self._row_to_session(row)
            if existing is not None and content_hash and content_hash != existing["content_hash"]:
                # 内容在上传后继续变化：content_revision 自增，同步状态回到 changed。
                conn.execute(
                    "UPDATE sessions SET content_revision = content_revision + 1, content_hash = ?, "
                    "title = ?, updated_at = ? "
                    "WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                    (content_hash, (title or "").strip()[:300] or None, updated_at,
                     project_key, agent_id, device_id, session_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO sessions(project_key, agent_id, device_id, session_id, title, updated_at,
                                         content_revision, content_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_key, agent_id, device_id, session_id) DO UPDATE SET
                      title = excluded.title,
                      updated_at = excluded.updated_at
                    """,
                    (project_key, agent_id, device_id, session_id, (title or "").strip()[:300] or None,
                     updated_at, 1 if content_hash else 0, content_hash, now_iso()),
                )
            row = conn.execute(
                "SELECT * FROM sessions WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                (project_key, agent_id, device_id, session_id),
            ).fetchone()
            return self._row_to_session(row)

    def mark_session_synced(
        self,
        agent_id: str,
        session_id: str,
        project_key: str = "",
        device_id: str = "",
        memory_id: Optional[str] = None,
        content_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """标记会话已上传（提炼入库后调用）；不存在则创建一条已同步记录。"""
        agent_id = agent_id.strip()[:128]
        session_id = session_id.strip()[:255]
        project_key = project_key.strip()[:255]
        device_id = device_id.strip()[:128]
        if not agent_id or not session_id or not project_key:
            raise ValidationError("project_key, agent_id and session_id are required")
        with self.write_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM sessions WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                (project_key, agent_id, device_id, session_id),
            ).fetchone()
            synced_at = now_iso()
            revision = content_revision if content_revision is not None else (int(existing["content_revision"]) if existing else 0)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO sessions(project_key, agent_id, device_id, session_id, title, updated_at,
                                         synced_at, synced_revision, synced_memory_id, created_at)
                    VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (project_key, agent_id, device_id, session_id, synced_at, synced_at, revision, memory_id, synced_at),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET synced_at = ?, synced_revision = ?, synced_memory_id = ?, "
                    "last_sync_error = NULL "
                    "WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                    (synced_at, revision, memory_id, project_key, agent_id, device_id, session_id),
                )
            row = conn.execute(
                "SELECT * FROM sessions WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                (project_key, agent_id, device_id, session_id),
            ).fetchone()
            return self._row_to_session(row)

    def complete_session_sync(
        self,
        agent_id: str,
        session_id: str,
        session_identity: Dict[str, Any],
        memory_payload: Dict[str, Any],
        op_key: str,
        req_hash: str,
    ) -> Tuple[int, Dict[str, Any]]:
        """复合原子操作：同一事务内写入（或去重命中）记忆并标记会话同步。

        与「先 POST /v1/memories 再 POST synced」的两步流程不同，这一步要么
        整体成功、要么整体回滚，离线队列重放不会出现「记忆已存但会话未标记」
        的中间态，也不会重复生成摘要。
        """
        project_key = session_identity.get("project_key", "")
        device_id = session_identity.get("device_id", "")
        content_hash_value = session_identity.get("content_hash")
        agent_id = agent_id.strip()[:128]
        session_id = session_id.strip()[:255]
        project_key = str(project_key).strip()[:255]
        device_id = str(device_id).strip()[:128]
        if not agent_id or not session_id or not project_key:
            raise ValidationError("project_key, agent_id and session_id are required")
        self._validate_scope(memory_payload["scope"], memory_payload.get("project_key"))
        if memory_payload["scope"] != "project":
            raise ValidationError("session sync memory must use scope=project")
        if memory_payload.get("project_key") != project_key:
            raise ValidationError("memory.project_key must match session project_key")
        source_session_id = memory_payload.get("source_session_id")
        if source_session_id not in (None, session_id):
            raise ValidationError("memory.source_session_id must match session path")
        memory_payload = dict(memory_payload)
        memory_payload["source_session_id"] = session_id

        def operation(conn: sqlite3.Connection) -> Tuple[int, Dict[str, Any]]:
            timestamp = now_iso()
            existing = conn.execute(
                "SELECT content_revision, synced_revision, content_hash, synced_memory_id FROM sessions "
                "WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                (project_key, agent_id, device_id, session_id),
            ).fetchone()
            if (
                existing is not None
                and existing["content_hash"]
                and content_hash_value
                and content_hash_value != existing["content_hash"]
            ):
                raise ConflictError(
                    "session content changed while it was being synchronized; refresh and retry"
                )
            linked_memory_id = existing["synced_memory_id"] if existing is not None else None
            linked_memory = None
            if linked_memory_id:
                linked_memory = conn.execute(
                    BrainStore._current_select() + " WHERE m.id = ? AND m.deleted_at IS NULL",
                    (linked_memory_id,),
                ).fetchone()
            if linked_memory is not None:
                unchanged = (
                    existing is not None
                    and existing["content_hash"] == content_hash_value
                    and int(existing["synced_revision"]) >= int(existing["content_revision"])
                    and linked_memory["title"] == memory_payload["title"].strip()
                    and linked_memory["content_text"] == memory_payload["content_text"].strip()
                    and linked_memory["kind"] == memory_payload["kind"]
                    and int(linked_memory["trust_level"]) == int(memory_payload.get("trust_level", 0))
                )
                if unchanged:
                    status = 200
                    memory_result = BrainStore._row_to_memory(linked_memory)
                    memory_result["deduplicated"] = True
                else:
                    status, memory_result = BrainStore._append_memory_version_op(
                        conn,
                        linked_memory_id,
                        memory_payload,
                        timestamp,
                    )
            else:
                # First sync, or the previously linked memory was tombstoned:
                # create a new durable record and relink the session. Session
                # memories never content-dedupe across identities/devices,
                # because every linked record is independently mutable.
                status, memory_result = BrainStore._insert_memory_op(
                    conn,
                    memory_payload,
                    timestamp,
                    deduplicate=False,
                )

            content_revision = int(existing["content_revision"]) if existing is not None else 0
            if content_hash_value and (existing is None or content_hash_value != existing["content_hash"]):
                content_revision += 1
            if existing is None:
                conn.execute(
                    "INSERT INTO sessions(project_key, agent_id, device_id, session_id, title, updated_at, "
                    "synced_at, content_revision, content_hash, synced_revision, synced_memory_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_key, agent_id, device_id, session_id,
                     (memory_payload.get("title") or "").strip()[:300] or None, timestamp, timestamp,
                     content_revision, content_hash_value, content_revision, memory_result["id"], timestamp),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET content_revision = ?, content_hash = ?, "
                    "synced_at = ?, synced_revision = ?, synced_memory_id = ?, last_sync_error = NULL "
                    "WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                    (content_revision, content_hash_value, timestamp, content_revision, memory_result["id"],
                     project_key, agent_id, device_id, session_id),
                )
            row = conn.execute(
                "SELECT * FROM sessions WHERE project_key = ? AND agent_id = ? AND device_id = ? AND session_id = ?",
                (project_key, agent_id, device_id, session_id),
            ).fetchone()
            session_result = self._row_to_session(row)
            session_result["sync_status"] = "synced"
            session_result["deduplicated"] = memory_result.get("deduplicated", False)
            result = {"memory": memory_result, "session": session_result, "sync_status": "synced"}
            return status, result

        return self._idempotent(op_key, req_hash, operation)

    def list_sessions(
        self,
        project_key: str = "",
        agent_id: Optional[str] = None,
        synced: Optional[bool] = None,
        device_id: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """项目隔离的会话目录：project_key 是强制过滤维度。

        Keyset 分页：返回 {items, next_cursor, has_more}。
        """
        filters = ["project_key = ?"]
        params: List[Any] = [project_key]
        if agent_id:
            filters.append("agent_id = ?")
            params.append(agent_id)
        if device_id is not None:
            filters.append("device_id = ?")
            params.append(device_id)
        if synced is not None:
            pending = "(synced_at IS NULL OR synced_revision < content_revision OR last_sync_error IS NOT NULL)"
            filters.append("NOT " + pending if synced else pending)
        if cursor:
            cursor_updated_at, cursor_agent_id, cursor_device_id, cursor_session_id = _decode_cursor(
                cursor, expected_parts=4
            )
            filters.append("(updated_at, agent_id, device_id, session_id) < (?, ?, ?, ?)")
            params += [cursor_updated_at, cursor_agent_id, cursor_device_id, cursor_session_id]
        where = " WHERE " + " AND ".join(filters)
        sql = (
            "SELECT * FROM sessions" + where
            + " ORDER BY updated_at DESC, agent_id DESC, device_id DESC, session_id DESC LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, (*params, limit + 1)).fetchall()
            has_more = len(rows) > limit
            items = [self._row_to_session(row) for row in rows[:limit]]
            next_cursor = None
            if has_more and items:
                last = items[-1]
                next_cursor = _encode_cursor(
                    last["updated_at"], last["agent_id"], last["device_id"], last["session_id"]
                )
            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def list_agents(
        self, project_key: str = "", device_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """distinct agent + 该 agent 的会话数与未上传数（remember 第一级选择用，项目隔离）。

        device_id 传入时只统计该设备上报的会话（上传选择器只看本机可读会话）。
        """
        filters = ["project_key = ?"]
        params: List[Any] = [project_key]
        if device_id is not None:
            filters.append("device_id = ?")
            params.append(device_id)
        sql = (
            "SELECT agent_id, COUNT(*) AS total_count, "
            "SUM(CASE WHEN synced_at IS NULL OR synced_revision < content_revision "
            "OR last_sync_error IS NOT NULL THEN 1 ELSE 0 END) AS unsynced_count "
            "FROM sessions WHERE "
            + " AND ".join(filters)
            + " GROUP BY agent_id ORDER BY agent_id LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
            return [
                {
                    "agent_id": row["agent_id"],
                    "total_count": int(row["total_count"]),
                    "unsynced_count": int(row["unsynced_count"] or 0),
                }
                for row in rows
            ]

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Dict[str, Any]:
        content_revision = int(row["content_revision"] or 0)
        synced_revision = int(row["synced_revision"] or 0)
        if row["last_sync_error"]:
            sync_status = "failed"
        elif row["synced_at"] is None:
            sync_status = "never_synced"
        elif synced_revision >= content_revision:
            sync_status = "synced"
        else:
            sync_status = "changed"
        return {
            "project_key": row["project_key"],
            "agent_id": row["agent_id"],
            "device_id": row["device_id"],
            "session_id": row["session_id"],
            "title": row["title"],
            "updated_at": row["updated_at"],
            "synced_at": row["synced_at"],
            "created_at": row["created_at"],
            "content_revision": content_revision,
            "synced_revision": synced_revision,
            "content_hash": row["content_hash"],
            "synced_memory_id": row["synced_memory_id"],
            "last_sync_error": row["last_sync_error"],
            "sync_status": sync_status,
        }
