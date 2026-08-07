from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from aml_adapter.app import create_app
from aml_adapter.schemas import AddRequest, MemoryEvidence, Message, RetainItem
from aml_adapter.service import IdempotencyPolicy, MemoryService
from aml_adapter.storage import IdempotencyStore


@dataclass(frozen=True)
class RetainCall:
    bank_id: str
    items: list[RetainItem]


@dataclass(frozen=True)
class RecallCall:
    bank_id: str
    query: str


class FakeGateway:
    def __init__(self) -> None:
        self.retain_calls: list[RetainCall] = []
        self.recall_calls: list[RecallCall] = []
        self.retain_started = asyncio.Event()
        self.retain_gate = asyncio.Event()
        self.retain_gate.set()
        self.healthy = True
        self.closed = False
        self._retain_failure: Exception | None = None
        self._recall_failure: Exception | None = None
        self._recall_results: dict[str, list[MemoryEvidence]] = {}
        self._memories: dict[str, dict[str, RetainItem]] = {}

    def fail_next_retain(self, error: Exception) -> None:
        self._retain_failure = error

    def fail_next_recall(self, error: Exception) -> None:
        self._recall_failure = error

    def set_recall_results(self, bank_id: str, results: list[MemoryEvidence]) -> None:
        self._recall_results[bank_id] = results

    def memories_for(self, bank_id: str) -> list[RetainItem]:
        return list(self._memories.get(bank_id, {}).values())

    async def retain(self, bank_id: str, items: list[RetainItem]) -> None:
        self.retain_calls.append(RetainCall(bank_id=bank_id, items=list(items)))
        self.retain_started.set()
        await self.retain_gate.wait()
        if self._retain_failure is not None:
            error = self._retain_failure
            self._retain_failure = None
            raise error

        bank = self._memories.setdefault(bank_id, {})
        for item in items:
            # Hindsight treats the stable document ID as the source identity.
            # Model that upsert here so abandoned-claim retries cannot create duplicates.
            bank[item.document_id] = item

    async def recall(self, bank_id: str, query: str) -> list[MemoryEvidence]:
        self.recall_calls.append(RecallCall(bank_id=bank_id, query=query))
        if self._recall_failure is not None:
            error = self._recall_failure
            self._recall_failure = None
            raise error
        if bank_id in self._recall_results:
            return list(self._recall_results[bank_id])

        retained_items = list(self._memories.get(bank_id, {}).values())
        timestamped = sorted(
            (item for item in retained_items if isinstance(item.timestamp, datetime)),
            key=lambda item: item.timestamp,
            reverse=True,
        )
        memories = timestamped + [item for item in retained_items if not isinstance(item.timestamp, datetime)]
        return [
            MemoryEvidence(
                id=item.document_id,
                text=item.content,
                score=1.0 - (index / 1000),
                mentioned_at=item.timestamp.isoformat() if isinstance(item.timestamp, datetime) else None,
            )
            for index, item in enumerate(memories)
        ]

    async def health(self) -> bool:
        return self.healthy

    async def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class ServiceHarness:
    service: MemoryService
    gateway: FakeGateway
    database_path: Path


def add_request(
    *,
    request_id: str = "request-1",
    user_id: str = "user-1",
    session_id: str = "session-1",
    timestamp: int = 1_704_067_200_000,
    content: str = "我现在住在东京。",
) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        messages=[Message(role="user", timestamp=timestamp, content=content)],
    )


def build_harness(
    database_path: Path,
    *,
    lease_seconds: float = 1,
    wait_timeout_seconds: float = 1,
    poll_interval_seconds: float = 0.01,
) -> ServiceHarness:
    gateway = FakeGateway()
    store = IdempotencyStore(database_path, lease_seconds=lease_seconds)
    service = MemoryService(
        gateway=gateway,
        store=store,
        policy=IdempotencyPolicy(
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ),
    )
    return ServiceHarness(service=service, gateway=gateway, database_path=database_path)


@asynccontextmanager
async def app_client(service: MemoryService) -> AsyncIterator[AsyncClient]:
    app = create_app(service)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
