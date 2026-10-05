# PayRecon — Interview Cheat Sheet

## 30-second explanation
"It takes a messy payment transaction file, cleans and validates every row, and loads the good rows into a
MySQL database without ever counting a payment twice. Bad rows are kept aside with the reason. From the clean
data it shows how much each merchant should be paid after commission, and which users look fraudulent —
more than 5 failed payments in any 10 minutes. Analysts use it through a Streamlit dashboard on AWS."

## 1-minute architecture
"The user uploads a CSV on the dashboard running on EC2. We copy the original to S3 as proof. Python reads
every row as text and checks 10 rules; bad rows go to a dead-letter queue with reasons, good rows are
converted to UTC and INR. Everything is saved to RDS MySQL in one transaction, with the transaction ID as
primary key so re-uploads add nothing. SQL computes settlement and a true 10-minute sliding-window fraud
check, cross-checked by a second query. The database is private, passwords live in SSM, EC2 uses an IAM
role, and errors raise a CloudWatch alarm."

## Pipeline
Upload CSV → S3 raw copy → Python validate + clean → DLQ (bad) / clean rows → S3 processed copy
→ MySQL (one transaction, PK = txn_ref_no) → SQL settlement + fraud → Streamlit dashboard
Logs → CloudWatch → alarm on ERROR

## Diagram
```
User ──HTTP:80 + password──► EC2 (Streamlit + Python pipeline)
                               │  IAM role
          S3 (raw/processed/dlq) ◄─┤  SSM (passwords)
                               │  TLS 3306, SG→SG only
                     RDS MySQL 8.4 (private subnet)
          CloudWatch logs + ERROR alarm ◄─ EC2
```

## AWS services
| Service | Job in our project |
|---|---|
| S3 | keeps original, cleaned and rejected files (proof + audit) |
| RDS MySQL | clean data; SQL for joins, sums, time windows; PK stops duplicates |
| EC2 | always-on dashboard + pipeline |
| VPC / subnets / SGs | DB private; only the app server can reach it |
| IAM role | permissions without keys; only our own bucket, secrets, logs |
| SSM Parameter Store | encrypted passwords |
| CloudWatch | logs + alarm on any ERROR |
| NOT used: Lambda | dashboard must be always on; would add parts |
| NOT used: Glue | data is MBs; Glue is for GBs and slow to start |

## Tables
stg_transactions (PK txn_ref_no) · merchant_rates (PK merchant_id) · dlq_records (bad rows + reasons) ·
pipeline_runs (one row per run: counts, status, error). Money = DECIMAL.

## Python components
cleaning.py (read as text, 10 rules, UTC, INR, duplicates) · pipeline.py (runs steps, one transaction,
FAILED/SUCCESS) · loader.py (batch inserts) · s3.py · dashboard.py

## SQL
- Settlement: SUCCESS rows LEFT JOIN rates → amount − amount × commission (missing rate shown, not dropped)
- Fraud: COUNT(*) OVER (PARTITION BY user ORDER BY time RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING
  AND CURRENT ROW) > 5 — a look-back from every failure = true sliding window
- Cross-check: same rule as a self-join; both must agree

## Data quality
Missing ID / user / merchant · amount not a number or ≤ 0 or too big · unknown currency · bad date ·
unknown status · malformed line · duplicate ID · bad commission → DLQ with ALL reasons. Every run checks
read = good + bad + duplicate. MySQL CHECK constraints as last line of defence.

## Security
Private DB, TLS required, limited DB user, IAM role (no keys), deny on other projects' secrets, passwords in
SSM, no SSH, non-root service, dashboard password. Gaps: HTTP not HTTPS, one shared password, no alert email.

## Failures
Bad row → DLQ, rest loads · bad file → FAILED, nothing saved · crash mid-load → rollback · DB down →
fails before saving + alarm · re-upload → 0 inserted · website crash/reboot → auto-restart.
No automatic retry: just re-run (safe).

