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
| 3 AWS infrastructure | ⏳ |
| 4 Data layer (MySQL) | ⏳ |
| 5 Python preprocessing + DLQ + batch load | ⏳ |
| 6 SQL settlement + sliding-window fraud | ⏳ |
| 7 pytest suite | ⏳ |
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

## Business rules (hackathon assumptions — all in `app/config.py`)

| # | Rule |
|---|---|
| A1 | Timestamps without a time zone are IST (Asia/Kolkata); all are stored in UTC |
| A2 | `commission_pct` is a fraction (0.02 = 2%); values above 1 are read as a percentage |
| A3 | Only `SUCCESS` transactions are settled to merchants |
| A4 | Fraud counts `FAILED` transactions; `PENDING`/`TIMEOUT` are reported as un-reconciled |
| A5 | If a `txn_ref_no` repeats, the first record is kept; later copies are logged, not loaded |
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
