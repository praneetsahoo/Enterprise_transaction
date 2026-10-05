"""PayRecon batch pipeline: raw files -> clean + DLQ -> (S3) -> MySQL.

    python -m app.pipeline                                   # sample files in data/raw, load to DB
    python -m app.pipeline --no-db                           # clean + DLQ files only (no database)
    python -m app.pipeline --transactions f.csv --rates r.csv
    python -m app.pipeline --s3-transactions raw/x/f.csv --s3-rates raw/x/r.csv   # read inputs from S3

Steps:
  1 read + check columns      (missing column -> run FAILED, nothing loaded)
  2 clean merchant rates      (percent -> fraction, bad rates -> DLQ)
  3 clean transactions        (UTC, currency, INR, validation, duplicates -> DLQ)
  4 write processed + DLQ files locally (data/processed, data/dlq)
  5 copy raw, processed and DLQ files to S3 (if S3_BUCKET is set)
  6 batch-load MySQL: rates, transactions (txn_ref_no PK), DLQ table, pipeline_runs row
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.config import LOG_FILE, PROCESSED_DIR, RAW_DIR, S3_BUCKET
from app.preprocessing.cleaning import (RATE_COLUMNS, TXN_COLUMNS, clean_rates, clean_transactions,
                                        read_csv)
from app.preprocessing.dlq import write_dlq

log = logging.getLogger("payrecon.pipeline")


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def run(transactions: Path, rates: Path, use_db: bool = True, run_id: str | None = None,
        processed_dir: Path | None = None, dlq_dir: Path | None = None,
        s3_bucket: str | None = None, engine=None) -> dict:
    # defaults are read at CALL time (not import time) so they can be overridden in tests
    run_id = run_id or new_run_id()
    processed_dir = processed_dir or PROCESSED_DIR
    s3_bucket = S3_BUCKET if s3_bucket is None else s3_bucket
    dlq_kwargs = {"dlq_dir": dlq_dir} if dlq_dir else {}
    log.info("run %s started: transactions=%s rates=%s", run_id, transactions.name, rates.name)

    if use_db:
        from app.database import loader
        from app.database.connection import get_engine
        from app.database.schema import apply_schema
        engine = engine or get_engine()
        apply_schema(engine)
        loader.start_run(engine, run_id, transactions.name, None)

    try:
        # 1-3 read, validate, clean
        raw_rates = read_csv(rates, RATE_COLUMNS)
        good_rates, bad_rates = clean_rates(raw_rates)
        raw_txn = read_csv(transactions, TXN_COLUMNS)
        clean, rejected, duplicates = clean_transactions(raw_txn)
        counts = {"read": len(raw_txn), "valid": len(clean), "rejected": len(rejected),
                  "duplicate": len(duplicates)}
        assert counts["read"] == counts["valid"] + counts["rejected"] + counts["duplicate"], counts

        # 4 local outputs
        processed_dir.mkdir(parents=True, exist_ok=True)
        processed = processed_dir / f"{run_id}__clean_transactions.csv"
        clean.to_csv(processed, index=False)
        dlq_all = rejected if duplicates.empty else pd.concat([rejected, duplicates], ignore_index=True)
        dlq_txn = write_dlq(dlq_all, run_id, transactions.name, **dlq_kwargs)
        dlq_rate = write_dlq(bad_rates, run_id, rates.name, **dlq_kwargs)

        # 5 S3
        s3_keys = {}
        if s3_bucket:
            from app.storage.s3 import upload
            s3_keys["raw"] = [upload(s3_bucket, p, "raw", run_id) for p in (transactions, rates)]
            s3_keys["processed"] = upload(s3_bucket, processed, "processed", run_id)
            s3_keys["dlq"] = [upload(s3_bucket, p, "dlq", run_id) for p in (dlq_txn, dlq_rate) if p]

        # 6 MySQL
        if use_db:
            loader.load_rates(engine, good_rates)
            counts["inserted"] = loader.load_transactions(engine, clean, run_id)
            loader.load_dlq(engine, dlq_all, run_id, transactions.name)
            loader.load_dlq(engine, bad_rates, run_id, rates.name)
            loader.finish_run(engine, run_id, "SUCCESS", counts)
    except Exception as exc:
        log.error("run %s FAILED: %s: %s", run_id, type(exc).__name__, exc)
        if use_db:
            loader.finish_run(engine, run_id, "FAILED", error=f"{type(exc).__name__}: {exc}")
        raise

    reasons = dlq_all["reason"].str.split("|").explode().value_counts().to_dict() if len(dlq_all) else {}
    summary = {"run_id": run_id, **counts, "rates_valid": len(good_rates), "rates_rejected": len(bad_rates),
               "reject_reasons": reasons, "processed_file": str(processed),
               "dlq_files": [str(p) for p in (dlq_txn, dlq_rate) if p], "s3": s3_keys}
    log.info("run %s SUCCESS: %s", run_id, json.dumps({k: summary[k] for k in counts}))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transactions", type=Path, default=RAW_DIR / "raw_payment_dump.csv")
    parser.add_argument("--rates", type=Path, default=RAW_DIR / "merchant_rates.csv")
    parser.add_argument("--s3-transactions", help="S3 key to download instead of --transactions")
    parser.add_argument("--s3-rates", help="S3 key to download instead of --rates")
    parser.add_argument("--no-db", action="store_true", help="clean + DLQ only, do not load MySQL")
    args = parser.parse_args(argv)
    setup_logging()

    if args.s3_transactions or args.s3_rates:
        from app.storage.s3 import download
        if args.s3_transactions:
            args.transactions = download(S3_BUCKET, args.s3_transactions, RAW_DIR / "from_s3")
        if args.s3_rates:
            args.rates = download(S3_BUCKET, args.s3_rates, RAW_DIR / "from_s3")
    try:
        summary = run(args.transactions, args.rates, use_db=not args.no_db)
    except Exception:
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
