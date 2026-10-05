"""Phase 11: failure tests — the system must FAIL SAFELY.

Fail safely means: a clear error, the run recorded as FAILED (when the database is reachable),
an ERROR log line (-> CloudWatch alarm), and NOTHING half-loaded.

Three groups:
  * no database needed (bad files, unreachable DB, S3 errors, error masking)
  * `live`  — real MySQL 8, inside a rolled-back transaction (atomic load)
  * `drill` — a throwaway database whose name must end in `_drill` (SCRATCH_DB_URL), because
              these tests commit on purpose (crash + retry, two runs at the same time)
"""
from __future__ import annotations

import os
import threading
from decimal import Decimal
from pathlib import Path
from unittest import mock

import boto3
import pandas as pd
import pytest
from moto import mock_aws
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app import pipeline
from app.database import loader
from app.preprocessing import cleaning
from app.preprocessing.cleaning import RATE_COLUMNS, SchemaError, TXN_COLUMNS, clean_transactions, read_csv
from scripts.generate_sample_data import COLUMNS, generate, write_csv

HEADER = ",".join(TXN_COLUMNS)
GOOD = "T1,U1,M001,100,INR,SUCCESS,00,2026-10-01T04:30:00Z"


def write(tmp_path, content, name="f.csv", mode="w"):
    p = tmp_path / name
    if mode == "wb":
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def sample(tmp_path):
    data, rates, _ = generate(300, 11)
    write_csv(tmp_path / "raw_payment_dump.csv", data, COLUMNS)
    write_csv(tmp_path / "merchant_rates.csv", rates, RATE_COLUMNS)
    return tmp_path


def unreachable_engine():
    return create_engine("mysql+pymysql://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 2})


def mocked_db():
    return mock.patch.multiple("app.database.loader", start_run=mock.DEFAULT, finish_run=mock.DEFAULT,
                               load_rates=mock.DEFAULT, load_dlq=mock.DEFAULT, load_transactions=mock.DEFAULT)


# ================================================================ bad input files

def test_one_malformed_line_goes_to_dlq_the_rest_of_the_file_still_loads(tmp_path):
    p = write(tmp_path, f"{HEADER}\n{GOOD}\nT2,U1,M001,100,INR,SUCCESS,00,2026-10-01T04:30:00Z,EXTRA\n"
                        f"T3,U1,M001\n{GOOD.replace('T1', 'T4')}\n")
    clean, rejected, _ = clean_transactions(read_csv(p, TXN_COLUMNS))
    assert list(clean["txn_ref_no"]) == ["T1", "T4"]
    assert list(rejected["reason"]) == ["MALFORMED_ROW", "MALFORMED_ROW"]
    assert list(rejected["source_row"]) == [3, 4]
    assert rejected.iloc[0]["raw_line"] == "T2,U1,M001,100,INR,SUCCESS,00,2026-10-01T04:30:00Z,EXTRA"
    assert rejected.iloc[1]["raw_line"] == "T3,U1,M001"


def test_malformed_line_is_never_guessed_even_if_the_first_fields_look_valid(tmp_path):
    p = write(tmp_path, f"{HEADER}\n{GOOD},surprise\n")
    clean, rejected, _ = clean_transactions(read_csv(p, TXN_COLUMNS))
    assert clean.empty and rejected.iloc[0]["reason"] == "MALFORMED_ROW"


def test_malformed_rate_line_goes_to_dlq(tmp_path):
    p = write(tmp_path, "merchant_id,tier,commission_pct\nM1,GOLD,0.01\nM2,GOLD\n", "rates.csv")
    good, bad = cleaning.clean_rates(read_csv(p, RATE_COLUMNS))
    assert list(good["merchant_id"]) == ["M1"] and bad.iloc[0]["reason"] == "MALFORMED_ROW"


@pytest.mark.parametrize("content,mode,message", [
    (b"\x89PNG\r\n\x1a\n\x00\x00\xff binary", "wb", "not a UTF-8"),
    ("", "w", "file is empty"),
    ("\n\n", "w", "file is empty"),
    ("merchant_id,tier,commission_pct\nM1,GOLD,0.01\n", "w", "missing column"),   # rates file in the wrong box
])
def test_unusable_files_fail_with_a_clear_message(tmp_path, content, mode, message):
    with pytest.raises(SchemaError, match=message):
        read_csv(write(tmp_path, content, mode=mode), TXN_COLUMNS)


def test_oversized_file_is_refused_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(cleaning, "MAX_FILE_MB", 0.0001)                # 100 bytes
    p = write(tmp_path, f"{HEADER}\n" + f"{GOOD}\n" * 10)
    with pytest.raises(SchemaError, match="limit is"):
        read_csv(p, TXN_COLUMNS)


def test_bad_file_run_is_recorded_failed_logged_and_loads_nothing(tmp_path, caplog):
    p = write(tmp_path, b"\x00\xff\xfe", "garbage.csv", mode="wb")
    with mock.patch("app.database.schema.apply_schema"), mocked_db() as m:
        engine = mock.MagicMock()
        with pytest.raises(SchemaError):
            pipeline.run(p, None, engine=engine, processed_dir=tmp_path, dlq_dir=tmp_path, s3_bucket="")
    assert m["finish_run"].call_args.args[2] == "FAILED"
    engine.begin.assert_not_called()                                         # load transaction never opened
    assert any(r.levelname == "ERROR" for r in caplog.records)


# ================================================================ database unreachable / credentials

def test_unreachable_database_fails_fast_logs_error_and_exit_code_1(sample, caplog, monkeypatch):
    with mock.patch("app.database.connection.get_engine", return_value=unreachable_engine()):
        with pytest.raises(OperationalError):
            pipeline.run(sample / "raw_payment_dump.csv", None, s3_bucket="")
        code = pipeline.main(["--transactions", str(sample / "raw_payment_dump.csv")])
    assert code == 1
    errors = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert errors and all("FAILED before start" in e for e in errors)        # visible -> alarm
    assert all("nothing" not in e for e in errors)                           # password never logged


@mock_aws
def test_missing_or_forbidden_ssm_parameter_fails_and_never_logs_a_secret(monkeypatch, sample, capsys):
    from app import config

    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "db.example.internal")
    monkeypatch.setenv("DB_PASSWORD_PARAM", "/somebody-else/db/password")   # not ours -> not found / denied
    config.database_url.cache_clear()
    from app.database import connection
    connection.get_engine.cache_clear()
    try:
        assert pipeline.main(["--transactions", str(sample / "raw_payment_dump.csv")]) == 1
    finally:
        config.database_url.cache_clear()
        connection.get_engine.cache_clear()
    out = capsys.readouterr().out                    # main() logs to stdout (and LOG_FILE on the server)
    assert "ERROR payrecon.pipeline" in out and "ParameterNotFound" in out


