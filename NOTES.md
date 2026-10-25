# Notes

## Main design decisions

**Postgres, everything in SQL transactions.** The hard requirements are "a run can be killed at
any moment" and "two runs can overlap". Postgres gives me the three tools that solve them
directly: transactions, `INSERT ... ON CONFLICT`, and advisory locks. The data is small
(~5.6k usage rows), so an OLAP engine would add operational cost without benefit here.

**Three layers.** `raw_usage` (latest version of each `record_id`) and `raw_commitment_snapshot`
(every snapshot ever loaded) → `contract_history` (SCD2) → the two published tables.

**Usage: the data is the watermark.** Each run fetches from `max(usage_date) − 35 days` to
today's date on our own clock, in 31-day windows. Since a restatement happens at most 30 days
after the usage date, and the previous run's API "today" was ≥ the watermark, every record that
can have changed since then is in that range (30 is the proof, 5 days are margin). Late days are
covered by the same range. The API's simulated "today" doesn't matter: days it hasn't published
just come back empty. There is no separate "last run" state that could drift from the data.

**Idempotency and crash safety.** Each window is one transaction: an upsert by `record_id` that
only overwrites when `updated_at` is newer, so replays are no-ops and stale data can't win. A
killed backfill keeps its finished windows, and the next run continues from them. Commitments
(raw snapshots + full SCD2 rebuild) is one transaction. The model is rebuilt and checked in one
transaction, so readers see the old tables or the new ones, never a mix.

**Concurrency.** A session-level advisory lock serialises runs; a second run waits and then
repeats the (idempotent) work. If a pod dies, Postgres drops its connection, rolls back and
releases the lock (TCP keepalives make this fast; SIGTERM is handled so a deleted pod releases
it immediately). The lock is a safety net: upserts and primary keys already prevent double
counting. `concurrencyPolicy: Forbid` is set but not relied on.

**Commitments.** The reader finds the sheet and header row by content, maps columns by a list
of known names, cleans values (`'$900,000.00'`, `'1 Jun 2026'`, `' K-1002 '`), skips
TOTAL/footnote rows, and drops exact duplicate rows (K-1008). SCD2 versions are keyed by
snapshot date (when the tracker showed the terms, not a business effective date). A contract that
disappears (K-1004) gets its version closed but stays in the history; the model uses each
contract's latest known terms. Attribution is by dates only: `status` (Signed/Active/At Risk) is
kept but does not decide anything.

**Model.** Rebuilt in full every run from a date spine (days without usage get 0). It takes
milliseconds at this size and has no incremental state to get wrong.

**Left out on purpose:** incremental model builds, a separate staging schema, metrics/alerting
inside the cluster, Helm/Kustomize, and backups. The Secret has dev values in git so `run.py up`
works from a clone.

## Production (real cluster on spot nodes, Airflow)

- **Scheduling:** one Airflow DAG per day with tasks `ingest_usage → ingest_commitments →
  build_model → checks → publish`, each running this image via `KubernetesPodOperator` on the
  spot pool, with retries. Since every task is idempotent, retries and preemptions are safe. The
  advisory lock stays to cover manual runs. Postgres (or a managed RDS/Cloud SQL) runs on
  on-demand nodes, never on spot.
- **Backfills / reprocessing:** the job already supports "start from date X" by lowering the
  watermark; I'd expose `--from-date` and `--full-refresh` as DAG params. Reprocessing the model
  is always a full rebuild, so a fix in SQL only needs one run.
- **Alerting:** failed DAG/task → Slack/PagerDuty; SLA if the model isn't published by 08:00
  UTC; freshness alert if the latest usage date is more than 2 days behind; DQ warnings into a
  daily digest. `pipeline_run` already records each run's counts for dashboards.
- **CI/CD:** unit tests and lint on every PR; an integration test that runs the mock API +
  Postgres in containers and compares the export to a golden file; image tagged by git SHA and
  promoted dev → prod through GitOps (Argo CD). Secrets from External Secrets / Vault.
- **Part 3 changes:** managed Postgres with backups, workbooks read from object storage (S3)
  instead of a ConfigMap, image tags by SHA instead of `:1.0`, NetworkPolicies, resource tuning.
  At larger scale I'd move the serving tables to ClickHouse/BigQuery and build them incrementally.

## When Finance changes the workbook layout again

- Reordered columns, new extra columns, new title rows, a renamed sheet: handled.
- A renamed *required* column, or a value we can't parse: the run **fails before writing
  anything**, with a message listing the headers it found ("add the new name to
  COLUMN_ALIASES"). The previously published tables stay as they are. That failure is the
  alert. The fix is one line.
- I deliberately don't fuzzy-match column names: a wrong guess (e.g. mapping "Contract Value" to
  the commitment) would silently publish wrong numbers, which is worse than a failed run.
- Better long-term: agree on a template with Finance (data validation, locked header), or move
  the tracker to a small app/form with a schema.

## Data-quality checks

Blocking (the publish transaction rolls back, the old tables stay):
- workbook layout recognised; dates and amounts parse; `start ≤ end`; amount > 0; the same
  contract twice in one snapshot with different terms;
- contracts of the same customer don't overlap (otherwise spend would be counted twice);
- reconciliation: attributed + unattributed spend = total non-void spend, to the cent;
- the model isn't empty; no negative usage amounts.

Warnings (logged and stored in `pipeline_run`): contracts removed from the tracker, void
records, customers with usage but no contract. With more time: usage volume per day vs the
trailing average (catches a partial API publish), a large drop in a customer's spend, a
workbook whose total commitment changed by more than X% between snapshots.

## How I used AI tools

I used Claude as a pair programmer: to go through the brief and the data and list the traps,
to discuss the storage choice, to draft the code, manifests and these notes, and to write an
independent reference calculation in plain Python that I compared against the pipeline's
output. 
I reviewed and tested every part myself and can explain each decision.

While you will run in the Terminal: python scripts/grading_scenario.py
grading_scenario.py will print at the end “All steps passed”

The full summary of how I worked with it (the design discussion, the decisions and trade-offs, and my interview-prep notes) is available here: https://claude.ai/artifact/RzsaFKJHhMBRKs4iWkR34K


What's on the page now:

The two sources (the API and the Excel files), the three steps inside the pipeline pod, the tables in Postgres, and where the results go. The numbers in it come from your cluster.

The flow, step by step. 
Each step lists the file it lives in, what it does, and a real example from your data:
  1.The second run fetched 1,080 records and changed 0. That's your proof that a repeated run changes nothing.
  2.K-1009 was amended from $500K to $650K, and its end date was pushed out.
  3.K-1004 was removed from the Excel file but is still counted.
  4.C004 and C010 have spend on days with no active contract.
The results. A table of the 4 at-risk contracts.
The four detailed diagrams from before.

Points:
K-1001 has two versions only because the customer name changed (from "Acme Robotics" to "ACME Robotics Inc."). The history tracks every column, including name and status. This is a deliberate choice, and the page shows how to defend it.

The passwords in git are dev values. That's fine for a local assignment, but say up front that production would use Vault or External Secrets.


