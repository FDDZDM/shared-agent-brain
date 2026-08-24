"""Hermes /brain command output-channel contract.

Every command result must use the command-plane return value. Injecting a
receipt as role=user wakes Hermes and makes the model reinterpret operational
status as a human request.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from shared_brain import hermes_plugin
from shared_brain.hermes_plugin import SharedBrainMemoryProvider


class FakeCtx:
    """Minimal PluginContext: register_command + inject_message."""

    def __init__(self, inject=True, inject_raises=False):
        self.commands = {}
        self.inject_calls = []
        self._inject = inject
        self._inject_raises = inject_raises

    def register_command(self, name, handler, description, args_hint):
        self.commands[name] = handler

    def inject_message(self, text, role=None):
        if self._inject_raises:
            raise RuntimeError("injection backend unavailable")
        if not self._inject:
            return False
        self.inject_calls.append((text, role))
        return True


def _install_fake_client(monkeypatch, memories=None):
    class FakeClient:
        def __init__(self):
            self.memories = memories or [
                {
                    "id": "m1",
                    "scope": "project",
                    "kind": "fact",
                    "project_key": "alpha",
                    "current_version": 1,
                    "title": "PostgreSQL 16",
                    "content_text": "项目使用 PostgreSQL 16。",
                    "source_agent": "hermes",
                    "source_session_id": None,
                    "trust_level": 1,
                    "updated_at": "2026-08-24T00:00:00Z",
                }
            ]

        def search(self, query, **kwargs):
            return self.memories

        def list_recent_memories(self, **kwargs):
            return self.memories

        def list_sessions(self, **kwargs):
            return []

        def list_agents(self):
            return []

        def flush_queue(self):
            self.flushed = True
            return 0

    fake = FakeClient()
    monkeypatch.setattr(hermes_plugin, "_make_slash_client", lambda: fake)
    return fake


def _handler(ctx):
    hermes_plugin.register(ctx)
    return ctx.commands["brain"]


def test_hermes_provider_exposes_explicit_version_safe_tools():
    provider = SharedBrainMemoryProvider()
    schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
    assert set(schemas) == {"brain_search", "brain_remember", "brain_update", "brain_forget"}
    assert "expected_version" in schemas["brain_update"]["parameters"]["required"]
    assert "expected_version" in schemas["brain_forget"]["parameters"]["required"]
    assert "untrusted reference data" in provider.system_prompt_block()


def test_search_result_uses_command_channel_without_model_injection(monkeypatch):
    ctx = FakeCtx()
    handler = _handler(ctx)
    _install_fake_client(monkeypatch)

    result = asyncio.run(handler("search PostgreSQL"))

    assert "PostgreSQL 16" in result
    assert ctx.inject_calls == []


def test_search_falls_back_to_text_when_injection_fails(monkeypatch):
    ctx = FakeCtx(inject=False)
    handler = _handler(ctx)
    _install_fake_client(monkeypatch)

    result = asyncio.run(handler("search PostgreSQL"))

    assert "PostgreSQL 16" in result
    assert ctx.inject_calls == []


def test_search_falls_back_to_text_when_injection_raises(monkeypatch):
    ctx = FakeCtx(inject_raises=True)
    handler = _handler(ctx)
    _install_fake_client(monkeypatch)

    result = asyncio.run(handler("search PostgreSQL"))

    assert "PostgreSQL 16" in result


def test_help_returns_directly_without_waking_the_agent(monkeypatch):
    ctx = FakeCtx()
    handler = _handler(ctx)
    _install_fake_client(monkeypatch)

    result = asyncio.run(handler("help"))

    assert "/brain search" in result
    assert ctx.inject_calls == []


def test_setup_validates_config_and_flushes_queue(monkeypatch):
    ctx = FakeCtx()
    handler = _handler(ctx)
    client = _install_fake_client(monkeypatch)

    result = asyncio.run(handler("setup"))

    assert "不提供插件热重载" in result
    assert client.flushed is True
    assert ctx.inject_calls == []


def test_unconfigured_error_stays_in_command_channel(monkeypatch):
    """A host failure must not become model-facing role=user input."""
    ctx = FakeCtx()
    handler = _handler(ctx)
    # Do NOT install a fake client: _make_slash_client fails on missing config.
    monkeypatch.setenv("HERMES_HOME", "/nonexistent-hermes-home")
    for env in ("BRAIN_URL", "BRAIN_TOKEN"):
        monkeypatch.delenv(env, raising=False)

    result = asyncio.run(handler("search anything"))

    assert "not configured" in result
    assert ctx.inject_calls == []


def test_forget_timeout_stays_in_command_channel(monkeypatch):
    ctx = FakeCtx()
    handler = _handler(ctx)
    client = _install_fake_client(monkeypatch)

    def timeout(*_args, **_kwargs):
        raise TimeoutError("The operation was aborted due to timeout")

    client.forget = timeout
    result = asyncio.run(handler("forget 64810109-f64d-4947-b3f2-126be34f5bb9 1"))

    assert "Shared Brain forget failed" in result
    assert "timeout" in result
    assert ctx.inject_calls == []


def test_tool_result_is_structured_json_not_free_text(monkeypatch):
    provider = SharedBrainMemoryProvider()
    provider._client = _install_fake_client(monkeypatch)  # type: ignore[assignment]

    raw = provider.handle_tool_call("brain_search", {"query": "PostgreSQL"})

    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    assert parsed[0]["title"] == "PostgreSQL 16"
