"""Phase 7: S3 (mocked with moto — no real AWS calls), pipeline orchestration and failure paths,
and messy-file edge cases for the cleaning script."""
from __future__ import annotations

import json
from unittest import mock

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app import pipeline
from app.preprocessing.cleaning import RATE_COLUMNS, SchemaError, TXN_COLUMNS, clean_transactions, read_csv
from app.storage import s3
from scripts.generate_sample_data import COLUMNS, generate, write_csv

BUCKET = "payrecon-test-bucket"
REGION = "ap-southeast-2"


@pytest.fixture
def sample(tmp_path):
    data, rates, _ = generate(300, 7)
    write_csv(tmp_path / "raw_payment_dump.csv", data, COLUMNS)
    write_csv(tmp_path / "merchant_rates.csv", rates, RATE_COLUMNS)
    return tmp_path


@pytest.fixture
def fake_s3(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        yield client


def keys(client) -> list[str]:
    return sorted(o["Key"] for o in client.list_objects_v2(Bucket=BUCKET).get("Contents", []))


# ---------------------------------------------------------------- S3

def test_upload_uses_run_prefix_and_encryption(fake_s3, tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x\n1\n")
    key = s3.upload(BUCKET, f, "raw", "RUN1")
    assert key == "raw/RUN1/a.csv"
    assert fake_s3.head_object(Bucket=BUCKET, Key=key)["ServerSideEncryption"] == "AES256"


def test_download_round_trip(fake_s3, tmp_path):
    fake_s3.put_object(Bucket=BUCKET, Key="raw/x/file.csv", Body=b"hello")
    path = s3.download(BUCKET, "raw/x/file.csv", tmp_path / "dl")
    assert path.read_bytes() == b"hello"


def test_pipeline_copies_raw_processed_and_dlq_to_s3(fake_s3, sample, tmp_path):
    out = pipeline.run(sample / "raw_payment_dump.csv", sample / "merchant_rates.csv", use_db=False,
                       run_id="R1", processed_dir=tmp_path / "p", dlq_dir=tmp_path / "d", s3_bucket=BUCKET)
    assert keys(fake_s3) == ["dlq/R1/R1__raw_payment_dump__rejected.csv", "processed/R1/R1__clean_transactions.csv",
                             "raw/R1/merchant_rates.csv", "raw/R1/raw_payment_dump.csv"]
    assert out["s3"]["processed"] == "processed/R1/R1__clean_transactions.csv"


def test_cli_can_read_its_input_from_s3(fake_s3, sample, tmp_path, monkeypatch, capsys):
    fake_s3.upload_file(str(sample / "raw_payment_dump.csv"), BUCKET, "raw/in/raw_payment_dump.csv")
    fake_s3.upload_file(str(sample / "merchant_rates.csv"), BUCKET, "raw/in/merchant_rates.csv")
    monkeypatch.setattr(pipeline, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(pipeline, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(pipeline, "PROCESSED_DIR", tmp_path / "processed")
    from app.preprocessing.dlq import write_dlq
    to_tmp = lambda df, run_id, src, **_: write_dlq(df, run_id, src, dlq_dir=tmp_path / "dlq")
    with mock.patch("app.pipeline.write_dlq", side_effect=to_tmp):
        code = pipeline.main(["--s3-transactions", "raw/in/raw_payment_dump.csv",
                              "--s3-rates", "raw/in/merchant_rates.csv", "--no-db"])
    assert code == 0
    assert (tmp_path / "raw" / "from_s3" / "raw_payment_dump.csv").exists()
    stdout = capsys.readouterr().out
    summary = json.loads(stdout[stdout.index("{\n"):])           # the JSON summary printed at the end
    rows_in_file = len(pd.read_csv(sample / "raw_payment_dump.csv", dtype=str))
    assert summary["read"] == rows_in_file == summary["valid"] + summary["rejected"] + summary["duplicate"]
    assert summary["processed_file"].startswith(str(tmp_path / "processed"))
    assert summary["dlq_files"][0].startswith(str(tmp_path / "dlq"))


# ---------------------------------------------------------------- orchestration + failure paths

def test_run_records_success_and_loads_in_order(sample, tmp_path):
    with mock.patch("app.database.connection.get_engine"), \
         mock.patch("app.database.schema.apply_schema"), \
         mock.patch.multiple("app.database.loader", start_run=mock.DEFAULT, finish_run=mock.DEFAULT,
                             load_rates=mock.DEFAULT, load_dlq=mock.DEFAULT,
                             load_transactions=mock.DEFAULT) as m:
        m["load_transactions"].return_value = 250
        out = pipeline.run(sample / "raw_payment_dump.csv", sample / "merchant_rates.csv", run_id="R2",
                           processed_dir=tmp_path, dlq_dir=tmp_path / "d", s3_bucket="")
    m["start_run"].assert_called_once()
    status, counts = m["finish_run"].call_args.args[2], m["finish_run"].call_args.args[3]
    assert status == "SUCCESS" and counts["inserted"] == 250 == out["inserted"]
    assert counts["read"] == counts["valid"] + counts["rejected"] + counts["duplicate"]


def test_bad_file_marks_run_failed_and_loads_nothing(tmp_path, caplog):
    bad = tmp_path / "raw_payment_dump.csv"
    bad.write_text("txn_ref_no,amount\nT1,10\n")                    # most columns missing
    rates = tmp_path / "merchant_rates.csv"
    rates.write_text("merchant_id,tier,commission_pct\nM1,GOLD,0.01\n")
    with mock.patch("app.database.connection.get_engine"), \
         mock.patch("app.database.schema.apply_schema"), \
         mock.patch.multiple("app.database.loader", start_run=mock.DEFAULT, finish_run=mock.DEFAULT,
                             load_rates=mock.DEFAULT, load_dlq=mock.DEFAULT,
                             load_transactions=mock.DEFAULT) as m:
        with pytest.raises(SchemaError):
            pipeline.run(bad, rates, run_id="R3", processed_dir=tmp_path, s3_bucket="")
    assert m["finish_run"].call_args.args[2] == "FAILED"
    assert "missing column" in m["finish_run"].call_args.kwargs["error"]
    m["load_transactions"].assert_not_called()
    m["load_rates"].assert_not_called()
    assert any(r.levelname == "ERROR" and "FAILED" in r.message for r in caplog.records)   # -> CloudWatch alarm


def test_cli_returns_exit_code_1_on_failure(tmp_path):
    bad = tmp_path / "t.csv"
    bad.write_text("nope\n1\n")
    assert pipeline.main(["--transactions", str(bad), "--rates", str(bad), "--no-db"]) == 1


# ---------------------------------------------------------------- messy file edge cases

def write(tmp_path, text: str, name="f.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


GOOD = "T1,U1,M001,100,INR,SUCCESS,00,2026-10-01T04:30:00Z"


def test_excel_bom_upper_case_and_spaced_headers(tmp_path):
    header = "﻿ TXN_REF_NO ,User_Id,MERCHANT_ID,Amount,Currency,Gateway_Status,Gateway_Response_Code,Created_At"
    df = read_csv(write(tmp_path, f"{header}\n{GOOD}\n"), TXN_COLUMNS)
    assert list(df.columns) == ["source_row", *TXN_COLUMNS] and df.iloc[0]["txn_ref_no"] == "T1"


def test_columns_in_any_order_and_extra_columns_ignored(tmp_path):
    text = ("created_at,extra,amount,currency,txn_ref_no,user_id,merchant_id,gateway_status,gateway_response_code\n"
            "2026-10-01T04:30:00Z,junk,100,INR,T1,U1,M001,SUCCESS,00\n")
    clean, rejected, _ = clean_transactions(read_csv(write(tmp_path, text), TXN_COLUMNS))
    assert len(clean) == 1 and rejected.empty and "extra" not in clean.columns


def test_header_only_file_gives_empty_but_valid_run(tmp_path):
    df = read_csv(write(tmp_path, ",".join(TXN_COLUMNS) + "\n"), TXN_COLUMNS)
    clean, rejected, dups = clean_transactions(df)
    assert len(df) == len(clean) == len(rejected) == len(dups) == 0


def test_quoted_amount_with_thousands_separator_and_leading_zero_codes(tmp_path):
    text = ",".join(TXN_COLUMNS) + '\nT1,U1,M001,"1,250.50",INR,SUCCESS,007,1790829000\n'
    clean, _, _ = clean_transactions(read_csv(write(tmp_path, text), TXN_COLUMNS))
    row = clean.iloc[0]
    assert str(row["amount_inr"]) == "1250.50" and row["gateway_response_code"] == "007"   # not 7.0
    assert row["created_at_raw"] == "1790829000"                                             # not 1.79e9


def test_cleaning_is_deterministic(sample):
    a = clean_transactions(read_csv(sample / "raw_payment_dump.csv", TXN_COLUMNS))
    b = clean_transactions(read_csv(sample / "raw_payment_dump.csv", TXN_COLUMNS))
    for x, y in zip(a, b):
        pd.testing.assert_frame_equal(x, y)


def test_rates_file_is_optional_existing_rates_are_kept(sample, tmp_path):
    with mock.patch("app.database.connection.get_engine"), \
         mock.patch("app.database.schema.apply_schema"), \
         mock.patch.multiple("app.database.loader", start_run=mock.DEFAULT, finish_run=mock.DEFAULT,
                             load_rates=mock.DEFAULT, load_dlq=mock.DEFAULT,
                             load_transactions=mock.DEFAULT) as m:
        m["load_transactions"].return_value = 1
        out = pipeline.run(sample / "raw_payment_dump.csv", None, run_id="R4", processed_dir=tmp_path,
                           dlq_dir=tmp_path / "d", s3_bucket="")
    m["load_rates"].assert_not_called()
    assert out["rates_valid"] == 0 and m["finish_run"].call_args.args[2] == "SUCCESS"


def test_tests_never_log_to_the_production_log_file():
    import os

    assert "LOG_FILE" not in os.environ          # removed by conftest before the app is imported
