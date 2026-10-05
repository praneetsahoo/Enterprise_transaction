-- Merchant settlement (rule A3: only SUCCESS transactions are settled).
--   net payable = amount - (amount * commission_pct)
-- Commission is rounded to the paisa PER TRANSACTION (each payment is settled on its own),
-- then summed per merchant. LEFT JOIN: a merchant with no rate is still listed, flagged
-- RATE_MISSING, and gets NULL settlement (we do not pay out on a guessed rate).
-- Parameter  settlement_status  (default 'SUCCESS')

SELECT
    t.merchant_id,
    r.tier,
    r.commission_pct,
    COUNT(*)                                                           AS txn_count,
    SUM(t.amount_inr)                                                  AS gross_inr,
    SUM(ROUND(t.amount_inr * r.commission_pct, 2))                     AS commission_inr,
    SUM(t.amount_inr - ROUND(t.amount_inr * r.commission_pct, 2))      AS net_settlement_inr,
    CASE WHEN r.merchant_id IS NULL THEN 'RATE_MISSING' ELSE 'OK' END  AS settlement_flag
FROM stg_transactions AS t
LEFT JOIN merchant_rates AS r
       ON r.merchant_id = t.merchant_id
WHERE t.gateway_status = :settlement_status
GROUP BY t.merchant_id, r.merchant_id, r.tier, r.commission_pct
ORDER BY settlement_flag DESC, net_settlement_inr DESC
