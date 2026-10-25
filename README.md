# Orbital Compute: commitment utilization pipeline

Daily pipeline that loads usage from the billing API and contract terms from Finance's
commitments workbooks into Postgres, and publishes `contract_utilization_daily` and
`unattributed_usage_daily`. It runs as a Kubernetes CronJob on a local kind cluster.

The assignment brief is in [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md); design notes are in
[NOTES.md](NOTES.md).

## Running it

Requirements: Python 3.10+, Docker, kind, kubectl (no Python packages needed on the host).

```bash
python run.py up                    # cluster + images + billing API + Postgres + CronJob
kubectl -n orbital create job run-1 --from=cronjob/orbital-pipeline
kubectl -n orbital logs -f job/run-1
python run.py export out/           # out/contract_utilization_daily.csv, out/unattributed_usage_daily.csv
python run.py down
```

A full first run takes about 40 seconds (the API is slow and rate limited on purpose); later
runs take about 10 seconds.

To replay the reviewers' whole scenario (pod killed mid-run, incremental, repeat, concurrent
runs, full reload) and check that every export matches:

```bash
python scripts/grading_scenario.py
```

Useful queries (`kubectl -n orbital exec -it postgres-0 -- psql -U orbital`):

```sql
SELECT run_id, started_at, status, details->'model' FROM pipeline_run ORDER BY run_id;
SELECT * FROM contract_history ORDER BY contract_id, version;   -- SCD2 history
SELECT * FROM contract_latest_terms;                             -- what the model uses
```

## Layout

```
run.py                       up / export / down
k8s/                         ConfigMap + Secret, Postgres StatefulSet, CronJob
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
mock_api/, data/             as provided
```

## Running locally without Kubernetes

```bash
python mock_api/server.py --as-of 2026-09-20 &
pip install -r pipeline/requirements.txt
export PGHOST=localhost PGUSER=... PGPASSWORD=... PGDATABASE=...
export BILLING_API_URL=http://127.0.0.1:8765 BILLING_API_CLIENT_ID=candidate \
       BILLING_API_CLIENT_SECRET=orbital-s3cret COMMITMENTS_DIR="$PWD/data/commitments"
cd pipeline && python -m orbital_pipeline
python -m unittest discover -s tests -t .
```
