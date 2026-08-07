from __future__ import annotations

from pathlib import Path

import pytest

from aml_adapter.schemas import MemoryEvidence, SearchResponse
from aml_adapter.service import user_to_bank_id
from aml_adapter.temporal_reranking import TemporalIntent, analyze_temporal_query
from tests.aml_adapter.support import app_client, build_harness


@pytest.mark.asyncio
async def test_current_query_promotes_newer_conflicting_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    user_id = "temporal-user"
    harness.gateway.set_recall_results(
        user_to_bank_id(user_id),
        [
            MemoryEvidence(
                id="old-home",
                text="The user lived in Shanghai.",
                score=0.99,
                mentioned_at="2025-01-01T00:00:00Z",
            ),
            MemoryEvidence(
                id="new-home",
                text="The user moved to Tokyo.",
                score=0.98,
                mentioned_at="2026-01-01T00:00:00Z",
            ),
        ],
    )

    async with app_client(harness.service) as client:
        response = await client.post(
            "/search",
            json={"query": "Where does the user currently live?", "user_id": user_id, "top_k": 2},
        )

    payload = SearchResponse.model_validate(response.json())
    assert [item.id for item in payload.data] == ["new-home", "old-home"]
    assert payload.data[0].score is not None
    assert payload.data[1].score is not None
    assert payload.data[0].score > payload.data[1].score


@pytest.mark.asyncio
async def test_explicit_historical_year_promotes_matching_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    user_id = "temporal-user"
    harness.gateway.set_recall_results(
        user_to_bank_id(user_id),
        [
            MemoryEvidence(
                id="new-home",
                text="The user moved to Tokyo.",
                score=0.99,
                mentioned_at="2026-01-01T00:00:00Z",
            ),
            MemoryEvidence(
                id="old-home",
                text="The user lived in Shanghai.",
                score=0.98,
                mentioned_at="2025-01-01T00:00:00Z",
            ),
        ],
    )

    async with app_client(harness.service) as client:
        response = await client.post(
            "/search",
            json={"query": "Where did the user live in 2025?", "user_id": user_id, "top_k": 2},
        )

    payload = SearchResponse.model_validate(response.json())
    assert [item.id for item in payload.data] == ["old-home", "new-home"]


@pytest.mark.asyncio
async def test_previous_query_promotes_older_conflicting_evidence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    user_id = "temporal-user"
    harness.gateway.set_recall_results(
        user_to_bank_id(user_id),
        [
            MemoryEvidence(
                id="new-home",
                text="The user moved to Tokyo.",
                score=0.99,
                mentioned_at="2026-01-01T00:00:00Z",
            ),
            MemoryEvidence(
                id="old-home",
                text="The user lived in Shanghai.",
                score=0.98,
                mentioned_at="2025-01-01T00:00:00Z",
            ),
        ],
    )

    async with app_client(harness.service) as client:
        response = await client.post(
            "/search",
            json={"query": "Where did the user previously live?", "user_id": user_id, "top_k": 2},
        )

    payload = SearchResponse.model_validate(response.json())
    assert [item.id for item in payload.data] == ["old-home", "new-home"]


@pytest.mark.asyncio
async def test_non_temporal_query_preserves_relevance_order_and_scores(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    user_id = "temporal-user"
    harness.gateway.set_recall_results(
        user_to_bank_id(user_id),
        [
            MemoryEvidence(
                id="first",
                text="The user likes tea.",
                score=0.99,
                mentioned_at="2025-01-01T00:00:00Z",
            ),
            MemoryEvidence(
                id="second",
                text="The user likes coffee.",
                score=0.98,
                mentioned_at="2026-01-01T00:00:00Z",
            ),
        ],
    )

    async with app_client(harness.service) as client:
        response = await client.post(
            "/search",
            json={"query": "What beverages does the user like?", "user_id": user_id, "top_k": 2},
        )

    payload = SearchResponse.model_validate(response.json())
    assert [item.id for item in payload.data] == ["first", "second"]
    assert [item.score for item in payload.data] == [1 / 61, 1 / 62]


@pytest.mark.asyncio
async def test_missing_event_time_is_not_invented_or_boosted(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    user_id = "temporal-user"
    harness.gateway.set_recall_results(
        user_to_bank_id(user_id),
        [MemoryEvidence(id="unknown-time", text="The user lives in Tokyo.", score=0.99)],
    )

    async with app_client(harness.service) as client:
        response = await client.post(
            "/search",
            json={"query": "Where does the user currently live?", "user_id": user_id, "top_k": 1},
        )

    payload = SearchResponse.model_validate(response.json())
    assert payload.data[0].created_at is None
    assert payload.data[0].score == 1 / 61


def test_temporal_intent_recognizes_chinese_and_year_relations() -> None:
    assert analyze_temporal_query("用户现在住在哪里？").intent == TemporalIntent.CURRENT
    assert analyze_temporal_query("用户以前住在哪里？").intent == TemporalIntent.PREVIOUS
    assert analyze_temporal_query("用户在 2025 年住在哪里？").intent == TemporalIntent.TARGET_YEAR
    assert analyze_temporal_query("用户在 2025 年之后住在哪里？").intent == TemporalIntent.AFTER_YEAR
    assert analyze_temporal_query("用户在 2025 年之前住在哪里？").intent == TemporalIntent.BEFORE_YEAR
    assert analyze_temporal_query("What was reservation code 2025?").intent == TemporalIntent.NONE
    assert analyze_temporal_query("What were codes 2025 and 1984?").intent == TemporalIntent.NONE
    assert analyze_temporal_query("What workshop did Caroline attend recently?").intent == TemporalIntent.NONE
    assert analyze_temporal_query("她最近参加了什么活动？").intent == TemporalIntent.NONE
