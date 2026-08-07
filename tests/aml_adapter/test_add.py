from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from hindsight_client import Hindsight

from aml_adapter.schemas import AddResponse, Message, RetainItem
from aml_adapter.service import HindsightGateway, MemoryDependencyError, user_to_bank_id
from tests.aml_adapter.support import add_request, app_client, build_harness


@pytest.mark.asyncio
async def test_add_returns_ids_and_retains_complete_original_message(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request()

    async with app_client(harness.service) as client:
        response = await client.post("/add", json=request.model_dump(mode="json"))

    assert response.status_code == 200
    assert AddResponse.model_validate(response.json()) == AddResponse(
        request_id=request.request_id,
        user_id=request.user_id,
        session_id=request.session_id,
    )
    assert len(harness.gateway.retain_calls) == 1
    call = harness.gateway.retain_calls[0]
    assert call.bank_id == user_to_bank_id(request.user_id)
    assert len(call.items) == 1

    item = call.items[0]
    assert item.document_id == hashlib.sha256(b"request-1:0").hexdigest()
    assert item.timestamp == datetime(2024, 1, 1, tzinfo=UTC)
    assert item.content == (
        "Speaker: user\nSession: session-1\nEvent time: 2024-01-01T00:00:00.000Z\nMessage: 我现在住在东京。"
    )
    assert item.metadata == {
        "request_id": "request-1",
        "session_id": "session-1",
        "role": "user",
        "original_timestamp": "1704067200000",
    }


@pytest.mark.asyncio
async def test_add_assigns_a_stable_document_id_to_each_message(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request()
    request.messages.append(Message(role="assistant", timestamp=1_704_067_201_000, content="我记住了。"))

    async with app_client(harness.service) as client:
        response = await client.post("/add", json=request.model_dump(mode="json"))

    assert response.status_code == 200
    items = harness.gateway.retain_calls[0].items
    assert [item.document_id for item in items] == [
        hashlib.sha256(b"request-1:0").hexdigest(),
        hashlib.sha256(b"request-1:1").hexdigest(),
    ]
    assert "Speaker: assistant" in items[1].content
    assert items[1].metadata["role"] == "assistant"


@pytest.mark.asyncio
async def test_add_accepts_message_without_optional_timestamp(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    payload = {
        "request_id": "request-without-timestamp",
        "messages": [{"role": "user", "content": "I live in Kyoto."}],
        "user_id": "user-1",
        "session_id": "session-1",
    }

    async with app_client(harness.service) as client:
        response = await client.post("/add", json=payload)

    assert response.status_code == 200
    item = harness.gateway.retain_calls[0].items[0]
    assert item.timestamp == "unset"
    assert "Event time: Unknown" in item.content
    assert item.metadata["original_timestamp"] == "unset"


@pytest.mark.asyncio
async def test_add_validation_error_uses_structured_422_detail(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    payload = {
        "request_id": "invalid-request",
        "messages": [{"role": "user", "content": "   "}],
        "user_id": "user-1",
        "session_id": "session-1",
    }

    async with app_client(harness.service) as client:
        response = await client.post("/add", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["type"] == "value_error"


@pytest.mark.asyncio
async def test_add_failure_is_not_completed_and_can_be_retried(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    harness.gateway.fail_next_retain(MemoryDependencyError("retain unavailable"))
    request = add_request()

    async with app_client(harness.service) as client:
        failed = await client.post("/add", json=request.model_dump(mode="json"))
        retried = await client.post("/add", json=request.model_dump(mode="json"))

    assert failed.status_code == 502
    assert failed.json() == {"detail": {"reason": "retain unavailable"}}
    assert retried.status_code == 200
    assert len(harness.gateway.retain_calls) == 2
    assert len(harness.gateway.memories_for(user_to_bank_id(request.user_id))) == 1


@pytest.mark.asyncio
async def test_health_requires_both_dependencies(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")

    async with app_client(harness.service) as client:
        healthy = await client.get("/health")
        harness.gateway.healthy = False
        unhealthy = await client.get("/health")

    assert healthy.status_code == 200
    assert healthy.json()["status"] == "healthy"
    assert unhealthy.status_code == 503
    assert unhealthy.json()["status"] == "unhealthy"


class StubHindsight:
    def __init__(self, response: SimpleNamespace | Exception) -> None:
        self.response = response
        self.bank_id: str | None = None
        self.items: list[dict[str, Any]] | None = None
        self.retain_async: bool | None = None

    async def aretain_batch(
        self,
        *,
        bank_id: str,
        items: list[dict[str, Any]],
        retain_async: bool,
    ) -> SimpleNamespace:
        self.bank_id = bank_id
        self.items = items
        self.retain_async = retain_async
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class HangingMonitoring:
    async def health_endpoint_health_get(self) -> None:
        await asyncio.Event().wait()


class HangingHealthHindsight:
    def __init__(self) -> None:
        self.monitoring = HangingMonitoring()


@pytest.mark.asyncio
async def test_hindsight_gateway_uses_synchronous_batch_retain() -> None:
    stub = StubHindsight(SimpleNamespace(success=True, var_async=False, items_count=1))
    gateway = HindsightGateway(cast(Hindsight, stub))
    item = RetainItem(
        content="Message: 东京",
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        metadata={"role": "user"},
        document_id="stable-document-id",
    )

    await gateway.retain("bank-1", [item])

    assert stub.bank_id == "bank-1"
    assert stub.retain_async is False
    assert stub.items is not None
    assert stub.items[0]["document_id"] == "stable-document-id"
    assert stub.items[0]["timestamp"] == datetime(2024, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_hindsight_gateway_rejects_incomplete_retain_confirmation() -> None:
    stub = StubHindsight(SimpleNamespace(success=True, var_async=True, items_count=1))
    gateway = HindsightGateway(cast(Hindsight, stub))
    item = RetainItem(
        content="Message: 东京",
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        metadata={"role": "user"},
        document_id="stable-document-id",
    )

    with pytest.raises(MemoryDependencyError, match="complete synchronous retain"):
        await gateway.retain("bank-1", [item])


@pytest.mark.asyncio
async def test_hindsight_gateway_converts_timeout_to_dependency_failure() -> None:
    stub = StubHindsight(TimeoutError("request timed out"))
    gateway = HindsightGateway(cast(Hindsight, stub))
    item = RetainItem(
        content="Message: 东京",
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        metadata={"role": "user"},
        document_id="stable-document-id",
    )

    with pytest.raises(MemoryDependencyError, match="retain failed"):
        await gateway.retain("bank-1", [item])


@pytest.mark.asyncio
async def test_hindsight_gateway_health_has_an_independent_short_timeout() -> None:
    gateway = HindsightGateway(cast(Hindsight, HangingHealthHindsight()), health_timeout_seconds=0.01)

    healthy = await asyncio.wait_for(gateway.health(), timeout=0.5)

    assert healthy is False
