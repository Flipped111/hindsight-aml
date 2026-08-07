from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from aml_adapter.schemas import AddRequest, SearchRequest, SearchResponse, SearchResult


class EvaluationQuery(SearchRequest):
    query_id: str
    expected_terms: list[str] = Field(default_factory=list)


class SearchPayload(BaseModel):
    query: str
    options: list[str] = Field(default_factory=list)
    user_id: str
    top_k: int

    @classmethod
    def from_query(cls, query: EvaluationQuery) -> SearchPayload:
        return cls(query=query.query, options=query.options, user_id=query.user_id, top_k=query.top_k)


class EvaluationAddResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    request_id: str
    user_id: str
    session_id: str


class EvaluationCase(BaseModel):
    case_id: str
    adds: list[AddRequest] = Field(min_length=1)
    searches: list[EvaluationQuery] = Field(min_length=1)


class EvaluationManifest(BaseModel):
    cases: list[EvaluationCase] = Field(min_length=1)


@dataclass(frozen=True)
class EndpointOutcome:
    status_code: int
    elapsed_ms: float
    payload: BaseModel | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return 200 <= self.status_code < 300 and self.error is None


class AmlClientProtocol(Protocol):
    async def add(self, request: AddRequest) -> EndpointOutcome: ...

    async def search(self, request: EvaluationQuery) -> EndpointOutcome: ...


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class AmlClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> AmlClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def add(self, request: AddRequest) -> EndpointOutcome:
        outcome = await self._post("/add", request, EvaluationAddResponse)
        if not outcome.succeeded or not isinstance(outcome.payload, EvaluationAddResponse):
            return outcome
        if not outcome.payload.success:
            return EndpointOutcome(
                status_code=outcome.status_code,
                elapsed_ms=outcome.elapsed_ms,
                payload=outcome.payload,
                error="Add response reported success=false",
            )
        if (
            outcome.payload.request_id != request.request_id
            or outcome.payload.user_id != request.user_id
            or outcome.payload.session_id != request.session_id
        ):
            return EndpointOutcome(
                status_code=outcome.status_code,
                elapsed_ms=outcome.elapsed_ms,
                payload=outcome.payload,
                error="Add response IDs do not match the request",
            )
        return outcome

    async def search(self, request: EvaluationQuery) -> EndpointOutcome:
        return await self._post("/search", SearchPayload.from_query(request), SearchResponse)

    async def _post(
        self,
        path: str,
        request: BaseModel,
        response_model: type[ResponseModel],
    ) -> EndpointOutcome:
        started = time.perf_counter()
        try:
            response = await self._client.post(path, json=request.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            elapsed_ms = _elapsed_ms(started)
            return EndpointOutcome(status_code=0, elapsed_ms=elapsed_ms, error=f"{type(exc).__name__}: {exc}")

        elapsed_ms = _elapsed_ms(started)
        if not 200 <= response.status_code < 300:
            detail = response.text.strip().replace("\n", " ")[:500]
            return EndpointOutcome(
                status_code=response.status_code, elapsed_ms=elapsed_ms, error=detail or "HTTP error"
            )
        try:
            payload = response_model.model_validate_json(response.content)
        except Exception as exc:
            return EndpointOutcome(
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
                error=f"invalid response: {type(exc).__name__}: {exc}",
            )
        return EndpointOutcome(status_code=response.status_code, elapsed_ms=elapsed_ms, payload=payload)


class EvidenceSnapshot(BaseModel):
    id: str
    content: str
    score: float | None = None
    created_at: str | None = None


class AddMetric(BaseModel):
    request_id: str
    status_code: int
    succeeded: bool
    elapsed_ms: float
    error: str | None = None

    @classmethod
    def from_outcome(cls, request_id: str, outcome: EndpointOutcome) -> AddMetric:
        return cls(
            request_id=request_id,
            status_code=outcome.status_code,
            succeeded=outcome.succeeded,
            elapsed_ms=outcome.elapsed_ms,
            error=outcome.error,
        )


class SearchMetric(BaseModel):
    query_id: str
    status_code: int
    succeeded: bool
    elapsed_ms: float
    expected_terms: list[str]
    first_hit_rank: int | None = None
    all_expected_terms_found: bool = False
    hit_at_1: bool = False
    hit_at_5: bool = False
    hit_at_10: bool = False
    hit_at_100: bool = False
    reciprocal_rank: float | None = None
    evidence: list[EvidenceSnapshot] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_outcome(cls, query: EvaluationQuery, outcome: EndpointOutcome) -> SearchMetric:
        if not isinstance(outcome.payload, SearchResponse):
            return cls(
                query_id=query.query_id,
                status_code=outcome.status_code,
                succeeded=False,
                elapsed_ms=outcome.elapsed_ms,
                expected_terms=query.expected_terms,
                error=outcome.error or "missing search response",
            )

        evidence = [EvidenceSnapshot.model_validate(item.model_dump(mode="json")) for item in outcome.payload.data]
        first_hit_rank = _first_hit_rank(evidence, query.expected_terms)
        return cls(
            query_id=query.query_id,
            status_code=outcome.status_code,
            succeeded=outcome.succeeded,
            elapsed_ms=outcome.elapsed_ms,
            expected_terms=query.expected_terms,
            first_hit_rank=first_hit_rank,
            all_expected_terms_found=_all_terms_found(evidence, query.expected_terms),
            hit_at_1=first_hit_rank is not None and first_hit_rank <= 1,
            hit_at_5=first_hit_rank is not None and first_hit_rank <= 5,
            hit_at_10=first_hit_rank is not None and first_hit_rank <= 10,
            hit_at_100=first_hit_rank is not None and first_hit_rank <= 100,
            reciprocal_rank=1 / first_hit_rank if first_hit_rank is not None else None,
            evidence=evidence,
            error=outcome.error,
        )


class CaseMetric(BaseModel):
    case_id: str
    adds: list[AddMetric]
    searches: list[SearchMetric]


class AggregateMetrics(BaseModel):
    cases: int
    add_requests: int
    add_successes: int
    add_success_rate: float | None
    search_requests: int
    search_successes: int
    search_success_rate: float | None
    evaluated_searches: int
    hit_at_1: float | None
    hit_at_5: float | None
    hit_at_10: float | None
    hit_at_100: float | None
    mrr: float | None
    add_p50_ms: float | None
    add_p95_ms: float | None
    add_p99_ms: float | None
    search_p50_ms: float | None
    search_p95_ms: float | None
    search_p99_ms: float | None


class EvaluationReport(BaseModel):
    aggregate: AggregateMetrics
    cases: list[CaseMetric]

    @property
    def has_failures(self) -> bool:
        return (
            self.aggregate.add_successes != self.aggregate.add_requests
            or self.aggregate.search_successes != self.aggregate.search_requests
        )


async def run_manifest(
    manifest: EvaluationManifest,
    client: AmlClientProtocol,
    concurrency: int = 1,
) -> EvaluationReport:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_bounded(case: EvaluationCase) -> CaseMetric:
        async with semaphore:
            return await _run_case(case, client)

    case_results = await asyncio.gather(*(run_bounded(case) for case in manifest.cases))
    return EvaluationReport(aggregate=_aggregate(case_results), cases=case_results)


async def _run_case(case: EvaluationCase, client: AmlClientProtocol) -> CaseMetric:
    add_metrics: list[AddMetric] = []
    adds_succeeded = True
    for request in case.adds:
        outcome = await client.add(request)
        add_metrics.append(AddMetric.from_outcome(request.request_id, outcome))
        adds_succeeded = adds_succeeded and outcome.succeeded

    search_metrics: list[SearchMetric] = []
    for query in case.searches:
        if adds_succeeded:
            outcome = await client.search(query)
        else:
            outcome = EndpointOutcome(status_code=0, elapsed_ms=0, error="skipped because an Add request failed")
        search_metrics.append(SearchMetric.from_outcome(query, outcome))
    return CaseMetric(case_id=case.case_id, adds=add_metrics, searches=search_metrics)


def load_manifest(path: Path) -> EvaluationManifest:
    return EvaluationManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _aggregate(cases: list[CaseMetric]) -> AggregateMetrics:
    adds = [metric for case in cases for metric in case.adds]
    searches = [metric for case in cases for metric in case.searches]
    scored = [metric for metric in searches if metric.expected_terms]
    return AggregateMetrics(
        cases=len(cases),
        add_requests=len(adds),
        add_successes=sum(metric.succeeded for metric in adds),
        add_success_rate=_rate(sum(metric.succeeded for metric in adds), len(adds)),
        search_requests=len(searches),
        search_successes=sum(metric.succeeded for metric in searches),
        search_success_rate=_rate(sum(metric.succeeded for metric in searches), len(searches)),
        evaluated_searches=len(scored),
        hit_at_1=_rate(sum(metric.hit_at_1 for metric in scored), len(scored)),
        hit_at_5=_rate(sum(metric.hit_at_5 for metric in scored), len(scored)),
        hit_at_10=_rate(sum(metric.hit_at_10 for metric in scored), len(scored)),
        hit_at_100=_rate(sum(metric.hit_at_100 for metric in scored), len(scored)),
        mrr=_mean([metric.reciprocal_rank for metric in scored]),
        add_p50_ms=_percentile([metric.elapsed_ms for metric in adds], 0.50),
        add_p95_ms=_percentile([metric.elapsed_ms for metric in adds], 0.95),
        add_p99_ms=_percentile([metric.elapsed_ms for metric in adds], 0.99),
        search_p50_ms=_percentile([metric.elapsed_ms for metric in searches if metric.succeeded], 0.50),
        search_p95_ms=_percentile([metric.elapsed_ms for metric in searches if metric.succeeded], 0.95),
        search_p99_ms=_percentile([metric.elapsed_ms for metric in searches if metric.succeeded], 0.99),
    )


def _first_hit_rank(evidence: list[EvidenceSnapshot], expected_terms: list[str]) -> int | None:
    if not expected_terms:
        return None
    normalized_terms = [term.casefold() for term in expected_terms if term.strip()]
    for rank, item in enumerate(evidence, start=1):
        content = item.content.casefold()
        if any(term in content for term in normalized_terms):
            return rank
    return None


def _all_terms_found(evidence: list[EvidenceSnapshot], expected_terms: list[str]) -> bool:
    normalized_terms = [term.casefold() for term in expected_terms if term.strip()]
    if not normalized_terms:
        return False
    content = "\n".join(item.content.casefold() for item in evidence)
    return all(term in content for term in normalized_terms)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic Add/Search metrics against an AML endpoint.")
    parser.add_argument("--manifest", type=Path, required=True, help="Path to an AML evaluation manifest JSON file")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="AML API base URL")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path instead of stdout")
    parser.add_argument("--concurrency", type=int, default=1, help="Maximum number of cases processed concurrently")
    parser.add_argument("--timeout", type=float, default=900, help="Per-request timeout in seconds")
    return parser


async def _run_from_args(args: argparse.Namespace) -> EvaluationReport:
    manifest = load_manifest(args.manifest)
    async with AmlClient(args.base_url, args.timeout) as client:
        return await run_manifest(manifest, client, args.concurrency)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = asyncio.run(_run_from_args(args))
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    return 1 if report.has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
