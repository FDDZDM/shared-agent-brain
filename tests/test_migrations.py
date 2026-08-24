"""Schema versioning and migration tests.

The sessions directory gains project_key + device_id and a composite primary
key; existing databases must upgrade in place without losing their session
catalog rows (they land in the explicit 'legacy' project so isolation is
preserved by default).
"""

from __future__ import annotations

import sqlite3

from shared_brain.db import BrainStore

TOKEN = "test-token-that-is-long-enough-123456"


def test_fresh_database_reaches_latest_schema_version(tmp_path):
    store = BrainStore(str(tmp_path / "brain.db"))
    store.initialize(TOKEN)
    with store.connect() as conn:
        version = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        assert version is not None
        assert int(version["version"]) == 3
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        assert "project_key" in columns
        assert "device_id" in columns
        assert "synced_at" in columns


def test_legacy_sessions_survive_upgrade_into_legacy_project(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
          agent_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          title TEXT,
          updated_at TEXT NOT NULL,
          synced_at TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (agent_id, session_id)
        );
        INSERT INTO sessions VALUES
          ('Mac-Hermes', 'sess-1', '旧会话', '2026-01-01T00:00:00Z', NULL, '2026-01-01T00:00:00Z'),
          ('Mac-DSH', 'sess-2', '旧DSH会话', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    store = BrainStore(path)
    store.initialize(TOKEN)

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT project_key, agent_id, device_id, session_id, title, synced_at "
            "FROM sessions ORDER BY agent_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["project_key"] == "legacy"
        assert rows[0]["agent_id"] == "Mac-DSH"
        assert rows[0]["device_id"] == ""
        assert rows[0]["title"] == "旧DSH会话"
        assert rows[0]["synced_at"] is not None
        assert rows[1]["project_key"] == "legacy"
        assert rows[1]["agent_id"] == "Mac-Hermes"


def test_initialize_is_idempotent_across_restarts(tmp_path):
    path = str(tmp_path / "brain.db")
    first = BrainStore(path)
    first.initialize(TOKEN)
    second = BrainStore(path)
    second.initialize(TOKEN)
    with second.connect() as conn:
        version = int(conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()["version"])
        assert version == 3
        count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        assert count == 0


def test_v2_database_upgrades_to_v3_keeping_sessions(tmp_path):
    """v2 库（sessions 已隔离但无同步状态列）升级到 v3 保留数据。"""
    store = BrainStore(str(tmp_path / "brain.db"))
    store.initialize(TOKEN)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO sessions(project_key, agent_id, device_id, session_id, title, updated_at, created_at) "
            "VALUES ('alpha', 'Mac-Hermes', 'mac-a', 'sess-1', '旧会话', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )

    store.initialize(TOKEN)  # 幂等重跑：v3 迁移对已有列应跳过
    with store.connect() as conn:
        version = int(conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()["version"])
        assert version == 3
        row = conn.execute(
            "SELECT project_key, agent_id, session_id, content_revision, synced_revision "
            "FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["content_revision"] == 0
        assert row["synced_revision"] == 0
