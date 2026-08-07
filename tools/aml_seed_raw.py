from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from aml_adapter.service import (
    _payload_hash,
    _raw_message_writes,
    _retain_items,
    user_to_bank_id,
)
from aml_adapter.storage import ClaimStatus, IdempotencyStore, RequestIdentity
from tools.aml_eval import EvaluationManifest, load_manifest


class SeedSummary(BaseModel):
    requests_seeded: int
    requests_already_present: int
    raw_messages_seeded: int


async def seed_raw_messages(
    manifest: EvaluationManifest,
    database_path: Path,
) -> SeedSummary:
    store = IdempotencyStore(database_path, lease_seconds=3600)
    await store.initialize()
    requests_seeded = 0
    requests_already_present = 0
    raw_messages_seeded = 0

    for case in manifest.cases:
        for request in case.adds:
            identity = RequestIdentity(
                request_id=request.request_id,
                user_id=request.user_id,
                session_id=request.session_id,
                payload_hash=_payload_hash(request),
            )
            claim = await store.claim(identity)
            if claim.status == ClaimStatus.COMPLETED:
                requests_already_present += 1
                continue
            if claim.status != ClaimStatus.ACQUIRED or claim.owner_token is None:
                raise RuntimeError(f"request_id '{request.request_id}' is already being seeded")

            retain_items = _retain_items(request)
            writes = _raw_message_writes(
                request,
                user_to_bank_id(request.user_id),
                retain_items,
            )
            try:
                await store.stage_raw_messages(request.request_id, claim.owner_token, writes)
                await store.complete(request.request_id, claim.owner_token)
            except BaseException:
                await asyncio.shield(store.release(request.request_id, claim.owner_token))
                raise
            requests_seeded += 1
            raw_messages_seeded += len(writes)

    return SeedSummary(
        requests_seeded=requests_seeded,
        requests_already_present=requests_already_present,
        raw_messages_seeded=raw_messages_seeded,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the candidate raw-message index without retaining into Hindsight again."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary = asyncio.run(seed_raw_messages(load_manifest(args.manifest), args.database))
    sys.stdout.write(json.dumps(summary.model_dump(mode="json"), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
