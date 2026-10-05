"""Run the analytics SQL in sql/ and save the results.

    python -m app.analytics.reports        # print + save CSVs (+ copy to S3 if S3_BUCKET is set)

The business logic is IN THE SQL FILES; Python only supplies parameters from app/config.py
and checks that the sliding-window fraud query and the independent self-join agree.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from app.config import (FRAUD_MAX_FAILURES, FRAUD_STATUS, FRAUD_WINDOW_MINUTES, PROCESSED_DIR,
                        S3_BUCKET, SETTLEMENT_STATUS, SQL_DIR)

log = logging.getLogger("payrecon.analytics")

REPORTS = {
    "settlement": "02_settlement.sql",
    "fraud_alerts": "03_fraud_sliding_window.sql",
    "fraud_crosscheck": "04_fraud_selfjoin_check.sql",
    "reconciliation": "05_reconciliation_summary.sql",
}
PARAMS = {
    "settlement_status": SETTLEMENT_STATUS,
    "fraud_status": FRAUD_STATUS,
    "window_minutes": FRAUD_WINDOW_MINUTES,
    "max_failures": FRAUD_MAX_FAILURES,
}


def sql_for(name: str) -> str:
    return (SQL_DIR / REPORTS[name]).read_text(encoding="utf-8")


def run_report(conn, name: str, **overrides) -> pd.DataFrame:
    """Run one report on an open connection. Only the parameters the SQL uses are sent."""
    sql = sql_for(name)
    params = {k: v for k, v in {**PARAMS, **overrides}.items() if f":{k}" in sql}
    result = conn.execute(text(sql), params)
    return pd.DataFrame(result.mappings().all(), columns=list(result.keys()))


def fraud_users(conn, **overrides) -> tuple[list[str], list[str]]:
    """(users from the window query, users from the self-join) — must be identical."""
    window = run_report(conn, "fraud_alerts", **overrides)["user_id"].tolist()
    check = run_report(conn, "fraud_crosscheck", **overrides)["user_id"].tolist()
    return sorted(window), sorted(check)


def run_all(engine, out_dir: Path = PROCESSED_DIR / "reports", s3_bucket: str = S3_BUCKET) -> dict:
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    with engine.connect() as conn:
        frames = {name: run_report(conn, name) for name in REPORTS}
    window_users = sorted(frames["fraud_alerts"]["user_id"])
    check_users = sorted(frames["fraud_crosscheck"]["user_id"])
    if window_users != check_users:
        log.error("fraud cross-check MISMATCH: window=%s self-join=%s", window_users, check_users)
        raise RuntimeError("fraud cross-check mismatch")

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("settlement", "fraud_alerts", "reconciliation"):
        path = out_dir / f"{stamp}__{name}.csv"
        frames[name].to_csv(path, index=False)
        files[name] = str(path)
        if s3_bucket:
            from app.storage.s3 import upload
            files[f"s3_{name}"] = upload(s3_bucket, path, "processed/reports", stamp)
    log.info("reports %s: %d merchants settled, %d fraud alerts (cross-check OK)",
             stamp, len(frames["settlement"]), len(window_users))
    return {"frames": frames, "files": files, "fraud_users": window_users}


def main() -> int:
    from app.database.connection import get_engine
    from app.pipeline import setup_logging

    setup_logging()
    try:
        out = run_all(get_engine())
    except Exception as exc:
        log.error("reports FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        for name, df in out["frames"].items():
            print(f"\n=== {name} ({len(df)} rows) ===")
            print(df.head(12).to_string(index=False))
    print("\nfiles:", json.dumps(out["files"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
