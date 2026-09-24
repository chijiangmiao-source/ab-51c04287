"""Shared state store.

One SQLite database is the single source of truth for ramp operations and the
global fencing-token sequence. All transitions are guarded by SQL predicates
(``UPDATE ... WHERE status=... AND fencing_token=...``) so a late actor loses
atomically instead of overwriting newer state.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id   TEXT PRIMARY KEY,
    magnet_id      TEXT NOT NULL,
    target_ma      INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('queued','running','succeeded')),
    fencing_token  INTEGER NOT NULL DEFAULT 0,
    lease_owner    TEXT,
    lease_expires_at REAL,
    receipt_id     TEXT,
    applied_count  INTEGER,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
-- Single-row monotonic sequence for fencing tokens.
CREATE TABLE IF NOT EXISTS sequences (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self._path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False: the API serves requests from a thread pool;
        # every method takes the lock and uses short-lived cursors instead.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO sequences(name, value) VALUES ('fencing', 0)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- command side ----------------------------------------------------

    def create_operation(self, operation_id: str, magnet_id: str, target_ma: int):
        """Insert a queued operation.

        Returns (row, created). On a duplicate operation_id, returns the
        existing row and created=False so the caller can apply idempotency
        rules (same parameters -> replay, different -> 409).
        """
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO operations"
                    " (operation_id, magnet_id, target_ma, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'queued', ?, ?)",
                    (operation_id, magnet_id, target_ma, now, now),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = self._conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return dict(row), created

    def get_operation(self, operation_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return dict(row) if row else None

    # -- executor side ---------------------------------------------------

    def claim_next(self, owner: str, lease_seconds: float):
        """Atomically claim the oldest queued (or expired-lease) operation.

        The fencing token is bumped in the same UPDATE that takes the lease,
        so any previous owner's token is immediately stale. Returns the claimed
        row (with its new token) or None when nothing is claimable.
        """
        now = time.time()
        expires = now + lease_seconds
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT operation_id FROM operations"
                    " WHERE status = 'queued'"
                    "    OR (status = 'running' AND lease_expires_at IS NOT NULL"
                    "        AND lease_expires_at < ?)"
                    " ORDER BY created_at LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                token = self._conn.execute(
                    "UPDATE sequences SET value = value + 1 WHERE name = 'fencing'"
                    " RETURNING value"
                ).fetchone()["value"]
                claimed = self._conn.execute(
                    "UPDATE operations"
                    " SET status = 'running', fencing_token = ?, lease_owner = ?,"
                    "     lease_expires_at = ?, updated_at = ?"
                    " WHERE operation_id = ?"
                    " RETURNING *",
                    (token, owner, expires, now, row["operation_id"]),
                ).fetchone()
                self._conn.execute("COMMIT")
                return dict(claimed)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def renew_lease(self, operation_id: str, owner: str, fencing_token: int,
                    lease_seconds: float) -> bool:
        """Extend the lease iff the caller still holds it. True on success."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE operations SET lease_expires_at = ?, updated_at = ?"
                " WHERE operation_id = ? AND status = 'running'"
                "   AND lease_owner = ? AND fencing_token = ?",
                (now + lease_seconds, now, operation_id, owner, fencing_token),
            )
            return cur.rowcount == 1

    def complete_operation(self, operation_id: str, owner: str, fencing_token: int,
                           receipt_id: str, applied_count: int) -> bool:
        """Mark an operation succeeded iff the fencing token still matches.

        This is the commit point. A late executor whose lease was reclaimed
        (token bumped) affects zero rows and gets False.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE operations"
                " SET status = 'succeeded', receipt_id = ?, applied_count = ?,"
                "     lease_owner = NULL, lease_expires_at = NULL, updated_at = ?"
                " WHERE operation_id = ? AND status = 'running'"
                "   AND lease_owner = ? AND fencing_token = ?",
                (receipt_id, applied_count, now, operation_id, owner, fencing_token),
            )
            return cur.rowcount == 1


INSTRUMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument_applications (
    operation_id     TEXT PRIMARY KEY,
    magnet_id        TEXT NOT NULL,
    target_ma        INTEGER NOT NULL,
    receipt_id       TEXT NOT NULL,
    applied_count    INTEGER NOT NULL,
    max_fencing_token INTEGER NOT NULL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
"""


class InstrumentStore:
    """Separate persistence for the instrument simulator (its own ledger).

    The simulator is an independent service: it keeps its own database so the
    receipt it issued survives an API/executor restart, which is exactly what
    lets a reclaimed lease converge on the original receipt.
    """

    def __init__(self, path: str):
        self._path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(INSTRUMENT_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def apply(self, operation_id: str, magnet_id: str, target_ma: int,
              fencing_token: int):
        """Idempotent-by-operation_id application with fencing-token guard.

        Returns (outcome, row): outcome is "applied" (first time),
        "replayed" (already applied, same/newer token), or "stale" (token
        below the max seen -> caller must not proceed).
        """
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM instrument_applications WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is not None and fencing_token < row["max_fencing_token"]:
                    self._conn.execute("COMMIT")
                    return "stale", dict(row)
                outcome = "replayed"
                if row is None:
                    outcome = "applied"
                    self._conn.execute(
                        "INSERT INTO instrument_applications"
                        " (operation_id, magnet_id, target_ma, receipt_id,"
                        "  applied_count, max_fencing_token, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                        (operation_id, magnet_id, target_ma,
                         f"rcpt-{operation_id}", fencing_token, now, now),
                    )
                else:
                    self._conn.execute(
                        "UPDATE instrument_applications"
                        " SET max_fencing_token = MAX(max_fencing_token, ?), updated_at = ?"
                        " WHERE operation_id = ?",
                        (fencing_token, now, operation_id),
                    )
                row = self._conn.execute(
                    "SELECT * FROM instrument_applications WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                self._conn.execute("COMMIT")
                return outcome, dict(row)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get(self, operation_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM instrument_applications WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return dict(row) if row else None
