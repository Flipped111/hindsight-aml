from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aml_adapter.schemas import AddRequest, Message
from tools.aml_eval import EvaluationCase, EvaluationManifest, EvaluationQuery

_DEFAULT_DATASET = Path("hindsight-dev/benchmarks/locomo/datasets/locomo10.json")
_DIALOGUE_ID_RE = re.compile(r"D(\d+):(\d+)", flags=re.IGNORECASE)
_MALFORMED_DIALOGUE_ID_RE = re.compile(r"D:(\d+):(\d+)", flags=re.IGNORECASE)


@dataclass(frozen=True)
class ConversionStats:
    source_samples: int
    cases: int
    adds: int
    searches: int
    skipped_questions_without_evidence: int


def convert_locomo(
    dataset: list[dict[str, Any]],
    *,
    max_samples: int | None = None,
    max_sessions_per_sample: int | None = None,
    max_questions_per_sample: int | None = None,
    categories: set[str] | None = None,
    top_k: int = 100,
) -> tuple[EvaluationManifest, ConversionStats]:
    selected_samples = dataset[:max_samples] if max_samples is not None else dataset
    cases: list[EvaluationCase] = []
    skipped_questions = 0

    for sample in selected_samples:
        sample_id = str(sample["sample_id"])
        conversation = sample["conversation"]
        evidence_index: dict[str, str] = {}
        adds: list[AddRequest] = []

        session_keys = _session_keys(conversation)
        if max_sessions_per_sample is not None:
            session_keys = session_keys[:max_sessions_per_sample]
        for session_number, session_key in session_keys:
            timestamp_ms = _parse_session_timestamp(conversation[f"{session_key}_date_time"])
            messages: list[Message] = []
            for turn in conversation[session_key]:
                content = _turn_content(turn)
                if not content:
                    continue
                role = str(turn.get("speaker") or "unknown")
                messages.append(Message(role=role, timestamp=timestamp_ms, content=content))
                dialogue_id = _normalize_dialogue_id(str(turn.get("dia_id") or ""))
                if dialogue_id:
                    evidence_index[dialogue_id] = content

            if messages:
                adds.append(
                    AddRequest(
                        request_id=f"locomo:{sample_id}:session-{session_number}",
                        messages=messages,
                        user_id=f"locomo:{sample_id}",
                        session_id=f"locomo:{sample_id}:session-{session_number}",
                    )
                )

        searches: list[EvaluationQuery] = []
        for question_index, qa in enumerate(sample["qa"], start=1):
            category = str(qa.get("category", "uncategorized"))
            if categories is not None and category not in categories:
                continue

            evidence_terms = _resolve_evidence_terms(qa.get("evidence", []), evidence_index)
            if not evidence_terms:
                skipped_questions += 1
                continue
            searches.append(
                EvaluationQuery(
                    query_id=f"locomo:{sample_id}:q-{question_index}",
                    category=category,
                    query=str(qa["question"]),
                    options=[],
                    user_id=f"locomo:{sample_id}",
                    top_k=top_k,
                    expected_terms=_answer_terms(qa.get("answer")),
                    expected_evidence_terms=evidence_terms,
                )
            )
            if max_questions_per_sample is not None and len(searches) >= max_questions_per_sample:
                break

        if adds and searches:
            cases.append(EvaluationCase(case_id=f"locomo:{sample_id}", adds=adds, searches=searches))

    manifest = EvaluationManifest(cases=cases)
    return manifest, ConversionStats(
        source_samples=len(selected_samples),
        cases=len(cases),
        adds=sum(len(case.adds) for case in cases),
        searches=sum(len(case.searches) for case in cases),
        skipped_questions_without_evidence=skipped_questions,
    )


def load_locomo(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("LoCoMo dataset root must be a list")
    return payload


def _session_keys(conversation: dict[str, Any]) -> list[tuple[int, str]]:
    sessions: list[tuple[int, str]] = []
    for key, value in conversation.items():
        match = re.fullmatch(r"session_(\d+)", key)
        if match and isinstance(value, list):
            sessions.append((int(match.group(1)), key))
    return sorted(sessions)


def _parse_session_timestamp(value: str) -> int:
    parsed = datetime.strptime(value, "%I:%M %p on %d %B, %Y").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _turn_content(turn: dict[str, Any]) -> str:
    parts: list[str] = []
    text = str(turn.get("text") or "").strip()
    if text:
        parts.append(text)
    caption = str(turn.get("blip_caption") or "").strip()
    if caption:
        parts.append(f"Image description: {caption}")
    image_query = str(turn.get("query") or "").strip()
    if image_query:
        parts.append(f"Image query: {image_query}")
    return "\n".join(parts)


def _resolve_evidence_terms(evidence: list[Any], evidence_index: dict[str, str]) -> list[str]:
    terms: list[str] = []
    for raw_reference in evidence:
        for dialogue_id in _extract_dialogue_ids(str(raw_reference)):
            content = evidence_index.get(dialogue_id)
            if content and content not in terms:
                terms.append(content)
    return terms


def _answer_terms(answer: Any) -> list[str]:
    if answer is None:
        return []
    value = str(answer).strip()
    if not value:
        return []
    parts = [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
    terms = parts if len(parts) > 1 else [value]
    for part in parts:
        for word in re.findall(r"[^\W\d_]{7,}", part, flags=re.UNICODE):
            if word not in terms:
                terms.append(word)
    return terms


def _extract_dialogue_ids(value: str) -> list[str]:
    normalized = _MALFORMED_DIALOGUE_ID_RE.sub(r"D\1:\2", value)
    return list(dict.fromkeys(_normalize_dialogue_id(match.group(0)) for match in _DIALOGUE_ID_RE.finditer(normalized)))


def _normalize_dialogue_id(value: str) -> str:
    match = _DIALOGUE_ID_RE.fullmatch(value.strip())
    if match is None:
        return ""
    return f"D{int(match.group(1))}:{int(match.group(2))}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert LoCoMo evidence annotations to an AML Add/Search manifest.")
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET, help="Path to locomo10.json")
    parser.add_argument("--output", type=Path, required=True, help="Destination AML manifest JSON")
    parser.add_argument("--max-samples", type=int, help="Keep only the first N conversations")
    parser.add_argument("--max-sessions-per-sample", type=int, help="Keep only the first N sessions per conversation")
    parser.add_argument("--max-questions-per-sample", type=int, help="Keep at most N valid questions per conversation")
    parser.add_argument("--categories", help="Comma-separated LoCoMo category IDs")
    parser.add_argument("--top-k", type=int, default=100, choices=range(1, 101), metavar="1..100")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    categories = {item.strip() for item in args.categories.split(",") if item.strip()} if args.categories else None
    manifest, stats = convert_locomo(
        load_locomo(args.dataset),
        max_samples=args.max_samples,
        max_sessions_per_sample=args.max_sessions_per_sample,
        max_questions_per_sample=args.max_questions_per_sample,
        categories=categories,
        top_k=args.top_k,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sys.stderr.write(
        "LoCoMo AML manifest: "
        f"{stats.cases} cases, {stats.adds} Add requests, {stats.searches} Search requests, "
        f"{stats.skipped_questions_without_evidence} questions skipped without resolvable evidence.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
