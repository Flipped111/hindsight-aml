from __future__ import annotations

from pathlib import Path

import pytest

from aml_adapter.schemas import AddResponse, SearchResponse, SearchResult
from tools.aml_compare import compare_reports
from tools.aml_eval import AmlClientProtocol, EndpointOutcome, EvaluationManifest, EvaluationQuery, run_manifest
from tests.aml_eval.test_runner import build_manifest


class ComparisonClient:
    def __init__(self, search_outcomes: list[EndpointOutcome]) -> None:
        self._search_outcomes = iter(search_outcomes)

    async def add(self, request) -> EndpointOutcome:
        return EndpointOutcome(
            status_code=200,
            elapsed_ms=10,
            payload=AddResponse(
                request_id=request.request_id,
                user_id=request.user_id,
                session_id=request.session_id,
            ),
        )

    async def search(self, request: EvaluationQuery) -> EndpointOutcome:
        return next(self._search_outcomes)


def comparison_manifest() -> EvaluationManifest:
    manifest = build_manifest()
    original = manifest.cases[0].searches[0]
    manifest.cases[0].searches = [
        original.model_copy(
            update={
                "query_id": "q-1",
                "category": "1",
                "expected_terms": ["target-one"],
                "expected_evidence_terms": ["target-one"],
            }
        ),
        original.model_copy(
            update={
                "query_id": "q-2",
                "category": "2",
                "expected_terms": ["target-two"],
                "expected_evidence_terms": ["target-two"],
            }
        ),
    ]
    return manifest


@pytest.mark.asyncio
async def test_compare_reports_counts_rank_wins_and_category_deltas() -> None:
    baseline_client: AmlClientProtocol = ComparisonClient(
        [
            EndpointOutcome(
                status_code=200,
                elapsed_ms=20,
                payload=SearchResponse(
                    data=[
                        SearchResult(id="other", content="other"),
                        SearchResult(id="target-one", content="target-one"),
                    ]
                ),
            ),
            EndpointOutcome(status_code=200, elapsed_ms=40, payload=SearchResponse(data=[])),
        ]
    )
    candidate_client: AmlClientProtocol = ComparisonClient(
        [
            EndpointOutcome(
                status_code=200,
                elapsed_ms=25,
                payload=SearchResponse(data=[SearchResult(id="target-one", content="target-one")]),
            ),
            EndpointOutcome(
                status_code=200,
                elapsed_ms=45,
                payload=SearchResponse(data=[SearchResult(id="target-two", content="target-two")]),
            ),
        ]
    )
    manifest = comparison_manifest()

    baseline = await run_manifest(manifest, baseline_client)
    candidate = await run_manifest(manifest, candidate_client)
    comparison = compare_reports(baseline, candidate)

    assert comparison.overall.rank.wins == 2
    assert comparison.overall.rank.losses == 0
    assert comparison.overall.rank.new_hits == 1
    assert comparison.overall.evidence_rank.wins == 2
    assert comparison.overall.hit_at_1.delta == 1
    assert comparison.overall.mrr.baseline == 0.25
    assert comparison.overall.mrr.candidate == 1
    assert comparison.by_category["1"].rank.wins == 1
    assert comparison.by_category["2"].rank.new_hits == 1


@pytest.mark.asyncio
async def test_compare_reports_rejects_different_query_sets(tmp_path: Path) -> None:
    manifest = comparison_manifest()
    client: AmlClientProtocol = ComparisonClient(
        [
            EndpointOutcome(status_code=200, elapsed_ms=20, payload=SearchResponse(data=[])),
            EndpointOutcome(status_code=200, elapsed_ms=20, payload=SearchResponse(data=[])),
        ]
    )
    baseline = await run_manifest(manifest, client)
    candidate = baseline.model_copy(deep=True)
    candidate.cases[0].searches.pop()

    with pytest.raises(ValueError, match="different query IDs"):
        compare_reports(baseline, candidate)
