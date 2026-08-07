#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 6 ]]; then
  echo "Usage: $0 <workspace> <runtime-env> <manifest> <output-dir> [run-id] [both|baseline|candidate|shared|shared-replay]" >&2
  exit 2
fi

workspace=$(cd "$1" && pwd)
runtime_env=$(realpath "$2")
manifest=$(realpath "$3")
output_dir=$4
run_id=${5:-$(date -u +%Y%m%dT%H%M%SZ)}
mode=${6:-both}
runtime="$workspace/runtime"
python="$runtime/bin/python"
hindsight_api="$runtime/bin/hindsight-api"
uvicorn="$runtime/bin/uvicorn"
candidate_source="$workspace/candidate"

mkdir -p "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
chmod 700 "$output_dir"

set -a
# shellcheck disable=SC1090
source "$runtime_env"
set +a

export CUDA_VISIBLE_DEVICES=${AML_AB_CUDA_DEVICE:-0}
export HINDSIGHT_API_LLM_PROVIDER=${HINDSIGHT_API_LLM_PROVIDER:-openai}
export HINDSIGHT_API_LLM_MODEL=${HINDSIGHT_API_LLM_MODEL:-gpt-4o-mini}
export HINDSIGHT_API_EMBEDDINGS_PROVIDER=local
export HINDSIGHT_API_RERANKER_PROVIDER=local
export HINDSIGHT_API_RETAIN_BATCH_ENABLED=false
export HINDSIGHT_API_FAIL_ON_EXTRACTION_ERRORS=true
# Keep the fact set immutable between baseline and candidate Search passes.
export HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=false

hindsight_pid=""
adapter_pid=""

stop_services() {
  stop_adapter
  if [[ -n "$hindsight_pid" ]] && kill -0 "$hindsight_pid" 2>/dev/null; then
    kill "$hindsight_pid" 2>/dev/null || true
    wait "$hindsight_pid" 2>/dev/null || true
  fi
  hindsight_pid=""
}

stop_adapter() {
  if [[ -n "$adapter_pid" ]] && kill -0 "$adapter_pid" 2>/dev/null; then
    kill "$adapter_pid" 2>/dev/null || true
    wait "$adapter_pid" 2>/dev/null || true
  fi
  adapter_pid=""
}
trap stop_services EXIT INT TERM

wait_for_health() {
  local url=$1
  local label=$2
  local attempts=${3:-180}
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$python" -c 'import sys, urllib.request; response = urllib.request.urlopen(sys.argv[1], timeout=5); raise SystemExit(0 if 200 <= response.status < 300 else 1)' "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  echo "$label did not become healthy: $url" >&2
  return 1
}

verify_adapter_version() {
  local url=$1
  local expected=$2
  "$python" -c 'import json, sys, urllib.request; payload = json.load(urllib.request.urlopen(sys.argv[1], timeout=5)); actual = payload["info"]["version"]; print(f"adapter version: {actual}"); raise SystemExit(0 if actual == sys.argv[2] else 1)' \
    "$url/openapi.json" "$expected"
}

run_variant() {
  local variant=$1
  local source_dir=$2
  local hindsight_port=$3
  local adapter_port=$4
  local variant_dir="$output_dir/$variant"
  mkdir -p "$variant_dir"
  chmod 700 "$variant_dir"

  stop_services
  HINDSIGHT_API_DATABASE_URL="pg0://aml_${run_id}_${variant}" \
    HINDSIGHT_API_HOST=127.0.0.1 \
    HINDSIGHT_API_PORT="$hindsight_port" \
    nohup "$hindsight_api" --host 127.0.0.1 --port "$hindsight_port" \
    >"$variant_dir/hindsight.log" 2>&1 &
  hindsight_pid=$!
  wait_for_health "http://127.0.0.1:$hindsight_port/health" "$variant Hindsight"

  PYTHONPATH="$source_dir" \
    HINDSIGHT_BASE_URL="http://127.0.0.1:$hindsight_port" \
    HINDSIGHT_TIMEOUT_SECONDS=900 \
    AML_IDEMPOTENCY_DB_PATH="$variant_dir/idempotency.sqlite3" \
    nohup "$uvicorn" --app-dir "$source_dir" aml_adapter.app:app --host 127.0.0.1 --port "$adapter_port" \
    >"$variant_dir/adapter.log" 2>&1 &
  adapter_pid=$!
  wait_for_health "http://127.0.0.1:$adapter_port/health" "$variant AML adapter" 60
  if [[ "$variant" == "baseline" ]]; then
    verify_adapter_version "http://127.0.0.1:$adapter_port" "0.2.1"
  else
    verify_adapter_version "http://127.0.0.1:$adapter_port" "0.3.0"
  fi

  PYTHONPATH="$candidate_source" "$python" -m tools.aml_eval \
    --manifest "$manifest" \
    --base-url "http://127.0.0.1:$adapter_port" \
    --output "$variant_dir/report.json" \
    --concurrency 1 \
    --timeout 900
  stop_services
}

