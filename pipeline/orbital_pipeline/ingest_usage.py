"""Load usage records from the billing API into raw_usage.

Incremental strategy
--------------------
The watermark is the latest usage_date already in raw_usage. We never store a separate
"last run" marker: the data itself is the state, so it can't get out of sync with it.

Each run fetches from (watermark - lookback_days) up to today's date on our own clock. That
range covers everything that could have changed since the last run:
- a restatement happens at most 30 days after the usage date, and the API's "today" during the
  previous run was at least the watermark, so anything restated since then has
  usage_date > watermark - 30;
- late-arriving days are inside the same range;
- the API's real "today" is hidden from us, but it is never later than our clock, and the API
  simply returns nothing for days it hasn't published yet.
The first run (empty table) backfills from usage_start_date.

Writes
------
Each 31-day window is written in its own transaction, as an upsert keyed by record_id that only
replaces a row with a newer updated_at. Consequences:
- re-loading the same data is a no-op (idempotent);
- a run killed halfway keeps the windows it finished, and the next run continues from there
  (the watermark only moves forward after older windows are committed, because windows are
  processed oldest first);
- an older copy of a record can never overwrite a newer one.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import psycopg

from .billing_api import BillingApiClient

log = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 31  # API limit, both dates inclusive

UPSERT_SQL = """
INSERT INTO raw_usage (record_id, customer_id, usage_date, sku, quantity, unit,
                       amount_usd, status, updated_at)
VALUES (%(record_id)s, %(customer_id)s, %(usage_date)s, %(sku)s, %(quantity)s, %(unit)s,
        %(amount_usd)s, %(status)s, %(updated_at)s)
ON CONFLICT (record_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    usage_date  = EXCLUDED.usage_date,
    sku         = EXCLUDED.sku,
    quantity    = EXCLUDED.quantity,
    unit        = EXCLUDED.unit,
    amount_usd  = EXCLUDED.amount_usd,
    status      = EXCLUDED.status,
    updated_at  = EXCLUDED.updated_at,
    loaded_at   = now()
WHERE EXCLUDED.updated_at > raw_usage.updated_at
"""

REQUIRED_FIELDS = ("record_id", "customer_id", "usage_date", "sku", "quantity", "unit",
                   "amount_usd", "status", "updated_at")


def plan_windows(start: date, end: date, max_days: int = MAX_WINDOW_DAYS) -> list[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive windows of at most max_days days."""
    windows = []
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=max_days - 1), end)
        windows.append((window_start, window_end))
        window_start = window_end + timedelta(days=1)
    return windows


def fetch_start_date(conn: psycopg.Connection, usage_start_date: date, lookback_days: int) -> date:
    watermark = conn.execute("SELECT MAX(usage_date) FROM raw_usage").fetchone()[0]
    if watermark is None:
        log.info("raw_usage is empty: full backfill from %s", usage_start_date)
        return usage_start_date
    start = max(usage_start_date, watermark - timedelta(days=lookback_days))
    log.info("Latest usage date loaded is %s: incremental load from %s", watermark, start)
    return start


def load_usage(conn: psycopg.Connection, client: BillingApiClient,
               usage_start_date: date, lookback_days: int, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    start = fetch_start_date(conn, usage_start_date, lookback_days)

    fetched = changed = 0
    for window_start, window_end in plan_windows(start, today):
        records = [_validate(r, window_start, window_end)
                   for r in client.iter_usage(window_start, window_end)]
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, records)
                # Rows inserted or updated; unchanged records are skipped by the WHERE clause.
                window_changed = max(cur.rowcount, 0)
        fetched += len(records)
        changed += window_changed
        log.info("Usage %s..%s: %d records fetched, %d new or changed",
                 window_start, window_end, len(records), window_changed)

    summary = {"usage_fetch_start": start, "usage_fetch_end": today,
               "usage_records_fetched": fetched, "usage_records_changed": changed,
               "api_requests": client.request_count}
    log.info("Usage load done: %s", summary)
    return summary


def _validate(record: dict, window_start: date, window_end: date) -> dict:
    missing = [f for f in REQUIRED_FIELDS if record.get(f) is None]
    if missing:
        raise ValueError(f"Usage record {record.get('record_id')} is missing {missing}")
    usage_date = date.fromisoformat(record["usage_date"])
    if not window_start <= usage_date <= window_end:
        raise ValueError(f"API returned {record['record_id']} outside {window_start}..{window_end}")
    return {field: record[field] for field in REQUIRED_FIELDS}
