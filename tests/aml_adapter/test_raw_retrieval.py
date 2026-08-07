from __future__ import annotations

from pathlib import Path

import pytest

from aml_adapter.raw_retrieval import RawMessageDocument, rank_raw_messages
from aml_adapter.schemas import AddRequest, MemoryEvidence, Message, SearchResponse
from aml_adapter.service import MemoryDependencyError, user_to_bank_id
from tests.aml_adapter.support import add_request, app_client, build_harness


def test_raw_ranker_handles_cjk_updates_and_exact_numbers() -> None:
    documents = [
        RawMessageDocument(
            document_id="tokyo",
            content="Speaker: user\nMessage: 我现在住在东京。",
            session_id="session-2",
            role="user",
            timestamp_ms=1_736_899_200_000,
        ),
        RawMessageDocument(
            document_id="shanghai",
            content="Speaker: user\nMessage: 我以前住在上海。",
            session_id="session-1",
            role="user",
            timestamp_ms=1_704_067_200_000,
        ),
        RawMessageDocument(
            document_id="reservation",
            content="Speaker: assistant\nMessage: Your reservation number is ZX-4187.",
            session_id="session-3",
            role="assistant",
            timestamp_ms=None,
        ),
    ]

    current = rank_raw_messages("现在住在哪里？", documents, 3)
    historical = rank_raw_messages("以前住在哪里？", documents, 3)
    numeric = rank_raw_messages("What was the reservation number ZX-4187?", documents, 3)

    assert current[0].document_id == "tokyo"
    assert historical[0].document_id == "shanghai"
    assert numeric[0].document_id == "reservation"


def test_raw_ranker_places_speaker_only_matches_after_text_matches() -> None:
    documents = [
        RawMessageDocument(
            document_id="speaker-only",
            content="Speaker: Caroline\nMessage: I enjoyed the sunny weather.",
            search_text="I enjoyed the sunny weather.",
            session_id="session-1",
            role="Caroline",
            timestamp_ms=None,
        ),
        RawMessageDocument(
            document_id="text-match",
            content="Speaker: Caroline\nMessage: I painted a lake sunrise.",
            search_text="I painted a lake sunrise.",
            session_id="session-1",
            role="Caroline",
            timestamp_ms=None,
        ),
    ]

    ranked = rank_raw_messages("What did Caroline paint?", documents, 2)

    assert [item.document_id for item in ranked] == ["text-match", "speaker-only"]


def test_raw_ranker_does_not_treat_an_addressed_speaker_name_as_message_evidence() -> None:
    documents = [
        RawMessageDocument(
            document_id="addressed-name",
            content="Speaker: Melanie\nMessage: Hi Caroline, the weather is lovely.",
            search_text="Hi Caroline, the weather is lovely.",
            session_id="session-1",
            role="Melanie",
            timestamp_ms=None,
        ),
        RawMessageDocument(
            document_id="self-authored",
            content="Speaker: Caroline\nMessage: I enjoy the sunny weather.",
            search_text="I enjoy the sunny weather.",
            session_id="session-1",
            role="Caroline",
            timestamp_ms=None,
        ),
    ]

    ranked = rank_raw_messages("What field would Caroline pursue in education?", documents, 2)

    assert [item.document_id for item in ranked] == ["self-authored"]
    assert ranked[0].lexical_score == 1e-6


