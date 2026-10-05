"""Phase 4: data layer.

Static tests run anywhere. Live tests run only when a database is configured (DB_URL or DB_HOST);
they run inside a transaction that is ROLLED BACK, so they never leave data behind.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal

import pytest

from app.database.schema import EXPECTED_TABLES, load_statements, split_statements

# ---------- static (no database) ----------

def test_split_ignores_comments_and_blank_statements():
    sql = "-- header; with semicolon\nCREATE TABLE a (x INT); -- trailing\n\n;CREATE TABLE b (y INT);"
    assert split_statements(sql) == ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]


def test_schema_creates_exactly_the_expected_tables_idempotently():
    stmts = load_statements()
    names = {re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1) for s in stmts}
    assert names == EXPECTED_TABLES
    assert all(s.startswith("CREATE TABLE IF NOT EXISTS") for s in stmts)


def test_destructive_sql_is_refused(tmp_path):
    (tmp_path / "bad.sql").write_text("CREATE TABLE IF NOT EXISTS t (x INT); DROP TABLE t;")
    with pytest.raises(ValueError, match="destructive"):
        load_statements(("bad.sql",), sql_dir=tmp_path)


def test_txn_ref_no_is_primary_key_and_money_is_decimal():
    stg = next(s for s in load_statements() if "stg_transactions" in s)
    assert "PRIMARY KEY (txn_ref_no)" in stg
    assert re.search(r"amount_inr\s+DECIMAL\(18,2\)", stg)
    assert not re.search(r"\b(FLOAT|DOUBLE)\b", " ".join(load_statements()), re.IGNORECASE)


# ---------- live (MySQL 8) ----------

live = pytest.mark.skipif(not (os.getenv("DB_URL") or os.getenv("DB_HOST")),
                          reason="no database configured (set DB_URL or DB_HOST)")


@pytest.fixture(scope="module")
def engine():
    from app.database.connection import get_engine
    from app.database.schema import apply_schema

    eng = get_engine()
    apply_schema(eng)
    return eng


@pytest.fixture
def conn(engine):
    """A connection whose work is always rolled back."""
    with engine.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()


def _txn(ref, amount="100.00", status="SUCCESS"):
    return dict(ref=ref, user="U_TEST", merchant="M_TEST", amt=Decimal(amount), cur="INR", fx=Decimal("1"),
                inr=Decimal(amount), status=status, code="00", ts="2026-10-01 10:00:00",
                raw="01/10/2026 15:30:00", run="pytest-run")


INSERT_TXN = """
INSERT INTO stg_transactions (txn_ref_no, user_id, merchant_id, amount_original, currency_original,
    fx_rate_to_inr, amount_inr, gateway_status, gateway_response_code, created_at_utc, created_at_raw, run_id)
VALUES (:ref, :user, :merchant, :amt, :cur, :fx, :inr, :status, :code, :ts, :raw, :run)
ON DUPLICATE KEY UPDATE txn_ref_no = txn_ref_no
"""


@live
def test_schema_is_mysql8_and_apply_is_idempotent(engine):
    from sqlalchemy import text

    from app.database.schema import apply_schema

    assert EXPECTED_TABLES <= set(apply_schema(engine))     # second run: no error, nothing lost
    with engine.connect() as c:
        version = c.execute(text("SELECT VERSION()")).scalar()
    assert int(version.split(".")[0]) >= 8, f"sliding window needs MySQL 8+, got {version}"


@live
def test_primary_key_prevents_double_counting(conn):
    from sqlalchemy import text

    conn.execute(text(INSERT_TXN), [_txn("PYTEST-DUP-1"), _txn("PYTEST-DUP-1", amount="999.00")])
    rows = conn.execute(text("SELECT amount_inr FROM stg_transactions WHERE txn_ref_no='PYTEST-DUP-1'")).all()
    assert rows == [(Decimal("100.00"),)]                    # one row, first record kept (A5)


@live
@pytest.mark.parametrize("bad", [_txn("PYTEST-NEG", amount="-5.00"), _txn("PYTEST-ZERO", amount="0.00"),
                                 _txn("PYTEST-STATUS", status="REVERSED")])
def test_check_constraints_are_a_last_line_of_defence(conn, bad):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with pytest.raises(OperationalError, match="3819"):      # MySQL 3819 = check constraint violated
        conn.execute(text(INSERT_TXN), bad)


@live
def test_dlq_keeps_original_record_as_json(conn):
    from sqlalchemy import text

    original = {"txn_ref_no": "", "amount": "-50", "currency": "Rs.", "created_at": "not-a-date"}
    conn.execute(text("INSERT INTO dlq_records (run_id, source_file, source_row, txn_ref_no, reason, raw_record) "
                      "VALUES ('pytest-run', 'raw_payment_dump.csv', 7, NULL, "
                      "'MISSING_TXN_REF|AMOUNT_NOT_POSITIVE|BAD_TIMESTAMP', :raw)"),
                 {"raw": json.dumps(original)})
    stored = conn.execute(text("SELECT raw_record FROM dlq_records WHERE run_id='pytest-run'")).scalar()
    assert json.loads(stored) == original
