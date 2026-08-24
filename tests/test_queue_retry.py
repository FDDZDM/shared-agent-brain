"""Offline queue retry semantics and CLI queue management tests."""

from __future__ import annotations

import json

import httpx
import pytest

from shared_brain.client import BrainClientError, SharedBrainClient
from shared_brain.queue import OfflineQueue

TOKEN = "token-that-is-long-enough-123456"


def _make_client(queue_path, handler):
    return SharedBrainClient(
        "https://brain.invalid",
        TOKEN,
        "hermes",
        "alpha",
        queue_path,
        transport=httpx.MockTransport(handler),
    )


def test_queue_backoff_grows_and_permanent_failure_after_max_attempts(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.enqueue("POST", "/v1/memories", {"title": "t"})
    op_key = queue.list()[0]["op_key"]

    queue.mark_failed(op_key, "boom", retryable=True, max_attempts=3)
    first = queue.list()[0]
    assert first["status"] == "pending"
    assert first["next_retry_at"] is not None

    queue.mark_failed(op_key, "boom", retryable=True, max_attempts=3)
    second = queue.list()[0]
    assert second["next_retry_at"] > first["next_retry_at"]  # 指数退避递增

    queue.mark_failed(op_key, "boom", retryable=True, max_attempts=3)
    final = queue.list()[0]
    assert final["status"] == "failed"
    assert final["next_retry_at"] is None
    assert queue.count("failed") == 1


def test_non_retryable_error_marks_permanent(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.enqueue("POST", "/v1/memories", {"title": "t"})
    op_key = queue.list()[0]["op_key"]
    queue.mark_failed(op_key, "422 invalid", retryable=False)
    assert queue.list()[0]["status"] == "failed"


def test_retry_and_remove_management(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.enqueue("POST", "/v1/memories", {"title": "t"})
    op_key = queue.list()[0]["op_key"]
    queue.mark_failed(op_key, "x", retryable=False)
    assert queue.list()[0]["status"] == "failed"

    assert queue.retry(op_key) is True
    assert queue.list()[0]["status"] == "pending"
    assert queue.list()[0]["next_retry_at"] is None

    assert queue.remove(op_key) is True
    assert queue.count() == 0
    assert queue.retry(op_key) is False
    assert queue.remove(op_key) is False


def test_flush_409_does_not_block_the_rest_of_the_queue(tmp_path):
    """409 conflict on item 1 must not prevent item 2 from being replayed."""
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.enqueue("POST", "/v1/memories", {"title": "first"}, op_key="op-conflict-1")
    queue.enqueue("POST", "/v1/memories", {"title": "second"}, op_key="op-second-01")

    seen = []

    def handler(request):
        seen.append(request)
        if request.headers.get("idempotency-key") == "op-conflict-1":
            return httpx.Response(409, json={"error": {"code": "CONFLICT"}}, request=request)
        return httpx.Response(201, json={"id": "m2", "current_version": 1}, request=request)

    client = _make_client(str(tmp_path / "queue.db"), handler)
    result = client.flush_queue()
    client.close()

    assert result == {"sent": 1, "failed": 1, "remaining": 1}
    # 第二个任务成功发出；冲突任务仍在队列中（可重试，未永久失败）
    assert len(seen) == 2
    remaining = queue.list()
    assert remaining[0]["op_key"] == "op-conflict-1"
    assert remaining[0]["status"] == "pending"
    assert remaining[0]["attempts"] == 1


def test_flush_permanent_422_is_skipped_without_stopping(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.enqueue("POST", "/v1/memories", {"title": "bad"}, op_key="op-bad-422-01")
    queue.enqueue("POST", "/v1/memories", {"title": "good"}, op_key="op-good-001")

    seen = []

    def handler(request):
        seen.append(request)
        if request.headers.get("idempotency-key") == "op-bad-422-01":
            return httpx.Response(422, json={"error": {"code": "VALIDATION_ERROR"}}, request=request)
        return httpx.Response(201, json={"id": "m-good", "current_version": 1}, request=request)

    client = _make_client(str(tmp_path / "queue.db"), handler)
    result = client.flush_queue()
    client.close()

    assert result["sent"] == 1
    remaining = queue.list()
    assert remaining[0]["op_key"] == "op-bad-422-01"
    assert remaining[0]["status"] == "failed"  # 不可重试 → 永久失败
    assert seen[1].headers["idempotency-key"] == "op-good-001"


def test_flush_network_error_keeps_fifo_order(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.enqueue("POST", "/v1/memories", {"title": "a"}, op_key="op-a-0000001")
    queue.enqueue("POST", "/v1/memories", {"title": "b"}, op_key="op-b-0000001")

    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("offline", request=request)

    client = _make_client(str(tmp_path / "queue.db"), handler)
    result = client.flush_queue()
    client.close()

    assert result == {"sent": 0, "failed": 1, "remaining": 2}
    assert len(calls) == 1  # 网络错误立即停止，保持 FIFO
    assert queue.list()[0]["op_key"] == "op-a-0000001"


def test_brain_client_error_carries_status(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))

    def handler(request):
        return httpx.Response(409, json={}, request=request)

    client = _make_client(str(tmp_path / "queue.db"), handler)
    try:
        with pytest.raises(BrainClientError) as exc_info:
            client.remember("t", "c", op_key="op-status-0001")
    finally:
        client.close()
    assert exc_info.value.status == 409
