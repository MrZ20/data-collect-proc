"""Persistent run, failure streak, and notification state."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator
import zlib


class State:
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, attempt INTEGER NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_jobs (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS observations (
                workflow TEXT NOT NULL, job_name TEXT NOT NULL, step_name TEXT NOT NULL,
                run_id INTEGER NOT NULL, status TEXT NOT NULL, completed_at TEXT NOT NULL,
                UNIQUE (workflow, job_name, step_name, run_id)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                job_id INTEGER PRIMARY KEY, record TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS http_cache (
                key TEXT PRIMARY KEY, etag TEXT NOT NULL, payload BLOB NOT NULL,
                window_end TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()

    def get_meta(self, key: str, default=None):
        row = self._db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value),
        )

    def get_http_cache(self, key: str) -> tuple[str, dict] | None:
        row = self._db.execute("SELECT etag, payload FROM http_cache WHERE key = ?", (key,)).fetchone()
        return (row[0], json.loads(zlib.decompress(row[1]))) if row else None

    def put_http_cache(self, key: str, etag: str, payload: dict, window_end: str) -> None:
        compressed = zlib.compress(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        self._db.execute(
            "INSERT INTO http_cache (key, etag, payload, window_end) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET etag = excluded.etag, "
            "payload = excluded.payload, window_end = excluded.window_end",
            (key, etag, compressed, window_end),
        )

    def delete_http_cache(self, key: str) -> None:
        self._db.execute("DELETE FROM http_cache WHERE key = ?", (key,))

    def prune_http_cache(self, before: str) -> None:
        self._db.execute("DELETE FROM http_cache WHERE window_end < ?", (before,))

    def track_run(self, run_dict: dict) -> None:
        self._db.execute(
            "INSERT INTO runs (id, attempt, status) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET attempt = excluded.attempt, status = excluded.status",
            (run_dict["id"], run_dict["run_attempt"], run_dict["status"]),
        )

    def pending_run_ids(self) -> list[int]:
        return [row[0] for row in self._db.execute(
            "SELECT id FROM runs WHERE status != 'completed' ORDER BY id"
        )]

    def forget_run(self, run_id: int) -> None:
        self._db.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    def known_attempt(self, run_id: int) -> int | None:
        row = self._db.execute(
            "SELECT attempt FROM runs WHERE id = ? AND status = 'completed'", (run_id,)
        ).fetchone()
        return row[0] if row else None

    def has_job(self, job_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM seen_jobs WHERE id = ?", (job_id,)).fetchone() is not None

    def mark_job(self, job_id: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO seen_jobs (id) VALUES (?)", (job_id,))

    def observe(self, workflow: str, job_name: str, step_name: str, run_id: int,
                status: str, completed_at: str) -> None:
        if status == "skip":
            return
        if status not in ("failure", "success"):
            raise ValueError("observation status must be failure, success, or skip")
        self._db.execute(
            "INSERT OR IGNORE INTO observations "
            "(workflow, job_name, step_name, run_id, status, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (workflow, job_name, step_name, run_id, status, completed_at),
        )

    def streak(self, workflow: str, job_name: str, step_name: str, at: str | None = None) -> int:
        query = ("SELECT status FROM observations WHERE workflow = ? AND job_name = ? AND step_name = ?")
        params = [workflow, job_name, step_name]
        if at is not None:
            query += " AND completed_at <= ?"
            params.append(at)
        query += " ORDER BY completed_at DESC, rowid DESC"
        count = 0
        for (status,) in self._db.execute(query, params):
            if status == "success":
                break
            count += 1
        return count

    def enqueue(self, job_id: int, record_dict: dict) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO outbox (job_id, record) VALUES (?, ?)",
            (job_id, json.dumps(record_dict, ensure_ascii=False)),
        )

    def pending(self) -> list[tuple[int, dict]]:
        return [(job_id, json.loads(record)) for job_id, record in self._db.execute(
            "SELECT job_id, record FROM outbox WHERE acknowledged = 0 ORDER BY rowid"
        )]

    def acknowledge(self, job_ids: list[int]) -> None:
        self._db.executemany("UPDATE outbox SET acknowledged = 1 WHERE job_id = ?", ((job_id,) for job_id in job_ids))
