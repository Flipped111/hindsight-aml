# Hindsight AML Adapter

This repository adds an independent compatibility layer for the Agent Memory Leaderboard (AML) text-memory
contract. It preserves the upstream Hindsight API and exposes only the three endpoints required by the leaderboard:

```text
AML POST /add    -> AML adapter -> Hindsight retain
AML POST /search -> AML adapter -> Hindsight facts + raw messages -> fused memory evidence
AML GET  /health -> AML adapter + Hindsight dependency health
```

## Release status and fixed baseline

- Submission repository: [Flipped111/hindsight-aml](https://github.com/Flipped111/hindsight-aml)
- Published baseline tag: `aml-v0.2.1` (frozen; it is not changed by the work described below)
- Final hybrid-retrieval release: `aml-v0.3.0`
- Upstream project: [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)
- Hindsight version: `0.8.6`
- Upstream baseline commit: `436bc7c156f1c94714ea1f757bfc930ab89f883b`
- License: MIT; see [LICENSE](./LICENSE) and [ATTRIBUTION.md](./ATTRIBUTION.md)

The Compose stack builds Hindsight and the AML adapter from the checked-out source. It does not use a floating
`latest` image. The hybrid retrieval behavior documented below belongs to `aml-v0.3.0`, not the frozen `aml-v0.2.1`
tag.

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

Then start the fixed-source stack. The candidate Compose file uses the distinct project name `hindsight-aml-v030`, so
the standard command cannot silently reuse the `aml-v0.2.1` volumes:

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

Completed request identities are stored in SQLite before retain begins. The candidate also stages the original
messages in the same store with a `pending` status. It activates them atomically with idempotency completion only after
Hindsight confirms the complete synchronous retain, so a failed or incomplete Add cannot leak partial raw evidence.
Identical retries return the original success, conflicting reuse returns HTTP 409, concurrent identical requests wait
for the first owner, and abandoned claims can be reacquired safely using stable document IDs plus an owner-token lease.

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

Search uses only data stored for the SHA-256 scope derived from the request's exact `user_id`. It concurrently retrieves
Hindsight facts with `recall(..., budget="mid")` and active original messages from SQLite. Raw messages use a
lightweight BM25-style lexical ranker over message text, with conservative English stemming, alphanumeric terms,
numeric-token weighting, and CJK bigrams. Speaker names are matched separately: a speaker match can reinforce a
substantive message-text match, while a name-only match remains a weak tail candidate.

The two independently ranked lists are combined with weighted reciprocal-rank fusion using `k=60`, fact weight `1.0`,
strong raw-message weight `0.95`, and weak name-only raw-message weight `0.55`. This preserves the first four fact
positions before a strong raw candidate can enter, while keeping weak speaker matches available deeper in a large
result set. At most four raw candidates are admitted while fact candidates remain available; deferred raw candidates
backfill the response only when facts cannot fill `top_k`. The adapter removes exact normalized-content duplicates,
limits one source document to at most two returned items, applies a deterministic tie-break order, and then truncates
to `top_k`. Raw result IDs are stable and use the form `raw:<document_id>`; their content is the original stored
message envelope rather than a generated answer.

Before the final sort, the candidate applies a bounded temporal multiplier only when the query contains explicit time
intent. Current/latest wording can add up to 8% according to relative event-time recency; previous/earlier wording can
apply the same bounded preference in the opposite direction. Explicit years, year ranges, and before/after-year queries
boost only matching event years. Queries without recognized time intent keep their original RRF scores and order, and
evidence without a valid event timestamp receives no temporal boost. Bare `recently`/`最近` wording does not trigger
global recency scoring without a reliable query-time anchor, because it may refer to the time of an earlier conversation.

Search never calls `reflect`, never generates a final answer, and ignores `options` for retrieval and reasoning. If
nothing is found, the response is always `{"data":[]}`. `created_at` is omitted unless the source supplies a reliable
timezone-aware event time.

### `GET /health`

```bash
curl --fail http://localhost:8000/health
```

The endpoint returns 200 only when both the SQLite idempotency store and Hindsight (including its database) are healthy;
otherwise it returns 503.

Business failures use standard non-2xx statuses and the JSON shape `{"detail":{"reason":"..."}}`. Invalid request
fields use FastAPI's structured HTTP 422 validation response. Add conflicts return 409, dependency failures return 502,
and an identical request that waits past its idempotency deadline returns 503; these responses never claim a successful
write.

## Persistence

By default, the optimization-candidate Compose project creates two named volumes:

- `hindsight-aml-v030_hindsight-data` stores the embedded pg0 database and retained memories.
- `hindsight-aml-v030_aml-data` stores SQLite idempotency records and the candidate's original-message search index.

Ordinary `docker compose stop`, `start`, or container recreation keeps both volumes. Do not use `down --volumes` unless
you intentionally want to erase all memories and idempotency records.

### Fresh volumes for the optimization candidate

The `aml-v0.2.1` database contains no SQLite raw-message index. The optimization candidate therefore must not be
formally evaluated by reusing an existing baseline project or its volumes: previously completed requests would remain
in Hindsight but would be missing from raw-message retrieval. The Compose file's default `hindsight-aml-v030` project
name isolates the first candidate run. For another independent formal run, override it with a new project name, for
example:

```bash
docker compose -p hindsight-aml-v030-run2 -f docker-compose.aml.yml up --build
```

Use a different project name for a separate run. Do not copy the old Hindsight or AML data volumes into the new
project. A future in-place upgrade would require an explicit, validated raw-index backfill before serving Search.

### Evaluation-data lifecycle

Evaluation inputs, stored memories, raw-message rows, logs, and evaluation reports are used only to operate and assess
this AML submission. They must not be used for model training, unrelated product analysis, dataset reconstruction, or
redistribution. Access must be limited to people and services required to run the evaluation, and deployments should
avoid logging request bodies or raw memory content unless necessary for short-lived debugging.

Unless written permission requires a different retention period, remove all evaluation data and derivatives as soon as
the task finishes and no later than 30 days afterward. For the default candidate run, first archive only the
non-sensitive aggregate metrics that are permitted to be retained, then deliberately delete both data volumes:

```bash
docker compose -f docker-compose.aml.yml down --volumes
```

This command permanently removes the run's Hindsight memories, SQLite idempotency/raw-message data, and containers.
If the run used `-p`, repeat the same `-p <exact-project-name>` when deleting it. Confirm the project name before
running the command. Also delete request-body logs, local manifests containing evaluation data, and evidence-bearing
evaluation reports from any external storage or backups within the same deadline.

## Tests

The fake/mock suite covers the AML HTTP contract, synchronous retain confirmation, stable IDs, original data and time
preservation, strict user isolation, same-session chunks, ordering and `top_k`, empty results, conflicts, concurrent
idempotency, failed-request retry, abandoned leases, dependency failures, SQLite restart persistence, hybrid retrieval,
bounded temporal reranking, evaluation comparison, LoCoMo manifest conversion, and shared-state A/B support. The
current candidate suite contains 59 tests:

```bash
uv sync --project hindsight-api-slim
uv pip install --python .venv/bin/python ./hindsight-clients/python
PYTHONPATH=. .venv/bin/pytest tests/aml_adapter tests/aml_eval -n 0
```

Real Hindsight and Docker restart smoke tests still require an environment with the model credential and Docker daemon.

## AML Evaluation Runner

The repository includes a deterministic Add/Search runner for comparing adapter versions without generating answers or
using a model judge. It records per-request status and latency, preserves returned evidence, and calculates separate
answer-term and labeled-source-evidence Hit@1/5/10/100, MRR, and latency percentiles. `tools/aml_compare.py` compares
baseline and candidate reports by query and category, while `tools/aml_locomo_manifest.py` converts bounded LoCoMo
subsets into manifests.

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

For search-only optimization, `tools/aml_direct_ab.sh` also supports a shared-state workflow. It retains the manifest
once, verifies that the baseline adapter reports version `0.2.1`, searches it, seeds only the candidate's deterministic
raw-message SQLite index, verifies candidate version `0.3.0`, and searches the same Hindsight memory state. Its
`shared-replay` mode reuses that fixed state for later search-ranking changes, avoiding a second stochastic LLM
extraction run.

### Controlled Stage A snapshot

On 2026-08-07, a shared-state five-query LoCoMo smoke comparison produced identical answer ranks for all five queries
and identical answer MRR (`0.65`). Labeled source-evidence Hit@5 changed from `0.0` to `0.2`, Hit@10 from `0.0` to
`0.6`, Hit@100 from `0.0` to `1.0`, and evidence MRR from `0.0` to approximately `0.0966`. This is a pipeline and
regression result, not an official leaderboard score or sufficient evidence to tag a release. Latency from this ordered
five-query replay is not used as a release claim because the candidate ran against a warmed shared Hindsight process.

### Controlled Stage B snapshot

On 2026-08-07, the same shared-state method was run on one complete LoCoMo conversation: 19 Add requests containing
419 messages and 197 valid Search questions. All Add and Search requests succeeded. After fixing the Hindsight state
and candidate raw-message database, the final ranking-only replay changed answer Hit@1 from `0.3249` to `0.3249`,
Hit@5 from `0.4264` to `0.4365`, Hit@10 from `0.4721` to `0.4975`, Hit@100 from `0.5736` to `0.5787`, and MRR from
`0.3762` to `0.3789`. There were no lost Top-100 answer hits. Labeled source-evidence Hit@5 was `0.3046`, Hit@10
was `0.4721`, Hit@100 was `0.5330`, and evidence MRR was `0.0889` for the candidate; the fact-only baseline returned
no exact labeled raw-message evidence.

The four-item primary raw quota recovered three facts that an unrestricted raw merge had pushed beyond Top-100 while
retaining one new answer hit. Category 1 and category 3 still showed small-sample Hit@5 regressions. Because the final
submission deadline did not permit Stage C, `aml-v0.3.0` is released with this classification risk explicitly accepted;
the snapshot is not claimed as an official leaderboard score. Ordered replay latency is recorded for diagnostics only
and is not claimed as an improvement because the candidate ran second against a warmed shared service.

## Known limitations and planned work

The frozen `aml-v0.2.1` baseline delegates retrieval and temporal relevance scoring to Hindsight recall. The current
candidate adds raw-message retrieval, rank fusion, exact-content deduplication, and limited per-document source
diversification, plus explicit bounded temporal reranking. It does not yet resolve complex relative event references or
perform multi-hop query decomposition. Raw retrieval currently scans the active messages for one user scope in SQLite,
so larger hosted workloads still require concurrency and latency validation before a release is tagged. The complete
Stage B conversation also exposed category-specific regressions that must be checked on a larger held-out sample before
changing the fusion policy or tagging a release.