def test_a_failure_while_recording_failure_does_not_hide_the_original_error(sample, caplog):
    with mock.patch("app.database.schema.apply_schema"), mocked_db() as m:
        m["load_transactions"].side_effect = ValueError("original problem")
        m["finish_run"].side_effect = OperationalError("UPDATE", {}, Exception("db went away"))
        with pytest.raises(ValueError, match="original problem"):
            pipeline.run(sample / "raw_payment_dump.csv", None, engine=mock.MagicMock(),
                         processed_dir=sample / "p", dlq_dir=sample / "d", s3_bucket="")
    messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any("original problem" in x for x in messages)
    assert any("could not record FAILED status" in x for x in messages)


# ================================================================ S3 failures

@pytest.fixture
def aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": "ap-southeast-2"}.items():
        monkeypatch.setenv(k, v)


@mock_aws
def test_s3_write_failure_marks_run_failed_before_anything_is_loaded(aws_env, sample, caplog):
    with mock.patch("app.database.schema.apply_schema"), mocked_db() as m:
        engine = mock.MagicMock()
        with pytest.raises(Exception, match="NoSuchBucket"):
            pipeline.run(sample / "raw_payment_dump.csv", None, engine=engine, processed_dir=sample / "p",
                         dlq_dir=sample / "d", s3_bucket="bucket-that-does-not-exist")
    assert m["finish_run"].call_args.args[2] == "FAILED"
    engine.begin.assert_not_called()
    m["load_transactions"].assert_not_called()


