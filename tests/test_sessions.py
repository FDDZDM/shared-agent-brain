"""Session directory API tests: upsert / list / mark-synced / agents."""

from __future__ import annotations

from conftest import auth_headers, op  # noqa: F401  (fixtures re-exported)


def _upsert(client, headers, agent, session, title, updated_at):
    return client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent,
            "session_id": session,
            "title": title,
            "updated_at": updated_at,
        },
    )


def test_sessions_require_auth(api):
    response = api.get("/v1/sessions")
    assert response.status_code == 401
    response = api.post("/v1/sessions", json={"agent_id": "a", "session_id": "s"})
    assert response.status_code == 401


def test_upsert_and_list(api, auth_headers):
    r = _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "会话甲", "2026-08-21T10:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "Mac-Hermes"
    assert body["synced_at"] is None

    _upsert(api, auth_headers, "Mac-Hermes", "sess-2", "会话乙", "2026-08-21T11:00:00Z")
    _upsert(api, auth_headers, "Mac-DSH", "sess-3", "会话丙", "2026-08-21T12:00:00Z")

    r = api.get("/v1/sessions", headers=auth_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    # 按 updated_at 倒序
    assert items[0]["session_id"] == "sess-3"

    # 按 agent 过滤
    r = api.get("/v1/sessions", headers=auth_headers, params={"agent": "Mac-Hermes"})
    assert [i["session_id"] for i in r.json()["items"]] == ["sess-2", "sess-1"]

    # 按未上传过滤
    r = api.get("/v1/sessions", headers=auth_headers, params={"synced": "false"})
    assert r.json()["count"] == 3
    r = api.get("/v1/sessions", headers=auth_headers, params={"synced": "true"})
    assert r.json()["count"] == 0


def test_upsert_is_idempotent_and_keeps_synced(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "旧标题", "2026-08-21T10:00:00Z")
    r = api.post(
        "/v1/sessions/Mac-Hermes/sess-1/synced", headers=auth_headers
    )
    assert r.status_code == 200
    assert r.json()["synced_at"] is not None

    # 再次上报（新标题/新时间）不得清掉 synced 状态
    r = _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "新标题", "2026-08-21T11:00:00Z")
    assert r.json()["title"] == "新标题"
    assert r.json()["synced_at"] is not None

    r = api.get("/v1/sessions", headers=auth_headers, params={"agent": "Mac-Hermes"})
    assert r.json()["count"] == 1


def test_mark_synced_creates_record_if_absent(api, auth_headers):
    r = api.post("/v1/sessions/Mac-DSH/ghost-session/synced", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["synced_at"] is not None
    r = api.get("/v1/sessions", headers=auth_headers, params={"synced": "true"})
    assert r.json()["count"] == 1


def test_list_agents_with_counts(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "s1", "t1", "2026-08-21T10:00:00Z")
    _upsert(api, auth_headers, "Mac-Hermes", "s2", "t2", "2026-08-21T11:00:00Z")
    _upsert(api, auth_headers, "Mac-DSH", "s3", "t3", "2026-08-21T12:00:00Z")
    api.post("/v1/sessions/Mac-DSH/s3/synced", headers=auth_headers)

    r = api.get("/v1/sessions/agents", headers=auth_headers)
    assert r.status_code == 200
    agents = {a["agent_id"]: a for a in r.json()["items"]}
    assert agents["Mac-Hermes"]["total_count"] == 2
    assert agents["Mac-Hermes"]["unsynced_count"] == 2
    assert agents["Mac-DSH"]["total_count"] == 1
    assert agents["Mac-DSH"]["unsynced_count"] == 0


def test_validation(api, auth_headers):
    r = api.post("/v1/sessions", headers=auth_headers, json={"agent_id": "", "session_id": "s"})
    assert r.status_code == 422
    r = _upsert(api, auth_headers, "   ", "s", "t", "2026-08-21T10:00:00Z")
    assert r.status_code == 422  # agent_id 空白 → 数据库层校验（400 或 422）
