"""Database helpers: connecting, creating the schema, and the single-run lock."""

import json
import logging
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

log = logging.getLogger(__name__)

# Any fixed number works; every pipeline run uses the same one.
PIPELINE_LOCK_ID = 7_231_001

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect() -> psycopg.Connection:
    """Connect using the standard libpq environment variables (PGHOST, PGUSER, ...).

    The connection is in autocommit mode: every write in the pipeline happens inside an explicit
    `with conn.transaction():` block, which makes the transaction boundaries easy to see.
    TCP keepalives make the server notice quickly when a pod dies, which releases its lock.
    """
    return psycopg.connect(
        "",
        autocommit=True,
        application_name="orbital-pipeline",
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=3,
    )


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))


@contextmanager
def pipeline_lock(conn: psycopg.Connection, wait_seconds: int) -> Iterator[None]:
    """Make sure only one pipeline run works at a time.

    This is a session-level Postgres advisory lock: it is held by this connection, and Postgres
    releases it automatically if the connection goes away (for example, the pod is killed).
    A second run waits for the first one to finish and then runs normally. Because every run is
    idempotent, the second run produces exactly the same result.

    The lock is a safety net, not the only line of defence: the writes themselves are upserts
    and single-transaction swaps, so even two unlocked runs could not double-count.
    """
    log.info("Waiting for the pipeline lock (up to %ss)", wait_seconds)
    conn.execute(f"SET lock_timeout = '{int(wait_seconds)}s'")
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (PIPELINE_LOCK_ID,))
    except psycopg.errors.LockNotAvailable:
        raise RuntimeError(f"Another pipeline run held the lock for more than {wait_seconds}s") from None
    finally:
        conn.execute("RESET lock_timeout")
    log.info("Acquired the pipeline lock")
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (PIPELINE_LOCK_ID,))
        log.info("Released the pipeline lock")


def start_run(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO pipeline_run (hostname) VALUES (%s) RETURNING run_id", (socket.gethostname(),)
    ).fetchone()
    return row[0]


def finish_run(conn: psycopg.Connection, run_id: int, status: str, details: dict) -> None:
    conn.execute(
        "UPDATE pipeline_run SET finished_at = now(), status = %s, details = %s WHERE run_id = %s",
        (status, json.dumps(details, default=str), run_id),
    )
