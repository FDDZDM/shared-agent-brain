from __future__ import annotations

from conftest import op


def memory_payload(**overrides):
    value = {
        "scope": "project",
        "kind": "fact",
        "project_key": "alpha",
        "title": "数据库版本",
        "content_text": "项目使用 PostgreSQL 16，不要降级数据库。",
        "source_agent": "hermes",
        "source_session_id": "h-1",
        "trust_level": 1,
    }
    value.update(overrides)
    return value


def test_authentication_is_required(api):
    response = api.get("/v1/memories/search", params={"q": "数据库"})
    assert response.status_code == 401


def test_create_search_and_project_isolation(api, auth_headers):
    created = api.post("/v1/memories", json=memory_payload(), headers=op(auth_headers, "create-alpha"))
    assert created.status_code == 201
    assert created.json()["current_version"] == 1

    found = api.get(
        "/v1/memories/search",
        params={"q": "PostgreSQL 16", "project_key": "alpha"},
        headers=auth_headers,
    )
    assert found.status_code == 200
    assert [item["id"] for item in found.json()["items"]] == [created.json()["id"]]

    chinese = api.get(
        "/v1/memories/search",
        params={"q": "数据库", "project_key": "alpha"},
        headers=auth_headers,
    )
    assert chinese.json()["count"] == 1

    isolated = api.get(
        "/v1/memories/search",
        params={"q": "数据库", "project_key": "beta"},
        headers=auth_headers,
    )
    assert isolated.json()["count"] == 0


def test_idempotency_and_duplicate_content(api, auth_headers):
    headers = op(auth_headers, "same-operation")
    first = api.post("/v1/memories", json=memory_payload(), headers=headers)
    replay = api.post("/v1/memories", json=memory_payload(), headers=headers)
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()

    collision = api.post(
        "/v1/memories",
        json=memory_payload(title="Different"),
        headers=headers,
    )
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "CONFLICT"

    duplicate = api.post(
        "/v1/memories",
        json=memory_payload(),
        headers=op(auth_headers, "different-operation"),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["id"] == first.json()["id"]


def test_optimistic_versions_and_tombstone_changes(api, auth_headers):
    created = api.post(
        "/v1/memories",
        json=memory_payload(),
        headers=op(auth_headers, "create-versioned"),
    ).json()
    memory_id = created["id"]

    updated = api.post(
        f"/v1/memories/{memory_id}/versions",
        json={
            "expected_version": 1,
            "content_text": "项目使用 PostgreSQL 17。",
            "source_agent": "deepseek-harness",
        },
        headers=op(auth_headers, "update-v2"),
    )
    assert updated.status_code == 200
    assert updated.json()["current_version"] == 2
    assert updated.json()["source_agent"] == "deepseek-harness"

    stale = api.post(
        f"/v1/memories/{memory_id}/versions",
        json={"expected_version": 1, "title": "stale", "source_agent": "hermes"},
        headers=op(auth_headers, "stale-update"),
    )
    assert stale.status_code == 409

    versions = api.get(f"/v1/memories/{memory_id}/versions", headers=auth_headers).json()["items"]
    assert [row["version"] for row in versions] == [2, 1]
    assert versions[0]["supersedes_id"] == versions[1]["version_id"]

    deleted = api.request(
        "DELETE",
        f"/v1/memories/{memory_id}",
        json={"expected_version": 2, "source_agent": "deepseek-harness"},
        headers=op(auth_headers, "delete-versioned"),
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"] is not None
    assert api.get(f"/v1/memories/{memory_id}", headers=auth_headers).status_code == 404

    changes = api.get(
        "/v1/memories/changes",
        params={"cursor": 0, "project_key": "alpha"},
        headers=auth_headers,
    ).json()
    assert [item["change_action"] for item in changes["items"]] == ["create", "update", "delete"]
    assert changes["items"][-1]["deleted_at"] is not None
    assert changes["next_cursor"] == 3

    no_replay = api.get(
        "/v1/memories/changes",
        params={"cursor": changes["next_cursor"], "project_key": "alpha"},
        headers=auth_headers,
    ).json()
    assert no_replay["items"] == []


def test_project_key_validation(api, auth_headers):
    response = api.post(
        "/v1/memories",
        json=memory_payload(project_key=None),
        headers=op(auth_headers, "missing-project"),
    )
    assert response.status_code == 422


def test_update_requires_a_mutable_field(api, auth_headers):
    created = api.post(
        "/v1/memories",
        json=memory_payload(),
        headers=op(auth_headers, "create-for-empty-update"),
    ).json()
    response = api.post(
        f"/v1/memories/{created['id']}/versions",
        json={"expected_version": 1, "source_agent": "deepseek-harness"},
        headers=op(auth_headers, "empty-update"),
    )
    assert response.status_code == 422


def test_whitespace_only_memory_is_rejected(api, auth_headers):
    response = api.post(
        "/v1/memories",
        json=memory_payload(title="   ", content_text="\n\t"),
        headers=op(auth_headers, "whitespace-memory"),
    )
    assert response.status_code == 422
