# PayRecon — mentor Q&A (short, confident answers)

## Problem & design
**Q: Explain your project in 30 seconds.**
A raw payment dump is cleaned in Python (UTC, INR, validation), bad rows go to a dead-letter queue with reasons, clean rows are batch-loaded into MySQL with `txn_ref_no` as primary key, and SQL computes merchant settlement and fraud alerts over a genuine 10-minute sliding window. It runs on AWS and the Streamlit dashboard is the working product.

**Q: Why Python for cleaning and SQL for analytics?**
Cleaning is row-by-row rules with reasons — natural in Python and unit-testable. Settlement and windows are set operations over all rows — what SQL is built for, and readable by an analyst.

**Q: What assumptions did you make?**
A1 no time zone = IST · A2 commission > 1 means a percentage · A3 only SUCCESS is settled · A4 fraud counts FAILED only · A5 first copy of a duplicate ID wins · A6 both ends of the 10-minute window count. All in `app/config.py`, one line to change each.

## Data cleaning
**Q: How do you handle different timestamp formats?**
Six formats: ISO with `Z`, ISO with offset, epoch seconds, epoch milliseconds, `YYYY-MM-DD` and `DD/MM/YYYY`. Zone-less times are IST and converted to UTC. Impossible dates like 31/02 are rejected, never guessed.

**Q: Why read everything as text?**
pandas type-guessing damages data: response code `007` becomes `7`, epoch becomes `1.79e9`. Reading as text keeps the original exact for the DLQ.

**Q: How do you convert currency?**
An alias map (`₹, Rs, Rs.` → INR; `$, US$` → USD…) then a fixed rate table, as the brief demands — no API, so results are deterministic. `Decimal`, rounded half-up to the paisa.

**Q: What goes to the DLQ?**
Every rejected row with its original values and **all** reasons (e.g. `MISSING_TXN_REF|AMOUNT_NOT_POSITIVE`), plus duplicates and malformed lines. Kept in a directory (brief), S3 (audit) and a table (dashboard).

**Q: How do you know you didn't lose rows?**
Every run checks `read = valid + rejected + duplicate` and records it in `pipeline_runs`. On the sample, 100 rejected = exactly the 100 errors we planted.

**Q: How do you handle duplicates?**
First valid copy wins; later copies go to the DLQ as EXACT or CONFLICT (same ID, different status — flagged for review). Across files, the primary key blocks re-inserts.

## Database & SQL
**Q: How do you prevent double counting?**
`txn_ref_no` is the primary key, inserted with `ON DUPLICATE KEY UPDATE` as a no-op. Uploading the same file again inserts 0 — I can show it live.

**Q: Why not `INSERT IGNORE`?**
It also silently ignores other errors like constraint violations. Ours only skips duplicate keys.

**Q: Why batch inserts, and what if it fails halfway?**
1,000 rows per round trip for speed, but all batches of a run are in **one transaction** — a crash rolls everything back, the run is marked FAILED, and a retry is safe.

**Q: Why DECIMAL, not FLOAT?**
Floats can't represent 0.1 exactly; money must reconcile to the paisa.

**Q: Explain the fraud query.**
For every failed transaction, count that user's failures in the 10 minutes ending at it: `COUNT(*) OVER (PARTITION BY user_id ORDER BY created_at_utc RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW)`. If any count is > 5, flag. The busiest window always ends at some failure, so this checks every possible window.

**Q: Why RANGE, not ROWS?**
`ROWS 5 PRECEDING` counts the last 6 rows regardless of time — it would flag 6 failures spread over 50 minutes. `RANGE` is by time.

**Q: Why not GROUP BY day or 10-minute buckets?**
Buckets split bursts: 3 failures before 05:10 and 3 after are missed (our user U9004). A daily GROUP BY flags slow failures as fraud (U9003). Both are wrong; the dashboard shows it.

