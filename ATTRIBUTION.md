# Attribution and modifications

## Upstream project

This work is based on [Hindsight](https://github.com/vectorize-io/hindsight), an agent-memory system developed by
Vectorize AI, Inc. and its contributors.

- Upstream version: `0.8.6`
- Fixed upstream baseline: `436bc7c156f1c94714ea1f757bfc930ab89f883b`
- Upstream copyright: Copyright (c) 2025 Vectorize AI, Inc.
- License: MIT; the complete license text is preserved in [LICENSE](./LICENSE)
- Technical report: [Hindsight: Agent Memory That Works Like Human Memory](https://arxiv.org/abs/2512.12818)

## Release status

- Repository: [Flipped111/hindsight-aml](https://github.com/Flipped111/hindsight-aml)
- Published baseline tag: `aml-v0.2.1` (frozen)
- Final hybrid-retrieval release: `aml-v0.3.0`

The upstream Hindsight implementation, generated clients, documentation, and existing notices remain under their
original terms. No claim is made that the AML adapter is part of or endorsed by the upstream project.

## Modifications in this repository

The Agent Memory Leaderboard work adds an independent `aml_adapter/` layer and does not change the semantics of the
upstream Hindsight retain, recall, or reflect APIs. The added work includes:

- AML-compatible `POST /add`, `POST /search`, and unauthenticated `GET /health` endpoints.
- Deterministic SHA-256 mapping from AML `user_id` to an isolated Hindsight Bank.
- Stable per-message document IDs derived from `request_id` and message index.
- Preservation of original speaker, session, event time, message text, and required metadata.
- Synchronous `aretain_batch(..., retain_async=False)` writes with complete-response validation.
- A persistent SQLite idempotency claim store with conflict detection, in-flight waiting, abandoned-claim recovery,
  stable retry behavior, owner-token lease protection, and an active/pending original-message index.
- Candidate hybrid retrieval that runs Hindsight fact recall and user-isolated original-message lexical retrieval in
  parallel. The raw ranker indexes message text using BM25-style term scoring, conservative English stemming,
  numeric-token weighting, and CJK bigrams, with separately bounded speaker-name matches.
- Weighted reciprocal-rank fusion of fact and raw-message candidates, with stable result IDs, deterministic ordering,
  exact normalized-content deduplication, per-document result limits, a four-item primary raw-message quota that
  protects deep fact recall while backfilling when facts are sparse, reliable event timestamps, and `top_k`
  truncation. The adapter does not call `reflect`, use answer options, or generate answers.
- Candidate query-time temporal intent detection for current/latest, previous/earlier, explicit-year, year-range, and
  before/after-year requests. A bounded event-time multiplier can adjust the fused evidence ranking without inventing
  timestamps or changing non-temporal query scores.
- Deterministic evaluation tools for Add/Search execution, bounded LoCoMo manifest conversion, baseline/candidate
  comparison, raw-index seeding, and shared-memory search replay. Reports separate answer-term and labeled-source
  evidence Hit@K/MRR without generating answers or judging with an LLM.
- A fixed-source Docker Compose deployment with a candidate-specific project name and separate persistent volumes for
  Hindsight pg0 data and AML idempotency/raw-message state, preventing accidental reuse of the baseline volumes.
- Fake/mock contract, isolation, concurrency, failure, hybrid-retrieval, and restart-persistence tests under
  `tests/aml_adapter/`, plus evaluation-runner tests under `tests/aml_eval/`.
- AML deployment, configuration, security, and reproducibility documentation.

The adapter runtime additionally uses FastAPI, Uvicorn, Pydantic, and the maintained Hindsight Python client according
to the dependency declarations and licenses shipped with those projects.

## Version-specific scope

The published `aml-v0.2.1` baseline does not contain adapter-level raw-message retrieval, rank fusion, or
deduplication/source diversification. Those changes belong to `aml-v0.3.0` and must not be attributed retroactively to
the frozen baseline tag. Explicit adapter-level temporal reranking and multi-hop query decomposition also remain absent
from the baseline; `aml-v0.3.0` implements bounded temporal reranking, while multi-hop query decomposition remains
unimplemented.
