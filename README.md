# PayRecon — Enterprise Transaction Reconciliation & Fraud Telemetry

Batch pipeline for UPI / merchant-settlement transaction dumps:

```text
RAW CSV → S3 → Python cleaning (UTC, INR, validation) → DLQ + clean data → RDS MySQL
        → SQL merchant settlement  +  SQL fraud telemetry (10-minute sliding window)
```

Built with **Python + SQL** on **AWS** (S3, RDS MySQL, EC2, VPC, Security Groups, IAM, CloudWatch).

## Project status

| Phase | Status |
|---|---|
| 0 Problem analysis | ✅ |
| 1 Architecture & technical design | ✅ |
| 2 Project foundation | ✅ |
| 3 AWS infrastructure | ✅ (8/8 live connectivity checks — see `infra/README.md`) |
| 4 Data layer (MySQL) | ✅ (16/16 tests on RDS MySQL 8.4) |
| 5 Python preprocessing + DLQ + batch load | ✅ (75 tests; live run on EC2 → S3 + RDS) |
| 6 SQL settlement + sliding-window fraud | ✅ (95/95 tests on RDS) |
| 7 pytest suite | ✅ (109 tests on EC2/RDS — map in `tests/README.md`) |
| 8 Streamlit dashboard (optional) | ⏳ |
| 9–14 Deploy, integration, failure tests, security, polish, demo | ⏳ |

## Project structure

```text
app/
  config.py         business rules (assumptions) + environment settings — one place
  preprocessing/    cleaning, validation, DLQ               (Phase 5)
  database/         MySQL connection + batch loader          (Phase 4–5)
  analytics/        runs the settlement / fraud SQL          (Phase 6)
  storage/          S3 upload/download                       (Phase 5)
data/
  raw/              input files (git-ignored; regenerate with the script below)
  processed/        cleaned output (git-ignored)
  dlq/              dead-letter queue: invalid rows + reasons (git-ignored)
sql/                schema and analytics SQL                  (Phase 4, 6)
scripts/
  generate_sample_data.py   deterministic messy sample data with planted fraud cases
tests/              pytest
```

## Cleaning pipeline (`app/pipeline.py`)

```bash
python scripts/generate_sample_data.py
python -m app.pipeline --no-db     # clean + DLQ files only
python -m app.pipeline             # + S3 copy (if S3_BUCKET set) + MySQL batch load
```

| Step | Rule | Rejection code (DLQ) |
|---|---|---|
| Read | every value read as text; required columns checked | whole run FAILED if a column is missing |
| IDs | `txn_ref_no`, `user_id`, `merchant_id` trimmed, required, length-checked | `MISSING_TXN_REF`, `MISSING_USER_ID`, `MISSING_MERCHANT_ID`, `*_TOO_LONG` |
| Amount | `1,250.50` accepted; must be a finite number, > 0, ≤ 100 crore | `AMOUNT_NOT_NUMERIC`, `AMOUNT_NOT_POSITIVE`, `AMOUNT_OUT_OF_RANGE` |
| Currency | `₹ / Rs / Rs. / inr` → `INR`, `$ / US$` → `USD` … fixed dictionary | `UNKNOWN_CURRENCY` |
| INR | `amount × FX_TO_INR`, rounded half-up to the paisa; original amount + rate kept | — |
| Timestamp | epoch s/ms, ISO `Z`/offset, zone-less `YYYY-MM-DD` / `DD/MM/YYYY` (= IST) → UTC | `BAD_TIMESTAMP` (incl. 31/02, years outside 2000–2100) |
| Status | case-insensitive; SUCCESS / FAILED / PENDING / TIMEOUT | `UNKNOWN_STATUS` |
| Duplicates | first valid copy of a `txn_ref_no` wins (A5) | `DUPLICATE_TXN_REF_EXACT`, `DUPLICATE_TXN_REF_CONFLICT` |
| Rates | `2.5` → `0.025`; negative / ≥ 100% / repeated merchant rejected | `BAD_COMMISSION`, `DUPLICATE_MERCHANT_ID` |