## Observability
Pipeline runs tab (status, counts, error) → CloudWatch logs → alarm. First check: Pipeline runs tab, then logs.

---

## Mentor questions — short answers

**1. What does it do / who uses it?** See 30-second explanation. Users: payments ops / reconciliation analysts.

**2. Business value?** Merchants are paid the right amount, no payment counted twice, bad data is visible
instead of lost, and fraud bursts are caught that daily checks would miss.

**3. Walk me through the architecture.** See 1-minute architecture.

**4. Walk me through one record.** Upload → S3 copy → read as text → rules pass → £67.40 → ₹7,077, time
→ UTC, status → SUCCESS → saved with its ID as key → counted in merchant M038's settlement → shown on dashboard.

**5. Why S3 if you have a database?** S3 keeps original files cheaply as proof; the database holds
cleaned rows we can query.

**6. Why RDS, not DynamoDB?** We need joins, sums, time windows and transactions — that's SQL.
DynamoDB is for fast key lookups.

**7. Why EC2, not Lambda?** The dashboard must be always on; Lambda runs short jobs on demand.

**8. Why not Glue?** Our files are megabytes; Glue is built for gigabytes and takes ~1 min to start.
We'd use it at large scale.

**9. How is data validated?** 10 rules per row in Python; bad rows go to the DLQ with every reason.

**10. How are duplicates handled?** First valid copy wins; later copies go to the DLQ. The primary key
blocks duplicates across files too — re-upload adds 0.

**11. Explain the fraud SQL.** For every failed payment, count that user's failures in the 10 minutes
before it; if more than 5, flag. Fixed buckets would miss bursts crossing a clock line.

**12. Explain settlement.** amount − amount × commission for successful payments; LEFT JOIN so a shop
without a rate is shown and held, not dropped.

**13. How is the database protected?** Private subnet, only the app's security group can connect, TLS
required, limited user, encrypted.

**14. Where are credentials?** Nowhere in code. Passwords in SSM, EC2 uses an IAM role.

**15. What if the DB goes down?** Run fails before saving anything, logs ERROR, alarm fires; re-run later.

**16. What if a file is malformed?** Whole-file problem → run FAILED, nothing saved. One bad line → only
that line to DLQ.

**17. What if it crashes mid-load?** One transaction → everything rolls back; re-run is safe.

**18. What at 10× volume?** Fine. At 100–1000×: vectorised/chunked processing, S3-triggered Lambda + Glue,
bulk loads, bigger RDS.

**19. Batch or streaming?** Batch — the input is a daily dump file.

**20. Did you use AI?** Yes, to write code faster. We designed it, tested with planted errors, two SQL
versions and 136 tests, and fixed bugs the tests found.

**21. Is it over-engineered?** Each service maps to a need; we deliberately left out Lambda and Glue.

**22. Production-ready?** A solid MVP. For production: HTTPS, per-user login, alert emails, Multi-AZ DB,
automatic S3 trigger.

## 15 decisions (one line each)
PK on txn_ref_no · DLQ with reasons · fixed FX table · true sliding window · LEFT JOIN settlement ·
read CSV as text · one transaction per run · Python rows / SQL maths · EC2 not Lambda · no Glue ·
RDS not DynamoDB · private DB + IAM + SSM · Streamlit · assumptions written down · HTTP + one password (MVP)

## 10 mistakes to avoid
Saying we use Lambda · fraud "by day" · "we delete bad rows" · "S3 triggers the pipeline" · "machine
learning" · "secured by a password" only · "production-ready" · "retries automatically" · "live FX rates" ·
"AI built it".

## 10 things to remember
1 purpose · 2 flow · 3 PK = no double count · 4 DLQ keeps bad rows · 5 sliding window · 6 settlement
formula + LEFT JOIN · 7 each service's job · 8 why no Lambda/Glue · 9 all-or-nothing loads · 10 tested
with known answers.
