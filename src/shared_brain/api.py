"""FastAPI application for the Shared Brain Phase 1 API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from .db import BrainStore
from .errors import BrainError, ConflictError, NotFoundError, ValidationError
from .models import MemoryCreate, MemoryDelete, MemoryKind, MemoryScope, MemoryUpdate, SessionCreate
from .security import request_hash, verify_token


def _dump(model: object) -> dict:
    return model.model_dump(mode="json", exclude_none=False)  # type: ignore[attr-defined]


def create_app(db_path: Optional[str] = None, token: Optional[str] = None) -> FastAPI:
    resolved_db = db_path or os.environ.get("BRAIN_DB_PATH", "./data/shared-brain.db")
    resolved_token = token or os.environ.get("BRAIN_TOKEN")
    if not resolved_token or len(resolved_token) < 24:
        raise RuntimeError("BRAIN_TOKEN must be set to a high-entropy value of at least 24 characters")

    store = BrainStore(resolved_db)
    store.initialize(resolved_token)

    app = FastAPI(
        title="Shared Brain",
        version="0.1.0",
        description="Zero-LLM shared memory backend for open agent harnesses.",
    )
    app.state.store = store

    @app.exception_handler(BrainError)
    async def domain_error_handler(_: Request, exc: BrainError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            status = 404
            code = "NOT_FOUND"
        elif isinstance(exc, ConflictError):
            status = 409
            code = "CONFLICT"
        elif isinstance(exc, ValidationError):
            status = 422
            code = "VALIDATION_ERROR"
        else:
            status = 400
            code = "BRAIN_ERROR"
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(exc)}})

    def authenticate(authorization: Optional[str] = Header(default=None)) -> None:
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not credential or not verify_token(credential, store.token_hash()):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="invalid or missing Brain token")

    def idempotency_key(value: str = Header(alias="Idempotency-Key", min_length=8, max_length=200)) -> str:
        return value

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": app.version}

    @app.post("/v1/memories", dependencies=[Depends(authenticate)])
    def create_memory(body: MemoryCreate, op_key: str = Depends(idempotency_key)) -> JSONResponse:
        payload = _dump(body)
        status, result = store.create_memory(
            payload,
            op_key,
            request_hash("POST", "/v1/memories", payload),
        )
        return JSONResponse(status_code=status, content=result)

    @app.get("/v1/memories/search", dependencies=[Depends(authenticate)])
    def search_memories(
        q: str = Query(min_length=1, max_length=1000),
        scope: Optional[MemoryScope] = None,
        project_key: Optional[str] = Query(default=None, max_length=255),
        kind: Optional[MemoryKind] = None,
        source_agent: Optional[str] = Query(default=None, max_length=128),
        min_trust_level: int = Query(default=0, ge=0, le=3),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict:
        items = store.search_memories(
            q,
            scope=scope.value if scope else None,
            project_key=project_key,
            kind=kind.value if kind else None,
            source_agent=source_agent,
            min_trust_level=min_trust_level,
            limit=limit,
        )
        return {"items": items, "count": len(items)}

    @app.get("/v1/memories", dependencies=[Depends(authenticate)])
    def list_memories(
        scope: Optional[MemoryScope] = None,
        project_key: Optional[str] = Query(default=None, max_length=255),
        source_agent: Optional[str] = Query(default=None, max_length=128),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict:
        items = store.list_recent_memories(
            scope=scope.value if scope else None,
            project_key=project_key,
            source_agent=source_agent,
            limit=limit,
        )
        return {"items": items, "count": len(items)}

    @app.get("/v1/memories/changes", dependencies=[Depends(authenticate)])
    def memory_changes(
        cursor: int = Query(default=0, ge=0),
        project_key: Optional[str] = Query(default=None, max_length=255),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        items = store.changes_since(cursor, project_key, limit)
        next_cursor = items[-1]["change_seq"] if items else cursor
        return {"items": items, "count": len(items), "next_cursor": next_cursor}

    @app.get("/v1/memories/{memory_id}", dependencies=[Depends(authenticate)])
    def get_memory(memory_id: str, include_deleted: bool = False) -> dict:
        return store.get_memory(memory_id, include_deleted=include_deleted)

    @app.get("/v1/memories/{memory_id}/versions", dependencies=[Depends(authenticate)])
    def list_versions(memory_id: str) -> dict:
        items = store.list_versions(memory_id)
        return {"items": items, "count": len(items)}

    @app.post("/v1/memories/{memory_id}/versions", dependencies=[Depends(authenticate)])
    def update_memory(
        memory_id: str, body: MemoryUpdate, op_key: str = Depends(idempotency_key)
    ) -> JSONResponse:
        payload = _dump(body)
        path = f"/v1/memories/{memory_id}/versions"
        status, result = store.update_memory(
            memory_id,
            payload,
            op_key,
            request_hash("POST", path, payload),
        )
        return JSONResponse(status_code=status, content=result)

    @app.delete("/v1/memories/{memory_id}", dependencies=[Depends(authenticate)])
    def delete_memory(
        memory_id: str, body: MemoryDelete, op_key: str = Depends(idempotency_key)
    ) -> JSONResponse:
        payload = _dump(body)
        path = f"/v1/memories/{memory_id}"
        status, result = store.delete_memory(
            memory_id,
            payload,
            op_key,
            request_hash("DELETE", path, payload),
        )
        return JSONResponse(status_code=status, content=result)

    # -- 会话目录 -------------------------------------------------------------

    @app.post("/v1/sessions", dependencies=[Depends(authenticate)])
    def upsert_session(body: SessionCreate) -> dict:
        return store.upsert_session(
            body.agent_id, body.session_id, body.title, body.updated_at
        )

    @app.post("/v1/sessions/{agent_id}/{session_id}/synced", dependencies=[Depends(authenticate)])
    def mark_session_synced(agent_id: str, session_id: str) -> dict:
        return store.mark_session_synced(agent_id, session_id)

    @app.get("/v1/sessions", dependencies=[Depends(authenticate)])
    def list_sessions(
        agent: Optional[str] = Query(default=None, max_length=128),
        synced: Optional[bool] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        items = store.list_sessions(agent_id=agent, synced=synced, limit=limit)
        return {"items": items, "count": len(items)}

    @app.get("/v1/sessions/agents", dependencies=[Depends(authenticate)])
    def list_agents() -> dict:
        items = store.list_agents()
        return {"items": items, "count": len(items)}

    return app


def app_from_env() -> FastAPI:
    return create_app()


# Uvicorn imports this symbol. Importing without a token deliberately fails closed.
if os.environ.get("BRAIN_TOKEN"):
    app = app_from_env()
else:
    app = FastAPI(title="Shared Brain (not configured)")

    @app.get("/health")
    def unconfigured_health() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"status": "unconfigured", "error": "BRAIN_TOKEN is not set"},
        )
