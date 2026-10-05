"""Batch writes to MySQL: executemany, BATCH_SIZE rows per round trip.

The load functions take an open Connection: the pipeline runs ALL of a run's writes (rates,
transactions, DLQ) inside ONE transaction, so a run is loaded completely or not at all.
start_run / finish_run use their own short transactions so the audit row survives a failed load.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import Connection, Engine, text

from app.config import BATCH_SIZE

TXN_FIELDS = ["txn_ref_no", "user_id", "merchant_id", "amount_original", "currency_original",
              "fx_rate_to_inr", "amount_inr", "gateway_status", "gateway_response_code",
              "created_at_utc", "created_at_raw"]

# Duplicate key -> do nothing (keep the first record, rule A5). Unlike INSERT IGNORE this does
# NOT hide other errors such as a CHECK-constraint violation.
INSERT_TXN = text(f"""
    INSERT INTO stg_transactions ({", ".join(TXN_FIELDS)}, run_id)
    VALUES ({", ".join(":" + f for f in TXN_FIELDS)}, :run_id)
    ON DUPLICATE KEY UPDATE txn_ref_no = txn_ref_no""")

# Rates are reference data: a newer file replaces the old rate.
UPSERT_RATE = text("""
    INSERT INTO merchant_rates (merchant_id, tier, commission_pct)
    VALUES (:merchant_id, :tier, :commission_pct) AS new
    ON DUPLICATE KEY UPDATE tier = new.tier, commission_pct = new.commission_pct""")

INSERT_DLQ = text("""
    INSERT INTO dlq_records (run_id, source_file, source_row, txn_ref_no, reason, raw_record)
    VALUES (:run_id, :source_file, :source_row, :txn_ref_no, :reason, :raw_record)""")


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of dicts with every missing value (NaN/NaT/None) as None = SQL NULL."""
    return [{k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v) for k, v in r.items()}
            for r in df.to_dict("records")]


def _batches(rows: list[dict], size: int):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _execute_batches(conn: Connection, stmt, rows: list[dict], batch_size: int) -> int:
    batches = 0
    for batch in _batches(rows, batch_size):
        conn.execute(stmt, batch)                       # no commit here: the caller owns the transaction
        batches += 1
    return batches


def load_transactions(conn: Connection, clean: pd.DataFrame, run_id: str, batch_size: int = BATCH_SIZE) -> int:
    """Insert clean rows; return how many were NEW (already-loaded txn_ref_no are skipped)."""
    rows = [{**{f: r[f] for f in TXN_FIELDS}, "run_id": run_id} for r in _records(clean)]
    _execute_batches(conn, INSERT_TXN, rows, batch_size)
    return conn.execute(text("SELECT COUNT(*) FROM stg_transactions WHERE run_id = :r"),   # new rows carry this run's id
                        {"r": run_id}).scalar_one()


def load_rates(conn: Connection, rates: pd.DataFrame, batch_size: int = BATCH_SIZE) -> int:
    _execute_batches(conn, UPSERT_RATE, _records(rates), batch_size)
    return len(rates)


def load_dlq(conn: Connection, rejected: pd.DataFrame, run_id: str, source_file: str,
             batch_size: int = BATCH_SIZE) -> int:
    rows = []
    for r in _records(rejected):
        original = {k: ("" if v is None else v) for k, v in r.items() if k not in ("source_row", "reason")}
        rows.append({"run_id": run_id, "source_file": source_file, "source_row": int(r["source_row"]),
                     "txn_ref_no": (r.get("txn_ref_no") or "").strip()[:64] or None,
                     "reason": r["reason"][:255], "raw_record": json.dumps(original, ensure_ascii=False)})
    _execute_batches(conn, INSERT_DLQ, rows, batch_size)
    return len(rows)


def start_run(engine: Engine, run_id: str, source_file: str, s3_key: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pipeline_runs (run_id, source_file, s3_key) VALUES (:r, :f, :k)"),
                     {"r": run_id, "f": source_file, "k": s3_key})


def finish_run(engine: Engine, run_id: str, status: str, counts: dict | None = None,
               error: str | None = None) -> None:
    counts = counts or {}
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs SET status = :status, error_message = :error, finished_at = :now,
                   rows_read = :read, rows_valid = :valid, rows_rejected = :rejected,
                   rows_duplicate = :duplicate, rows_inserted = :inserted
            WHERE run_id = :run_id"""),
            {"status": status, "error": (error or "")[:2000] or None, "run_id": run_id,
             "now": datetime.now(timezone.utc).replace(tzinfo=None),
             **{k: counts.get(k) for k in ("read", "valid", "rejected", "duplicate", "inserted")}})
