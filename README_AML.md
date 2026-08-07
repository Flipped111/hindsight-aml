# Hindsight AML Adapter

This repository adds an independent compatibility layer for the Agent Memory Leaderboard (AML) text-memory
contract. It preserves the upstream Hindsight API and exposes only the three endpoints required by the leaderboard:

```text
AML POST /add    -> AML adapter -> Hindsight retain
AML POST /search -> AML adapter -> Hindsight recall -> memory evidence
AML GET  /health -> AML adapter + Hindsight dependency health
```

## Fixed baseline

- Submission repository: [Flipped111/hindsight-aml](https://github.com/Flipped111/hindsight-aml)
- Submission tag: `aml-v0.2.0`
- Upstream project: [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)
- Hindsight version: `0.8.6`
- Upstream baseline commit: `436bc7c156f1c94714ea1f757bfc930ab89f883b`
- License: MIT; see [LICENSE](./LICENSE) and [ATTRIBUTION.md](./ATTRIBUTION.md)

The Compose stack builds Hindsight and the AML adapter from this repository. It does not use a floating `latest`
image.

## Requirements

- Docker Engine with Docker Compose v2
- An OpenAI-compatible API key that can call `gpt-4o-mini`
- Network access during the first image build to install locked dependencies and download the local embedding and
  reranker models
- Enough disk and memory for the Hindsight standalone API and its local ML models

## Start

```bash
cp .env.example .env
```

Set the key in `.env` without committing that file:

```dotenv
HINDSIGHT_API_LLM_API_KEY=
```

Then start the fixed-source stack:

```bash
docker compose -f docker-compose.aml.yml up --build
```

The AML API is available at `http://localhost:8000`. Hindsight remains internal to the Compose network.

## Configuration

The leaderboard configuration is fixed to `openai` and `gpt-4o-mini`. Embeddings and reranking use the local models
bundled by the Hindsight standalone build. The following adapter settings may be adjusted for deployment constraints:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HINDSIGHT_API_LLM_API_KEY` | empty | Required model API key, read only from the environment |
| `HINDSIGHT_API_LLM_BASE_URL` | `https://api.openai.com/v1` | Optional OpenAI-compatible base URL |
| `HINDSIGHT_TIMEOUT_SECONDS` | `600` | Adapter timeout for one Hindsight request |
| `AML_IDEMPOTENCY_WAIT_TIMEOUT_SECONDS` | `900` | Maximum wait for an identical in-flight request |
| `AML_IDEMPOTENCY_LEASE_SECONDS` | `1200` | Recovery lease for an abandoned processing claim; must exceed the Hindsight timeout |
| `AML_IDEMPOTENCY_POLL_INTERVAL_SECONDS` | `0.1` | Poll interval while waiting for the first request owner |

The stack forces `HINDSIGHT_API_RETAIN_BATCH_ENABLED=false` because provider batch processing is asynchronous. It also
forces `HINDSIGHT_API_FAIL_ON_EXTRACTION_ERRORS=true`, so partial fact-extraction failures cannot be reported as a
successful AML Add.

## API

### `POST /add`

```bash
curl --fail --request POST http://localhost:8000/add \
  --header 'Content-Type: application/json' \
  --data '{
    "request_id":"eval:run:chunk-0",
    "messages":[{"role":"user","timestamp":1704067200000,"content":"我现在住在东京。"}],
    "user_id":"eval:run:user-0",
    "session_id":"eval:run:session-0"
  }'
```

Successful response:

```json
{"success":true,"request_id":"eval:run:chunk-0","user_id":"eval:run:user-0","session_id":"eval:run:session-0"}
```

The adapter maps `user_id` to a dedicated Hindsight Bank using SHA-256. Every message receives a stable document ID
derived from `request_id` and its index. The retained source preserves the speaker, session, UTC event time, original
message, and required metadata. Add uses `aretain_batch(..., retain_async=False)` and returns 200 only after Hindsight
confirms the whole synchronous retain.

The AML `timestamp` field is optional. When it is omitted, the adapter records `Event time: Unknown` and sends
Hindsight's explicit `unset` timestamp instead of inventing an event time.

Completed request identities are stored in SQLite before retain begins. Identical retries return the original success,
conflicting reuse returns HTTP 409, concurrent identical requests wait for the first owner, and abandoned claims can be
reacquired safely using stable document IDs plus an owner-token lease.

### `POST /search`

```bash
curl --fail --request POST http://localhost:8000/search \
  --header 'Content-Type: application/json' \
  --data '{
    "query":"我现在住在哪里？",
    "options":["上海","东京"],
    "user_id":"eval:run:user-0",
    "top_k":5
  }'
```

Response shape:

```json
{"data":[{"id":"stable-memory-id","content":"memory evidence","score":0.91,"created_at":"2024-01-01T00:00:00Z"}]}
```

Search queries only the Bank derived from the request's `user_id`, calls Hindsight `recall` with the `mid` budget, and
returns evidence from Hindsight's result text. It never calls `reflect`, never generates a final answer, ignores
`options` for reasoning, sorts by `scores.final`, filters empty evidence, and truncates to `top_k`. If nothing is found,
the response is always `{"data":[]}`. `created_at` is omitted unless Hindsight supplies a reliable timezone-aware event
time.

### `GET /health`

```bash
curl --fail http://localhost:8000/health
```

The endpoint returns 200 only when both the SQLite idempotency store and Hindsight (including its database) are healthy;
otherwise it returns 503.

## Persistence

Compose creates two named volumes:

- `hindsight-aml_hindsight-data` stores the embedded pg0 database and retained memories.
- `hindsight-aml_aml-data` stores the SQLite idempotency database.

Ordinary `docker compose stop`, `start`, or container recreation keeps both volumes. Do not use `down --volumes` unless
you intentionally want to erase all memories and idempotency records.

## Tests

The fake/mock suite covers the AML HTTP contract, synchronous retain confirmation, stable IDs, original data and time
preservation, strict user isolation, same-session chunks, ordering and `top_k`, empty results, conflicts, concurrent
idempotency, failed-request retry, abandoned leases, dependency failures, and SQLite restart persistence:

```bash
uv sync --project hindsight-api-slim
uv pip install --python .venv/bin/python ./hindsight-clients/python
PYTHONPATH=. .venv/bin/pytest tests/aml_adapter -n 0
```

Real Hindsight and Docker restart smoke tests still require an environment with the model credential and Docker daemon.

## AML Evaluation Runner

The repository includes a deterministic Add/Search runner for comparing adapter versions without generating answers or
using a model judge. It records per-request status and latency, preserves returned evidence, and calculates Hit@1,
Hit@5, Hit@10, Hit@100, MRR, and latency percentiles from expected content terms.

The manifest format is shown in [tools/aml_eval.example.json](./tools/aml_eval.example.json). Run it against a started
AML API with the repository development environment:

```bash
PYTHONPATH=. .venv/bin/python -m tools.aml_eval \
  --manifest tools/aml_eval.example.json \
  --base-url http://127.0.0.1:8000 \
  --output aml-eval-report.json
```

The runner exits non-zero when Add or Search requests fail. The report is local measurement output and must not be
submitted as benchmark ground truth.

## Known limitations and planned work

This baseline delegates retrieval and temporal relevance scoring to Hindsight recall. It does not yet add adapter-level
raw-message retrieval, explicit temporal reranking, deduplication/source diversification, or multi-hop search. These are
planned optimizations, not claimed capabilities of the current submission.
