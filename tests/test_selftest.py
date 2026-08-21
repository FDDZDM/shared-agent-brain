"""Unit tests for the self-test suite (no live server needed)."""

from __future__ import annotations

import unittest.mock as mock

from shared_brain.client import BrainClientError
from shared_brain.selftest import SELFTEST_PROJECT, render_test_report, run_selftest


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json


class FakeBareClient:
    """Stands in for the unauthenticated httpx.Client used by T1/T2/T9."""

    def __init__(self) -> None:
        self.health_status = 200
        self.search_status = 401
        self.post_calls: list = []

    def get(self, url: str, **kwargs):
        if url.endswith("/health"):
            return FakeResponse(self.health_status, {"status": "ok"})
        if "/search" in url:
            return FakeResponse(self.search_status)
        return FakeResponse(self.health_status)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        # Idempotent server: same payload always yields the same record.
        return FakeResponse(201, {"id": "idem-1", "current_version": 1})

    def close(self) -> None:
        pass


class FakeClient:
    """Duck-typed stand-in for SharedBrainClient with project isolation."""

    def __init__(self) -> None:
        self.server_url = "http://brain.test"
        self.token = "tok"
        self.project_key = "alpha"
        self.agent_id = "hermes-test"
        self.memories: list[dict] = []
        self._next_id = 0

    def remember(self, title, content_text, scope="project", kind="fact", project_key=None, **kwargs):
        self._next_id += 1
        record = {
            "id": f"m{self._next_id}",
            "current_version": 1,
            "title": title,
            "content_text": content_text,
            "_project_key": project_key or self.project_key,
        }
        self.memories.append(record)
        return record

    def search(self, query, project_key=None, limit=10, **kwargs):
        pk = project_key or self.project_key
        return [m for m in self.memories if m["_project_key"] == pk and query in m["content_text"]]

    def update(self, memory_id, expected_version, **kwargs):
        for record in self.memories:
            if record["id"] == memory_id:
                if expected_version != record["current_version"]:
                    raise BrainClientError("409: version conflict")
                record["current_version"] += 1
                return record
        raise BrainClientError("404: not found")

    def forget(self, memory_id, expected_version, **kwargs):
        for record in self.memories:
            if record["id"] == memory_id:
                if expected_version != record["current_version"]:
                    raise BrainClientError("409: version conflict")
                self.memories.remove(record)
                return {"id": memory_id, "current_version": expected_version}
        raise BrainClientError("404: not found")


def _run(fake_bare: FakeBareClient, fake_client: FakeClient, **kwargs) -> dict:
    with mock.patch("shared_brain.selftest.httpx.Client", return_value=fake_bare):
        return run_selftest(fake_client, **kwargs)  # type: ignore[arg-type]  # duck-typed stub


def test_full_selftest_all_pass() -> None:
    report = _run(FakeBareClient(), FakeClient())
    assert report["passed"] is True
    ids = [r["id"] for r in report["results"]]
    assert ids == [f"T{i}" for i in range(1, 13)]
    assert all(r["status"] == "pass" for r in report["results"])


def test_full_selftest_leaves_no_test_data_behind() -> None:
    fake_client = FakeClient()
    _run(FakeBareClient(), fake_client)
    assert all(m["_project_key"] != SELFTEST_PROJECT for m in fake_client.memories)
    assert fake_client.memories == []


def test_quick_selftest_runs_first_five_plus_cleanup() -> None:
    report = _run(FakeBareClient(), FakeClient(), quick=True)
    ids = [r["id"] for r in report["results"]]
    # quick = 连通/读写 5 项 + 清理（quick 也会创建测试数据，必须一并清理）。
    assert ids == ["T1", "T2", "T3", "T4", "T5", "T12"]
    assert report["mode"] == "quick"
    assert report["passed"] is True


def test_server_down_aborts_and_skips_rest() -> None:
    fake_bare = FakeBareClient()
    fake_bare.health_status = 500
    report = _run(fake_bare, FakeClient())
    assert report["passed"] is False
    assert report["results"][0]["status"] == "fail"
    assert all(r["status"] == "skip" for r in report["results"][1:])


def test_missing_config_aborts_data_group() -> None:
    fake_client = FakeClient()
    fake_client.project_key = ""
    report = _run(FakeBareClient(), fake_client)
    t3 = next(r for r in report["results"] if r["id"] == "T3")
    assert t3["status"] == "fail"
    assert "project_key" in t3["detail"]
    assert all(r["status"] == "skip" for r in report["results"] if r["id"] not in ("T1", "T2", "T3"))


def test_report_render_contains_verdict() -> None:
    report = _run(FakeBareClient(), FakeClient())
    text = render_test_report(report)
    assert "🧠 Shared Brain 自检报告" in text
    assert "✅" in text
    assert "12 通过 / 0 失败 / 0 跳过" in text
    assert "✅ PASS" in text
