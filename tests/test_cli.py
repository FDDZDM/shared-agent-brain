"""CLI tests: queue listing hides payloads by default, management commands work."""

from __future__ import annotations

import json
import stat
from typing import Optional

import pytest

from shared_brain.cli import main
from shared_brain.config import load_config, save_config
from shared_brain.queue import OfflineQueue

TOKEN = "token-that-is-long-enough-123456"


def test_config_is_atomically_saved_with_private_permissions(tmp_path):
    target = tmp_path / "private" / "config.json"
    saved = save_config({"token": TOKEN, "agent_id": "hermes"}, str(target))
    assert load_config(str(saved))["token"] == TOKEN
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert not saved.with_suffix(".tmp").exists()


def _config(tmp_path, queue_path=None):
    config = {
        "server_url": "https://brain.invalid",
        "token": TOKEN,
        "agent_id": "hermes",
        "project_key": "alpha",
    }
    if queue_path is not None:
        config["queue_path"] = str(queue_path)
    path = tmp_path / "amm.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _enqueue(queue_path, title="秘密内容 token=SECRET"):
    queue = OfflineQueue(str(queue_path))
    queue.enqueue("POST", "/v1/memories", {"title": title, "content_text": "含敏感信息的 payload"})
    return queue.list()[0]["op_key"]


def test_queue_list_hides_payload_by_default(tmp_path, capsys):
    queue_path = tmp_path / "queue.db"
    op_key = _enqueue(queue_path)
    config_path = _config(tmp_path, queue_path)

    main(["--config-path", config_path, "queue", "list"])
    out = capsys.readouterr().out
    items = json.loads(out)
    assert items[0]["op_key"] == op_key
    assert "payload" not in items[0]
    assert "秘密内容" not in out
    assert "SECRET" not in out


def test_queue_list_verbose_shows_payload(tmp_path, capsys):
    queue_path = tmp_path / "queue.db"
    _enqueue(queue_path)
    config_path = _config(tmp_path, queue_path)

    main(["--config-path", config_path, "queue", "list", "--verbose"])
    items = json.loads(capsys.readouterr().out)
    assert "payload" in items[0]
    assert items[0]["payload"]["title"] == "秘密内容 token=SECRET"


def test_queue_remove_and_retry(tmp_path, capsys):
    queue_path = tmp_path / "queue.db"
    op_key = _enqueue(queue_path)
    config_path = _config(tmp_path, queue_path)

    main(["--config-path", config_path, "queue", "remove", op_key])
    assert "removed" in capsys.readouterr().out

    # 不存在的 op_key → 非零退出码
    with pytest.raises(SystemExit) as exc_info:
        main(["--config-path", config_path, "queue", "remove", "no-such-op-key-0001"])
    assert exc_info.value.code != 0

    fresh_op_key = _enqueue(queue_path)
    main(["--config-path", config_path, "queue", "retry", fresh_op_key])
    assert "retry scheduled" in capsys.readouterr().out


class _FakeClient:
    """doctor 用的假客户端：可注入故障。"""

    server_url = "http://brain.test"
    agent_id = "hermes"
    project_key = "alpha"
    health_result = {"status": "ok", "version": "0.1.0"}
    whoami_result = {"server_version": "0.1.0", "schema_version": 3, "capabilities": ["memories"]}
    search_result = []
    pending = 0
    failed = 0
    health_error: Optional[Exception] = None
    whoami_error: Optional[Exception] = None
    search_error: Optional[Exception] = None

    def health(self):
        if self.health_error:
            raise self.health_error
        return self.health_result

    def whoami(self):
        if self.whoami_error:
            raise self.whoami_error
        return self.whoami_result

    def search(self, query, **kwargs):
        if self.search_error:
            raise self.search_error
        return self.search_result

    def close(self):
        return None

    class queue:
        @staticmethod
        def count(status=None):
            if status == "failed":
                return _FakeClient.failed
            return _FakeClient.pending


def _install_fake_client(monkeypatch, fake):
    monkeypatch.setattr("shared_brain.cli._client", lambda config_path=None: fake)


def test_doctor_all_checks_pass(tmp_path, capsys, monkeypatch):
    _install_fake_client(monkeypatch, _FakeClient())
    main(["--config-path", str(tmp_path / "amm.json"), "doctor"])
    out = capsys.readouterr().out
    assert "doctor: OK" in out
    assert "auth" in out.lower()
    assert "queue" in out.lower()
    assert "{" not in out  # 人类可读输出


def test_doctor_json_mode(tmp_path, capsys, monkeypatch):
    _install_fake_client(monkeypatch, _FakeClient())
    main(["--config-path", str(tmp_path / "amm.json"), "doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["checks"]["network"]["ok"] is True
    assert report["checks"]["auth"]["ok"] is True
    assert report["checks"]["server"]["detail"] == "v0.1.0 · schema v3"
    assert report["checks"]["project"]["detail"] == "alpha"


def test_doctor_auth_failure_exits_nonzero(tmp_path, capsys, monkeypatch):
    from shared_brain.client import BrainClientError

    fake = _FakeClient()
    fake.whoami_error = BrainClientError("401: unauthorized", status=401)
    _install_fake_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config-path", str(tmp_path / "amm.json"), "doctor"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "auth" in out.lower()
    assert "✗" in out


def test_doctor_unreachable_server_exits_nonzero(tmp_path, capsys, monkeypatch):
    import httpx

    fake = _FakeClient()
    fake.health_error = httpx.ConnectError("offline", request=None)
    _install_fake_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config-path", str(tmp_path / "amm.json"), "doctor"])
    assert exc_info.value.code == 1
    assert "doctor: FAILED" in capsys.readouterr().out


def test_doctor_reports_failed_queue(tmp_path, capsys, monkeypatch):
    fake = _FakeClient()
    _FakeClient.failed = 2  # 类属性：嵌套 queue.count 读的是它
    _install_fake_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config-path", str(tmp_path / "amm.json"), "doctor"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "2 failed" in out
    _FakeClient.failed = 0