run_shared() {
  local baseline_dir="$output_dir/baseline"
  local candidate_dir="$output_dir/candidate"
  mkdir -p "$baseline_dir" "$candidate_dir"
  chmod 700 "$baseline_dir" "$candidate_dir"

  stop_services
  HINDSIGHT_API_DATABASE_URL="pg0://aml_${run_id}_shared" \
    HINDSIGHT_API_HOST=127.0.0.1 \
    HINDSIGHT_API_PORT=38888 \
    nohup "$hindsight_api" --host 127.0.0.1 --port 38888 \
    >"$output_dir/hindsight.log" 2>&1 &
  hindsight_pid=$!
  wait_for_health "http://127.0.0.1:38888/health" "shared Hindsight"

  PYTHONPATH="$workspace/baseline" \
    HINDSIGHT_BASE_URL="http://127.0.0.1:38888" \
    HINDSIGHT_TIMEOUT_SECONDS=900 \
    AML_IDEMPOTENCY_DB_PATH="$baseline_dir/idempotency.sqlite3" \
    nohup "$uvicorn" --app-dir "$workspace/baseline" aml_adapter.app:app --host 127.0.0.1 --port 38000 \
    >"$baseline_dir/adapter.log" 2>&1 &
  adapter_pid=$!
  wait_for_health "http://127.0.0.1:38000/health" "shared baseline AML adapter" 60
  verify_adapter_version "http://127.0.0.1:38000" "0.2.1"
  PYTHONPATH="$candidate_source" "$python" -m tools.aml_eval \
    --manifest "$manifest" \
    --base-url "http://127.0.0.1:38000" \
    --output "$baseline_dir/report.json" \
    --concurrency 1 \
    --timeout 900
  stop_adapter

  PYTHONPATH="$candidate_source" "$python" -m tools.aml_seed_raw \
    --manifest "$manifest" \
    --database "$candidate_dir/idempotency.sqlite3" \
    >"$candidate_dir/raw-seed.json"
  PYTHONPATH="$workspace/candidate" \
    HINDSIGHT_BASE_URL="http://127.0.0.1:38888" \
    HINDSIGHT_TIMEOUT_SECONDS=900 \
    AML_IDEMPOTENCY_DB_PATH="$candidate_dir/idempotency.sqlite3" \
    nohup "$uvicorn" --app-dir "$workspace/candidate" aml_adapter.app:app --host 127.0.0.1 --port 48000 \
    >"$candidate_dir/adapter.log" 2>&1 &
  adapter_pid=$!
  wait_for_health "http://127.0.0.1:48000/health" "shared candidate AML adapter" 60
  verify_adapter_version "http://127.0.0.1:48000" "0.3.0"
  PYTHONPATH="$candidate_source" "$python" -m tools.aml_eval \
    --manifest "$manifest" \
    --base-url "http://127.0.0.1:48000" \
    --output "$candidate_dir/report.json" \
    --concurrency 1 \
    --timeout 900 \
    --skip-adds
  stop_services

  PYTHONPATH="$candidate_source" "$python" -m tools.aml_compare \
    --baseline "$baseline_dir/report.json" \
    --candidate "$candidate_dir/report.json" \
    --output "$output_dir/comparison.json"
}

run_shared_replay() {
  local replay_name=${AML_AB_REPLAY_NAME:-candidate-replay}
  local replay_dir="$output_dir/$replay_name"
  local existing_database="$output_dir/candidate/idempotency.sqlite3"
  if [[ ! -f "$output_dir/baseline/report.json" || ! -f "$existing_database" ]]; then
    echo "Shared replay requires an existing shared baseline report and candidate raw database" >&2
    return 1
  fi
  mkdir -p "$replay_dir"
  chmod 700 "$replay_dir"

  stop_services
  HINDSIGHT_API_DATABASE_URL="pg0://aml_${run_id}_shared" \
    HINDSIGHT_API_HOST=127.0.0.1 \
    HINDSIGHT_API_PORT=38888 \
    nohup "$hindsight_api" --host 127.0.0.1 --port 38888 \
    >"$replay_dir/hindsight.log" 2>&1 &
  hindsight_pid=$!
  wait_for_health "http://127.0.0.1:38888/health" "shared replay Hindsight"

  PYTHONPATH="$workspace/candidate" \
    HINDSIGHT_BASE_URL="http://127.0.0.1:38888" \
    HINDSIGHT_TIMEOUT_SECONDS=900 \
    AML_IDEMPOTENCY_DB_PATH="$existing_database" \
    nohup "$uvicorn" --app-dir "$workspace/candidate" aml_adapter.app:app --host 127.0.0.1 --port 48000 \
    >"$replay_dir/adapter.log" 2>&1 &
  adapter_pid=$!
  wait_for_health "http://127.0.0.1:48000/health" "shared replay candidate AML adapter" 60
  verify_adapter_version "http://127.0.0.1:48000" "0.3.0"
  PYTHONPATH="$candidate_source" "$python" -m tools.aml_eval \
    --manifest "$manifest" \
    --base-url "http://127.0.0.1:48000" \
    --output "$replay_dir/report.json" \
    --concurrency 1 \
    --timeout 900 \
    --skip-adds
  stop_services

  PYTHONPATH="$candidate_source" "$python" -m tools.aml_compare \
    --baseline "$output_dir/baseline/report.json" \
    --candidate "$replay_dir/report.json" \
    --output "$replay_dir/comparison.json"
}

case "$mode" in
  both)
    run_variant baseline "$workspace/baseline" 18888 18000
    run_variant candidate "$workspace/candidate" 28888 28000
    PYTHONPATH="$candidate_source" "$python" -m tools.aml_compare \
      --baseline "$output_dir/baseline/report.json" \
      --candidate "$output_dir/candidate/report.json" \
      --output "$output_dir/comparison.json"
    ;;
  baseline)
    run_variant baseline "$workspace/baseline" 18888 18000
    ;;
  candidate)
    run_variant candidate "$workspace/candidate" 28888 28000
    ;;
  shared)
    run_shared
    ;;
  shared-replay)
    run_shared_replay
    ;;
  *)
    echo "Unknown mode: $mode" >&2
    exit 2
    ;;
esac

echo "Run outputs written to $output_dir"