@pytest.mark.asyncio
async def test_hybrid_search_recovers_exact_detail_from_raw_message(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request(content="The private project codename was Cobalt-17.")
    bank_id = user_to_bank_id(request.user_id)

    async with app_client(harness.service) as client:
        added = await client.post("/add", json=request.model_dump(mode="json"))
        harness.gateway.set_recall_results(
            bank_id,
            [
                MemoryEvidence(id=f"fact-{index}", text=f"General project fact {index}", score=1 - index / 10)
                for index in range(5)
            ],
        )
        response = await client.post(
            "/search",
            json={
                "query": "What was the private project codename?",
                "user_id": request.user_id,
                "top_k": 5,
            },
        )

    assert added.status_code == 200
    payload = SearchResponse.model_validate(response.json())
    raw_results = [item for item in payload.data if item.id.startswith("raw:")]
    assert len(raw_results) == 1
    assert "Cobalt-17" in raw_results[0].content


@pytest.mark.asyncio
async def test_raw_messages_survive_service_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "idempotency.sqlite3"
    first = build_harness(database_path)
    request = add_request(content="The launch phrase is silver comet.")

    async with app_client(first.service) as client:
        added = await client.post("/add", json=request.model_dump(mode="json"))
    assert added.status_code == 200

    restarted = build_harness(database_path)
    restarted.gateway.set_recall_results(user_to_bank_id(request.user_id), [])
    async with app_client(restarted.service) as client:
        response = await client.post(
            "/search",
            json={"query": "What is the launch phrase?", "user_id": request.user_id, "top_k": 5},
        )

    payload = SearchResponse.model_validate(response.json())
    assert len(payload.data) == 1
    assert payload.data[0].id.startswith("raw:")
    assert "silver comet" in payload.data[0].content


@pytest.mark.asyncio
async def test_failed_add_does_not_expose_pending_raw_message(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    harness.gateway.fail_next_retain(MemoryDependencyError("retain unavailable"))
    request = add_request(content="The hidden phrase is amber kite.")

    async with app_client(harness.service) as client:
        failed = await client.post("/add", json=request.model_dump(mode="json"))
        harness.gateway.set_recall_results(user_to_bank_id(request.user_id), [])
        response = await client.post(
            "/search",
            json={"query": "What is the hidden phrase?", "user_id": request.user_id, "top_k": 5},
        )

    assert failed.status_code == 502
    assert response.json() == {"data": []}


@pytest.mark.asyncio
async def test_raw_message_search_is_strictly_user_isolated(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request(user_id="user-a", content="My private marker is violet harbor.")

    async with app_client(harness.service) as client:
        added = await client.post("/add", json=request.model_dump(mode="json"))
        harness.gateway.set_recall_results(user_to_bank_id("user-a"), [])
        harness.gateway.set_recall_results(user_to_bank_id("user-b"), [])
        own = await client.post(
            "/search",
            json={"query": "What is my private marker?", "user_id": "user-a", "top_k": 5},
        )
        other = await client.post(
            "/search",
            json={"query": "What is my private marker?", "user_id": "user-b", "top_k": 5},
        )

    assert added.status_code == 200
    assert len(own.json()["data"]) == 1
    assert other.json() == {"data": []}


@pytest.mark.asyncio
async def test_raw_search_indexes_speaker_name_for_self_authored_messages(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = AddRequest(
        request_id="speaker-index-request",
        messages=[Message(role="Caroline", timestamp=1_683_552_960_000, content="I am a marine biologist.")],
        user_id="speaker-index-user",
        session_id="speaker-index-session",
    )

    async with app_client(harness.service) as client:
        added = await client.post("/add", json=request.model_dump(mode="json"))
        harness.gateway.set_recall_results(user_to_bank_id(request.user_id), [])
        response = await client.post(
            "/search",
            json={"query": "What is Caroline's occupation?", "user_id": request.user_id, "top_k": 5},
        )

    assert added.status_code == 200
    payload = SearchResponse.model_validate(response.json())
    assert len(payload.data) == 1
    assert payload.data[0].id.startswith("raw:")
    assert "marine biologist" in payload.data[0].content


@pytest.mark.asyncio
async def test_exact_fact_and_raw_message_are_returned_once(tmp_path: Path) -> None:
    harness = build_harness(tmp_path / "idempotency.sqlite3")
    request = add_request(content="The project codename was Cobalt-17.")
    document_id = ""

    async with app_client(harness.service) as client:
        added = await client.post("/add", json=request.model_dump(mode="json"))
        document_id = harness.gateway.retain_calls[0].items[0].document_id
        retained_content = harness.gateway.retain_calls[0].items[0].content
        harness.gateway.set_recall_results(
            user_to_bank_id(request.user_id),
            [
                MemoryEvidence(
                    id="fact-1",
                    text=retained_content,
                    score=0.9,
                    document_id=document_id,
                )
            ],
        )
        response = await client.post(
            "/search",
            json={"query": "What was the project codename?", "user_id": request.user_id, "top_k": 5},
        )

    assert added.status_code == 200
    assert response.json()["data"] == [{"id": "fact-1", "content": retained_content, "score": 1 / 61}]
