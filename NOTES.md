# Notes

# Orbital Compute: commitment utilization pipeline

Daily pipeline that loads usage from the billing API and contract terms from Finance's
commitments workbooks into Postgres, and publishes `contract_utilization_daily` and
`unattributed_usage_daily` to Postgres. 
It runs as a Kubernetes CronJob on a local kind cluster.

The assignment brief is in [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md)
design notes are in [NOTES.md](NOTES.md).

# Layers
`raw_usage` (latest version of each API record) 
```sql
SELECT * 
FROM public.raw_usage
WHERE customer_id = 'C009'
ORDER BY usage_date 
```

`raw_commitment_snapshot` (every workbook snapshot) → `contract_history` (SCD2) 
```sql
SELECT * 
FROM public.raw_commitment_snapshot
WHERE contract_id in ('K-1009')
ORDER BY snapshot_date 
```

# Layout

1.docker build -t orbital-billing-api:1.0 mock_api → ENTRYPOINT ["python", "server.py"]
2.docker build -t orbital-pipeline:1.0 pipeline → ENTRYPOINT ["python", "-m", "orbital_pipeline"]

```
run.py                       "up" - create the cluster and deploy everything.  
                             "export" - write the model tables.
                             "down" - delete the cluster.
k8s/                         ConfigMap + Secret, CronJob, Postgres StatefulSet.
pipeline/
  Dockerfile
  orbital_pipeline/
    __main__.py              one run: lock -> usage -> commitments -> model
    billing_api.py           API client: token refresh, retries, pagination
    ingest_usage.py          incremental usage load (watermark + lookback, upsert)
    commitments_workbook.py  reads a workbook whatever its layout
    ingest_commitments.py    snapshots + SCD2 contract history
    model.sql, model.py      the two tables, data-quality checks, atomic publish
    schema.sql               all tables and views
    db.py                    connection, schema, advisory lock
  tests/                     unit tests (python -m unittest, run from pipeline/)
scripts/grading_scenario.py  end-to-end check of the review scenario
mock_api/, data/             billing API and contract terms as provided
```

## Running it

The commands work as specified:
```bash
python run.py up          # cluster + images + billing API + Postgres + CronJob
kubectl -n orbital create job run-1 --from=cronjob/orbital-pipeline
python run.py export out/ # out/contract_utilization_daily.csv, out/unattributed_usage_daily.csv
python run.py down
```

For scenario (pod killed mid-run, incremental, repeat, concurrent runs, full reload) and check that every export matches:

```bash
python scripts/grading_scenario.py
```
It takes a few minutes and ends with "All steps passed."

## Design decisions and trade-offs

**Postgres, every write in a transaction.** 
The hard requirements are "a run can be killed at any moment" and "two runs can overlap". Postgres solves them directly with transactions,
`INSERT ... ON CONFLICT` and advisory locks. 
The data is small (~5.6k usage rows), so an OLAP engine would add operational cost without benefit.

**Usage: the data is the watermark.** 
Each run fetches from `max(usage_date) − 35 days` to today,
in 31-day windows. Restatements happen at most 30 days after the usage date, so everything that
can have changed is in range (5 days of margin); late days are covered too. 
There is no separate "last run" state that could drift from the data.

**Idempotent and crash-safe.** 
Each window is one transaction: an upsert by `record_id` that only
overwrites when `updated_at` is newer, so replays change nothing. A killed backfill keeps its
finished windows and the next run continues from them. Commitments (snapshots + SCD2 rebuild) is
one transaction. The model is rebuilt, checked and published in one transaction, so readers see
the old tables or the new ones, never a mix.

**Concurrency.** A session-level advisory lock serialises runs; a second run waits, then repeats
the idempotent work. If a pod dies, Postgres rolls back and releases the lock (TCP keepalives;
SIGTERM is handled). Upserts and primary keys prevent double counting even without the lock.
`concurrencyPolicy: Forbid` is set but not relied on, since `kubectl create job` bypasses it.

