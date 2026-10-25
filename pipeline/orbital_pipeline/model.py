"""Build the Part 2 tables and publish them atomically, guarded by data-quality checks.

Everything happens in a single transaction:
    rebuild both tables -> run the checks -> commit.
If a blocking check fails (or the pod is killed at any point), the transaction rolls back and
the previously published tables stay exactly as they were.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MODEL_FILE = Path(__file__).with_name("model.sql")


class DataQualityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    blocking: bool
    sql: str  # returns the violating rows; no rows means the check passed


CHECKS = [
    Check(
        name="Contracts of the same customer must not overlap",
        blocking=True,
        sql="""
            SELECT a.contract_id, b.contract_id, a.customer_id
            FROM contract_latest_terms a
            JOIN contract_latest_terms b
              ON a.customer_id = b.customer_id
             AND a.contract_id < b.contract_id
             AND a.start_date <= b.end_date
             AND b.start_date <= a.end_date
        """,
    ),
    Check(
        name="Every dollar of spend is counted exactly once (attributed + unattributed = total)",
        blocking=True,
        sql="""
            SELECT total, attributed, unattributed
            FROM (SELECT
                    (SELECT COALESCE(SUM(amount_usd), 0) FROM customer_spend_daily)          AS total,
                    (SELECT COALESCE(SUM(consumed_usd), 0) FROM contract_utilization_daily)  AS attributed,
                    (SELECT COALESCE(SUM(amount_usd), 0) FROM unattributed_usage_daily)      AS unattributed
                 ) t
            WHERE total <> attributed + unattributed
        """,
    ),
    Check(
        name="The model is not empty",
        blocking=True,
        sql="SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM contract_utilization_daily)",
    ),
    Check(
        name="No negative usage amounts",
        blocking=True,
        sql="SELECT record_id, amount_usd FROM raw_usage WHERE amount_usd < 0",
    ),
    # Warnings: worth a look, but they describe the data rather than a broken pipeline.
    Check(
        name="Contracts removed from the tracker (kept with their last known terms)",
        blocking=False,
        sql="SELECT contract_id, end_date FROM contract_latest_terms WHERE removed_from_tracker",
    ),
    Check(
        name="Customers with usage but no contract ever",
        blocking=False,
        sql="""
            SELECT DISTINCT customer_id FROM raw_usage
            WHERE customer_id NOT IN (SELECT customer_id FROM contract_latest_terms)
        """,
    ),
    Check(
        name="Void usage records (excluded from spend)",
        blocking=False,
        sql="SELECT record_id FROM raw_usage WHERE status = 'void'",
    ),
]


def build_and_publish(conn: psycopg.Connection) -> dict:
    with conn.transaction():
        # Writers wait here; readers keep seeing the old data until we commit.
        conn.execute("LOCK TABLE contract_utilization_daily, unattributed_usage_daily IN EXCLUSIVE MODE")
        conn.execute(MODEL_FILE.read_text(encoding="utf-8"))
        warnings = run_checks(conn)

        summary = {
            "contract_utilization_rows": _count(conn, "contract_utilization_daily"),
            "unattributed_rows": _count(conn, "unattributed_usage_daily"),
            "last_usage_date": conn.execute("SELECT MAX(usage_date) FROM raw_usage").fetchone()[0],
            "contracts_at_risk_latest_day": conn.execute(
                """SELECT COUNT(*) FROM contract_utilization_daily u
                   WHERE at_risk AND date = (SELECT MAX(date) FROM contract_utilization_daily x
                                             WHERE x.contract_id = u.contract_id)"""
            ).fetchone()[0],
            "dq_warnings": warnings,
        }
    log.info("Published model: %s", summary)
    return summary


def run_checks(conn: psycopg.Connection) -> list[str]:
    """Run all checks. Raise on a failed blocking check; return the failed warnings."""
    warnings = []
    for check in CHECKS:
        rows = conn.execute(check.sql).fetchall()
        if not rows:
            log.info("DQ ok:   %s", check.name)
            continue
        sample = rows[:5]
        if check.blocking:
            raise DataQualityError(f"Blocking check failed: {check.name}. Examples: {sample}")
        log.warning("DQ warn: %s (%d rows, e.g. %s)", check.name, len(rows), sample)
        warnings.append(f"{check.name}: {len(rows)}")
    return warnings


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
