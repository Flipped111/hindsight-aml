from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aml_adapter.schemas import AddRequest
from aml_adapter.service import MemoryDependencyError, user_to_bank_id
from aml_adapter.storage import ClaimStatus, IdempotencyStore, LeaseLostError, RequestIdentity
from tests.aml_adapter.support import add_request, app_client, build_harness


@pytest.mark.asyncio
async def test_completed_request_retry_does_not_retain_twice(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request()

    async with app_client(harness.service) as client:
        first = await client.post("/add", json=request.model_dump(mode="json"))
        second = await client.post("/add", json=request.model_dump(mode="json"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(harness.gateway.retain_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_request",
    [
        add_request(user_id="other-user"),
        add_request(session_id="other-session"),
        add_request(content="不同的 payload"),
    ],
    ids=["user", "session", "payload"],
)
async def test_reused_request_id_with_different_identity_conflicts(
    tmp_path: Path,
    changed_request: AddRequest,
) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    original = add_request()

    async with app_client(harness.service) as client:
        first = await client.post("/add", json=original.model_dump(mode="json"))
        conflict = await client.post("/add", json=changed_request.model_dump(mode="json"))

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert len(harness.gateway.retain_calls) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_requests_wait_for_one_retain(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3", wait_timeout_seconds=2)
    harness.gateway.retain_gate.clear()
    request = add_request()

    async with app_client(harness.service) as client:
        first_task = asyncio.create_task(client.post("/add", json=request.model_dump(mode="json")))
        await asyncio.wait_for(harness.gateway.retain_started.wait(), timeout=1)
        second_task = asyncio.create_task(client.post("/add", json=request.model_dump(mode="json")))
        await asyncio.sleep(0.05)
        assert not second_task.done()
        harness.gateway.retain_gate.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(harness.gateway.retain_calls) == 1


@pytest.mark.asyncio
async def test_waiting_duplicate_returns_503_after_deadline(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path / "idempotency.sqlite3",
        wait_timeout_seconds=0.03,
        poll_interval_seconds=0.005,
    )
    harness.gateway.retain_gate.clear()
    request = add_request()

    async with app_client(harness.service) as client:
        first_task = asyncio.create_task(client.post("/add", json=request.model_dump(mode="json")))
        await asyncio.wait_for(harness.gateway.retain_started.wait(), timeout=1)
        try:
            waiting = await client.post("/add", json=request.model_dump(mode="json"))
        finally:
            harness.gateway.retain_gate.set()
        first = await first_task

    assert first.status_code == 200
    assert waiting.status_code == 503
    assert len(harness.gateway.retain_calls) == 1


@pytest.mark.asyncio
async def test_completed_idempotency_record_survives_service_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "idempotency.sqlite3"
    request = add_request()
    first_harness = build_harness(database_path)

    async with app_client(first_harness.service) as client:
        first = await client.post("/add", json=request.model_dump(mode="json"))

    second_harness = build_harness(database_path)
    async with app_client(second_harness.service) as client:
        second = await client.post("/add", json=request.model_dump(mode="json"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(first_harness.gateway.retain_calls) == 1
    assert second_harness.gateway.retain_calls == []


@pytest.mark.asyncio
async def test_abandoned_claim_can_be_reacquired_without_old_owner_completing(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.sqlite3", lease_seconds=0.01)
    await store.initialize()
    identity = RequestIdentity(
        request_id="request-1",
        user_id="user-1",
        session_id="session-1",
        payload_hash="payload-hash",
    )

    first = await store.claim(identity)
    assert first.status == ClaimStatus.ACQUIRED
    assert first.owner_token is not None
    await asyncio.sleep(0.02)
    replacement = await store.claim(identity)
    assert replacement.status == ClaimStatus.ACQUIRED
    assert replacement.owner_token is not None
    assert replacement.owner_token != first.owner_token

    with pytest.raises(LeaseLostError):
        await store.complete(identity.request_id, first.owner_token)
    await store.complete(identity.request_id, replacement.owner_token)
    completed = await store.claim(identity)
    assert completed.status == ClaimStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_retain_releases_claim_for_safe_retry(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request()
    harness.gateway.fail_next_retain(MemoryDependencyError("temporary failure"))

    async with app_client(harness.service) as client:
        failed = await client.post("/add", json=request.model_dump(mode="json"))
        retried = await client.post("/add", json=request.model_dump(mode="json"))

    assert failed.status_code == 502
    assert retried.status_code == 200
    assert len(harness.gateway.retain_calls) == 2
    assert len(harness.gateway.memories_for(user_to_bank_id(request.user_id))) == 1
