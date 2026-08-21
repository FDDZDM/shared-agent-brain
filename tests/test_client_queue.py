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

