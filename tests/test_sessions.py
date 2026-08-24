"""Session directory API tests: upsert / list / mark-synced / agents.

All session endpoints are project-scoped: project_key is a required filter on
every read, and the composite identity is
(project_key, agent_id, device_id, session_id). A session uploaded under one
project or device must never appear under another.
"""

from __future__ import annotations

from conftest import auth_headers, op  # noqa: F401  (fixtures re-exported)


def _upsert(client, headers, agent, session, title, updated_at, project="alpha", device="mac-a"):
    return client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "project_key": project,
            "agent_id": agent,
            "device_id": device,
            "session_id": session,
            "title": title,
            "updated_at": updated_at,
        },
    )


def _synced(client, headers, agent, session, project="alpha", device="mac-a"):
    return client.post(
        f"/v1/sessions/{agent}/{session}/synced",
        headers=headers,
        params={"project_key": project, "device_id": device},
    )


def _list(client, headers, **params):
    return client.get("/v1/sessions", headers=headers, params=params)


def test_sessions_require_auth(api):
    response = api.get("/v1/sessions")
    assert response.status_code == 401
    response = api.post("/v1/sessions", json={"agent_id": "a", "session_id": "s"})
    assert response.status_code == 401


def test_upsert_and_list(api, auth_headers):
    r = _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "会话甲", "2026-08-21T10:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["project_key"] == "alpha"
    assert body["agent_id"] == "Mac-Hermes"
    assert body["device_id"] == "mac-a"
    assert body["synced_at"] is None

    _upsert(api, auth_headers, "Mac-Hermes", "sess-2", "会话乙", "2026-08-21T11:00:00Z")
    _upsert(api, auth_headers, "Mac-DSH", "sess-3", "会话丙", "2026-08-21T12:00:00Z")

    r = _list(api, auth_headers, project_key="alpha")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    # 按 updated_at 倒序
    assert items[0]["session_id"] == "sess-3"

    # 按 agent 过滤
    r = _list(api, auth_headers, project_key="alpha", agent="Mac-Hermes")
    assert [i["session_id"] for i in r.json()["items"]] == ["sess-2", "sess-1"]

    # 按未上传过滤
    r = _list(api, auth_headers, project_key="alpha", synced="false")
    assert r.json()["count"] == 3
    r = _list(api, auth_headers, project_key="alpha", synced="true")
    assert r.json()["count"] == 0


def test_upsert_is_idempotent_and_keeps_synced(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "旧标题", "2026-08-21T10:00:00Z")
    r = _synced(api, auth_headers, "Mac-Hermes", "sess-1")
    assert r.status_code == 200
    assert r.json()["synced_at"] is not None

    # 再次上报（新标题/新时间）不得清掉 synced 状态
    r = _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "新标题", "2026-08-21T11:00:00Z")
    assert r.json()["title"] == "新标题"
    assert r.json()["synced_at"] is not None

    r = _list(api, auth_headers, project_key="alpha", agent="Mac-Hermes")
    assert r.json()["count"] == 1


def test_late_session_report_cannot_roll_back_newer_metadata(api, auth_headers):
    newer = {
        "project_key": "alpha", "agent_id": "Mac-Hermes", "device_id": "mac-a",
        "session_id": "sess-1", "title": "新标题",
        "updated_at": "2026-08-21T11:00:00Z", "content_hash": "new-hash",
    }
    older = {**newer, "title": "旧标题", "updated_at": "2026-08-21T10:00:00Z", "content_hash": "old-hash"}
    assert api.post("/v1/sessions", headers=auth_headers, json=newer).status_code == 200
    result = api.post("/v1/sessions", headers=auth_headers, json=older)

    assert result.status_code == 200
    assert result.json()["title"] == "新标题"
    assert result.json()["content_hash"] == "new-hash"
    assert result.json()["content_revision"] == 1


def test_session_timestamp_requires_timezone_and_is_normalized(api, auth_headers):
    invalid = _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21 10:00:00")
    assert invalid.status_code == 422
    valid = _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "标题", "2026-08-21T18:00:00+08:00")
    assert valid.status_code == 200
    assert valid.json()["updated_at"] == "2026-08-21T10:00:00.000000Z"


def test_mark_synced_creates_record_if_absent(api, auth_headers):
    r = _synced(api, auth_headers, "Mac-DSH", "ghost-session")
    assert r.status_code == 200
    assert r.json()["synced_at"] is not None
    r = _list(api, auth_headers, project_key="alpha", synced="true")
    assert r.json()["count"] == 1


def test_list_agents_with_counts(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "s1", "t1", "2026-08-21T10:00:00Z")
    _upsert(api, auth_headers, "Mac-Hermes", "s2", "t2", "2026-08-21T11:00:00Z")
    _upsert(api, auth_headers, "Mac-DSH", "s3", "t3", "2026-08-21T12:00:00Z")
    _synced(api, auth_headers, "Mac-DSH", "s3")

    r = api.get("/v1/sessions/agents", headers=auth_headers, params={"project_key": "alpha"})
    assert r.status_code == 200
    agents = {a["agent_id"]: a for a in r.json()["items"]}
    assert agents["Mac-Hermes"]["total_count"] == 2
    assert agents["Mac-Hermes"]["unsynced_count"] == 2
    assert agents["Mac-DSH"]["total_count"] == 1
    assert agents["Mac-DSH"]["unsynced_count"] == 0


def test_projects_are_isolated_from_each_other(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "alpha 会话", "2026-08-21T10:00:00Z", project="alpha")
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "beta 会话", "2026-08-21T11:00:00Z", project="beta")

    alpha_items = _list(api, auth_headers, project_key="alpha").json()["items"]
    beta_items = _list(api, auth_headers, project_key="beta").json()["items"]
    assert [item["session_id"] for item in alpha_items] == ["sess-1"]
    assert alpha_items[0]["title"] == "alpha 会话"
    assert beta_items[0]["title"] == "beta 会话"

    # agent 统计也按项目隔离
    alpha_agents = api.get(
        "/v1/sessions/agents", headers=auth_headers, params={"project_key": "alpha"}
    ).json()["items"]
    assert alpha_agents[0]["total_count"] == 1


def test_devices_are_isolated_from_each_other(api, auth_headers):
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "本机", "2026-08-21T10:00:00Z", device="mac-a")
    _upsert(api, auth_headers, "Mac-Hermes", "sess-1", "他机", "2026-08-21T11:00:00Z", device="mac-b")

    mine = _list(api, auth_headers, project_key="alpha", device_id="mac-a").json()["items"]
    other = _list(api, auth_headers, project_key="alpha", device_id="mac-b").json()["items"]
    assert [item["title"] for item in mine] == ["本机"]
    assert [item["title"] for item in other] == ["他机"]


def test_sessions_require_project_key(api, auth_headers):
    r = api.get("/v1/sessions", headers=auth_headers)
    assert r.status_code == 422
    r = api.get("/v1/sessions/agents", headers=auth_headers)
    assert r.status_code == 422
    r = api.post("/v1/sessions", headers=auth_headers, json={"agent_id": "a", "session_id": "s"})
    assert r.status_code == 422
    r = api.post("/v1/sessions/Mac-Hermes/s1/synced", headers=auth_headers)
    assert r.status_code == 422


def test_validation(api, auth_headers):
    r = _upsert(api, auth_headers, "", "s", "t", "2026-08-21T10:00:00Z")
    assert r.status_code == 422  # agent_id 空白 → 422（Pydantic min_length）
