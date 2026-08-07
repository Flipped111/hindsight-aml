# Hindsight AML Adapter

This repository provides a fixed-source Hindsight adapter for the Agent Memory Leaderboard (AML) text-memory
contract.

- Deployment, configuration, API examples, persistence, tests, and limitations: [README_AML.md](./README_AML.md)
- Upstream attribution and modifications: [ATTRIBUTION.md](./ATTRIBUTION.md)
- License: [LICENSE](./LICENSE)

## Release

- Repository: [Flipped111/hindsight-aml](https://github.com/Flipped111/hindsight-aml)
- Submission tag: `aml-v0.1.1`
- Hindsight version: `0.8.6`
- Upstream baseline: `436bc7c156f1c94714ea1f757bfc930ab89f883b`

## Start

```bash
cp .env.example .env
# Set HINDSIGHT_API_LLM_API_KEY in .env.
docker compose -f docker-compose.aml.yml up --build
```

The AML API listens on `http://localhost:8000` and exposes:

- `POST /add` for synchronous memory ingestion
- `POST /search` for recall-only memory evidence
- `GET /health` for adapter and dependency health

The adapter is implemented in `aml_adapter/` and does not change the semantics of the upstream Hindsight API.
