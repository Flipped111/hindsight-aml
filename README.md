# Hindsight AML Adapter

This repository provides a fixed-source Hindsight adapter for the Agent Memory Leaderboard (AML) text-memory
contract.

- Deployment, configuration, API examples, persistence, tests, and limitations: [README_AML.md](./README_AML.md)
- Upstream attribution and modifications: [ATTRIBUTION.md](./ATTRIBUTION.md)
- License: [LICENSE](./LICENSE)

## Release

- Repository: [Flipped111/hindsight-aml](https://github.com/Flipped111/hindsight-aml)
- Submission tag: `aml-v0.3.0`
- Frozen baseline tag: `aml-v0.2.1`
- Hindsight version: `0.8.6`
- Upstream baseline: `436bc7c156f1c94714ea1f757bfc930ab89f883b`

## Start

```bash
cp .env.example .env
# Set HINDSIGHT_API_LLM_API_KEY in .env.
docker compose -f docker-compose.aml.yml up --build -d
curl --fail http://localhost:8000/health
```

The AML API listens on `http://localhost:8000` and exposes:

- `POST /add` for synchronous memory ingestion
- `POST /search` for recall-only memory evidence
- `GET /health` for adapter and dependency health

The HTTP Add/Search/Health wrapper is implemented in `aml_adapter/app.py`; request models are in
`aml_adapter/schemas.py`, and the core Add/Search flow is in `aml_adapter/service.py`.

## Method scope

The original method is Hindsight 0.8.6 by Vectorize AI, Inc. and contributors. Technical report:
[Hindsight: Agent Memory That Works Like Human Memory](https://arxiv.org/abs/2512.12818). The upstream retain, recall,
and reflect semantics remain unchanged.

The AML work adds strict SHA-256 user isolation, stable evidence IDs, synchronous retain confirmation, persistent
SQLite idempotency and original-message storage, BM25-style raw-message retrieval, weighted RRF fusion with a
four-item primary raw quota, exact-content deduplication, per-document diversification, and bounded explicit temporal
reranking. Search returns stored evidence only: it does not call `reflect`, generate answers, or use `options` to alter
retrieval. Evaluation support includes LoCoMo manifest conversion, shared-state A/B replay, comparison reports, and 59
automated tests.
