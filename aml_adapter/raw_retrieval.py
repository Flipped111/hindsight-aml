from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

_ENGLISH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "to",
    "unknown",
    "user",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
    "your",
    "assistant",
    "event",
    "message",
    "session",
    "speaker",
    "time",
}
_CJK_QUESTION_BIGRAMS = {"什么", "哪个", "哪些", "哪里", "怎么", "如何", "为何", "多少", "是否"}
_WORD_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)


@dataclass(frozen=True)
class RawMessageDocument:
    document_id: str
    content: str
    session_id: str
    role: str
    timestamp_ms: int | None
    search_text: str | None = None


@dataclass(frozen=True)
class RawMessageHit:
    document_id: str
    content: str
    session_id: str
    role: str
    timestamp_ms: int | None
    lexical_score: float


def rank_raw_messages(
    query: str,
    documents: list[RawMessageDocument],
    limit: int,
) -> list[RawMessageHit]:
    if limit <= 0 or not documents:
        return []

    query_terms = _term_weights(query)
    if not query_terms:
        return []

    document_terms = [_term_counts(document.search_text or document.content) for document in documents]
    role_terms = [_term_counts(document.role) for document in documents]
    all_role_terms = set().union(*(set(terms) for terms in role_terms))
    role_query_terms = set(query_terms).intersection(all_role_terms)
    substantive_query_terms = set(query_terms) - role_query_terms
    document_frequency: Counter[str] = Counter()
    for terms in document_terms:
        document_frequency.update(terms.keys())

    average_length = sum(sum(terms.values()) for terms in document_terms) / len(document_terms)
    normalized_query = _normalize(query)
    ranked: list[RawMessageHit] = []
    for document, terms, speaker_terms in zip(documents, document_terms, role_terms, strict=True):
        score = _bm25_score(
            query_terms=query_terms,
            document_terms=terms,
            document_frequency=document_frequency,
            document_count=len(documents),
            average_length=max(average_length, 1.0),
        )
        speaker_match = any(term in speaker_terms for term in query_terms)
        substantive_match = any(term in terms for term in substantive_query_terms)
        if not substantive_match:
            if not speaker_match:
                continue
            score = 1e-6
        elif speaker_match:
            # A speaker name is useful for finding self-authored statements, but
            # speaker-only matches must not crowd stronger message-text evidence.
            score += 0.05
        normalized_content = _normalize(document.search_text or document.content)
        if normalized_query and len(normalized_query) >= 4 and normalized_query in normalized_content:
            score += 2.0
        if score <= 0:
            continue
        ranked.append(
            RawMessageHit(
                document_id=document.document_id,
                content=document.content,
                session_id=document.session_id,
                role=document.role,
                timestamp_ms=document.timestamp_ms,
                lexical_score=score,
            )
        )

    ranked.sort(key=lambda item: (-item.lexical_score, item.document_id))
    return ranked[:limit]


def _bm25_score(
    *,
    query_terms: dict[str, float],
    document_terms: Counter[str],
    document_frequency: Counter[str],
    document_count: int,
    average_length: float,
) -> float:
    document_length = max(sum(document_terms.values()), 1)
    k1 = 1.2
    b = 0.75
    score = 0.0
    for term, query_weight in query_terms.items():
        frequency = document_terms.get(term, 0)
        if frequency == 0:
            continue
        frequency_in_documents = document_frequency.get(term, 0)
        inverse_document_frequency = math.log(
            1 + (document_count - frequency_in_documents + 0.5) / (frequency_in_documents + 0.5)
        )
        normalized_frequency = frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * document_length / average_length))
        score += query_weight * inverse_document_frequency * normalized_frequency
    return score


def _term_weights(text: str) -> dict[str, float]:
    counts = _term_counts(text)
    weights: dict[str, float] = {}
    for term in counts:
        if term.startswith("n:"):
            weights[term] = 2.5
        elif term.startswith("c:"):
            weights[term] = 1.35
        elif term.startswith("s:"):
            weights[term] = 0.65
        else:
            weights[term] = 1.0
    return weights


def _term_counts(text: str) -> Counter[str]:
    normalized = _normalize(text)
    terms: Counter[str] = Counter()

    without_cjk = "".join(" " if _is_cjk(character) else character for character in normalized)
    for match in _WORD_RE.finditer(without_cjk):
        token = match.group(0)
        if token.isdigit():
            terms[f"n:{token}"] += 1
        elif len(token) >= 2 and token not in _ENGLISH_STOP_WORDS:
            terms[f"w:{token}"] += 1
            terms[f"s:{_english_stem(token)}"] += 1

    for sequence in _cjk_sequences(normalized):
        if len(sequence) == 1:
            terms[f"c:{sequence}"] += 1
            continue
        for index in range(len(sequence) - 1):
            bigram = sequence[index : index + 2]
            if bigram not in _CJK_QUESTION_BIGRAMS:
                terms[f"c:{bigram}"] += 1
    return terms


def _cjk_sequences(text: str) -> list[str]:
    sequences: list[str] = []
    current: list[str] = []
    for character in text:
        if _is_cjk(character):
            current.append(character)
        elif current:
            sequences.append("".join(current))
            current = []
    if current:
        sequences.append("".join(current))
    return sequences


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _english_stem(token: str) -> str:
    """Return a conservative dependency-free stem for lexical recall."""
    if len(token) > 4 and token.endswith(("ied", "ies")):
        return f"{token[:-3]}y"
    if len(token) > 5 and token.endswith("ing"):
        return _trim_doubled_consonant(token[:-3])
    if len(token) > 4 and token.endswith("ed"):
        return _trim_doubled_consonant(token[:-2])
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _trim_doubled_consonant(token: str) -> str:
    if len(token) >= 3 and token[-1] == token[-2] and token[-1] not in "aeiou":
        return token[:-1]
    return token


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())
