"""Cursor pagination protocol tests: memories, sessions, and change log.

List endpoints return {items, count, next_cursor, has_more}; pages are
keyed by the last item (updated_at + id), so no offset drift occurs when
new rows arrive between pages.
"""

from __future__ import annotations

from conftest import auth_headers, op  # noqa: F401


def memory_payload(**overrides):
    value = {
        "scope": "project",
        "kind": "fact",
        "project_key": "alpha",
        "title": "分页记忆",
        "content_text": "分页测试内容",
        "source_agent": "hermes",
        "source_session_id": None,
        "trust_level": 1,
    }
    value.update(overrides)
    return value


def _seed_memories(api, headers, count=5):
    for index in range(count):
        api.post(
            "/v1/memories",
            json=memory_payload(title=f"记忆{index}", content_text=f"内容{index}"),
            headers=op(headers, f"page-mem-{index}"),
        )


def _page(api, headers, **params):
    return api.get("/v1/memories", headers=headers, params=params).json()


def test_memories_paginate_without_overlap_or_gaps(api, auth_headers):
    _seed_memories(api, auth_headers, 5)

    first = _page(api, auth_headers, project_key="alpha", limit=2)
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = _page(api, auth_headers, project_key="alpha", limit=2, cursor=first["next_cursor"])
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(second["items"]) == 2
    assert second["has_more"] is True

    third = _page(api, auth_headers, project_key="alpha", limit=2, cursor=second["next_cursor"])
    assert len(third["items"]) == 1
    assert third["has_more"] is False
    assert third["next_cursor"] is None
    third_ids = {item["id"] for item in third["items"]}
    assert third_ids.isdisjoint(first_ids | second_ids)

    # 全部 5 条不重不漏
    total = len(first_ids | second_ids | third_ids)
    assert total == 5


def test_memories_pagination_respects_project_isolation(api, auth_headers):
    _seed_memories(api, auth_headers, 3)
    beta = api.get(
        "/v1/memories", headers=auth_headers, params={"project_key": "beta", "limit": 10}
    ).json()
    assert beta["items"] == []
    assert beta["has_more"] is False


def test_sessions_paginate(api, auth_headers):
    for index in range(4):
        api.post(
            "/v1/sessions",
            headers=auth_headers,
            json={
                "project_key": "alpha",
                "agent_id": "Mac-Hermes",
                "device_id": "mac-a",
                "session_id": f"sess-{index}",
                "title": f"会话{index}",
                "updated_at": f"2026-08-21T1{index}:00:00Z",
            },
        )

    first = api.get(
        "/v1/sessions", headers=auth_headers, params={"project_key": "alpha", "limit": 2}
    ).json()
    assert len(first["items"]) == 2
    assert first["has_more"] is True

    second = api.get(
        "/v1/sessions",
        headers=auth_headers,
        params={"project_key": "alpha", "limit": 2, "cursor": first["next_cursor"]},
    ).json()
    first_ids = {item["session_id"] for item in first["items"]}
    second_ids = {item["session_id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert second["has_more"] is False


def test_sessions_cursor_uses_full_composite_identity(api, auth_headers):
    for agent in ("Hermes", "DSH"):
        api.post(
            "/v1/sessions",
            headers=auth_headers,
            json={
                "project_key": "alpha",
                "agent_id": agent,
                "device_id": "same-device",
                "session_id": "same-session",
                "title": agent,
                "updated_at": "2026-08-21T10:00:00Z",
            },
        )

    first = api.get(
        "/v1/sessions", headers=auth_headers, params={"project_key": "alpha", "limit": 1}
    ).json()
    second = api.get(
        "/v1/sessions",
        headers=auth_headers,
        params={"project_key": "alpha", "limit": 1, "cursor": first["next_cursor"]},
    ).json()
    assert first["has_more"] is True
    assert len(second["items"]) == 1
    assert first["items"][0]["agent_id"] != second["items"][0]["agent_id"]


def test_invalid_cursor_returns_validation_error(api, auth_headers):
    response = api.get(
        "/v1/memories",
        headers=auth_headers,
        params={"project_key": "alpha", "cursor": "%%%not-base64"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_changes_report_has_more(api, auth_headers):
    created = api.post(
        "/v1/memories",
        json=memory_payload(),
        headers=op(auth_headers, "page-change-1"),
    ).json()
    api.post(
        f"/v1/memories/{created['id']}/versions",
        json={"expected_version": 1, "content_text": "v2 内容", "source_agent": "hermes"},
        headers=op(auth_headers, "page-change-2"),
    )

    first = api.get(
        "/v1/memories/changes",
        headers=auth_headers,
        params={"project_key": "alpha", "cursor": 0, "limit": 1},
    ).json()
    assert len(first["items"]) == 1
    assert first["has_more"] is True

    second = api.get(
        "/v1/memories/changes",
        headers=auth_headers,
        params={"project_key": "alpha", "cursor": first["next_cursor"], "limit": 1},
    ).json()
    assert len(second["items"]) == 1
    assert second["has_more"] is False
