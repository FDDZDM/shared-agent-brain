"""Compound session-sync and revision state machine tests.

POST /v1/sessions/{agent}/{session}/sync writes the memory AND marks the
session synced in one transaction (idempotent replay, no duplicate
summaries); sessions expose a sync_status of never_synced / synced / changed
driven by content_revision vs synced_revision.
"""

from __future__ import annotations

from conftest import auth_headers, op  # noqa: F401


def _upsert(client, headers, agent, session, title, updated_at, content_hash=None):
    return client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "project_key": "alpha",
            "agent_id": agent,
            "device_id": "mac-a",
            "session_id": session,
            "title": title,
            "updated_at": updated_at,
            "content_hash": content_hash,
        },
    )


def _sync_body(**overrides):
    memory = {
        "scope": "project",
        "kind": "fact",
        "project_key": "alpha",
        "title": "数据库版本",
        "content_text": "项目使用 PostgreSQL 16。",
        "source_agent": "hermes",
        "source_session_id": "sess-1",
        "trust_level": 1,
    }
    value = {
        "project_key": "alpha",
        "device_id": "mac-a",
        "content_hash": "h1",
        "memory": memory,
    }
    value.update(overrides)
    return value


def test_compound_sync_writes_memory_and_marks_session(api, auth_headers):
    response = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-test-1"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["sync_status"] == "synced"
    memory_id = body["memory"]["id"]
    assert body["session"]["synced_memory_id"] == memory_id
    assert body["session"]["content_revision"] == 1
    assert body["session"]["synced_revision"] == 1

    # 记忆立即可检索
    found = api.get(
        "/v1/memories/search",
        params={"q": "PostgreSQL", "project_key": "alpha"},
        headers=auth_headers,
    ).json()
    assert found["count"] == 1
    assert found["items"][0]["id"] == memory_id


def test_compound_sync_replay_is_idempotent(api, auth_headers):
    first = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-replay-1"),
    ).json()
    replay = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-replay-1"),
    )
    assert replay.status_code == first.get("_status", 201) or replay.status_code == 201
    assert replay.json()["memory"]["id"] == first["memory"]["id"]
    # 未重复生成记忆
    found = api.get(
        "/v1/memories/search",
        params={"q": "PostgreSQL", "project_key": "alpha"},
        headers=auth_headers,
    ).json()
    assert found["count"] == 1


def test_same_sync_with_a_new_operation_key_is_a_business_noop(api, auth_headers):
    first = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-business-noop-1"),
    ).json()
    second = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-business-noop-2"),
    )
    assert second.status_code == 200
    assert second.json()["memory"]["id"] == first["memory"]["id"]
    assert second.json()["memory"]["current_version"] == 1
    assert second.json()["memory"]["deduplicated"] is True


def test_changed_session_sync_updates_the_linked_memory_version(api, auth_headers):
    first = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-version-1"),
    )
    assert first.status_code == 201
    first_memory = first.json()["memory"]

    _upsert(
        api,
        auth_headers,
        "Mac-Hermes",
        "sess-1",
        "数据库版本",
        "2026-08-21T11:00:00Z",
        content_hash="h2",
    )
    changed = api.get(
        "/v1/sessions",
        headers=auth_headers,
        params={"project_key": "alpha", "synced": "false"},
    ).json()["items"]
    assert changed[0]["sync_status"] == "changed"

    second_body = _sync_body(content_hash="h2")
    second_body["memory"]["content_text"] = "项目已升级到 PostgreSQL 17。"
    second = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=second_body,
        headers=op(auth_headers, "sync-version-2"),
    )
    assert second.status_code == 200
    result = second.json()
    assert result["memory"]["id"] == first_memory["id"]
    assert result["memory"]["current_version"] == 2
    assert result["session"]["synced_memory_id"] == first_memory["id"]
    assert result["session"]["sync_status"] == "synced"

    versions = api.get(
        f"/v1/memories/{first_memory['id']}/versions",
        headers=auth_headers,
    ).json()["items"]
    assert [item["version"] for item in versions] == [2, 1]

    memories = api.get(
        "/v1/memories",
        headers=auth_headers,
        params={"project_key": "alpha"},
    ).json()["items"]
    assert len(memories) == 1


def test_equal_summaries_from_different_sessions_do_not_share_mutable_memory(api, auth_headers):
    first = api.post(
        "/v1/sessions/hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-distinct-session-1"),
    ).json()
    second_body = _sync_body()
    second_body["memory"]["source_session_id"] = "sess-2"
    second = api.post(
        "/v1/sessions/hermes/sess-2/sync",
        json=second_body,
        headers=op(auth_headers, "sync-distinct-session-2"),
    ).json()

    assert first["memory"]["id"] != second["memory"]["id"]

    _upsert(api, auth_headers, "hermes", "sess-2", "数据库版本", "2026-08-21T11:00:00Z", "h2")
    second_body["content_hash"] = "h2"
    second_body["memory"]["content_text"] = "仅第二个会话升级到 PostgreSQL 17。"
    updated = api.post(
        "/v1/sessions/hermes/sess-2/sync",
        json=second_body,
        headers=op(auth_headers, "sync-distinct-session-3"),
    )
    assert updated.status_code == 200
    unchanged = api.get(f"/v1/memories/{first['memory']['id']}", headers=auth_headers).json()
    assert unchanged["current_version"] == 1
    assert unchanged["content_text"] == "项目使用 PostgreSQL 16。"


