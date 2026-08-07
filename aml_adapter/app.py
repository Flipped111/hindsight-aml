from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from hindsight_client import Hindsight
from pydantic import BaseModel, Field, model_validator

from aml_adapter.schemas import AddRequest, AddResponse, HealthResponse, SearchRequest, SearchResponse
from aml_adapter.service import (
    HindsightGateway,
    IdempotencyPolicy,
    IdempotencyWaitTimeoutError,
    MemoryDependencyError,
    MemoryService,
)
from aml_adapter.storage import IdempotencyStore, RequestConflictError


class BusinessError(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


class ErrorDetail(BaseModel):
    reason: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class Settings(BaseModel):
    hindsight_base_url: str = "http://hindsight:8888"
    hindsight_timeout_seconds: float = Field(default=600, gt=0)
    idempotency_db_path: Path = Path("/data/idempotency.sqlite3")
    idempotency_wait_timeout_seconds: float = Field(default=900, gt=0)
    idempotency_lease_seconds: float = Field(default=1200, gt=0)
    idempotency_poll_interval_seconds: float = Field(default=0.1, gt=0)

    @model_validator(mode="after")
    def lease_must_outlast_hindsight_timeout(self) -> Settings:
        if self.idempotency_lease_seconds <= self.hindsight_timeout_seconds:
            raise ValueError("idempotency lease must exceed the Hindsight request timeout")
        return self

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            hindsight_base_url=os.getenv("HINDSIGHT_BASE_URL", "http://hindsight:8888"),
            hindsight_timeout_seconds=float(os.getenv("HINDSIGHT_TIMEOUT_SECONDS", "600")),
            idempotency_db_path=Path(os.getenv("AML_IDEMPOTENCY_DB_PATH", "/data/idempotency.sqlite3")),
            idempotency_wait_timeout_seconds=float(os.getenv("AML_IDEMPOTENCY_WAIT_TIMEOUT_SECONDS", "900")),
            idempotency_lease_seconds=float(os.getenv("AML_IDEMPOTENCY_LEASE_SECONDS", "1200")),
            idempotency_poll_interval_seconds=float(os.getenv("AML_IDEMPOTENCY_POLL_INTERVAL_SECONDS", "0.1")),
        )


def build_service(settings: Settings | None = None) -> MemoryService:
    resolved = settings or Settings.from_env()
    client = Hindsight(base_url=resolved.hindsight_base_url, timeout=resolved.hindsight_timeout_seconds)
    gateway = HindsightGateway(client)
    store = IdempotencyStore(resolved.idempotency_db_path, lease_seconds=resolved.idempotency_lease_seconds)
    policy = IdempotencyPolicy(
        wait_timeout_seconds=resolved.idempotency_wait_timeout_seconds,
        poll_interval_seconds=resolved.idempotency_poll_interval_seconds,
    )
    return MemoryService(gateway=gateway, store=store, policy=policy)


def create_app(service: MemoryService | None = None) -> FastAPI:
    memory_service = service or build_service()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await memory_service.initialize()
        try:
            yield
        finally:
            await memory_service.close()

    app = FastAPI(title="Hindsight AML Adapter", version="0.2.1", lifespan=lifespan)

    @app.exception_handler(BusinessError)
    async def business_error_handler(_: Request, error: BusinessError) -> JSONResponse:
        payload = ErrorResponse(detail=ErrorDetail(reason=error.reason))
        return JSONResponse(status_code=error.status_code, content=payload.model_dump(mode="json"))

    @app.post("/add", response_model=AddResponse)
    async def add(request: AddRequest) -> AddResponse:
        try:
            return await memory_service.add(request)
        except RequestConflictError as exc:
            raise BusinessError(status.HTTP_409_CONFLICT, str(exc)) from exc
        except IdempotencyWaitTimeoutError as exc:
            raise BusinessError(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except MemoryDependencyError as exc:
            raise BusinessError(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    @app.post("/search", response_model=SearchResponse, response_model_exclude_none=True)
    async def search(request: SearchRequest) -> SearchResponse:
        try:
            return await memory_service.search(request)
        except MemoryDependencyError as exc:
            raise BusinessError(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    @app.get("/health", response_model=HealthResponse)
    async def health(response: Response) -> HealthResponse:
        healthy = await memory_service.health()
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="unhealthy", hindsight="unavailable", idempotency_store="checked")
        return HealthResponse(status="healthy", hindsight="healthy", idempotency_store="healthy")

    return app


app = create_app()
