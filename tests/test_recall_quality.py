"""Natural-language recall quality tests.

The server must not treat the full user sentence as an exact FTS phrase:
a natural question like "这个项目现在用的是什么数据库？" must recall
"项目使用 PostgreSQL 16" within the top results, and every result must
carry score / matched_terms / match_strategy so clients can explain why
a memory was recalled.
"""

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
        "source_session_id": None,
        "trust_level": 1,
    }
    value.update(overrides)
    return value


def _seed(api, headers):
    rows = [
        ("数据库版本", "项目使用 PostgreSQL 16，不要降级数据库。"),
        ("部署方式", "部署脚本通过 SSH 上传到 47.107.158.15。"),
        ("mock 服务器", "Mock 服务器使用 better-sqlite3 作为存储。"),
    ]
    for index, (title, content) in enumerate(rows):
        api.post(
            "/v1/memories",
            json=memory_payload(title=title, content_text=content),
            headers=op(headers, f"recall-seed-{index}"),
        )


def _search(api, headers, q, **params):
    return api.get(
        "/v1/memories/search",
        params={"q": q, "project_key": "alpha", **params},
        headers=headers,
    )


def test_natural_question_recalls_in_top5(api, auth_headers):
    _seed(api, auth_headers)
    response = _search(api, auth_headers, "这个项目现在用的是什么数据库？")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert body["items"][0]["title"] == "数据库版本"
    assert body["strategy"] == "or_terms"


def test_results_carry_score_terms_and_strategy(api, auth_headers):
    _seed(api, auth_headers)
    body = _search(api, auth_headers, "这个项目现在用的是什么数据库？").json()
    item = body["items"][0]
    assert isinstance(item["score"], (int, float))
    assert "数据库" in item["matched_terms"]
    assert item["match_strategy"] == "or_terms"


def test_mixed_language_and_identifier_query(api, auth_headers):
    _seed(api, auth_headers)
    body = _search(api, auth_headers, "项目用的数据库是 PostgreSQL 16 吗").json()
    assert body["count"] >= 1
    assert body["items"][0]["title"] == "数据库版本"


def test_short_chinese_query_uses_like_fallback(api, auth_headers):
    _seed(api, auth_headers)
    body = _search(api, auth_headers, "版本").json()
    assert body["count"] >= 1
    assert body["items"][0]["title"] == "数据库版本"
    assert body["strategy"] == "like"


def test_no_match_falls_back_without_error(api, auth_headers):
    _seed(api, auth_headers)
    body = _search(api, auth_headers, "完全不存在的主题词").json()
    assert body["count"] == 0
    assert body["items"] == []
