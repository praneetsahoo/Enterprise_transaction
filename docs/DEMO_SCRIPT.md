# PayRecon — 4-minute demo script

**Verified against the live dashboard on 5 Oct 2026 (numbers grow if new files are loaded).**

**Before you start (1 min, off-camera):** open http://3.107.194.103 and sign in. In a second browser tab keep
the GitHub repo open. Have these files on your desktop: `payments_batch_2.csv` (already loaded) and
`merchant_rates_batch_2.csv` (a rates file, used to show a wrong-file upload).

---

### 0:00 — The problem (20 s) · *Overview tab*
> "A payment gateway gives us a raw, messy transaction dump. We must clean it, settle every merchant after
> commission, and detect users with more than 5 failed payments in **any** 10-minute window. Everything you
> see runs on AWS: S3, EC2, a private RDS MySQL, IAM and CloudWatch."

Point at the top row: **3,513 transactions loaded · ₹7,231,420.42 settled · ₹708,347.08 un-reconciled
(PENDING/TIMEOUT) · 8 fraud alerts** (≈ ₹72.3 lakh / ₹7.1 lakh).

### 0:20 — How a file flows (30 s) · *Overview chart*
> "Each row is validated in Python: timestamps to UTC, ₹/Rs./$ normalised and converted to INR with a fixed
> rate table, amounts ≤ 0 and missing transaction IDs rejected. Rejected rows are never dropped — they go to a
> dead-letter queue with the reason. Clean rows are batch-loaded into MySQL with `txn_ref_no` as the primary key."

Point at **"Why rows were rejected"** — every reason code is a rule.

### 0:50 — Live upload: no double counting (40 s) · *Upload & run*
Upload **payments_batch_2.csv** → **Run pipeline**.
> "This file was already loaded. Watch: 835 read, 784 valid, 41 rejected, 10 duplicates — and **0 inserted**.
> The primary key makes double counting impossible, even if someone uploads the same file twice."

### 1:30 — Live upload: wrong file fails safely (25 s) · *Upload & run*
Upload **merchant_rates_batch_2.csv** in the *Transactions* box → **Run pipeline**.
> "Wrong file. It fails with a clear reason — missing columns — and **nothing is loaded**. The whole load is
> one database transaction, so a run is loaded completely or not at all. This ERROR also raises a CloudWatch alarm."

### 1:55 — Fraud: a genuine sliding window (60 s) · *Fraud alerts*  ← strongest moment
> "The rule is more than 5 failures in **any** 10 minutes — not per day, not fixed clock buckets. In SQL we use
> `COUNT(*) OVER (PARTITION BY user ORDER BY time RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW)`."

Select **U9004** in *Show the evidence*.
> "U9004 failed 3 times before 05:10 and 3 times after — the dashed line is a clock bucket boundary. A
> bucket-based query sees 3 + 3 and misses it; a daily GROUP BY would also wrongly flag a user with 6 failures
> spread over 50 minutes. Ours flags exactly the planted cases: 6-in-10 flagged, exactly 5 not, spread-out not.
> A second query written as a self-join must agree on every run, or the report fails."

### 2:55 — Settlement (25 s) · *Settlement*
> "Net payable = amount − amount × commission, joined on merchant_id, successful payments only, commission
> rounded per transaction. Gross ₹7,231,420.42, commission ₹141,311.44, **net payable ₹7,090,108.98** (≈ ₹70.9 lakh)
> across 41 merchants.
> A merchant with no rate would be flagged and held, not silently dropped — that's why it's a LEFT JOIN."

### 3:20 — Audit trail (25 s) · *Dead-letter queue*, then *Pipeline runs*
> "Every rejected row with its exact original values and reason — downloadable for the data owner. And every
> run is recorded, including the failed one we just did, with its error."

### 3:45 — Close (15 s)
> "Python + SQL on AWS: private encrypted database with TLS only, least-privilege IAM, no keys in code,
> monitored by CloudWatch. 136 automated tests, an end-to-end check on AWS, and failure drills. Happy to take questions."

---

## If something goes wrong
| Problem | Do this |
|---|---|
| Site doesn't load | Show screenshots from the README / GitHub; explain from the SQL files in `sql/` |
| Login fails | Password is in SSM `/payrecon/dashboard/password` (AWS console → Systems Manager → Parameter Store) |
| Upload is slow | Talk through the flow diagram while it runs (takes ~3 s normally) |
| A number differs | Numbers grow when new files are loaded — the *rules* matter, not the totals |
