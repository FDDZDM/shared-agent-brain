"""HTTP request models and domain constants."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    PROJECT = "project"


class MemoryKind(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    PITFALL = "pitfall"


class MemoryModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class MemoryCreate(MemoryModel):
    scope: MemoryScope
    kind: MemoryKind
    project_key: Optional[str] = Field(default=None, max_length=255)
    title: str = Field(min_length=1, max_length=300)
    content_text: str = Field(min_length=1, max_length=16_384)
    source_agent: str = Field(min_length=1, max_length=128)
    source_session_id: Optional[str] = Field(default=None, max_length=255)
    trust_level: int = Field(default=0, ge=0, le=3)


class MemoryUpdate(MemoryModel):
    expected_version: int = Field(ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    content_text: Optional[str] = Field(default=None, min_length=1, max_length=16_384)
    kind: Optional[MemoryKind] = None
    trust_level: Optional[int] = Field(default=None, ge=0, le=3)
    source_agent: str = Field(min_length=1, max_length=128)
    source_session_id: Optional[str] = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_a_change(self) -> "MemoryUpdate":
        if all(
            value is None
            for value in (self.title, self.content_text, self.kind, self.trust_level)
        ):
            raise ValueError("at least one mutable memory field is required")
        return self


class MemoryDelete(MemoryModel):
    expected_version: int = Field(ge=1)
    source_agent: str = Field(min_length=1, max_length=128)


class SessionCreate(MemoryModel):
    project_key: str = Field(min_length=1, max_length=255)
    agent_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(default="", max_length=128)
    session_id: str = Field(min_length=1, max_length=255)
    title: Optional[str] = Field(default=None, max_length=300)
    updated_at: str = Field(min_length=1, max_length=64)
    content_hash: Optional[str] = Field(default=None, max_length=64)

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("updated_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("updated_at must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )


class SessionSync(MemoryModel):
    """复合原子同步：同一请求内写入记忆并标记会话已同步。"""

    project_key: str = Field(min_length=1, max_length=255)
    device_id: str = Field(default="", max_length=128)
    content_hash: Optional[str] = Field(default=None, max_length=64)
    memory: MemoryCreate

    @model_validator(mode="after")
    def require_project_memory_in_same_project(self) -> "SessionSync":
        if self.memory.scope != MemoryScope.PROJECT:
            raise ValueError("session sync memory must use scope=project")
        if self.memory.project_key != self.project_key:
            raise ValueError("memory.project_key must match session project_key")
        return self
