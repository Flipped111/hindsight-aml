from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from hindsight_client import Hindsight
from pydantic import BaseModel

from aml_adapter.schemas import (
    AddRequest,
    AddResponse,
    MemoryEvidence,
    RetainItem,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from aml_adapter.storage import ClaimStatus, IdempotencyStore, LeaseLostError, RequestIdentity


class MemoryDependencyError(Exception):
    """Hindsight could not complete a required operation."""


class IdempotencyWaitTimeoutError(Exception):
    """Another worker did not finish the same request before the wait deadline."""


class MemoryGateway(Protocol):
    async def retain(self, bank_id: str, items: list[RetainItem]) -> None: ...

    async def recall(self, bank_id: str, query: str) -> list[MemoryEvidence]: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...


class DependencyHealth(BaseModel):
    status: str
    database: str | None = None


class HindsightGateway:
    def __init__(self, client: Hindsight, health_timeout_seconds: float = 3) -> None:
        self._client = client
        self._health_timeout_seconds = health_timeout_seconds

    async def retain(self, bank_id: str, items: list[RetainItem]) -> None:
        # The maintained Hindsight wrapper accepts dict payloads. Keep that
        # untyped boundary here; the adapter uses RetainItem everywhere else.
        payloads: list[dict[str, Any]] = [item.model_dump(mode="python") for item in items]
        try:
            response = await self._client.aretain_batch(
                bank_id=bank_id,
                items=payloads,
                retain_async=False,
            )
        except Exception as exc:
            raise MemoryDependencyError("Hindsight retain failed") from exc

        if not response.success or response.var_async or response.items_count != len(items):
            raise MemoryDependencyError("Hindsight did not confirm a complete synchronous retain")

    async def recall(self, bank_id: str, query: str) -> list[MemoryEvidence]:
        try:
            response = await self._client.arecall(bank_id=bank_id, query=query, budget="mid")
        except Exception as exc:
            raise MemoryDependencyError("Hindsight recall failed") from exc

        evidence: list[MemoryEvidence] = []
        for result in response.results:
            score = float(result.scores.final) if result.scores is not None else None
            evidence.append(
                MemoryEvidence(
                    id=result.id,
                    text=result.text,
                    score=score,
                    mentioned_at=result.mentioned_at,
                )
            )
        return evidence

    async def health(self) -> bool:
        try:
            # Retain can legitimately take minutes, but orchestrator health probes
            # must not inherit that request timeout when Hindsight is unreachable.
            async with asyncio.timeout(self._health_timeout_seconds):
                payload = await self._client.monitoring.health_endpoint_health_get()
            health = DependencyHealth.model_validate(payload)
        except Exception:
            return False
        return health.status == "healthy" and health.database == "connected"

    async def close(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class IdempotencyPolicy:
    wait_timeout_seconds: float = 900
    poll_interval_seconds: float = 0.1


class MemoryService:
    def __init__(
        self,
        gateway: MemoryGateway,
        store: IdempotencyStore,
        policy: IdempotencyPolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._policy = policy or IdempotencyPolicy()

    async def initialize(self) -> None:
        await self._store.initialize()

    async def close(self) -> None:
        await self._gateway.close()

    async def health(self) -> bool:
        store_healthy, hindsight_healthy = await asyncio.gather(self._store.health(), self._gateway.health())
        return store_healthy and hindsight_healthy

    async def add(self, request: AddRequest) -> AddResponse:
        identity = RequestIdentity(
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
            payload_hash=_payload_hash(request),
        )
        deadline = time.monotonic() + self._policy.wait_timeout_seconds

        while True:
            claim = await self._store.claim(identity)
            if claim.status == ClaimStatus.COMPLETED:
                return _add_response(request)
            if claim.status == ClaimStatus.ACQUIRED:
                if claim.owner_token is None:
                    raise RuntimeError("acquired claim is missing its owner token")
                return await self._retain_claimed(request, claim.owner_token)
            if time.monotonic() >= deadline:
                raise IdempotencyWaitTimeoutError(f"request_id '{request.request_id}' is still processing")
            await asyncio.sleep(self._policy.poll_interval_seconds)

    async def search(self, request: SearchRequest) -> SearchResponse:
        evidence = await self._gateway.recall(user_to_bank_id(request.user_id), request.query)
        results = [_to_search_result(item) for item in evidence]
        valid_results = [item for item in results if item is not None]
        valid_results.sort(key=lambda item: item.score if item.score is not None else float("-inf"), reverse=True)
        return SearchResponse(data=valid_results[: request.top_k])

    async def _retain_claimed(self, request: AddRequest, owner_token: str) -> AddResponse:
        try:
            await self._gateway.retain(user_to_bank_id(request.user_id), _retain_items(request))
            await self._store.complete(request.request_id, owner_token)
        except LeaseLostError as exc:
            raise MemoryDependencyError("idempotency claim expired during retain") from exc
        except Exception:
            await self._store.release(request.request_id, owner_token)
            raise
        except BaseException:
            await asyncio.shield(self._store.release(request.request_id, owner_token))
            raise
        return _add_response(request)


def user_to_bank_id(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _payload_hash(request: AddRequest) -> str:
    canonical = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _retain_items(request: AddRequest) -> list[RetainItem]:
    items: list[RetainItem] = []
    for index, message in enumerate(request.messages):
        retain_timestamp: datetime | Literal["unset"]
        if message.timestamp is None:
            retain_timestamp = "unset"
            event_time_text = "Unknown"
            original_timestamp = "unset"
        else:
            retain_timestamp = datetime.fromtimestamp(message.timestamp / 1000, tz=UTC)
            event_time_text = retain_timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            original_timestamp = str(message.timestamp)
        document_id = hashlib.sha256(f"{request.request_id}:{index}".encode("utf-8")).hexdigest()
        content = (
            f"Speaker: {message.role}\n"
            f"Session: {request.session_id}\n"
            f"Event time: {event_time_text}\n"
            f"Message: {message.content}"
        )
        items.append(
            RetainItem(
                content=content,
                timestamp=retain_timestamp,
                document_id=document_id,
                metadata={
                    "request_id": request.request_id,
                    "session_id": request.session_id,
                    "role": message.role,
                    "original_timestamp": original_timestamp,
                },
            )
        )
    return items


def _to_search_result(evidence: MemoryEvidence) -> SearchResult | None:
    if not evidence.id.strip() or not evidence.text.strip():
        return None
    return SearchResult(
        id=evidence.id,
        content=evidence.text,
        score=evidence.score,
        created_at=_reliable_utc_timestamp(evidence.mentioned_at),
    )


def _reliable_utc_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _add_response(request: AddRequest) -> AddResponse:
    return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