def test_same_session_identifier_on_two_devices_has_independent_memory(api, auth_headers):
    first = api.post(
        "/v1/sessions/hermes/same-session/sync",
        json=_sync_body(device_id="mac-a", memory={**_sync_body()["memory"], "source_session_id": "same-session"}),
        headers=op(auth_headers, "sync-device-isolation-1"),
    ).json()
    second = api.post(
        "/v1/sessions/hermes/same-session/sync",
        json=_sync_body(device_id="mac-b", memory={**_sync_body()["memory"], "source_session_id": "same-session"}),
        headers=op(auth_headers, "sync-device-isolation-2"),
    ).json()
    assert first["memory"]["id"] != second["memory"]["id"]


def test_stale_session_sync_cannot_overwrite_newer_directory_state(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T10:00:00Z", "h2")
    stale = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(content_hash="h1"),
        headers=op(auth_headers, "sync-stale-content-1"),
    )
    assert stale.status_code == 409
    session = api.get(
        "/v1/sessions", headers=auth_headers, params={"project_key": "alpha"}
    ).json()["items"][0]
    assert session["content_hash"] == "h2"
    assert session["sync_status"] == "never_synced"


def test_deleting_linked_memory_makes_session_selectable_again(api, auth_headers):
    synced = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=_sync_body(),
        headers=op(auth_headers, "sync-delete-link-1"),
    ).json()
    deleted = api.request(
        "DELETE",
        f"/v1/memories/{synced['memory']['id']}",
        json={"expected_version": 1, "source_agent": "hermes"},
        headers=op(auth_headers, "sync-delete-link-2"),
    )
    assert deleted.status_code == 200
    pending = api.get(
        "/v1/sessions",
        headers=auth_headers,
        params={"project_key": "alpha", "synced": "false"},
    ).json()["items"]
    assert pending[0]["session_id"] == "sess-1"
    assert pending[0]["synced_memory_id"] is None
    assert pending[0]["sync_status"] == "never_synced"


def test_sync_state_machine(api, auth_headers):
    # 上报内容指纹但未同步 → never_synced
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T10:00:00Z", content_hash="h1")
    items = api.get("/v1/sessions", headers=auth_headers, params={"project_key": "alpha"}).json()["items"]
    assert items[0]["sync_status"] == "never_synced"
    assert items[0]["content_revision"] == 1
    assert items[0]["synced_revision"] == 0

    # 标记同步 → synced
    marked = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/synced",
        headers=auth_headers,
        params={"project_key": "alpha", "device_id": "mac-a", "memory_id": "m1"},
    ).json()
    assert marked["sync_status"] == "synced"
    assert marked["synced_revision"] == 1
    assert marked["synced_memory_id"] == "m1"

    # 内容在上传后继续变化 → changed
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T11:00:00Z", content_hash="h2")
    items = api.get("/v1/sessions", headers=auth_headers, params={"project_key": "alpha"}).json()["items"]
    assert items[0]["sync_status"] == "changed"
    assert items[0]["content_revision"] == 2

    # 再次同步 → 回到 synced
    marked = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/synced",
        headers=auth_headers,
        params={"project_key": "alpha", "device_id": "mac-a", "memory_id": "m2"},
    ).json()
    assert marked["sync_status"] == "synced"
    assert marked["synced_revision"] == 2


def test_sync_does_not_mark_session_when_memory_invalid(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T10:00:00Z", content_hash="h0")
    bad = _sync_body()
    bad["memory"]["title"] = "   "
    response = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=bad,
        headers=op(auth_headers, "sync-bad-1"),
    )
    assert response.status_code == 422
    items = api.get("/v1/sessions", headers=auth_headers, params={"project_key": "alpha"}).json()["items"]
    assert items[0]["sync_status"] == "never_synced"
    assert items[0]["synced_revision"] == 0


def test_sync_rejects_cross_project_and_missing_inner_project(api, auth_headers):
    cross_project = _sync_body()
    cross_project["memory"]["project_key"] = "beta"
    response = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=cross_project,
        headers=op(auth_headers, "sync-cross-project"),
    )
    assert response.status_code == 422

    missing_project = _sync_body()
    missing_project["memory"]["project_key"] = None
    response = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/sync",
        json=missing_project,
        headers=op(auth_headers, "sync-missing-project"),
    )
    assert response.status_code == 422


def test_changed_session_returns_to_pending_lists_and_agent_count(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T10:00:00Z", "h1")
    api.post(
        "/v1/sessions/Mac-Hermes/sess-1/synced",
        headers=auth_headers,
        params={"project_key": "alpha", "device_id": "mac-a"},
    )
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T11:00:00Z", "h2")

    pending = api.get(
        "/v1/sessions",
        headers=auth_headers,
        params={"project_key": "alpha", "synced": "false"},
    ).json()
    assert pending["count"] == 1
    assert pending["items"][0]["sync_status"] == "changed"

    agents = api.get(
        "/v1/sessions/agents", headers=auth_headers, params={"project_key": "alpha"}
    ).json()
    assert agents["items"][0]["unsynced_count"] == 1
