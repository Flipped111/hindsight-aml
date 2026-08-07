from __future__ import annotations

from pathlib import Path

import pytest

from aml_adapter.service import user_to_bank_id
from aml_adapter.storage import IdempotencyStore
from tests.aml_eval.test_runner import build_manifest
from tools.aml_seed_raw import seed_raw_messages


@pytest.mark.asyncio
async def test_seed_raw_messages_builds_idempotent_searchable_index(tmp_path: Path) -> None:
    database_path = tmp_path / "candidate.sqlite3"
    manifest = build_manifest()

    first = await seed_raw_messages(manifest, database_path)
    second = await seed_raw_messages(manifest, database_path)

    store = IdempotencyStore(database_path, lease_seconds=3600)
    hits = await store.search_raw_messages(
        user_to_bank_id("user-1"),
        "Where do I live?",
        5,
    )
    assert first.requests_seeded == 1
    assert first.raw_messages_seeded == 1
    assert second.requests_already_present == 1
    assert [hit.document_id for hit in hits]
    assert "Tokyo" in hits[0].content
