"""Session store — persistent session management for multi-turn conversations.

Provides two things:

1. **SQLite session registry** (via :mod:`aiosqlite`) — stores session metadata
   (session_id, project_name, created_at, last_active, turn_count) in
   ``data/sessions.db``.  Sessions older than ``SESSION_TTL_DAYS`` days are
   automatically eligible for cleanup.

2. **LangGraph checkpointer factory** — returns an in-process
   :class:`~langgraph.checkpoint.memory.InMemorySaver` that can be attached
   to the compiled graph for checkpointing.  Upgrade path: swap this for a
   SQLite- or Redis-backed saver when ``langgraph-checkpoint-sqlite`` becomes
   available.

### Usage

```python
from src.services.session_store import SessionStore

store = SessionStore()
await store.initialize()

session_id = await store.create_session("my-project")
await store.update_session(session_id, turn_count=1)
info = await store.get_session(session_id)   # SessionInfo | None
checkpointer = store.make_checkpointer()     # attach to compiled graph
await store.cleanup_expired_sessions()       # prune sessions > 7 days old
await store.close()
```
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_TTL_DAYS: int = 7
"""Sessions inactive for longer than this are eligible for cleanup."""

_DEFAULT_DB_PATH = Path("data/sessions.db")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class SessionInfo(BaseModel):
    """Metadata for a single multi-turn session."""

    session_id: str = Field(description="UUID for the session")
    project_name: str = Field(default="untitled")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    turn_count: int = Field(default=0, ge=0)
    pipeline_mode: str = Field(default="full")
    routing_decision: str = Field(default="new_request")
    is_active: bool = Field(default=True)


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


class SessionStore:
    """Async SQLite-backed session registry + LangGraph checkpointer factory.

    Args:
        db_path: Path to the SQLite database file.
            Defaults to ``data/sessions.db`` (relative to cwd).
    """

    def __init__(self, db_path: Path | str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._log = logger.bind(component="session_store", db=str(self._db_path))

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the database connection and create tables if needed.

        Creates ``data/`` directory if it does not exist.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._log.info("session_store_initializing", path=str(self._db_path))
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        self._log.info("session_store_initialized")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._log.info("session_store_closed")

    # ── Schema ─────────────────────────────────────────────────────────────

    async def _create_tables(self) -> None:
        """Create session tables if they do not already exist."""
        assert self._db is not None, "Call initialize() first"
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id     TEXT PRIMARY KEY,
                project_name   TEXT    NOT NULL DEFAULT 'untitled',
                created_at     TEXT    NOT NULL,
                last_active    TEXT    NOT NULL,
                turn_count     INTEGER NOT NULL DEFAULT 0,
                pipeline_mode  TEXT    NOT NULL DEFAULT 'full',
                routing_decision TEXT  NOT NULL DEFAULT 'new_request',
                is_active      INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await self._db.commit()

    # ── CRUD ───────────────────────────────────────────────────────────────

    async def create_session(
        self,
        project_name: str = "untitled",
        session_id: str | None = None,
    ) -> str:
        """Create a new session record.

        Args:
            project_name: Human-readable project identifier.
            session_id: Caller-supplied ID; auto-generated UUID if omitted.

        Returns:
            The ``session_id`` string.
        """
        assert self._db is not None, "Call initialize() first"
        sid = session_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO sessions
                (session_id, project_name, created_at, last_active,
                 turn_count, pipeline_mode, routing_decision, is_active)
            VALUES (?, ?, ?, ?, 0, 'full', 'new_request', 1)
            """,
            (sid, project_name, now, now),
        )
        await self._db.commit()
        self._log.info("session_created", session_id=sid, project=project_name)
        return sid

    async def get_session(self, session_id: str) -> SessionInfo | None:
        """Retrieve a session by ID.

        Args:
            session_id: The session UUID.

        Returns:
            ``SessionInfo`` if found, ``None`` otherwise.
        """
        assert self._db is not None, "Call initialize() first"
        async with self._db.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return SessionInfo(
            session_id=row["session_id"],
            project_name=row["project_name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_active=datetime.fromisoformat(row["last_active"]),
            turn_count=row["turn_count"],
            pipeline_mode=row["pipeline_mode"],
            routing_decision=row["routing_decision"],
            is_active=bool(row["is_active"]),
        )

    async def update_session(
        self,
        session_id: str,
        turn_count: int | None = None,
        pipeline_mode: str | None = None,
        routing_decision: str | None = None,
        is_active: bool | None = None,
    ) -> None:
        """Update mutable session fields.

        Always bumps ``last_active`` to now.

        Args:
            session_id: Session to update.
            turn_count: New turn count, if changed.
            pipeline_mode: New pipeline mode, if changed.
            routing_decision: New routing decision, if changed.
            is_active: Active flag, if changed.
        """
        assert self._db is not None, "Call initialize() first"
        now = datetime.now(timezone.utc).isoformat()

        updates: list[str] = ["last_active = ?"]
        params: list[Any] = [now]

        if turn_count is not None:
            updates.append("turn_count = ?")
            params.append(turn_count)
        if pipeline_mode is not None:
            updates.append("pipeline_mode = ?")
            params.append(pipeline_mode)
        if routing_decision is not None:
            updates.append("routing_decision = ?")
            params.append(routing_decision)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)

        params.append(session_id)
        sql = f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?"
        await self._db.execute(sql, params)
        await self._db.commit()
        self._log.debug("session_updated", session_id=session_id)

    async def list_sessions(self, active_only: bool = True) -> list[SessionInfo]:
        """List all sessions, optionally filtered to active ones.

        Args:
            active_only: If ``True``, return only active sessions.

        Returns:
            List of ``SessionInfo`` objects.
        """
        assert self._db is not None, "Call initialize() first"
        sql = "SELECT * FROM sessions"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY last_active DESC"

        async with self._db.execute(sql) as cursor:
            rows = await cursor.fetchall()

        return [
            SessionInfo(
                session_id=row["session_id"],
                project_name=row["project_name"],
                created_at=datetime.fromisoformat(row["created_at"]),
                last_active=datetime.fromisoformat(row["last_active"]),
                turn_count=row["turn_count"],
                pipeline_mode=row["pipeline_mode"],
                routing_decision=row["routing_decision"],
                is_active=bool(row["is_active"]),
            )
            for row in rows
        ]

    # ── TTL cleanup ────────────────────────────────────────────────────────

    async def cleanup_expired_sessions(self) -> int:
        """Mark sessions inactive if they have not been used within TTL.

        Sessions that have not been active for ``SESSION_TTL_DAYS`` days are
        soft-deleted (``is_active = 0``).

        Returns:
            Number of sessions deactivated.
        """
        assert self._db is not None, "Call initialize() first"
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)
        ).isoformat()

        result = await self._db.execute(
            """
            UPDATE sessions
            SET is_active = 0
            WHERE is_active = 1 AND last_active < ?
            """,
            (cutoff,),
        )
        await self._db.commit()
        deactivated = result.rowcount or 0
        if deactivated > 0:
            self._log.info(
                "expired_sessions_cleaned",
                count=deactivated,
                ttl_days=SESSION_TTL_DAYS,
            )
        return deactivated

    # ── LangGraph checkpointer ─────────────────────────────────────────────

    @staticmethod
    def make_checkpointer() -> MemorySaver:
        """Return a LangGraph checkpointer for graph compilation.

        Currently returns an in-memory saver.  For production upgrade,
        swap for a ``SqliteSaver`` or ``RedisSaver`` when the dependency
        is available.

        Returns:
            A ``MemorySaver`` (``InMemorySaver``) instance.
        """
        return MemorySaver()
