from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from tools.aml_eval import EvaluationReport, SearchMetric


class MetricDelta(BaseModel):
    baseline: float | None
    candidate: float | None
    delta: float | None


class RankComparison(BaseModel):
    queries: int
    wins: int
    losses: int
    ties: int
    new_hits: int
    lost_hits: int


class ComparisonSlice(BaseModel):
    rank: RankComparison
    evidence_rank: RankComparison
    hit_at_1: MetricDelta
    hit_at_5: MetricDelta
    hit_at_10: MetricDelta
    hit_at_100: MetricDelta
    mrr: MetricDelta
    evidence_hit_at_1: MetricDelta
    evidence_hit_at_5: MetricDelta
    evidence_hit_at_10: MetricDelta
    evidence_hit_at_100: MetricDelta
    evidence_mrr: MetricDelta
    search_p50_ms: MetricDelta
    search_p95_ms: MetricDelta
    search_p99_ms: MetricDelta


class ComparisonReport(BaseModel):
    baseline_path: str
    candidate_path: str
    overall: ComparisonSlice
    by_category: dict[str, ComparisonSlice]


@dataclass(frozen=True)
class _MatchedQuery:
    baseline: SearchMetric
    candidate: SearchMetric


def compare_reports(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    *,
    baseline_path: str = "baseline",
    candidate_path: str = "candidate",
) -> ComparisonReport:
    baseline_queries = _query_index(baseline)
    candidate_queries = _query_index(candidate)
    if baseline_queries.keys() != candidate_queries.keys():
        missing_candidate = sorted(baseline_queries.keys() - candidate_queries.keys())
        missing_baseline = sorted(candidate_queries.keys() - baseline_queries.keys())
        raise ValueError(
            "reports contain different query IDs: "
            f"missing from candidate={missing_candidate[:5]}, missing from baseline={missing_baseline[:5]}"
        )

    matched = [
        _MatchedQuery(baseline=baseline_queries[query_id], candidate=candidate_queries[query_id])
        for query_id in sorted(baseline_queries)
    ]
    categories = sorted({pair.baseline.category or pair.candidate.category or "uncategorized" for pair in matched})
    return ComparisonReport(
        baseline_path=baseline_path,
        candidate_path=candidate_path,
        overall=_comparison_slice(matched),
        by_category={
            category: _comparison_slice(
                [
                    pair
                    for pair in matched
                    if (pair.baseline.category or pair.candidate.category or "uncategorized") == category
                ]
            )
            for category in categories
        },
    )


def load_report(path: Path) -> EvaluationReport:
    return EvaluationReport.model_validate_json(path.read_text(encoding="utf-8"))


def _query_index(report: EvaluationReport) -> dict[str, SearchMetric]:
    index: dict[str, SearchMetric] = {}
    for case in report.cases:
        for metric in case.searches:
            if metric.query_id in index:
                raise ValueError(f"duplicate query_id in report: {metric.query_id}")
            index[metric.query_id] = metric
    return index


def _comparison_slice(pairs: list[_MatchedQuery]) -> ComparisonSlice:
    return ComparisonSlice(
        rank=_rank_comparison(pairs, "first_hit_rank"),
        evidence_rank=_rank_comparison(pairs, "evidence_first_hit_rank"),
        hit_at_1=_boolean_metric_delta(pairs, "hit_at_1"),
        hit_at_5=_boolean_metric_delta(pairs, "hit_at_5"),
        hit_at_10=_boolean_metric_delta(pairs, "hit_at_10"),
        hit_at_100=_boolean_metric_delta(pairs, "hit_at_100"),
        mrr=_numeric_metric_delta(pairs, "reciprocal_rank"),
        evidence_hit_at_1=_boolean_metric_delta(pairs, "evidence_hit_at_1"),
        evidence_hit_at_5=_boolean_metric_delta(pairs, "evidence_hit_at_5"),
        evidence_hit_at_10=_boolean_metric_delta(pairs, "evidence_hit_at_10"),
        evidence_hit_at_100=_boolean_metric_delta(pairs, "evidence_hit_at_100"),
        evidence_mrr=_numeric_metric_delta(pairs, "evidence_reciprocal_rank"),
        search_p50_ms=_latency_delta(pairs, 0.50),
        search_p95_ms=_latency_delta(pairs, 0.95),
        search_p99_ms=_latency_delta(pairs, 0.99),
    )


def _rank_comparison(pairs: list[_MatchedQuery], field: str) -> RankComparison:
    wins = losses = ties = new_hits = lost_hits = 0
    for pair in pairs:
        baseline_rank = getattr(pair.baseline, field)
        candidate_rank = getattr(pair.candidate, field)
        if baseline_rank is None and candidate_rank is not None:
            wins += 1
            new_hits += 1
        elif baseline_rank is not None and candidate_rank is None:
            losses += 1
            lost_hits += 1
        elif baseline_rank is None and candidate_rank is None:
            ties += 1
        else:
            assert baseline_rank is not None and candidate_rank is not None
            if candidate_rank < baseline_rank:
                wins += 1
            elif candidate_rank > baseline_rank:
                losses += 1
            else:
                ties += 1
    return RankComparison(
        queries=len(pairs),
        wins=wins,
        losses=losses,
        ties=ties,
        new_hits=new_hits,
        lost_hits=lost_hits,
    )


def _boolean_metric_delta(pairs: list[_MatchedQuery], field: str) -> MetricDelta:
    baseline = _mean([float(getattr(pair.baseline, field)) for pair in pairs])
    candidate = _mean([float(getattr(pair.candidate, field)) for pair in pairs])
    return _metric_delta(baseline, candidate)


def _numeric_metric_delta(pairs: list[_MatchedQuery], field: str) -> MetricDelta:
    baseline = _mean([getattr(pair.baseline, field) or 0.0 for pair in pairs])
    candidate = _mean([getattr(pair.candidate, field) or 0.0 for pair in pairs])
    return _metric_delta(baseline, candidate)


def _latency_delta(pairs: list[_MatchedQuery], quantile: float) -> MetricDelta:
    baseline = _percentile([pair.baseline.elapsed_ms for pair in pairs if pair.baseline.succeeded], quantile)
    candidate = _percentile([pair.candidate.elapsed_ms for pair in pairs if pair.candidate.succeeded], quantile)
    return _metric_delta(baseline, candidate)


def _metric_delta(baseline: float | None, candidate: float | None) -> MetricDelta:
    delta = candidate - baseline if baseline is not None and candidate is not None else None
    return MetricDelta(baseline=baseline, candidate=candidate, delta=delta)


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare baseline and candidate AML evaluation reports.")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Write comparison JSON instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = compare_reports(
        load_report(args.baseline),
        load_report(args.candidate),
        baseline_path=str(args.baseline),
        candidate_path=str(args.candidate),
    )
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
