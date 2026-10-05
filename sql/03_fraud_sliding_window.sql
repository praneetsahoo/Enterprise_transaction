-- Fraud telemetry: users with MORE THAN max_failures FAILED transactions inside ANY
-- window_minutes-long window. A genuine sliding window, not a daily GROUP BY and not
-- fixed clock buckets.
--
-- How: for every failed transaction, count the same user's failures in the
-- window_minutes ending at that transaction (RANGE = by TIME, not by row count).
-- The busiest possible window always ends on some failure, so checking a window ending
-- at every failure checks every window. Both ends are inclusive (rule A6).
-- Rows with the same timestamp are counted together (RANGE treats them as peers).
--
-- Parameters  fraud_status ('FAILED'), window_minutes (10), max_failures (5)
-- Needs MySQL 8.0+ (RANGE frames with INTERVAL). Index idx_fraud covers this query.

WITH failed AS (
    SELECT user_id, txn_ref_no, created_at_utc
    FROM stg_transactions
    WHERE gateway_status = :fraud_status
),
windowed AS (
    SELECT
        user_id,
        txn_ref_no,
        created_at_utc                AS window_end,
        MIN(created_at_utc) OVER w    AS window_start,
        COUNT(*)            OVER w    AS failures_in_window
    FROM failed
    WINDOW w AS (
        PARTITION BY user_id
        ORDER BY created_at_utc
        RANGE BETWEEN INTERVAL :window_minutes MINUTE PRECEDING AND CURRENT ROW
    )
),
peak AS (
    SELECT
        windowed.*,
        ROW_NUMBER() OVER (PARTITION BY user_id
                           ORDER BY failures_in_window DESC, window_end) AS rn
    FROM windowed
    WHERE failures_in_window > :max_failures
)
SELECT
    p.user_id,
    p.failures_in_window                                AS peak_failures_in_window,
    p.window_start,
    p.window_end,
    TIMESTAMPDIFF(SECOND, p.window_start, p.window_end) AS window_span_seconds,
    (SELECT COUNT(*) FROM failed f WHERE f.user_id = p.user_id) AS total_failures
FROM peak AS p
WHERE p.rn = 1
ORDER BY peak_failures_in_window DESC, p.user_id
