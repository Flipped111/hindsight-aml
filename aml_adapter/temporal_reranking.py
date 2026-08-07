from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

_MAX_TEMPORAL_BOOST = 0.08
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_CURRENT_RE = re.compile(
    r"(?:现在|目前|如今|当前|至今|现居|最新|后来|之后|此后|最终|搬家后|"
    r"\bnow\b|\bcurrently\b|\bat present\b|\bas of now\b|\blatest\b|"
    r"\bmost recent\b|\bnowadays\b|\bthese days\b|\beventually\b|"
    r"\bafterwards?\b|\blater\b)"
)
_PREVIOUS_RE = re.compile(
    r"(?:以前|之前|曾经|过去|原来|最初|早先|当时|"
    r"\bpreviously\b|\bformerly\b|\bused to\b|\bbefore that\b|\bat the time\b|"
    r"\boriginally\b|\binitially\b|\bearlier\b)"
)


class TemporalIntent(StrEnum):
    NONE = "none"
    CURRENT = "current"
    PREVIOUS = "previous"
    TARGET_YEAR = "target_year"
    YEAR_RANGE = "year_range"
    BEFORE_YEAR = "before_year"
    AFTER_YEAR = "after_year"


@dataclass(frozen=True)
class TemporalQuery:
    intent: TemporalIntent
    start_year: int | None = None
    end_year: int | None = None


def analyze_temporal_query(query: str) -> TemporalQuery:
    normalized = _normalize(query)
    years = _extract_temporal_years(normalized)

    if len(years) == 1:
        year = years[0]
        if _references_year_before(normalized, year):
            return TemporalQuery(intent=TemporalIntent.BEFORE_YEAR, start_year=year)
        if _references_year_after(normalized, year):
            return TemporalQuery(intent=TemporalIntent.AFTER_YEAR, start_year=year)
        return TemporalQuery(intent=TemporalIntent.TARGET_YEAR, start_year=year, end_year=year)

    if len(years) >= 2:
        start_year, end_year = sorted(years[:2])
        return TemporalQuery(intent=TemporalIntent.YEAR_RANGE, start_year=start_year, end_year=end_year)

    if _CURRENT_RE.search(normalized):
        return TemporalQuery(intent=TemporalIntent.CURRENT)
    if _PREVIOUS_RE.search(normalized):
        return TemporalQuery(intent=TemporalIntent.PREVIOUS)
    return TemporalQuery(intent=TemporalIntent.NONE)


def temporal_score_multipliers(query: str, event_times: list[datetime | None]) -> list[float]:
    temporal_query = analyze_temporal_query(query)
    if temporal_query.intent == TemporalIntent.NONE:
        return [1.0] * len(event_times)

    normalized_times = [_as_utc(value) for value in event_times]
    if temporal_query.intent in {
        TemporalIntent.TARGET_YEAR,
        TemporalIntent.YEAR_RANGE,
        TemporalIntent.BEFORE_YEAR,
        TemporalIntent.AFTER_YEAR,
    }:
        return [_explicit_time_multiplier(temporal_query, value) for value in normalized_times]

    dated_values = [value for value in normalized_times if value is not None]
    if len(dated_values) < 2:
        return [1.0] * len(event_times)

    oldest = min(dated_values)
    newest = max(dated_values)
    span_seconds = (newest - oldest).total_seconds()
    if span_seconds <= 0:
        return [1.0] * len(event_times)

    multipliers: list[float] = []
    for value in normalized_times:
        if value is None:
            multipliers.append(1.0)
            continue
        recency = (value - oldest).total_seconds() / span_seconds
        temporal_weight = recency if temporal_query.intent == TemporalIntent.CURRENT else 1.0 - recency
        multipliers.append(1.0 + _MAX_TEMPORAL_BOOST * temporal_weight)
    return multipliers


def _explicit_time_multiplier(query: TemporalQuery, value: datetime | None) -> float:
    if value is None or query.start_year is None:
        return 1.0

    matches = False
    if query.intent == TemporalIntent.TARGET_YEAR:
        matches = value.year == query.start_year
    elif query.intent == TemporalIntent.YEAR_RANGE and query.end_year is not None:
        matches = query.start_year <= value.year <= query.end_year
    elif query.intent == TemporalIntent.BEFORE_YEAR:
        matches = value.year < query.start_year
    elif query.intent == TemporalIntent.AFTER_YEAR:
        matches = value.year > query.start_year
    return 1.0 + _MAX_TEMPORAL_BOOST if matches else 1.0


def _references_year_before(query: str, year: int) -> bool:
    text = str(year)
    return bool(
        re.search(rf"\bbefore\s+(?:the\s+year\s+)?{text}\b", query)
        or re.search(rf"{text}\s*年?\s*(?:以前|之前|前)", query)
    )


def _references_year_after(query: str, year: int) -> bool:
    text = str(year)
    return bool(
        re.search(rf"\b(?:after|since)\s+(?:the\s+year\s+)?{text}\b", query)
        or re.search(rf"{text}\s*年?\s*(?:以后|之后|后)", query)
    )


def _extract_temporal_years(query: str) -> list[int]:
    matches = list(_YEAR_RE.finditer(query))
    if not matches:
        return []

    has_range_context = False
    if len(matches) >= 2:
        before_first = query[max(0, matches[0].start() - 16) : matches[0].start()]
        between_first_two = query[matches[0].end() : matches[1].start()]
        has_range_context = bool(
            re.search(r"(?:\bbetween\b|\bfrom\b|从)\s*$", before_first)
            and re.fullmatch(r"\s*(?:\band\b|\bto\b|到|至|[-–—])\s*", between_first_two)
        ) or bool(re.fullmatch(r"\s*(?:\bto\b|到|至|[-–—])\s*", between_first_two))
    years: list[int] = []
    for match in matches:
        prefix = query[max(0, match.start() - 24) : match.start()]
        suffix = query[match.end() : match.end() + 12]
        has_prefix_context = bool(
            re.search(
                r"(?:\bin\b|\bduring\b|\bfrom\b|\bsince\b|\bbefore\b|\bafter\b|"
                r"\buntil\b|\bthrough\b|\bbetween\b|在|于)\s*(?:the\s+year\s+)?$",
                prefix,
            )
        )
        has_suffix_context = bool(re.match(r"\s*(?:年|[-/.]\s*\d{1,2})", suffix))
        if has_range_context or has_prefix_context or has_suffix_context:
            year = int(match.group(1))
            if year not in years:
                years.append(year)
    return years


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(UTC)