A row gets **every** reason that applies (joined by `|`). Rows read = valid + rejected + duplicate,
checked on every run. Rejected rows keep their original text in `data/dlq/<run_id>__<file>__rejected.csv`,
in S3 `dlq/<run_id>/`, and in the `dlq_records` table.

Sample run (seed 42): **2,053 read → 1,923 loaded, 100 rejected, 30 duplicates**. Running the
same file again inserts **0** rows.

## SQL analytics (`sql/02`–`05`, run by `python -m app.analytics.reports`)

| File | Answers |
|---|---|
| `02_settlement.sql` | Per merchant: SUCCESS gross, commission (`amount × commission_pct`, rounded per transaction), **net = amount − amount × commission_pct**. `LEFT JOIN` on `merchant_id`, so a merchant without a rate is shown as `RATE_MISSING` with no payout |
| `03_fraud_sliding_window.sql` | Users with **more than 5 FAILED in any 10-minute window**: `COUNT(*) OVER (PARTITION BY user_id ORDER BY created_at_utc RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW)`, with the peak window's start/end |
| `04_fraud_selfjoin_check.sql` | Same rule written as a self-join (no window functions); the report fails if the two disagree |
| `05_reconciliation_summary.sql` | Count and INR by status; PENDING / TIMEOUT = un-reconciled |

Thresholds come from `app/config.py`. Results go to `data/processed/reports/` and S3 `processed/reports/`.

Verified on the sample data: flagged **U9001** (6 in 8 min) and **U9004** (6 across a 10-minute clock
boundary); not flagged **U9002** (exactly 5) and **U9003** (6 over 50 min). For comparison, fixed
10-minute buckets would miss U9004, and a daily `GROUP BY` would wrongly flag U9003.

## Data model (`sql/01_schema.sql`)

| Table | Key | Purpose |
|---|---|---|
| `merchant_rates` | `merchant_id` | Commission per merchant (fraction, CHECK 0 ≤ rate < 1) |
| `stg_transactions` | **`txn_ref_no`** | Cleaned transactions. The primary key makes double counting impossible |
| `dlq_records` | `dlq_id` | Every rejected row: original record (JSON) + reason codes |
| `pipeline_runs` | `run_id` | One row per run: rows read / valid / rejected / duplicate / inserted, status |

* Money is `DECIMAL`, never `FLOAT`. Times are stored in UTC.
* `idx_fraud (gateway_status, user_id, created_at_utc)` covers the sliding-window fraud query.
* CHECK constraints (amount > 0, valid status) are a last line of defence behind the Python validation.
* The schema is `CREATE TABLE IF NOT EXISTS` only; `app/database/schema.py` refuses DROP / TRUNCATE / DELETE.

```bash
python -m app.database.schema     # create missing tables (safe to re-run)
```

## Business rules (hackathon assumptions — all in `app/config.py`)

| # | Rule |
|---|---|
| A1 | Timestamps without a time zone are IST (Asia/Kolkata); all are stored in UTC |
| A2 | `commission_pct` is a fraction (0.02 = 2%); values above 1 are read as a percentage |
| A3 | Only `SUCCESS` transactions are settled to merchants |
| A4 | Fraud counts `FAILED` transactions; `PENDING`/`TIMEOUT` are reported as un-reconciled |
| A5 | If a `txn_ref_no` repeats, the first record is kept; later copies are logged, not loaded |
| A6 | The 10-minute fraud window includes both ends (failures at 10:00 and 10:10 are in one window) |
| — | Currency is converted with a fixed dictionary (no external API) |

## Quick start (local)

```bash
pip install -r requirements-dev.txt
python scripts/generate_sample_data.py      # writes data/raw/*.csv (same files every time)
pytest
```

## Security

* No credentials in code or git. On AWS the app uses an EC2 IAM role; the database password is
  read from SSM Parameter Store at runtime.
* `.env` and all data files are git-ignored; only `.env.example` (no secrets) is committed.
