# Attribution and modifications

## Upstream project

This work is based on [Hindsight](https://github.com/vectorize-io/hindsight), an agent-memory system developed by
Vectorize AI, Inc. and its contributors.

- Upstream version: `0.8.6`
- Fixed upstream baseline: `436bc7c156f1c94714ea1f757bfc930ab89f883b`
- Upstream copyright: Copyright (c) 2025 Vectorize AI, Inc.
- License: MIT; the complete license text is preserved in [LICENSE](./LICENSE)
- Technical report: [Hindsight: Agent Memory That Works Like Human Memory](https://arxiv.org/abs/2512.12818)

## Submission release

- Repository: [Flipped111/hindsight-aml](https://github.com/Flipped111/hindsight-aml)
- Release tag: `aml-v0.2.1`

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
  stable retry behavior, and owner-token lease protection.
- Hindsight recall-only evidence conversion using `scores.final`, stable result IDs, reliable event timestamps, sorting,
  filtering, and `top_k` truncation. The adapter does not call `reflect` or generate answers.
- A deterministic `tools/aml_eval.py` Add/Search runner that records status, latency, evidence snapshots, Hit@K, and
  MRR from caller-supplied expected content terms without generating answers or judging with an LLM.
- A fixed-source Docker Compose deployment with separate persistent volumes for Hindsight pg0 data and AML idempotency
  state.
- Fake/mock contract, isolation, concurrency, failure, and restart-persistence tests under `tests/aml_adapter/`.
- AML deployment, configuration, security, and reproducibility documentation.

The adapter runtime additionally uses FastAPI, Uvicorn, Pydantic, and the maintained Hindsight Python client according
to the dependency declarations and licenses shipped with those projects.

## Not implemented in the baseline

Adapter-level raw-message retrieval, explicit temporal reranking, deduplication/source diversification, and multi-hop
search remain planned work. They are not represented as completed modifications or current submission capabilities.
