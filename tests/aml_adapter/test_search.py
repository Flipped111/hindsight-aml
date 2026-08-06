from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from hindsight_client import Hindsight

from aml_adapter.schemas import MemoryEvidence, SearchRequest, SearchResponse
from aml_adapter.service import HindsightGateway, MemoryDependencyError, user_to_bank_id
from tests.aml_adapter.support import app_client, build_harness


def search_request(*, user_id: str = "user-1", top_k: int = 5) -> SearchRequest:
    return SearchRequest(query="现在住在哪里？", options=["东京", "上海"], user_id=user_id, top_k=top_k)


@pytest.mark.asyncio
async def test_search_filters_sorts_and_truncates_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = search_request(top_k=2)
    bank_id = user_to_bank_id(request.user_id)
    harness.gateway.set_recall_results(
        bank_id,
        [
            MemoryEvidence(id="low", text="较低相关度", score=0.2, mentioned_at="2024-01-01T00:00:00Z"),
            MemoryEvidence(id="", text="缺少 ID", score=1.0),
            MemoryEvidence(id="high", text="东京", score=0.9, mentioned_at="2026-01-15T09:00:00+09:00"),
            MemoryEvidence(id="empty", text="   ", score=0.8),
            MemoryEvidence(id="mid", text="日本", score=0.7, mentioned_at="not-a-time"),
        ],
    )

    async with app_client(harness.service) as client:
        response = await client.post("/search", json=request.model_dump(mode="json"))

    assert response.status_code == 200
    payload = SearchResponse.model_validate(response.json())
    assert [item.id for item in payload.data] == ["high", "mid"]
    assert payload.data[0].created_at == "2026-01-15T00:00:00Z"
    assert payload.data[1].created_at is None
    assert harness.gateway.recall_calls[0].bank_id == bank_id
    assert harness.gateway.recall_calls[0].query == request.query


@pytest.mark.asyncio
async def test_search_always_returns_data_for_no_results(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = search_request()

    async with app_client(harness.service) as client:
        response = await client.post("/search", json=request.model_dump(mode="json"))

    assert response.status_code == 200
    assert response.json() == {"data": []}


@pytest.mark.asyncio
async def test_search_dependency_failure_returns_non_2xx(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    harness.gateway.fail_next_recall(MemoryDependencyError("recall unavailable"))

    async with app_client(harness.service) as client:
        response = await client.post("/search", json=search_request().model_dump(mode="json"))

    assert response.status_code == 502


class StubHindsight:
    def __init__(self) -> None:
        self.bank_id: str | None = None
        self.query: str | None = None
        self.budget: str | None = None

    async def arecall(self, *, bank_id: str, query: str, budget: str) -> SimpleNamespace:
        self.bank_id = bank_id
        self.query = query
        self.budget = budget
        scores = SimpleNamespace(final=0.91)
        result = SimpleNamespace(id="memory-1", text="东京", scores=scores, mentioned_at="2026-01-15T09:00:00Z")
        return SimpleNamespace(results=[result])


@pytest.mark.asyncio
async def test_hindsight_gateway_recall_uses_mid_budget_and_final_score() -> None:
    stub = StubHindsight()
    gateway = HindsightGateway(cast(Hindsight, stub))

    evidence = await gateway.recall("bank-1", "住在哪里？")

    assert stub.bank_id == "bank-1"
    assert stub.query == "住在哪里？"
    assert stub.budget == "mid"
    assert evidence == [MemoryEvidence(id="memory-1", text="东京", score=0.91, mentioned_at="2026-01-15T09:00:00Z")]
