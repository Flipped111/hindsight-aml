from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from aml_adapter.raw_retrieval import RawMessageDocument, RawMessageHit, rank_raw_messages


class RequestConflictError(Exception):
    """A request ID was reused with different immutable request data."""


class LeaseLostError(Exception):
    """The request claim expired before the owner completed it."""


class ClaimStatus(StrEnum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    WAIT = "wait"


@dataclass(frozen=True)
class RequestIdentity:
    request_id: str
    user_id: str
    session_id: str
    payload_hash: str


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    owner_token: str | None = None


@dataclass(frozen=True)
class RawMessageWrite:
    document_id: str
    request_id: str
    message_index: int
    user_scope: str
    session_id: str
    role: str
    content: str
    search_text: str
    timestamp_ms: int | None


class IdempotencyStore:
    def __init__(self, path: Path, lease_seconds: float) -> None:
        self._path = path
        self._lease_seconds = lease_seconds

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def health(self) -> bool:
        try:
            return await asyncio.to_thread(self._health_sync)
        except sqlite3.Error:
            return False

    async def claim(self, identity: RequestIdentity) -> ClaimResult:
        return await asyncio.to_thread(self._claim_sync, identity)

    async def complete(self, request_id: str, owner_token: str) -> None:
        await asyncio.to_thread(self._complete_sync, request_id, owner_token)

    async def release(self, request_id: str, owner_token: str) -> None:
        await asyncio.to_thread(self._release_sync, request_id, owner_token)

    async def stage_raw_messages(
        self,
        request_id: str,
        owner_token: str,
        messages: list[RawMessageWrite],
    ) -> None:
        await asyncio.to_thread(self._stage_raw_messages_sync, request_id, owner_token, messages)

    async def search_raw_messages(self, user_scope: str, query: str, limit: int) -> list[RawMessageHit]:
        return await asyncio.to_thread(self._search_raw_messages_sync, user_scope, query, limit)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_requests (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
                    completed_at TEXT,
                    owner_token TEXT,
                    processing_started_at REAL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_messages (
                    document_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    user_scope TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    timestamp_ms INTEGER,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'active')),
                    UNIQUE(request_id, message_index)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS raw_messages_user_status
                ON raw_messages(user_scope, status)
                """
            )

    def _health_sync(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 AS healthy").fetchone()
        return row is not None and row["healthy"] == 1

    def _claim_sync(self, identity: RequestIdentity) -> ClaimResult:
        now = time.time()
        owner_token = uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT user_id, session_id, payload_hash, status, processing_started_at
                FROM processed_requests
                WHERE request_id = ?
                """,
                (identity.request_id,),
            ).fetchone()

            if row is None:
                connection.execute(
                    """
                    INSERT INTO processed_requests (
                        request_id, user_id, session_id, payload_hash, status,
                        completed_at, owner_token, processing_started_at
                    ) VALUES (?, ?, ?, ?, 'processing', NULL, ?, ?)
                    """,
                    (
                        identity.request_id,
                        identity.user_id,
                        identity.session_id,
                        identity.payload_hash,
                        owner_token,
                        now,
                    ),
                )
                connection.commit()
                return ClaimResult(status=ClaimStatus.ACQUIRED, owner_token=owner_token)

            if (
                row["user_id"] != identity.user_id
                or row["session_id"] != identity.session_id
                or row["payload_hash"] != identity.payload_hash
            ):
                raise RequestConflictError(f"request_id '{identity.request_id}' was already used")

            if row["status"] == "completed":
                connection.commit()
                return ClaimResult(status=ClaimStatus.COMPLETED)

            started_at = row["processing_started_at"]
            if started_at is None or now - float(started_at) >= self._lease_seconds:
                # Stable document IDs make retrying an abandoned claim an upsert, while
                # the owner token prevents the old worker from completing the new lease.
                connection.execute(
                    """
                    UPDATE processed_requests
                    SET owner_token = ?, processing_started_at = ?
                    WHERE request_id = ? AND status = 'processing'
                    """,
                    (owner_token, now, identity.request_id),
                )
                connection.commit()
                return ClaimResult(status=ClaimStatus.ACQUIRED, owner_token=owner_token)

            connection.commit()
            return ClaimResult(status=ClaimStatus.WAIT)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _complete_sync(self, request_id: str, owner_token: str) -> None:
        completed_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE processed_requests
                SET status = 'completed', completed_at = ?, owner_token = NULL,
                    processing_started_at = NULL
                WHERE request_id = ? AND status = 'processing' AND owner_token = ?
                """,
                (completed_at, request_id, owner_token),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"claim for request_id '{request_id}' is no longer owned")
            connection.execute(
                """
                UPDATE raw_messages
                SET status = 'active'
                WHERE request_id = ? AND status = 'pending'
                """,
                (request_id,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _release_sync(self, request_id: str, owner_token: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM processed_requests
                WHERE request_id = ? AND status = 'processing' AND owner_token = ?
                """,
                (request_id, owner_token),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    DELETE FROM raw_messages
                    WHERE request_id = ? AND status = 'pending'
                    """,
                    (request_id,),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _stage_raw_messages_sync(
        self,
        request_id: str,
        owner_token: str,
        messages: list[RawMessageWrite],
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                """
                SELECT 1
                FROM processed_requests
                WHERE request_id = ? AND status = 'processing' AND owner_token = ?
                """,
                (request_id, owner_token),
            ).fetchone()
            if owner is None:
                raise LeaseLostError(f"claim for request_id '{request_id}' is no longer owned")

            for message in messages:
                connection.execute(
                    """
                    INSERT INTO raw_messages (
                        document_id, request_id, message_index, user_scope, session_id,
                        role, content, search_text, timestamp_ms, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    ON CONFLICT(document_id) DO UPDATE SET
                        request_id = excluded.request_id,
                        message_index = excluded.message_index,
                        user_scope = excluded.user_scope,
                        session_id = excluded.session_id,
                        role = excluded.role,
                        content = excluded.content,
                        search_text = excluded.search_text,
                        timestamp_ms = excluded.timestamp_ms,
                        status = 'pending'
                    """,
                    (
                        message.document_id,
                        message.request_id,
                        message.message_index,
                        message.user_scope,
                        message.session_id,
                        message.role,
                        message.content,
                        message.search_text,
                        message.timestamp_ms,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _search_raw_messages_sync(self, user_scope: str, query: str, limit: int) -> list[RawMessageHit]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT document_id, content, search_text, session_id, role, timestamp_ms
                FROM raw_messages
                WHERE user_scope = ? AND status = 'active'
                """,
                (user_scope,),
            ).fetchall()
        documents = [
            RawMessageDocument(
                document_id=row["document_id"],
                content=row["content"],
                session_id=row["session_id"],
                role=row["role"],
                timestamp_ms=row["timestamp_ms"],
                search_text=row["search_text"],
            )
            for row in rows
        ]
        return rank_raw_messages(query, documents, limit)
