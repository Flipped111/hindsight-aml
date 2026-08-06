from __future__ import annotations

from pathlib import Path

import pytest

from aml_adapter.schemas import SearchRequest, SearchResponse
from aml_adapter.service import user_to_bank_id
from tests.aml_adapter.support import add_request, app_client, build_harness


@pytest.mark.asyncio
async def test_users_are_strictly_isolated_by_bank(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    user_a = add_request(request_id="request-a", user_id="user-a", content="我现在住在东京。")
    user_b = add_request(request_id="request-b", user_id="user-b", content="我现在住在上海。")

    async with app_client(harness.service) as client:
        assert (await client.post("/add", json=user_a.model_dump(mode="json"))).status_code == 200
        assert (await client.post("/add", json=user_b.model_dump(mode="json"))).status_code == 200
        result_a = await client.post(
            "/search",
            json=SearchRequest(query="住在哪里？", options=[], user_id="user-a", top_k=100).model_dump(mode="json"),
        )
        result_b = await client.post(
            "/search",
            json=SearchRequest(query="住在哪里？", options=[], user_id="user-b", top_k=100).model_dump(mode="json"),
        )

    content_a = "\n".join(item.content for item in SearchResponse.model_validate(result_a.json()).data)
    content_b = "\n".join(item.content for item in SearchResponse.model_validate(result_b.json()).data)
    assert "东京" in content_a
    assert "上海" not in content_a
    assert "上海" in content_b
    assert "东京" not in content_b
    assert harness.gateway.recall_calls[0].bank_id == user_to_bank_id("user-a")
    assert harness.gateway.recall_calls[1].bank_id == user_to_bank_id("user-b")
    assert harness.gateway.recall_calls[0].bank_id != harness.gateway.recall_calls[1].bank_id


@pytest.mark.asyncio
async def test_different_requests_in_same_session_do_not_overwrite(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    first = add_request(request_id="chunk-1", session_id="shared-session", content="第一段：住在上海。")
    second = add_request(request_id="chunk-2", session_id="shared-session", content="第二段：计划搬家。")

    async with app_client(harness.service) as client:
        await client.post("/add", json=first.model_dump(mode="json"))
        await client.post("/add", json=second.model_dump(mode="json"))

    memories = harness.gateway.memories_for(user_to_bank_id("user-1"))
    assert len(memories) == 2
    assert len({item.document_id for item in memories}) == 2
    combined = "\n".join(item.content for item in memories)
    assert "第一段" in combined
    assert "第二段" in combined


@pytest.mark.asyncio
async def test_newer_event_is_returned_first_for_current_fact_query(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    old = add_request(
        request_id="location-2025",
        timestamp=1_735_689_600_000,
        content="我现在住在上海。",
    )
    new = add_request(
        request_id="location-2026",
        timestamp=1_767_225_600_000,
        content="我已经搬到东京，现在住在东京。",
    )
    search = SearchRequest(query="我现在住在哪里？", options=["上海", "东京"], user_id="user-1", top_k=5)

    async with app_client(harness.service) as client:
        await client.post("/add", json=old.model_dump(mode="json"))
        await client.post("/add", json=new.model_dump(mode="json"))
        response = await client.post("/search", json=search.model_dump(mode="json"))

    data = SearchResponse.model_validate(response.json()).data
    assert len(data) == 2
    assert "东京" in data[0].content
    assert data[0].score is not None and data[1].score is not None
    assert data[0].score > data[1].score
