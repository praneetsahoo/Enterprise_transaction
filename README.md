# PayRecon — Enterprise Transaction Reconciliation & Fraud Telemetry

Batch pipeline **and Streamlit dashboard** for UPI / merchant-settlement transaction dumps.
Upload a raw dump in the dashboard and see the cleaned result, rejected rows, merchant
settlement and fraud alerts:

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
| 8 Streamlit dashboard — the working product | ✅ (6 tabs, upload & run, AppTest) |
| 9 Deploy on EC2 | ✅ (systemd + CloudWatch agent; alarm fired on a real ERROR; survives reboot) |
| 10 End-to-end integration | ✅ (14/14 on AWS for 2 fresh batches + a user upload via the public dashboard) |
| 11 Failure tests | ✅ (17 failure tests + 8 live drills on AWS; found and fixed 6 gaps) |
| 12 Security review | ✅ (see `SECURITY.md`) |
| 13–14 Polish, demo | ⏳ |

## Project structure

```text
app/
  dashboard.py      Streamlit UI over the SQL results          (Phase 8)
  pipeline.py       command-line pipeline runner              (Phase 5)
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

## Dashboard — the working product (`app/dashboard.py`)

```bash
streamlit run app/dashboard.py
```

| Tab | Shows |
|---|---|
| Overview | loaded / settled / un-reconciled INR, fraud alert count, why rows were rejected |
| Upload & run | upload a transactions CSV (rates optional) and run the same pipeline as the CLI |
| Settlement | net payable per merchant, merchants with no rate held back, CSV download |
| Fraud alerts | flagged users, the peak 10-minute window as evidence, fixed-bucket lines for comparison, what-if thresholds |
| Dead-letter queue | rejected rows per run with the original values and reasons, CSV download |
| Pipeline runs | audit trail of every run, including failures and their error |

The dashboard contains no business logic: it only displays the SQL in `sql/` and calls `app/pipeline.py`.

## Deployment (EC2)

```text
http://<EC2 public IP>/          dashboard (login; password in SSM /payrecon/dashboard/password)
```

| Piece | Where |
|---|---|
| Code + virtualenv | `/opt/payrecon/app`, `/opt/payrecon/venv` |
| Settings (no secrets) | `/etc/payrecon/payrecon.env` (root:payrecon, 0640) |
| Dashboard service | `payrecon-dashboard.service` — user `payrecon` (no shell), port 80 via `CAP_NET_BIND_SERVICE`, restarts on failure, starts at boot |
| Logs | `/var/log/payrecon/pipeline.log` → CloudWatch `/payrecon/pipeline` (metric filter `ERROR` → alarm `payrecon-pipeline-errors`); `dashboard.log` → `/payrecon/dashboard` |

Runbook (all via SSM Run Command — no SSH):

```bash
# deploy / update after a git push (idempotent)
DB_HOST=<rds endpoint> S3_BUCKET=<bucket> bash /opt/payrecon/app/infra/deploy.sh
# run the pipeline + refresh reports on the server
/opt/payrecon/app/infra/run_pipeline.sh                                   # sample files
/opt/payrecon/app/infra/run_pipeline.sh --s3-transactions raw/<run>/file.csv
# status / logs
systemctl status payrecon-dashboard ; tail -f /var/log/payrecon/pipeline.log
```

## End-to-end check (`scripts/e2e_check.py`)

Runs a FRESH batch through the real deployed path, nothing mocked:
S3 landing → pipeline reads from S3 → clean + DLQ → RDS → audit row → S3 copies → SQL reports →
dashboard → same file again (must insert 0). Expected numbers come from what the generator planted,
and 25 loaded rows are re-derived independently from the raw file.

```bash
sudo runuser -u payrecon -- env $(cat /etc/payrecon/payrecon.env | xargs) HOME=/var/lib/payrecon \
    /opt/payrecon/venv/bin/python scripts/e2e_check.py --batch E      # any letter not used yet
```

Verified on AWS: batches C and D 14/14 each; batch B uploaded by a person through the public
dashboard matched the predicted counts exactly; all runs found in CloudWatch; database total
3,513 rows = sum of `rows_inserted` over all successful runs; window and self-join fraud queries
agree on 8 users (2 planted per batch).

## How it fails (Phase 11)

A run is loaded **completely or not at all** (one database transaction), every failure is logged as
`ERROR` (→ CloudWatch alarm), and when the database is reachable the run is recorded as `FAILED`
with its error in `pipeline_runs`.

| Failure | What happens | Proven by |
|---|---|---|
| One line with too many / too few fields | only that line → DLQ `MALFORMED_ROW` (exact line kept); rest of file loads | test + drill D7 |
| Binary / empty / oversized (> 200 MB) / wrong file | clear message, run FAILED, nothing loaded | tests + drill D6 |
| Database down, wrong host, wrong password | fails before loading, `ERROR … FAILED before start`, exit 1 | tests + drills D1–D3 |
| Reading another project's secret | IAM `AccessDenied` (explicit deny outside `/payrecon/*`) | drill D4 |
| Writing to another bucket / S3 error | run FAILED before any database write | test + drill D5 |
| Input missing in S3 | `ERROR input download from S3 FAILED`, exit 1 | test + drill D8 |
| Crash in the middle of the load | transaction rolled back: 0 rows from that run; retry loads everything once | drill test (throwaway DB) |
| Same file loaded twice at the same time | one run inserts, the other inserts 0 — never double counted | drill test (throwaway DB) |
| Error while recording the failure | the original error is still raised and logged | test |

```bash
sudo bash /opt/payrecon/app/infra/failure_drills.sh     # on EC2: 8 drills, staging row count must not change
SCRATCH_DB_URL=mysql+pymysql://root@127.0.0.1:3307/payrecon_drill pytest tests/test_failures.py   # local MySQL 8
```

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
