"""Hermes and DSH must interoperate against the same live application state."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn

from shared_brain.api import create_app
from shared_brain.client import BrainClientError, SharedBrainClient
from shared_brain.hermes_plugin import SharedBrainMemoryProvider
from shared_brain.selftest import run_selftest

from conftest import TOKEN


ROOT = Path(__file__).resolve().parents[1]
DSH_ROOT = ROOT / "integrations" / "deepseek-harness"
DSH_PROBE = DSH_ROOT / "test" / "interop-probe.mjs"


def _transport(api):
    def handler(request: httpx.Request) -> httpx.Response:
        response = api.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    return httpx.MockTransport(handler)


def _client(api, tmp_path, agent_id):
    return SharedBrainClient(
        "http://shared-brain.test",
        TOKEN,
        agent_id,
        "alpha",
        str(tmp_path / f"{agent_id}.queue.db"),
        transport=_transport(api),
        device_id=f"{agent_id}-device",
    )


@pytest.fixture(scope="module")
def built_dsh_client():
    subprocess.run(
        ["npm", "run", "build"],
        cwd=DSH_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return DSH_PROBE


@pytest.fixture()
def live_brain(tmp_path):
    app = create_app(str(tmp_path / "live-brain.db"), TOKEN)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        thread.join(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("test Shared Brain server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def _run_dsh(probe, server_url, tmp_path, action, **values):
    payload = {
        "serverUrl": server_url,
        "token": TOKEN,
        "queuePath": str(tmp_path / "dsh-interop-queue.json"),
        "action": action,
        **values,
    }
    completed = subprocess.run(
        ["node", str(probe)],
        input=json.dumps(payload),
        cwd=DSH_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_hermes_and_dsh_bidirectional_memory_interop(api, tmp_path):
    hermes = _client(api, tmp_path, "hermes")
    dsh = _client(api, tmp_path, "dsh")
    provider = SharedBrainMemoryProvider()
    provider._client = hermes
    try:
        hermes_result = json.loads(
            provider.handle_tool_call(
                "brain_remember",
                {"title": "Hermes runtime", "content": "Hermes writes Python 3.12"},
                session_id="hermes-session",
            )
        )
        seen_by_dsh = dsh.search("Hermes writes")
        assert seen_by_dsh[0]["id"] == hermes_result["id"]

        dsh_updated = dsh.update(
            hermes_result["id"], hermes_result["current_version"], content_text="DSH updated Python 3.13"
        )
        seen_by_hermes = json.loads(provider.handle_tool_call("brain_search", {"query": "Python 3.13"}))
        assert seen_by_hermes[0]["current_version"] == dsh_updated["current_version"]

        dsh_created = dsh.remember("DSH database", "DSH writes PostgreSQL 16")
        reverse_seen = json.loads(provider.handle_tool_call("brain_search", {"query": "PostgreSQL 16"}))
        assert reverse_seen[0]["id"] == dsh_created["id"]

        json.loads(
            provider.handle_tool_call(
                "brain_forget",
                {"memory_id": dsh_created["id"], "expected_version": dsh_created["current_version"]},
            )
        )
        assert dsh.search("PostgreSQL 16") == []
    finally:
        hermes.close()
        dsh.close()


def test_hermes_and_dsh_concurrent_update_has_one_winner(api, tmp_path):
    hermes = _client(api, tmp_path, "hermes")
    dsh = _client(api, tmp_path, "dsh")
    created = hermes.remember("Shared decision", "version one")

    def update(client, text):
        try:
            return client.update(created["id"], 1, content_text=text)
        except BrainClientError as exc:
            return exc.status

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: update(*item), [(hermes, "Hermes wins"), (dsh, "DSH wins")]))
        assert sorted(409 if result == 409 else 200 for result in results) == [200, 409]
        assert hermes.search("wins")[0]["current_version"] == 2
        assert dsh.search("wins")[0]["current_version"] == 2
    finally:
        hermes.close()
        dsh.close()


def test_hermes_session_sync_is_visible_to_dsh(api, tmp_path):
    hermes = _client(api, tmp_path, "hermes")
    dsh = _client(api, tmp_path, "dsh")
    try:
        result = hermes.sync_session(
            "hermes",
            "shared-session",
            {
                "scope": "project",
                "kind": "fact",
                "title": "Session summary",
                "content_text": "Hermes session is synchronized",
                "source_agent": "hermes",
            },
            content_hash="hash-v1",
        )
        assert result["session"]["sync_status"] == "synced"
        sessions = dsh.list_sessions(agent="hermes")
        assert sessions[0]["session_id"] == "shared-session"
        assert dsh.search("session synchronized")[0]["id"] == result["memory"]["id"]
    finally:
        hermes.close()
        dsh.close()


def test_real_python_hermes_and_typescript_dsh_interoperate(
    live_brain, built_dsh_client, tmp_path
):
    hermes = SharedBrainClient(
        live_brain,
        TOKEN,
        "hermes",
        "alpha",
        str(tmp_path / "hermes-live-queue.db"),
        device_id="hermes-device",
        timeout=10,
    )
    provider = SharedBrainMemoryProvider()
    provider._client = hermes
    try:
        from_hermes = json.loads(
            provider.handle_tool_call(
                "brain_remember",
                {"title": "Hermes live", "content": "Hermes to TypeScript interoperability"},
                session_id="hermes-live-session",
            )
        )
        seen_by_dsh = _run_dsh(
            built_dsh_client, live_brain, tmp_path, "search", query="TypeScript interoperability"
        )
        assert seen_by_dsh["ok"] is True
        assert seen_by_dsh["result"][0]["id"] == from_hermes["id"]

        from_dsh = _run_dsh(
            built_dsh_client,
            live_brain,
            tmp_path,
            "remember",
            title="DSH live",
            content="TypeScript to Hermes interoperability",
            sessionId="dsh-live-session",
        )
        assert from_dsh["ok"] is True
        seen_by_hermes = json.loads(
            provider.handle_tool_call("brain_search", {"query": "TypeScript to Hermes"})
        )
        assert from_dsh["result"]["id"] in {item["id"] for item in seen_by_hermes}

        synced = _run_dsh(
            built_dsh_client,
            live_brain,
            tmp_path,
            "sync",
            sessionId="dsh-synced-session",
            title="DSH session",
            content="DSH compound session visible to Hermes",
            contentHash="dsh-hash-v1",
        )
        assert synced["ok"] is True
        assert hermes.list_sessions(agent="dsh")[0]["session_id"] == "dsh-synced-session"

        concurrent = hermes.remember("Concurrent", "initial value")
        with ThreadPoolExecutor(max_workers=2) as pool:
            hermes_future = pool.submit(
                hermes.update, concurrent["id"], 1, None, "updated by Hermes"
            )
            dsh_future = pool.submit(
                _run_dsh,
                built_dsh_client,
                live_brain,
                tmp_path,
                "update",
                memoryId=concurrent["id"],
                expectedVersion=1,
                content="updated by DSH",
            )
            try:
                hermes_result = hermes_future.result()
            except BrainClientError as exc:
                hermes_result = {"status": exc.status}
            dsh_result = dsh_future.result()
        statuses = [
            409 if hermes_result.get("status") == 409 else 200,
            200 if dsh_result["ok"] else dsh_result["status"],
        ]
        assert sorted(statuses) == [200, 409]
    finally:
        hermes.close()


def test_live_selftest_t11_checks_deleted_id_not_total_hits(live_brain, tmp_path):
    client = SharedBrainClient(
        live_brain,
        TOKEN,
        "hermes",
        "alpha",
        str(tmp_path / "selftest-live-queue.db"),
        timeout=10,
    )
    try:
        report = run_selftest(client)
    finally:
        client.close()
    t11 = next(result for result in report["results"] if result["id"] == "T11")
    assert t11["status"] == "pass", report["text"]
    assert "目标已隐藏" in t11["detail"]
    assert "另有 1 条同批次命中" in t11["detail"]