**Q: How do you know your SQL is correct?**
The brief's three cases are tests; a second query written as a self-join must return the same users on every run; and on random data both match a brute-force Python count.

**Q: Settlement formula and why LEFT JOIN?**
`amount − amount × commission_pct` for SUCCESS rows, commission rounded per transaction. LEFT JOIN so a merchant without a rate is shown as RATE_MISSING and held — an INNER JOIN would silently drop its money.

**Q: Why MySQL 8?**
`RANGE … INTERVAL` window frames and enforced CHECK constraints need MySQL 8. MariaDB/5.7 can't run our fraud query.

## AWS & deployment
**Q: Walk me through your AWS architecture.**
VPC with RDS in private subnets (no internet route), EC2 in a public subnet with only port 80 open, S3 for raw/processed/DLQ/reports, IAM role for credentials, SSM Parameter Store for passwords, CloudWatch for logs and an ERROR alarm.

**Q: How does EC2 talk to RDS securely?**
Security group to security group on 3306 only, TLS required for every DB user, password fetched from SSM by the IAM role at runtime.

**Q: Where are your credentials?**
Nowhere in code or git. EC2 uses an IAM role (temporary credentials); passwords are AWS-generated SecureStrings in SSM. We scanned git history and logs for the real values: zero hits.

**Q: How do you deploy?**
`git push`, then `infra/deploy.sh` via SSM Run Command — idempotent, installs the service and log agent, health-checks. No SSH at all.

**Q: How would you know if the pipeline failed at night?**
Every failure is logged as ERROR → CloudWatch metric filter → alarm. Proven: the alarm fired on our drills. Next step would be an SNS email.

**Q: Why not Lambda / Glue / Airflow?**
The brief asks for S3, RDS, EC2; a batch job of this size runs in seconds. Fewer moving parts to secure and explain. Glue/Step Functions would come in at much larger volumes.

**Q: How would this scale to millions of rows?**
Vectorise the cleaning with pandas, stream the file in chunks, load with `LOAD DATA` or larger batches, partition the staging table by date. The fraud query is one indexed pass (`idx_fraud` covers it).

## Security
**Q: Least privilege — prove it.**
The role can only write `raw/ processed/ dlq/` in its own bucket and read `/payrecon/*` parameters. Our drills showed writes to another bucket and reads of another project's secret are denied — and that drill is how we found the AWS-managed SSM policy allowed too much, so we added an explicit deny.

**Q: Is the dashboard secure?**
Password from SSM, constant-time comparison, XSRF protection, 50 MB upload limit, runs as a non-root user. Known gap: HTTP not HTTPS — production fix is a load balancer with an ACM certificate.

**Q: What about SQL injection?**
Every value is a bound parameter; a test scans the code to enforce it.

## Testing
**Q: How did you test?**
136 automated tests: every cleaning rule, live MySQL 8 tests that roll back, S3 with moto, dashboard with Streamlit AppTest, failure and security tests. Plus an end-to-end check on AWS (14/14) and 8 failure drills on the server.

**Q: How do you know tests don't touch real data?**
Live tests run inside a rolled-back transaction, a guard fails the suite if tests write into the data folders, and tests can't write to the production log.

**Q: Did you use AI? How did you validate it?**
Yes, openly. We validated by testing against planted data with known answers, cross-checking SQL two ways, and reviewing failures — tests caught real bugs in generated code (a no-op assertion, a pandas 3 type change, a logging gap).

## Curveballs
**Q: What would you improve with more time?**
HTTPS, per-user login, SNS alerts, a scheduled S3-triggered run, and confirming the assumptions (A1–A6) with the business.

**Q: What was the hardest bug?**
Our monitoring caught our own tests writing fake ERRORs into the production log — fixed so tests can never log there.

**Q: What if the business says the window should be 15 minutes and 3 failures?**
Change two constants in `app/config.py` — the SQL takes them as parameters. The dashboard's what-if sliders show the effect live.
