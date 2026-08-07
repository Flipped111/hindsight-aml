from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from aml_adapter.schemas import AddRequest, AddResponse, Message, SearchResponse, SearchResult
from tools.aml_eval import (
    AmlClient,
    AmlClientProtocol,
    EndpointOutcome,
    EvaluationCase,
    EvaluationManifest,
    EvaluationQuery,
    SearchMetric,
    load_manifest,
    run_manifest,
)


class FakeAmlClient:
    def __init__(self, *, add_outcomes: list[EndpointOutcome], search_outcomes: list[EndpointOutcome]) -> None:
        self._add_outcomes = iter(add_outcomes)
        self._search_outcomes = iter(search_outcomes)
        self.add_requests: list[AddRequest] = []
        self.search_requests: list[EvaluationQuery] = []

    async def add(self, request: AddRequest) -> EndpointOutcome:
        self.add_requests.append(request)
        return next(self._add_outcomes)

    async def search(self, request: EvaluationQuery) -> EndpointOutcome:
        self.search_requests.append(request)
        return next(self._search_outcomes)


def build_manifest() -> EvaluationManifest:
    return EvaluationManifest(
        cases=[
            EvaluationCase(
                case_id="case-1",
                adds=[
                    AddRequest(
                        request_id="add-1",
                        messages=[Message(role="user", content="I live in Tokyo.")],
                        user_id="user-1",
                        session_id="session-1",
                    )
                ],
                searches=[
                    EvaluationQuery(
                        query_id="query-1",
                        category="temporal",
                        query="Where do I live?",
                        user_id="user-1",
                        top_k=5,
                        expected_terms=["Tokyo"],
                        expected_evidence_terms=["User lives in Tokyo."],
                    )
                ],
            )
        ]
    )


@pytest.mark.asyncio
async def test_run_manifest_records_latency_and_evidence_metrics() -> None:
    client: AmlClientProtocol = FakeAmlClient(
        add_outcomes=[
            EndpointOutcome(
                status_code=200,
                elapsed_ms=12.5,
                payload=AddResponse(request_id="add-1", user_id="user-1", session_id="session-1"),
            )
        ],
        search_outcomes=[
            EndpointOutcome(
                status_code=200,
                elapsed_ms=34.5,
                payload=SearchResponse(
                    data=[
                        SearchResult(id="other", content="User likes tea.", score=0.8),
                        SearchResult(id="tokyo", content="User lives in Tokyo.", score=0.7),
                    ]
                ),
            )
        ],
    )

    report = await run_manifest(build_manifest(), client)

    assert report.aggregate.add_success_rate == 1
    assert report.aggregate.search_success_rate == 1
    assert report.aggregate.hit_at_1 == 0
    assert report.aggregate.hit_at_5 == 1
    assert report.aggregate.mrr == 0.5
    assert report.aggregate.evidence_hit_at_5 == 1
    assert report.aggregate.evidence_mrr == 0.5
    assert report.by_category["temporal"].searches == 1
    assert report.by_category["temporal"].mrr == 0.5
    assert report.cases[0].searches[0].all_expected_terms_found is True
    assert report.cases[0].searches[0].all_expected_evidence_terms_found is True
    assert [request.request_id for request in client.add_requests] == ["add-1"]
    assert [request.query_id for request in client.search_requests] == ["query-1"]


@pytest.mark.asyncio
async def test_failed_add_skips_search_and_marks_report_failed() -> None:
    client: AmlClientProtocol = FakeAmlClient(
        add_outcomes=[EndpointOutcome(status_code=502, elapsed_ms=7, error="upstream unavailable")],
        search_outcomes=[],
    )

    report = await run_manifest(build_manifest(), client)

    assert report.has_failures is True
    assert report.cases[0].searches[0].status_code == 0
    assert report.cases[0].searches[0].error == "skipped because an Add request failed"
    assert client.search_requests == []


@pytest.mark.asyncio
async def test_run_manifest_can_skip_adds_for_shared_memory_comparison() -> None:
    client: AmlClientProtocol = FakeAmlClient(
        add_outcomes=[],
        search_outcomes=[
            EndpointOutcome(
                status_code=200,
                elapsed_ms=10,
                payload=SearchResponse(data=[SearchResult(id="tokyo", content="User lives in Tokyo.")]),
            )
        ],
    )

    report = await run_manifest(build_manifest(), client, skip_adds=True)

    assert report.aggregate.add_requests == 0
    assert report.aggregate.search_success_rate == 1
    assert report.aggregate.hit_at_1 == 1
    assert client.add_requests == []


@pytest.mark.asyncio
async def test_http_client_sends_only_official_search_fields() -> None:
    captured_body: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_body.append(json.loads(request.content))
        return httpx.Response(200, json={"data": []})

    query = EvaluationQuery(
        query_id="internal-query-id",
        category="temporal",
        query="Where do I live?",
        user_id="user-1",
        top_k=5,
        expected_terms=["Tokyo"],
        expected_evidence_terms=["User lives in Tokyo."],
    )
    async with AmlClient("http://test", timeout_seconds=5, transport=httpx.MockTransport(handler)) as client:
        outcome = await client.search(query)

    assert outcome.succeeded is True
    assert captured_body == [{"query": "Where do I live?", "options": [], "user_id": "user-1", "top_k": 5}]


@pytest.mark.asyncio
async def test_http_client_rejects_false_success_and_mismatched_add_ids() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                json={"success": False, "request_id": "add-1", "user_id": "user-1", "session_id": "session-1"},
            ),
            httpx.Response(
                200,
                json={"success": True, "request_id": "other", "user_id": "user-1", "session_id": "session-1"},
            ),
        ]
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    request = build_manifest().cases[0].adds[0]
    async with AmlClient("http://test", timeout_seconds=5, transport=httpx.MockTransport(handler)) as client:
        false_success = await client.add(request)
        mismatched_ids = await client.add(request)

    assert false_success.succeeded is False
    assert false_success.error == "Add response reported success=false"
    assert mismatched_ids.succeeded is False
    assert mismatched_ids.error == "Add response IDs do not match the request"


def test_load_manifest_accepts_optional_timestamp_and_options(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"cases":[{"case_id":"case-1","adds":[{"request_id":"add-1",'
        '"messages":[{"role":"user","content":"No date."}],"user_id":"user-1",'
        '"session_id":"session-1"}],"searches":[{"query_id":"query-1",'
        '"query":"What happened?","user_id":"user-1","top_k":5}]}]}',
        encoding="utf-8",
    )

    manifest = load_manifest(manifest_path)

    assert manifest.cases[0].adds[0].messages[0].timestamp is None
    assert manifest.cases[0].searches[0].options == []
    assert manifest.cases[0].searches[0].expected_terms == []


def test_search_metric_matches_human_date_against_iso_evidence() -> None:
    query = EvaluationQuery(
        query_id="date-query",
        query="When did the event happen?",
        user_id="user-1",
        top_k=5,
        expected_terms=["7 May 2023"],
    )
    outcome = EndpointOutcome(
        status_code=200,
        elapsed_ms=10,
        payload=SearchResponse(data=[SearchResult(id="date", content="When: 2023-05-07")]),
    )

    metric = SearchMetric.from_outcome(query, outcome)

    assert metric.first_hit_rank == 1
