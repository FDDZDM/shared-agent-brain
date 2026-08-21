"""HTTP request models and domain constants."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

