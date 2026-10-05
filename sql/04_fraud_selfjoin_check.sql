-- Independent cross-check of the fraud rule WITHOUT window functions (self-join).
-- For each failure a, count failures b of the same user with
--   a.created_at_utc - window_minutes  <=  b.created_at_utc  <=  a.created_at_utc
-- Must return exactly the same users as 03_fraud_sliding_window.sql.
-- Slower (compares pairs of rows) so it is used for verification, not reporting.

WITH failed AS (
    SELECT user_id, txn_ref_no, created_at_utc
    FROM stg_transactions
    WHERE gateway_status = :fraud_status
)
SELECT per_failure.user_id, MAX(cnt) AS peak_failures_in_window
FROM (
    SELECT a.user_id, a.txn_ref_no, COUNT(*) AS cnt
    FROM failed AS a
    JOIN failed AS b
      ON  b.user_id = a.user_id
      AND b.created_at_utc BETWEEN a.created_at_utc - INTERVAL :window_minutes MINUTE
                               AND a.created_at_utc
    GROUP BY a.user_id, a.txn_ref_no
) AS per_failure
WHERE cnt > :max_failures
GROUP BY per_failure.user_id
ORDER BY peak_failures_in_window DESC, per_failure.user_id
