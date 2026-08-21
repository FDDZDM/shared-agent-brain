"""Synchronous Shared Brain client used by CLI and Hermes."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .queue import OfflineQueue
from .security import render_untrusted_memories


class BrainClientError(RuntimeError):
    pass


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
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.project_key = project_key
        self.timeout = timeout
        path = queue_path or str(Path.home() / ".amm" / "queue.db")
        self.queue = OfflineQueue(path)
        self._client = httpx.Client(
            base_url=self.server_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> Dict[str, Any]:
        response = self._client.get("/health")
        response.raise_for_status()
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
            raise BrainClientError(f"{response.status_code}: {response.text}")
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

    def prefetch(self, query: str, limit: int = 5, min_trust_level: int = 0) -> str:
        return render_untrusted_memories(
            self.search(query, limit=limit, min_trust_level=min_trust_level)
        )

    def flush_queue(self, limit: int = 100) -> Dict[str, int]:
        sent = 0
        failed = 0
        for item in self.queue.list(limit):
            try:
                self._write(
                    item["method"],
                    item["path"],
                    item["payload"],
                    item["op_key"],
                    queue_on_failure=False,
                )
            except (httpx.TransportError, BrainClientError) as exc:
                self.queue.mark_failed(item["op_key"], str(exc))
                failed += 1
                break
            else:
                self.queue.remove(item["op_key"])
                sent += 1
        return {"sent": sent, "failed": failed, "remaining": self.queue.count()}

