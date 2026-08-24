"""Synchronous Shared Brain client used by CLI and Hermes."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import parse as urlparse

import httpx

from .queue import OfflineQueue
from .security import render_untrusted_memories


class BrainClientError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class SharedBrainClient:
    def __init__(
        self,
        server_url: str,
        token: str,
        agent_id: str,
        project_key: Optional[str] = None,
        queue_path: Optional[str] = None,
        timeout: float = 5.0,
        transport: Optional[httpx.BaseTransport] = None,
        device_id: str = "",
        trust_env: bool = False,
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.project_key = project_key
        self.device_id = device_id
        self.timeout = timeout
        path = queue_path or str(Path.home() / ".amm" / "queue.db")
        self.queue = OfflineQueue(path)
        self._client = httpx.Client(
            base_url=self.server_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            trust_env=trust_env,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> Dict[str, Any]:
        response = self._client.get("/health")
        response.raise_for_status()
        return response.json()

    def whoami(self) -> Dict[str, Any]:
        """鉴权身份/能力探测：验证 token 有效并返回服务/schema 版本。"""
        response = self._client.get("/v1/whoami")
        if response.status_code >= 400:
            raise BrainClientError(
                f"{response.status_code}: {response.text}", status=response.status_code
            )
        return response.json()

    def _write(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any],
        op_key: Optional[str] = None,
        queue_on_failure: bool = True,
    ) -> Dict[str, Any]:
        key = op_key or str(uuid.uuid4())
        try:
            response = self._client.request(
                method,
                path,
                json=payload,
                headers={"Idempotency-Key": key},
            )
        except httpx.TransportError as exc:
            if not queue_on_failure:
                raise
            self.queue.enqueue(method, path, payload, key, str(exc))
            return {"queued": True, "op_key": key, "error": str(exc)}
        if response.status_code >= 400:
            raise BrainClientError(
                f"{response.status_code}: {response.text}", status=response.status_code
            )
        return response.json()

    def remember(
        self,
        title: str,
        content_text: str,
        scope: str = "project",
        kind: str = "fact",
        project_key: Optional[str] = None,
        source_session_id: Optional[str] = None,
        trust_level: int = 0,
        op_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "scope": scope,
            "kind": kind,
            "project_key": project_key if project_key is not None else self.project_key,
            "title": title,
            "content_text": content_text,
            "source_agent": self.agent_id,
            "source_session_id": source_session_id,
            "trust_level": trust_level,
        }
        if scope != "project":
            payload["project_key"] = None
        return self._write("POST", "/v1/memories", payload, op_key)

    def update(
        self,
        memory_id: str,
        expected_version: int,
        title: Optional[str] = None,
        content_text: Optional[str] = None,
        kind: Optional[str] = None,
        trust_level: Optional[int] = None,
        source_session_id: Optional[str] = None,
        op_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "expected_version": expected_version,
            "source_agent": self.agent_id,
            "source_session_id": source_session_id,
        }
        for key, value in {
            "title": title,
            "content_text": content_text,
            "kind": kind,
            "trust_level": trust_level,
        }.items():
            if value is not None:
                payload[key] = value
        return self._write("POST", f"/v1/memories/{memory_id}/versions", payload, op_key)

    def forget(self, memory_id: str, expected_version: int, op_key: Optional[str] = None) -> Dict[str, Any]:
        return self._write(
            "DELETE",
            f"/v1/memories/{memory_id}",
            {"expected_version": expected_version, "source_agent": self.agent_id},
            op_key,
        )

    def search(
        self,
        query: str,
        project_key: Optional[str] = None,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        source_agent: Optional[str] = None,
        min_trust_level: int = 0,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        # 服务端 q 上限 1000 字符：客户端先行截断，避免自然问句超长触发 422。
        query = (query or "").strip()[:1000]
        params: Dict[str, Any] = {
            "q": query,
            "project_key": project_key if project_key is not None else self.project_key,
            "min_trust_level": min_trust_level,
            "limit": limit,
        }
        if scope:
            params["scope"] = scope
        if kind:
            params["kind"] = kind
        if source_agent:
            params["source_agent"] = source_agent
        response = self._client.get("/v1/memories/search", params=params)
        if response.status_code >= 400:
            raise BrainClientError(f"{response.status_code}: {response.text}")
        return response.json()["items"]

    def prefetch(self, query: str, limit: int = 5, min_trust_level: int = 0, max_chars: int = 6000) -> str:
        return render_untrusted_memories(
            self.search(query, limit=limit, min_trust_level=min_trust_level),
            max_chars=max_chars,
        )

    def list_recent_memories(
        self,
        project_key: Optional[str] = None,
        source_agent: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "project_key": project_key if project_key is not None else self.project_key,
            "limit": limit,
        }
        if source_agent:
            params["source_agent"] = source_agent
        response = self._client.get("/v1/memories", params=params)
        if response.status_code >= 400:
            raise BrainClientError(f"{response.status_code}: {response.text}")
        return response.json()["items"]

    def upsert_session(
        self,
        agent_id: str,
        session_id: str,
        title: Optional[str],
        updated_at: str,
        project_key: Optional[str] = None,
        device_id: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        response = self._client.post(
            "/v1/sessions",
            json={
                "project_key": project_key if project_key is not None else self.project_key or "",
                "agent_id": agent_id,
                "device_id": self.device_id if device_id is None else device_id,
                "session_id": session_id,
                "title": title,
                "updated_at": updated_at,
                "content_hash": content_hash,
            },
        )
        if response.status_code >= 400:
            raise BrainClientError(f"{response.status_code}: {response.text}")
        return response.json()

    def mark_session_synced(
        self,
        agent_id: str,
        session_id: str,
        project_key: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        agent = urlparse.quote(agent_id, safe="")
        session = urlparse.quote(session_id, safe="")
        params = {
            "project_key": project_key if project_key is not None else self.project_key or "",
            "device_id": self.device_id if device_id is None else device_id,
        }
        response = self._client.post(f"/v1/sessions/{agent}/{session}/synced", params=params)
        if response.status_code >= 400:
            raise BrainClientError(f"{response.status_code}: {response.text}")
        return response.json()

    def sync_session(
        self,
        agent_id: str,
        session_id: str,
        memory_payload: Dict[str, Any],
        content_hash: Optional[str] = None,
        device_id: Optional[str] = None,
        op_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """复合原子同步：一次请求内写入记忆并标记会话已同步（离线可排队）。"""
        agent = urlparse.quote(agent_id, safe="")
        session = urlparse.quote(session_id, safe="")
        memory = dict(memory_payload)
        if memory.get("scope", "project") == "project":
            memory["project_key"] = memory.get("project_key") or self.project_key or ""
        memory["source_session_id"] = session_id
        body = {
            "project_key": memory.get("project_key") or self.project_key or "",
            "device_id": self.device_id if device_id is None else device_id,
            "content_hash": content_hash,
            "memory": memory,
        }
        return self._write("POST", f"/v1/sessions/{agent}/{session}/sync", body, op_key)

    def list_sessions(
        self,
        project_key: Optional[str] = None,
        agent: Optional[str] = None,
        device_id: Optional[str] = None,
        synced: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "project_key": project_key if project_key is not None else self.project_key or "",
            "limit": limit,
        }
        if agent:
            params["agent"] = agent
        if device_id is not None:
            params["device_id"] = device_id
        if synced is not None:
            params["synced"] = str(synced).lower()
        response = self._client.get("/v1/sessions", params=params)
        if response.status_code >= 400:
            raise BrainClientError(f"{response.status_code}: {response.text}")
        return response.json()["items"]

    def list_agents(self, project_key: Optional[str] = None, device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"project_key": project_key if project_key is not None else self.project_key or ""}
        if device_id is not None:
            params["device_id"] = device_id
        response = self._client.get("/v1/sessions/agents", params=params)
        if response.status_code >= 400:
            raise BrainClientError(f"{response.status_code}: {response.text}")
        return response.json()["items"]

    def flush_queue(self, limit: int = 100) -> Dict[str, int]:
        """Replay due offline writes.

        Error handling:
        - transport errors  -> retryable, keep FIFO order, stop this pass
        - 5xx               -> retryable, stop this pass (server trouble)
        - 409 conflicts     -> non-retryable; the user must refresh the stale
                               version/session and explicitly retry
        - other 4xx         -> non-retryable (permanent) failure, skip
        """
        sent = 0
        failed = 0
        for item in self.queue.list(limit, due_only=True):
            try:
                self._write(
                    item["method"],
                    item["path"],
                    item["payload"],
                    item["op_key"],
                    queue_on_failure=False,
                )
            except httpx.TransportError as exc:
                self.queue.mark_failed(item["op_key"], str(exc), retryable=True)
                failed += 1
                break
            except BrainClientError as exc:
                retryable = exc.status is None or exc.status >= 500
                self.queue.mark_failed(item["op_key"], str(exc), retryable=retryable)
                failed += 1
                if retryable:
                    break
                # Permanent client/conflict errors do not block later tasks.
            else:
                self.queue.remove(item["op_key"])
                sent += 1
        return {"sent": sent, "failed": failed, "remaining": self.queue.count()}
