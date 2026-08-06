from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AmlModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Message(AmlModel):
    role: str
    timestamp: int = Field(ge=0)
    content: str

    @field_validator("role", "content")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("timestamp")
    @classmethod
    def require_supported_timestamp(cls, value: int) -> int:
        try:
            datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("must be a valid Unix timestamp in milliseconds") from exc
        return value


class AddRequest(AmlModel):
    request_id: str
    messages: list[Message] = Field(min_length=1)
    user_id: str
    session_id: str

    @field_validator("request_id", "user_id", "session_id")
    @classmethod
    def require_non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class AddResponse(AmlModel):
    success: bool = True
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(AmlModel):
    query: str
    options: list[str]
    user_id: str
    top_k: int = Field(ge=1, le=100)

    @field_validator("query", "user_id")
    @classmethod
    def require_non_blank_search_field(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SearchResult(AmlModel):
    id: str
    content: str
    score: float | None = None
    created_at: str | None = None


class SearchResponse(AmlModel):
    data: list[SearchResult]


class HealthResponse(AmlModel):
    status: str
    hindsight: str
    idempotency_store: str


class RetainItem(BaseModel):
    content: str
    timestamp: datetime
    metadata: dict[str, str]
    document_id: str


class MemoryEvidence(BaseModel):
    id: str
    text: str
    score: float | None = None
    mentioned_at: str | None = None