**Commitments.** 
The sheet and header row are found by content and columns by known names.
Values are cleaned (`'$900,000.00'`, `'1 Jun 2026'`, `' K-1002 '`), TOTAL rows skipped, exact
duplicates dropped (K-1008). SCD2 versions are keyed by snapshot date. A contract removed from
the tracker (K-1004) keeps its history, and the model uses each contract's latest known terms.
Attribution is by customer and date only; `status` is kept but decides nothing.

**Model.** 
Rebuilt in full every run from a date spine (days without usage get 0): milliseconds
at this size, and no incremental state to get wrong.

**Left out on purpose:** 
incremental model builds, a staging schema, in-cluster metrics and
alerting, Helm/Kustomize, backups. The Secret has dev values in git so `run.py up` works from a
clone.

## Production (spot nodes, Airflow)

- **Scheduling:** a daily DAG `ingest_usage → ingest_commitments → build_model → checks →
  publish`, each task running this image via `KubernetesPodOperator` on spot nodes with retries
  (safe, since every task is idempotent). 
  Managed Postgres (RDS/Cloud SQL), never on spot.
- **Backfills / reprocessing:** `--from-date` (lowers the watermark) and `--full-refresh` as DAG
  params. The model is always a full rebuild, so a SQL fix needs one run.
- **Alerting:** failed task → Slack/PagerDuty; SLA if not published by 08:00 UTC; freshness alert
  if the latest usage date is more than 2 days behind; DQ warnings in a daily digest.
- **CI/CD:** unit tests and lint on every PR

## When Finance changes the workbook layout again

- Reordered or extra columns, new title rows, a renamed sheet: handled.
- A renamed *required* column or an unparseable value: the run **fails before writing anything**,
  with an error listing the headers it found. The published tables stay as they are. That failure
  is the alert; the fix is one line in `COLUMN_ALIASES`.
- No fuzzy matching on purpose: a wrong guess (e.g. "Contract Value" as the commitment) would
  silently publish wrong numbers, which is worse than a failed run.
- Long term: agree on a template with Finance (locked header, data validation).


## How I used AI tools

I used Claude as a pair programmer: to go through the brief and the data and list the traps, to
discuss the storage choice, to draft the code, manifests and these notes, and to write an
independent reference calculation in plain Python that I compared against the pipeline's output.
I reviewed and tested every part myself and can explain the decision.

## Useful queries (`kubectl -n orbital exec -it postgres-0 -- psql -U orbital`):

```sql
SELECT run_id, started_at, status, details->'model' FROM pipeline_run ORDER BY run_id;
SELECT * FROM contract_history ORDER BY contract_id, version;   -- SCD2 history
SELECT * FROM contract_latest_terms;                            -- what the model uses

-- In order to show the risk per contract_id:
SELECT  date,consumed_usd, consumed_to_date_usd, expected_to_date_usd  ,utilization_pct ,at_risk  
FROM public.contract_utilization_daily
WHERE contract_id in ('K-1009') and customer_id = 'C009'
ORDER BY  date

SELECT DISTINCT ON (contract_id) contract_id, customer_id, date, round(utilization_pct, 2) AS util, at_risk
FROM contract_utilization_daily 
WHERE contract_id in ('K-1009') and customer_id = 'C009'
ORDER BY contract_id, date DESC;

-- Reconciliation: attributed spend + unattributed spend must equal total non-void spend.
-- A result of 0 means every dollar is counted exactly once: nothing is lost, and nothing is
-- double-counted between contracts and on-demand usage.
SELECT (SELECT sum(consumed_usd) FROM contract_utilization_daily)
     + (SELECT sum(amount_usd)   FROM unattributed_usage_daily)
     - (SELECT sum(amount_usd)   FROM customer_spend_daily
        WHERE date <= (SELECT max(date) FROM contract_utilization_daily)) AS diff;
```

## Run the reconciliation query

kubectl --context kind-orbital -n orbital exec postgres-0 -c postgres -- psql -U orbital -c "SELECT (SELECT sum(consumed_usd) FROM contract_utilization_daily) + (SELECT sum(amount_usd) FROM unattributed_usage_daily) - (SELECT sum(amount_usd) FROM customer_spend_daily WHERE date <= (SELECT max(date) FROM contract_utilization_daily)) AS diff;"

OUT
 diff 
------
 0.00

The query returns 0.00 on the cluster, so every dollar is counted exactly once. 



