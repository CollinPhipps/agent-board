"""SQLite-backed board. Every agent runs its own server process against one file."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

DB_NAME = "hackathon_board.db"
DEFAULT_LOCK_TTL = 900.0
MAX_MESSAGES = 100
TASK_STATUSES = ("todo", "in_progress", "blocked", "completed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner       TEXT,
    status      TEXT NOT NULL DEFAULT 'todo'
                CHECK (status IN ('todo', 'in_progress', 'blocked', 'completed'))
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL DEFAULT 'all',
    content   TEXT NOT NULL,
    timestamp DATETIME NOT NULL
);
CREATE TABLE IF NOT EXISTS file_locks (
    filepath  TEXT PRIMARY KEY,
    locked_by TEXT NOT NULL,
    locked_at DATETIME NOT NULL
);
"""


def _timestamp(at: datetime | None = None) -> str:
    """Fixed-width UTC, so timestamps compare correctly as strings in SQL."""
    return (at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


class Board:
    def __init__(
        self,
        root: str | Path,
        db_path: str | Path | None = None,
        lock_ttl: float = DEFAULT_LOCK_TTL,
    ) -> None:
        self.root = Path(root).resolve()
        self.db_path = Path(db_path) if db_path else self.root / DB_NAME
        self.lock_ttl = lock_ttl
        self._initialize()

    def _open(self) -> sqlite3.Connection:
        # A connection per operation keeps this safe under any server threading model.
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Several agents may start at once. Switching to WAL needs an exclusive
        # lock and fails immediately instead of honouring the busy timeout.
        for attempt in range(50):
            try:
                with self._connection() as conn:
                    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    conn.executescript(f"BEGIN IMMEDIATE;{SCHEMA}COMMIT;")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error) or attempt == 49:
                    raise
                time.sleep(0.05)
        if mode.lower() != "wal":
            log.warning("%s is not in WAL mode (got %s)", self.db_path, mode)

    # Messages

    def post_message(self, sender: str, content: str, recipient: str = "all") -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (sender, recipient, content, timestamp) VALUES (?, ?, ?, ?)",
                (
                    _require(sender, "sender"),
                    recipient.strip() or "all",
                    _require(content, "content"),
                    _timestamp(),
                ),
            )
            return cursor.lastrowid

    def read_messages(
        self, limit: int = 10, since_id: int = 0, agent_name: str = ""
    ) -> list[dict]:
        """since_id=0 gives the newest messages; since_id>0 pages forward without gaps."""
        limit = max(1, min(limit, MAX_MESSAGES))
        where, params = "id > ?", [since_id]
        if agent_name.strip():
            where += " AND recipient IN ('all', ?)"
            params.append(agent_name.strip())
        order = "ASC" if since_id > 0 else "DESC"
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY id {order} LIMIT ?",
                (*params, limit),
            ).fetchall()
        return sorted((dict(row) for row in rows), key=lambda message: message["id"])

    # Tasks

    def create_task(self, title: str, description: str = "") -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (title, description) VALUES (?, ?)",
                (_require(title, "title"), description),
            )
            return cursor.lastrowid

    def claim_task(self, task_id: int, agent_name: str) -> bool:
        """Claiming a task you already hold succeeds, so retries are safe."""
        agent = _require(agent_name, "agent_name")
        with self._connection() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status = 'in_progress', owner = ?
                   WHERE id = ? AND (status = 'todo' OR (status = 'in_progress' AND owner = ?))""",
                (agent, task_id, agent),
            )
            return cursor.rowcount == 1

    def complete_task(self, task_id: int, agent_name: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'completed' WHERE id = ? AND owner = ?",
                (task_id, _require(agent_name, "agent_name")),
            )
            return cursor.rowcount == 1

    def update_task_status(self, task_id: int, agent_name: str, status: str) -> bool:
        """Owner only. Moving to 'todo' gives the task up; use complete_task to finish."""
        if status not in ("todo", "in_progress", "blocked"):
            raise ValueError("status must be one of: todo, in_progress, blocked")
        agent = _require(agent_name, "agent_name")
        owner_sql = "NULL" if status == "todo" else "owner"
        with self._connection() as conn:
            cursor = conn.execute(
                f"""UPDATE tasks SET status = ?, owner = {owner_sql}
                    WHERE id = ? AND owner = ? AND status != 'completed'""",
                (status, task_id, agent),
            )
            return cursor.rowcount == 1

    def list_tasks(self, status: str = "all") -> list[dict]:
        if status != "all" and status not in TASK_STATUSES:
            raise ValueError(f"status must be 'all' or one of: {', '.join(TASK_STATUSES)}")
        query, params = "SELECT * FROM tasks", ()
        if status != "all":
            query, params = query + " WHERE status = ?", (status,)
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(query + " ORDER BY id", params)]

    def force_release_task(self, task_id: int) -> bool:
        """Human override for a task whose owner has gone away."""
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'todo', owner = NULL WHERE id = ?", (task_id,)
            )
            return cursor.rowcount == 1

    # File locks

    def normalize_path(self, filepath: str) -> str:
        """Map every spelling of a file to one repo-relative key.

        Agents on different machines have different checkouts, so a path from
        outside the repo can never identify the same file for everyone.
        """
        path = _require(filepath, "filepath").replace("\\", "/")
        if os.path.isabs(path) and Path(os.path.realpath(path)).is_relative_to(self.root):
            path = os.path.relpath(os.path.realpath(path), self.root)
        path = os.path.normpath(path)
        if os.path.isabs(path) or re.match(r"[A-Za-z]:/", path) or path.split("/")[0] == "..":
            raise ValueError(f"Use a path relative to the repository root, not {filepath!r}")
        return path

    def _expiry_cutoff(self) -> str:
        return _timestamp(datetime.now(timezone.utc) - timedelta(seconds=self.lock_ttl))

    def acquire_file_lock(self, filepath: str, agent_name: str) -> bool:
        """Succeeds if the file is free, expired, or already yours (renewing the lock)."""
        with self._connection() as conn:
            cursor = conn.execute(
                """INSERT INTO file_locks (filepath, locked_by, locked_at) VALUES (?, ?, ?)
                   ON CONFLICT (filepath) DO UPDATE
                   SET locked_by = excluded.locked_by, locked_at = excluded.locked_at
                   WHERE file_locks.locked_by = excluded.locked_by
                      OR file_locks.locked_at < ?""",
                (
                    self.normalize_path(filepath),
                    _require(agent_name, "agent_name"),
                    _timestamp(),
                    self._expiry_cutoff(),
                ),
            )
            return cursor.rowcount == 1

    def release_file_lock(self, filepath: str, agent_name: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM file_locks WHERE filepath = ? AND locked_by = ?",
                (self.normalize_path(filepath), _require(agent_name, "agent_name")),
            )
            return cursor.rowcount == 1

    def list_file_locks(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM file_locks WHERE locked_at >= ? ORDER BY filepath",
                (self._expiry_cutoff(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def force_release_lock(self, filepath: str) -> bool:
        """Human override for a lock whose holder has gone away."""
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM file_locks WHERE filepath = ?", (self.normalize_path(filepath),)
            )
            return cursor.rowcount == 1
