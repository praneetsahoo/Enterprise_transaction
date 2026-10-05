"""End-to-end check of the deployed system with a FRESH batch (Phase 10). Run on EC2:

    sudo runuser -u payrecon -- env $(cat /etc/payrecon/payrecon.env | xargs) HOME=/var/lib/payrecon \\
        /opt/payrecon/venv/bin/python scripts/e2e_check.py --batch C

Path tested (the real production path, nothing mocked):
  generate batch -> S3 raw/inbox/ -> pipeline downloads from S3 -> clean + DLQ -> RDS (batch insert)
  -> pipeline_runs audit -> S3 raw/processed/dlq copies -> SQL reports -> S3 reports -> dashboard
  -> re-run same file (must insert 0)

Expected numbers come from what the GENERATOR planted (its manifest), not from the pipeline's
own code, and 25 loaded rows are re-derived by hand from the raw file. Prints PASS/FAIL per
check; exit code 1 if anything fails. CloudWatch is checked from outside (the instance role
may write logs but not read them).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import AWS_REGION, S3_BUCKET  # noqa: E402
from scripts.generate_sample_data import COLUMNS, generate, write_csv  # noqa: E402

results: list[tuple[bool, str]] = []

# Deliberately written separately from app/preprocessing (an independent re-derivation).
FX = {"INR": "1", "USD": "83", "EUR": "90", "GBP": "105", "AED": "22.6", "SGD": "61.5"}
SPELLING = {"INR": "INR", "RS": "INR", "RS.": "INR", "₹": "INR", "USD": "USD", "$": "USD", "US$": "USD",
            "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP", "AED": "AED", "SGD": "SGD"}


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}", flush=True)


def make_batch(tag: str, rows: int, seed: int, folder: Path) -> tuple[Path, dict, dict]:
    """Fresh batch: unique txn ids (UPI26<tag>...) and unique planted fraud users (U9201..U9204 for batch C)."""
    data, _, manifest = generate(rows, seed)
    n = 90 + ord(tag.upper()) - ord("A")                               # B -> 91, C -> 92, ...
    users = {f"U900{i}": f"U{n}0{i}" for i in range(1, 5)}
    for r in data:
        if r["txn_ref_no"].strip():
            r["txn_ref_no"] = r["txn_ref_no"].replace("TXN", f"UPI26{tag.upper()}", 1)
        r["user_id"] = users.get(r["user_id"], r["user_id"])
    path = folder / f"payments_batch_{tag.lower()}.csv"
    write_csv(path, data, COLUMNS)
    return path, manifest, users


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="C", help="letter used in txn ids / fraud user ids")
    parser.add_argument("--rows", type=int, default=600)
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()
    if not S3_BUCKET:
        print("S3_BUCKET not set")
        return 1

    from app.analytics.reports import run_all
    from app.database.connection import get_engine
    from app.pipeline import run as run_pipeline
    from app.storage.s3 import download

    engine = get_engine()
    s3 = boto3.client("s3", region_name=AWS_REGION)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        local, manifest, users = make_batch(args.batch, args.rows, args.seed, tmp)
        exp_rejected = sum(manifest["planted_invalid"].values())
        exp_dup = manifest["exact_duplicates"] + manifest["duplicates_with_changed_status"]
        exp_valid = manifest["total_rows"] - exp_rejected - exp_dup
        print(f"batch {args.batch}: {manifest['total_rows']} rows planted -> expect valid {exp_valid}, "
              f"rejected {exp_rejected}, duplicate {exp_dup}")

        with engine.connect() as c:
            already = c.execute(text("SELECT COUNT(*) FROM stg_transactions WHERE txn_ref_no LIKE :p"),
                                {"p": f"UPI26{args.batch.upper()}%"}).scalar()
        if already:
            print(f"batch {args.batch} is already loaded ({already} rows) — pick another --batch letter")
            return 1

        # 1. land the raw file in S3 (as an upstream system would)
        inbox_key = f"raw/inbox/{stamp}/{local.name}"
        s3.upload_file(str(local), S3_BUCKET, inbox_key, ExtraArgs={"ServerSideEncryption": "AES256"})
        check(s3.head_object(Bucket=S3_BUCKET, Key=inbox_key)["ContentLength"] == local.stat().st_size,
              f"S3 landing: s3://{S3_BUCKET}/{inbox_key}")

        # 2. pipeline reads it FROM S3 (rates: keep those already loaded)
        from_s3 = download(S3_BUCKET, inbox_key, tmp / "from_s3")
        s = run_pipeline(from_s3, None, processed_dir=tmp / "processed", dlq_dir=tmp / "dlq")
        run_id = s["run_id"]
        print(f"run_id {run_id}")
        check((s["read"], s["valid"], s["rejected"], s["duplicate"]) ==
              (manifest["total_rows"], exp_valid, exp_rejected, exp_dup),
              f"cleaning matches what was planted: read {s['read']}, valid {s['valid']}, "
              f"rejected {s['rejected']}, duplicate {s['duplicate']}")
        check(s["inserted"] == exp_valid, f"RDS: inserted {s['inserted']} new rows (expected {exp_valid})")

        # 3. database state for this run
        with engine.connect() as c:
            q = lambda sql, **p: c.execute(text(sql), {"r": run_id, **p})  # noqa: E731
            run_row = q("SELECT status, rows_read, rows_valid, rows_rejected, rows_duplicate, rows_inserted "
                        "FROM pipeline_runs WHERE run_id = :r").one()
            check(tuple(run_row) == ("SUCCESS", manifest["total_rows"], exp_valid, exp_rejected, exp_dup, exp_valid),
                  f"pipeline_runs audit row: {tuple(run_row)}")
            check(q("SELECT COUNT(*) FROM stg_transactions WHERE run_id = :r").scalar() == exp_valid,
                  "stg_transactions rows tagged with this run_id")
            check(q("SELECT COUNT(*) FROM dlq_records WHERE run_id = :r").scalar() == exp_rejected + exp_dup,
                  "dlq_records rows for this run = rejected + duplicate")
            bad = q("SELECT COUNT(*) FROM stg_transactions WHERE run_id = :r AND (amount_inr <= 0 OR "
                    "gateway_status NOT IN ('SUCCESS','FAILED','PENDING','TIMEOUT'))").scalar()
            check(bad == 0, "no invalid amount/status reached the staging table")

            # 4. 25 random loaded rows re-derived independently from the raw file
            raw = pd.read_csv(local, dtype=str, keep_default_na=False)
            loaded = pd.DataFrame(q("SELECT txn_ref_no, user_id, merchant_id, amount_inr, currency_original, "
                                    "gateway_status, created_at_utc FROM stg_transactions WHERE run_id = :r")
                                  .mappings().all())
            sample = loaded.sample(25, random_state=1)
            mismatches = []
            for _, db in sample.iterrows():
                src = raw[raw["txn_ref_no"] == db["txn_ref_no"]].iloc[0]           # first copy wins (A5)
                cur = SPELLING[src["currency"].strip().upper()]
                inr = (Decimal(src["amount"].replace(",", "")) * Decimal(FX[cur])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                if (db["amount_inr"], db["currency_original"], db["gateway_status"], db["user_id"]) != \
                        (inr, cur, src["gateway_status"].strip().upper(), src["user_id"]):
                    mismatches.append(db["txn_ref_no"])
                created = src["created_at"]
                if created.endswith("Z"):                                             # unambiguous UTC rows
                    if db["created_at_utc"] != datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ"):
                        mismatches.append(db["txn_ref_no"] + " (time)")
            check(not mismatches, f"25 loaded rows re-derived by hand from the raw file match {mismatches or ''}")

        # 5. S3 copies of this run
        keys = {o["Key"] for o in s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="").get("Contents", [])
                if run_id in o["Key"]}
        check(any(k.startswith(f"raw/{run_id}/") for k in keys) and
              any(k.startswith(f"processed/{run_id}/") for k in keys) and
              any(k.startswith(f"dlq/{run_id}/") for k in keys),
              f"S3: raw/, processed/ and dlq/ copies for the run ({len(keys)} objects)")

        # 6. SQL reports: planted fraud cases of THIS batch
        rep = run_all(engine, out_dir=tmp / "reports")
        flagged = set(rep["fraud_users"])
        a, b, c_, d = (users[f"U900{i}"] for i in range(1, 5))
        check({a, d} <= flagged and not ({b, c_} & flagged),
              f"fraud: {a} (6 in 8 min) and {d} (crosses clock bucket) flagged; "
              f"{b} (exactly 5) and {c_} (spread out) not flagged")
        check("s3_fraud_alerts" in rep["files"], f"reports copied to S3 ({rep['files'].get('s3_fraud_alerts')})")
        settle = rep["frames"]["settlement"]
        check((settle["txn_count"] > 0).all() and len(settle) >= 40, f"settlement covers {len(settle)} merchants")

        # 7. dashboard shows this run (headless AppTest against the same database)
        import os

        from streamlit.testing.v1 import AppTest

        from app.config import dashboard_password
        os.environ.pop("DASHBOARD_PASSWORD_PARAM", None)        # headless check skips the login form
        dashboard_password.cache_clear()
        at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app" / "dashboard.py"),
                               default_timeout=90).run()
        fraud_tab_text = " ".join(df.value.to_csv() for df in at.dataframe)
        check(not at.exception and a in fraud_tab_text and run_id in fraud_tab_text,
              "dashboard renders and shows the new run and the new fraud alert")

        # 8. idempotency: the same file again inserts nothing
        again = run_pipeline(from_s3, None, processed_dir=tmp / "processed2", dlq_dir=tmp / "dlq2")
        check(again["inserted"] == 0 and again["valid"] == exp_valid,
              f"re-running the same file inserts 0 rows (run {again['run_id']})")

    failed = [label for ok, label in results if not ok]
    print(json.dumps({"run_id": run_id, "rerun_id": again["run_id"], "batch": args.batch.upper(),
                      "fraud_users": {"flag": [a, d], "no_flag": [b, c_]}}))
    print(f"\n{len(results) - len(failed)}/{len(results)} end-to-end checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
