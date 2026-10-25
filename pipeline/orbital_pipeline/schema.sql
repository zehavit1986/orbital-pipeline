-- Schema for the Orbital commitments pipeline. Every statement is idempotent, so it runs at the
-- start of every pipeline run.

-- ---------------------------------------------------------------------------------------------
-- Raw layer: what the sources told us, keyed so that reloading the same data is a no-op.
-- ---------------------------------------------------------------------------------------------

-- Latest known version of every usage record from the billing API (one row per record_id).
-- Restatements overwrite the row; void records are kept (status = 'void') and filtered later.
CREATE TABLE IF NOT EXISTS raw_usage (
    record_id    text           PRIMARY KEY,
    customer_id  text           NOT NULL,
    usage_date   date           NOT NULL,
    sku          text           NOT NULL,
    quantity     numeric        NOT NULL,
    unit         text           NOT NULL,
    amount_usd   numeric(18, 2) NOT NULL,
    status       text           NOT NULL,
    updated_at   timestamptz    NOT NULL,
    loaded_at    timestamptz    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS raw_usage_customer_date_idx ON raw_usage (customer_id, usage_date);

-- One row per contract per tracker snapshot, after cleaning. This table is the archive of every
-- snapshot we have ever seen, so history survives even if an old workbook is no longer around.
CREATE TABLE IF NOT EXISTS raw_commitment_snapshot (
    snapshot_date   date           NOT NULL,
    contract_id     text           NOT NULL,
    customer_id     text           NOT NULL,
    customer_name   text,
    start_date      date           NOT NULL,
    end_date        date           NOT NULL,
    commitment_usd  numeric(18, 2) NOT NULL,
    status          text,
    source_file     text           NOT NULL,
    source_sheet    text           NOT NULL,
    source_row      integer        NOT NULL,
    PRIMARY KEY (snapshot_date, contract_id)
);

-- ---------------------------------------------------------------------------------------------
-- History: SCD Type 2 of contract terms, rebuilt from raw_commitment_snapshot on every run.
-- valid_from / valid_to are snapshot dates (when the tracker showed these terms), not business
-- effective dates. valid_to IS NULL means "these are the terms in the latest snapshot".
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_history (
    contract_id     text           NOT NULL,
    version         integer        NOT NULL,
    customer_id     text           NOT NULL,
    customer_name   text,
    start_date      date           NOT NULL,
    end_date        date           NOT NULL,
    commitment_usd  numeric(18, 2) NOT NULL,
    status          text,
    valid_from      date           NOT NULL,
    valid_to        date,
    PRIMARY KEY (contract_id, version)
);

-- Latest known terms of every contract we have ever seen, including contracts Finance removed
-- from the tracker (removed_from_tracker = true). This is what the model uses.
CREATE OR REPLACE VIEW contract_latest_terms AS
SELECT DISTINCT ON (contract_id)
    contract_id,
    customer_id,
    customer_name,
    start_date,
    end_date,
    commitment_usd,
    status,
    valid_from,
    valid_to IS NOT NULL AS removed_from_tracker
FROM contract_history
ORDER BY contract_id, version DESC;

-- A customer's total non-void spend per day, across all SKUs.
CREATE OR REPLACE VIEW customer_spend_daily AS
SELECT
    customer_id,
    usage_date       AS date,
    SUM(amount_usd)  AS amount_usd
FROM raw_usage
WHERE status <> 'void'
GROUP BY customer_id, usage_date;

-- ---------------------------------------------------------------------------------------------
-- Published model (Part 2). Fully rebuilt and swapped in a single transaction on every run.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_utilization_daily (
    contract_id           text           NOT NULL,
    customer_id           text           NOT NULL,
    date                  date           NOT NULL,
    commitment_usd        numeric(18, 2) NOT NULL,
    term_days             integer        NOT NULL,
    days_elapsed          integer        NOT NULL,
    consumed_usd          numeric(18, 2) NOT NULL,
    consumed_to_date_usd  numeric(18, 2) NOT NULL,
    expected_to_date_usd  numeric(18, 6) NOT NULL,
    utilization_pct       numeric(18, 6) NOT NULL,
    projected_term_usd    numeric(18, 6) NOT NULL,
    at_risk               boolean        NOT NULL,
    PRIMARY KEY (contract_id, date)
);

CREATE TABLE IF NOT EXISTS unattributed_usage_daily (
    customer_id  text           NOT NULL,
    date         date           NOT NULL,
    amount_usd   numeric(18, 2) NOT NULL,
    PRIMARY KEY (customer_id, date)
);

-- ---------------------------------------------------------------------------------------------
-- Run log. A run that was killed stays in status 'running' forever, which is useful to see.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id       bigserial    PRIMARY KEY,
    started_at   timestamptz  NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text         NOT NULL DEFAULT 'running',
    hostname     text,
    details      jsonb
);
