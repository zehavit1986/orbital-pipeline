-- Part 2 model. Runs inside one transaction (see model.py): both tables are emptied and
-- rebuilt from the raw layer, so readers see either the old or the new version, never a mix.

DELETE FROM contract_utilization_daily;

INSERT INTO contract_utilization_daily
WITH last_loaded AS (
    SELECT MAX(usage_date) AS last_usage_date FROM raw_usage
),

-- One row per contract per day, from start_date to min(end_date, last usage date loaded),
-- using each contract's latest known terms. Built from a date spine so that days without
-- usage still get a row (with consumed_usd = 0).
contract_days AS (
    SELECT
        c.contract_id,
        c.customer_id,
        c.commitment_usd,
        c.start_date,
        (c.end_date - c.start_date + 1)  AS term_days,
        spine.day::date                  AS date
    FROM contract_latest_terms AS c
    CROSS JOIN last_loaded
    CROSS JOIN LATERAL generate_series(
        c.start_date,
        LEAST(c.end_date, last_loaded.last_usage_date),
        interval '1 day'
    ) AS spine(day)
),

daily AS (
    SELECT
        d.contract_id,
        d.customer_id,
        d.date,
        d.commitment_usd,
        d.term_days,
        (d.date - d.start_date + 1)     AS days_elapsed,
        COALESCE(s.amount_usd, 0)       AS consumed_usd
    FROM contract_days AS d
    LEFT JOIN customer_spend_daily AS s
           ON s.customer_id = d.customer_id
          AND s.date = d.date
),

cumulative AS (
    SELECT
        daily.*,
        SUM(consumed_usd) OVER (PARTITION BY contract_id ORDER BY date)  AS consumed_to_date_usd,
        commitment_usd * days_elapsed / term_days                        AS expected_to_date_usd
    FROM daily
)

SELECT
    contract_id,
    customer_id,
    date,
    commitment_usd,
    term_days,
    days_elapsed,
    consumed_usd,
    consumed_to_date_usd,
    expected_to_date_usd,
    consumed_to_date_usd / expected_to_date_usd                   AS utilization_pct,
    consumed_to_date_usd / days_elapsed * term_days               AS projected_term_usd,
    days_elapsed >= 30
        AND consumed_to_date_usd / days_elapsed * term_days < 0.9 * commitment_usd  AS at_risk
FROM cumulative;


DELETE FROM unattributed_usage_daily;

-- Spend on days when the customer had no active contract (on-demand usage).
INSERT INTO unattributed_usage_daily (customer_id, date, amount_usd)
SELECT s.customer_id, s.date, s.amount_usd
FROM customer_spend_daily AS s
WHERE NOT EXISTS (
    SELECT 1
    FROM contract_latest_terms AS c
    WHERE c.customer_id = s.customer_id
      AND s.date BETWEEN c.start_date AND c.end_date   -- end_date is inclusive
);
