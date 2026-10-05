-- Reconciliation summary by gateway status (rule A4).
--   SUCCESS          settled to merchants
--   FAILED           not settled; input to fraud telemetry
--   PENDING/TIMEOUT  UN-RECONCILED: customer may be debited but merchant not credited

SELECT
    gateway_status,
    CASE gateway_status
        WHEN 'SUCCESS' THEN 'SETTLED'
        WHEN 'FAILED'  THEN 'NOT_SETTLED'
        ELSE 'UNRECONCILED'
    END                       AS reconciliation_state,
    COUNT(*)                  AS txn_count,
    COUNT(DISTINCT user_id)   AS users,
    SUM(amount_inr)           AS amount_inr
FROM stg_transactions
GROUP BY gateway_status
ORDER BY txn_count DESC
