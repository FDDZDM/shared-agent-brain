from __future__ import annotations

import json

import httpx

from shared_brain.client import SharedBrainClient


def test_offline_write_is_queued_and_replayed_with_same_idempotency_key(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    queue_path = str(tmp_path / "queue.db")
    client = SharedBrainClient(
        "https://brain.invalid",
        "token-that-is-long-enough-123456",
        "hermes",
        "alpha",
        queue_path,
        transport=httpx.MockTransport(offline),
    )
    queued = client.remember("Python", "项目使用 Python 3.12")
    assert queued["queued"] is True
    pending = client.queue.list()
    assert len(pending) == 1
    original_key = pending[0]["op_key"]
    client.close()

    observed = []

    def online(request):
        observed.append(request)
        return httpx.Response(
            201,
            json={"id": "m1", "current_version": 1},
            request=request,
        )

    replay = SharedBrainClient(
        "https://brain.invalid",
        "token-that-is-long-enough-123456",
        "hermes",
        "alpha",
        queue_path,
        transport=httpx.MockTransport(online),
    )
    result = replay.flush_queue()
    replay.close()
    assert result == {"sent": 1, "failed": 0, "remaining": 0}
    assert observed[0].headers["idempotency-key"] == original_key
    assert json.loads(observed[0].content)["source_agent"] == "hermes"


def test_search_query_is_capped_at_1000_chars(tmp_path):
    captured = {}

    def handler(request):
        captured["q"] = request.url.params.get("q")
        return httpx.Response(200, json={"items": [], "count": 0}, request=request)

    client = SharedBrainClient(
        "https://brain.invalid",
        "token-that-is-long-enough-123456",
        "hermes",
        "alpha",
        str(tmp_path / "queue.db"),
        transport=httpx.MockTransport(handler),
    )
    try:
        client.search("x" * 5000)
    finally:
        client.close()
    assert captured["q"] == "x" * 1000


def test_compound_session_sync_uses_nested_memory_contract(tmp_path):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"memory": {"id": "m1"}, "session": {"sync_status": "synced"}},
            request=request,
        )

    client = SharedBrainClient(
        "https://brain.invalid",
        "token-that-is-long-enough-123456",
        "hermes",
        "alpha",
        str(tmp_path / "queue.db"),
        transport=httpx.MockTransport(handler),
        device_id="mac-a",
    )
    try:
        client.sync_session(
            "hermes",
            "session-1",
            {
                "scope": "project",
                "kind": "fact",
                "title": "Runtime",
                "content_text": "Python 3.12",
                "source_agent": "hermes",
            },
        )
    finally:
        client.close()

    assert set(captured["body"]) == {"project_key", "device_id", "content_hash", "memory"}
    assert captured["body"]["memory"]["project_key"] == "alpha"
    assert captured["body"]["memory"]["source_session_id"] == "session-1"