@mock_aws
def test_s3_download_failure_exits_1_with_error_log(aws_env, monkeypatch, capsys):
    boto3.client("s3", region_name="ap-southeast-2").create_bucket(
        Bucket="payrecon-drill-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
    monkeypatch.setattr(pipeline, "S3_BUCKET", "payrecon-drill-bucket")
    assert pipeline.main(["--s3-transactions", "raw/missing.csv", "--no-db"]) == 1
    assert "ERROR payrecon.pipeline input download from S3 FAILED" in capsys.readouterr().out


# ================================================================ live: atomic load (rolled back)

def _clean_frame(n: int, run: str, bad_at: int | None = None) -> pd.DataFrame:
    rows = []
    for i in range(n):
        amount = Decimal("-1.00") if i == bad_at else Decimal("10.00")       # -1 slips past Python on purpose
        rows.append({"txn_ref_no": f"PYT-ATOM-{run}-{i:05d}", "user_id": "PYT_U", "merchant_id": "PYT_M",
                     "amount_original": amount, "currency_original": "INR", "fx_rate_to_inr": Decimal("1"),
                     "amount_inr": amount, "gateway_status": "SUCCESS", "gateway_response_code": "00",
                     "created_at_utc": pd.Timestamp("2026-10-01 10:00:00").to_pydatetime(),
                     "created_at_raw": "x"})
    return pd.DataFrame(rows, dtype=object)


@pytest.mark.live
def test_load_is_all_or_nothing_when_the_database_rejects_a_later_batch(conn):
    savepoint = conn.begin_nested()
    with pytest.raises(OperationalError, match="3819"):                     # CHECK constraint, batch 3 of 4
        loader.load_transactions(conn, _clean_frame(350, "A", bad_at=250), "pytest-atomic", batch_size=100)
    savepoint.rollback()
    left = conn.execute(text("SELECT COUNT(*) FROM stg_transactions WHERE run_id = 'pytest-atomic'")).scalar()
    assert left == 0                                                         # batches 1-2 did not stick


# ================================================================ drills on a throwaway database

def _drill_engine():
    url = os.getenv("SCRATCH_DB_URL")
    if not url:
        pytest.skip("SCRATCH_DB_URL not set (needs a throwaway database named *_drill)")
    parsed = make_url(url)
    assert parsed.database.endswith("_drill"), "refusing to run drills on a non-drill database"
    server = create_engine(parsed.set(database="mysql"))      # set(database=None) would mean "unchanged"
    with server.begin() as c:                                                # fresh, empty drill database
        c.execute(text(f"DROP DATABASE IF EXISTS `{parsed.database}`"))
        c.execute(text(f"CREATE DATABASE `{parsed.database}`"))
    server.dispose()
    from app.database.schema import apply_schema
    engine = create_engine(url, pool_pre_ping=True)
    apply_schema(engine)
    return engine


def test_drill_crash_mid_load_then_retry(sample, tmp_path):
    engine = _drill_engine()
    common = dict(engine=engine, processed_dir=tmp_path / "p", dlq_dir=tmp_path / "d", s3_bucket="")
    real_load_dlq = loader.load_dlq
    calls = {"n": 0}

    def crash_after_transactions(*args, **kwargs):                          # transactions already inserted
        calls["n"] += 1
        raise ConnectionResetError("simulated crash in the middle of the load")

    with mock.patch.object(loader, "load_dlq", side_effect=crash_after_transactions):
        with pytest.raises(ConnectionResetError):
            pipeline.run(sample / "raw_payment_dump.csv", sample / "merchant_rates.csv", run_id="drill-crash", **common)
    with engine.connect() as c:
        assert calls["n"] == 1
        assert c.execute(text("SELECT COUNT(*) FROM stg_transactions")).scalar() == 0      # rolled back
        assert c.execute(text("SELECT COUNT(*) FROM merchant_rates")).scalar() == 0
        assert c.execute(text("SELECT status FROM pipeline_runs WHERE run_id='drill-crash'")).scalar() == "FAILED"

    assert loader.load_dlq is real_load_dlq
    retry = pipeline.run(sample / "raw_payment_dump.csv", sample / "merchant_rates.csv", run_id="drill-retry", **common)
    with engine.connect() as c:
        assert retry["inserted"] == retry["valid"] == c.execute(text("SELECT COUNT(*) FROM stg_transactions")).scalar()
        assert c.execute(text("SELECT COUNT(*) FROM dlq_records WHERE run_id='drill-retry'")).scalar() == \
            retry["rejected"] + retry["duplicate"]
    engine.dispose()


def test_drill_two_runs_of_the_same_file_at_the_same_time_never_double_count(sample, tmp_path):
    engine = _drill_engine()
    outcome: dict[str, object] = {}

    def go(name):
        try:
            outcome[name] = pipeline.run(sample / "raw_payment_dump.csv", sample / "merchant_rates.csv",
                                         run_id=f"drill-{name}", engine=engine, processed_dir=tmp_path / name,
                                         dlq_dir=tmp_path / name, s3_bucket="")
        except Exception as exc:                                             # e.g. deadlock: allowed, must be clean
            outcome[name] = exc

    threads = [threading.Thread(target=go, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [o for o in outcome.values() if isinstance(o, dict)]
    assert successes, outcome
    valid = successes[0]["valid"]
    with engine.connect() as c:
        total, distinct = c.execute(text("SELECT COUNT(*), COUNT(DISTINCT txn_ref_no) FROM stg_transactions")).one()
        inserted_sum = c.execute(text("SELECT SUM(rows_inserted) FROM pipeline_runs WHERE status='SUCCESS'")).scalar()
        statuses = dict(c.execute(text("SELECT run_id, status FROM pipeline_runs")).all())
    assert total == distinct == valid == inserted_sum                        # loaded once, never twice
    assert set(statuses.values()) <= {"SUCCESS", "FAILED"} and "RUNNING" not in statuses.values()
    engine.dispose()
