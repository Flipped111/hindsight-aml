from __future__ import annotations

import asyncio
import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from hindsight_client import Hindsight
from pydantic import BaseModel

from aml_adapter.raw_retrieval import RawMessageHit
from aml_adapter.schemas import (
    AddRequest,
    AddResponse,
    MemoryEvidence,
    RetainItem,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from aml_adapter.storage import (
    ClaimStatus,
    IdempotencyStore,
    LeaseLostError,
    RawMessageWrite,
    RequestIdentity,
)
from aml_adapter.temporal_reranking import temporal_score_multipliers

_RRF_K = 60
_FACT_RRF_WEIGHT = 1.0
_RAW_RRF_WEIGHT = 0.95
_WEAK_RAW_RRF_WEIGHT = 0.55
_WEAK_RAW_SCORE_MAX = 1e-5
_MAX_PRIMARY_RAW_RESULTS = 4
_MAX_RESULTS_PER_DOCUMENT = 2


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
                    document_id=getattr(result, "document_id", None),
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
        user_scope = user_to_bank_id(request.user_id)
        raw_limit = min(200, max(40, request.top_k * 2))
        evidence, raw_hits = await asyncio.gather(
            self._gateway.recall(user_scope, request.query),
            self._store.search_raw_messages(user_scope, request.query, raw_limit),
        )
        return SearchResponse(data=_merge_search_results(request.query, evidence, raw_hits, request.top_k))

    async def _retain_claimed(self, request: AddRequest, owner_token: str) -> AddResponse:
        try:
            user_scope = user_to_bank_id(request.user_id)
            retain_items = _retain_items(request)
            await self._store.stage_raw_messages(
                request.request_id,
                owner_token,
                _raw_message_writes(request, user_scope, retain_items),
            )
            await self._gateway.retain(user_scope, retain_items)
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


def _raw_message_writes(
    request: AddRequest,
    user_scope: str,
    retain_items: list[RetainItem],
) -> list[RawMessageWrite]:
    return [
        RawMessageWrite(
            document_id=item.document_id,
            request_id=request.request_id,
            message_index=index,
            user_scope=user_scope,
            session_id=request.session_id,
            role=request.messages[index].role,
            content=item.content,
            search_text=request.messages[index].content,
            timestamp_ms=request.messages[index].timestamp,
        )
        for index, item in enumerate(retain_items)
    ]


@dataclass(frozen=True)
class _FusionCandidate:
    result: SearchResult
    source: Literal["fact", "raw"]
    source_rank: int
    document_id: str | None
    event_time: datetime | None
    fused_score: float


def _merge_search_results(
    query: str,
    evidence: list[MemoryEvidence],
    raw_hits: list[RawMessageHit],
    top_k: int,
) -> list[SearchResult]:
    candidates: list[_FusionCandidate] = []
    fact_results = [(item, result) for item in evidence if (result := _to_search_result(item)) is not None]
    fact_results.sort(
        key=lambda pair: pair[0].score if pair[0].score is not None else float("-inf"),
        reverse=True,
    )
    for rank, (item, result) in enumerate(fact_results, start=1):
        fused_score = _FACT_RRF_WEIGHT / (_RRF_K + rank)
        candidates.append(
            _FusionCandidate(
                result=result.model_copy(update={"score": fused_score}),
                source="fact",
                source_rank=rank,
                document_id=item.document_id,
                event_time=_parse_reliable_utc_timestamp(item.mentioned_at),
                fused_score=fused_score,
            )
        )

    for rank, item in enumerate(raw_hits, start=1):
        source_weight = _WEAK_RAW_RRF_WEIGHT if item.lexical_score <= _WEAK_RAW_SCORE_MAX else _RAW_RRF_WEIGHT
        result = SearchResult(
            id=f"raw:{item.document_id}",
            content=item.content,
            score=source_weight / (_RRF_K + rank),
            created_at=_timestamp_ms_to_utc(item.timestamp_ms),
        )
        candidates.append(
            _FusionCandidate(
                result=result,
                source="raw",
                source_rank=rank,
                document_id=item.document_id,
                event_time=_timestamp_ms_to_datetime(item.timestamp_ms),
                fused_score=result.score or 0.0,
            )
        )

    temporal_multipliers = temporal_score_multipliers(query, [item.event_time for item in candidates])
    candidates = [
        replace(
            item,
            result=item.result.model_copy(update={"score": item.fused_score * multiplier}),
            fused_score=item.fused_score * multiplier,
        )
        for item, multiplier in zip(candidates, temporal_multipliers, strict=True)
    ]

    candidates.sort(
        key=lambda item: (
            -item.fused_score,
            0 if item.source == "fact" else 1,
            item.source_rank,
            item.result.id,
        )
    )
    selected: list[SearchResult] = []
    deferred_raw: list[_FusionCandidate] = []
    seen_content: set[str] = set()
    document_counts: dict[str, int] = {}
    raw_results = 0

    def select(candidate: _FusionCandidate) -> bool:
        nonlocal raw_results
        normalized_content = _normalize_evidence_text(candidate.result.content)
        if normalized_content in seen_content:
            return False
        if candidate.document_id is not None:
            count = document_counts.get(candidate.document_id, 0)
            if count >= _MAX_RESULTS_PER_DOCUMENT:
                return False
            document_counts[candidate.document_id] = count + 1
        seen_content.add(normalized_content)
        selected.append(candidate.result)
        if candidate.source == "raw":
            raw_results += 1
        return True

    for candidate in candidates:
        if candidate.source == "raw" and raw_results >= _MAX_PRIMARY_RAW_RESULTS:
            deferred_raw.append(candidate)
            continue
        if select(candidate) and len(selected) >= top_k:
            break

    if len(selected) < top_k:
        for candidate in deferred_raw:
            if select(candidate) and len(selected) >= top_k:
                break
    return selected


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
    parsed = _parse_reliable_utc_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed is not None else None


def _parse_reliable_utc_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _timestamp_ms_to_utc(value: int | None) -> str | None:
    parsed = _timestamp_ms_to_datetime(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed is not None else None


def _timestamp_ms_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return parsed


def _normalize_evidence_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _add_response(request: AddRequest) -> AddResponse:
    return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
