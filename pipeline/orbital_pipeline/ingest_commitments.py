"""Load the commitments tracker snapshots and rebuild the SCD2 contract history.

Steps:
1. Read every workbook in the directory first. If any of them can't be read, stop before
   touching the database.
2. In one transaction: replace those snapshots in raw_commitment_snapshot, then rebuild
   contract_history from *all* snapshots stored there.

Rebuilding the history from scratch every run is simple and obviously idempotent, and with a few
snapshots of a few dozen contracts it costs nothing. (With thousands of snapshots we would
process only new snapshots instead.)
"""

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg

from .commitments_workbook import ContractTerms, read_workbook, snapshot_date_from_filename

log = logging.getLogger(__name__)


@dataclass
class ContractVersion:
    contract_id: str
    version: int
    terms: ContractTerms
    valid_from: date               # snapshot date where these terms first appeared
    valid_to: date | None = None   # snapshot date where they changed or the contract vanished


def build_contract_history(snapshots: dict[date, dict[str, ContractTerms]]) -> list[ContractVersion]:
    """SCD Type 2 over the snapshots, oldest first.

    - A contract seen for the first time opens version 1.
    - If its terms change, the open version is closed and a new one opens.
    - If it is missing from a snapshot, its open version is closed. The version stays in the
      history, so the contract (and its latest terms) is never lost.
    - If it comes back later, a new version opens.
    """
    history: list[ContractVersion] = []
    open_versions: dict[str, ContractVersion] = {}
    version_counts: dict[str, int] = {}

    for snapshot_date in sorted(snapshots):
        contracts = snapshots[snapshot_date]

        for contract_id, terms in sorted(contracts.items()):
            current = open_versions.get(contract_id)
            if current is not None and current.terms == terms:
                continue  # unchanged
            if current is not None:
                current.valid_to = snapshot_date
            version_counts[contract_id] = version_counts.get(contract_id, 0) + 1
            new_version = ContractVersion(contract_id, version_counts[contract_id], terms, snapshot_date)
            history.append(new_version)
            open_versions[contract_id] = new_version

        for contract_id in sorted(set(open_versions) - set(contracts)):
            log.info("Contract %s is no longer in the tracker as of %s; keeping its last terms",
                     contract_id, snapshot_date)
            open_versions.pop(contract_id).valid_to = snapshot_date

    return history


def load_commitments(conn: psycopg.Connection, commitments_dir: Path) -> dict:
    files = sorted(p for p in commitments_dir.glob("*.xlsx") if snapshot_date_from_filename(p))
    if not files:
        raise FileNotFoundError(f"No commitments_<YYYY-MM-DD>.xlsx files in {commitments_dir}")

    parsed = {snapshot_date_from_filename(path): (path, read_workbook(path)) for path in files}

    with conn.transaction():
        for snapshot_date, (path, rows) in parsed.items():
            _replace_snapshot(conn, snapshot_date, path.name, rows)
        snapshots = _read_all_snapshots(conn)
        history = build_contract_history(snapshots)
        _replace_history(conn, history)

    summary = {"snapshots_loaded": [d.isoformat() for d in parsed],
               "snapshots_total": len(snapshots),
               "contracts": len({v.contract_id for v in history}),
               "contract_versions": len(history)}
    log.info("Commitments load done: %s", summary)
    return summary


def _replace_snapshot(conn, snapshot_date: date, file_name: str, rows) -> None:
    conn.execute("DELETE FROM raw_commitment_snapshot WHERE snapshot_date = %s", (snapshot_date,))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO raw_commitment_snapshot
                   (snapshot_date, contract_id, customer_id, customer_name, start_date, end_date,
                    commitment_usd, status, source_file, source_sheet, source_row)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(snapshot_date, r.contract_id, r.terms.customer_id, r.terms.customer_name,
              r.terms.start_date, r.terms.end_date, r.terms.commitment_usd, r.terms.status,
              file_name, r.source_sheet, r.source_row) for r in rows],
        )
    log.info("Snapshot %s: %d contracts from %s", snapshot_date, len(rows), file_name)


def _read_all_snapshots(conn) -> dict[date, dict[str, ContractTerms]]:
    snapshots: dict[date, dict[str, ContractTerms]] = {}
    for (snapshot_date, contract_id, customer_id, customer_name, start_date, end_date,
         commitment_usd, status) in conn.execute(
            """SELECT snapshot_date, contract_id, customer_id, customer_name, start_date,
                      end_date, commitment_usd, status
               FROM raw_commitment_snapshot"""):
        snapshots.setdefault(snapshot_date, {})[contract_id] = ContractTerms(
            customer_id, customer_name, start_date, end_date, commitment_usd, status)
    return snapshots


def _replace_history(conn, history: list[ContractVersion]) -> None:
    conn.execute("DELETE FROM contract_history")
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO contract_history
                   (contract_id, version, customer_id, customer_name, start_date, end_date,
                    commitment_usd, status, valid_from, valid_to)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(v.contract_id, v.version, v.terms.customer_id, v.terms.customer_name,
              v.terms.start_date, v.terms.end_date, v.terms.commitment_usd, v.terms.status,
              v.valid_from, v.valid_to) for v in history],
        )
