-- PayRecon schema (MySQL 8.0+). Idempotent: safe to run on every deploy.
-- This file NEVER drops or truncates anything.
--
--   merchant_rates     reference data: commission per merchant
--   stg_transactions   cleaned transactions; txn_ref_no PRIMARY KEY = no double counting
--   dlq_records        audit copy of every rejected row (original record + reason)
--   pipeline_runs      one row per pipeline run: counts in/out, status, error
--
-- Money is DECIMAL (exact), never FLOAT. All times are UTC.

CREATE TABLE IF NOT EXISTS merchant_rates (
    merchant_id     VARCHAR(32)   NOT NULL,
    tier            VARCHAR(16)   NULL,
    commission_pct  DECIMAL(7,6)  NOT NULL COMMENT 'fraction: 0.025 means 2.5 percent',
    updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (merchant_id),
    CONSTRAINT chk_rate_range CHECK (commission_pct >= 0 AND commission_pct < 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS stg_transactions (
    txn_ref_no            VARCHAR(64)    NOT NULL,
    user_id               VARCHAR(32)    NOT NULL,
    merchant_id           VARCHAR(32)    NOT NULL,
    amount_original       DECIMAL(18,2)  NOT NULL COMMENT 'amount as sent, in currency_original',
    currency_original     CHAR(3)        NOT NULL COMMENT 'ISO code after alias normalisation',
    fx_rate_to_inr        DECIMAL(12,4)  NOT NULL COMMENT 'fixed dictionary rate used',
    amount_inr            DECIMAL(18,2)  NOT NULL,
    gateway_status        VARCHAR(16)    NOT NULL,
    gateway_response_code VARCHAR(16)    NULL,
    created_at_utc        DATETIME       NOT NULL,
    created_at_raw        VARCHAR(64)    NOT NULL COMMENT 'original text, for audit',
    run_id                VARCHAR(40)    NOT NULL,
    loaded_at             DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (txn_ref_no),
    -- fraud query: per user, FAILED only, ordered by time  -> covering index
    KEY idx_fraud (gateway_status, user_id, created_at_utc),
    -- settlement query: per merchant, SUCCESS only
    KEY idx_settlement (gateway_status, merchant_id),
    KEY idx_run (run_id),
    CONSTRAINT chk_amount_positive CHECK (amount_original > 0 AND amount_inr > 0),
    CONSTRAINT chk_status CHECK (gateway_status IN ('SUCCESS', 'FAILED', 'PENDING', 'TIMEOUT'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dlq_records (
    dlq_id          BIGINT        NOT NULL AUTO_INCREMENT,
    run_id          VARCHAR(40)   NOT NULL,
    source_file     VARCHAR(255)  NOT NULL,
    source_row      INT           NOT NULL COMMENT 'line number in the original file',
    txn_ref_no      VARCHAR(64)   NULL,
    reason          VARCHAR(255)  NOT NULL COMMENT 'one or more codes joined by |, e.g. AMOUNT_NOT_POSITIVE|BAD_TIMESTAMP',
    raw_record      JSON          NOT NULL COMMENT 'original row exactly as received',
    rejected_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dlq_id),
    KEY idx_dlq_run (run_id),
    KEY idx_dlq_reason (reason)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          VARCHAR(40)   NOT NULL,
    source_file     VARCHAR(255)  NOT NULL,
    s3_key          VARCHAR(512)  NULL,
    rows_read       INT           NULL,
    rows_valid      INT           NULL,
    rows_rejected   INT           NULL,
    rows_duplicate  INT           NULL,
    rows_inserted   INT           NULL,
    status          VARCHAR(16)   NOT NULL DEFAULT 'RUNNING',
    error_message   TEXT          NULL,
    started_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     DATETIME      NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT chk_run_status CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
