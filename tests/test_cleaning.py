"""Phase 5: every cleaning rule on its own, then the whole file end to end (no database needed)."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.preprocessing.cleaning import (SchemaError, TXN_COLUMNS, clean_rates, clean_transactions,
                                        normalize_commission, normalize_currency, normalize_status,
                                        parse_amount, parse_timestamp, read_csv, to_inr,
                                        validate_transaction)
from scripts.generate_sample_data import COLUMNS, generate, write_csv

# ---------------------------------------------------------------- timestamps -> UTC

@pytest.mark.parametrize("raw", [
    "2026-10-01T04:30:00Z",              # ISO, UTC
    "2026-10-01T10:00:00+05:30",         # ISO with offset
    "2026-10-01 10:00:00",               # no zone -> IST (A1)
    "01/10/2026 10:00:00",               # DD/MM/YYYY, IST
    "1790829000",                        # epoch seconds
    "1790829000000",                     # epoch milliseconds
    "  2026-10-01T04:30:00Z  ",          # surrounding spaces
])
def test_every_timestamp_format_becomes_the_same_utc_moment(raw):
    assert parse_timestamp(raw) == datetime(2026, 10, 1, 4, 30)


@pytest.mark.parametrize("raw", ["", "not-a-date", "31/02/2026 10:00:00", "123456", "1999-12-31 23:00:00",
                                 "2026-13-01 10:00:00"])
def test_bad_timestamps_are_rejected(raw):
    assert parse_timestamp(raw) is None


def test_ist_midnight_rolls_back_to_previous_utc_day():
    assert parse_timestamp("2026-10-01 00:10:00") == datetime(2026, 9, 30, 18, 40)


# ---------------------------------------------------------------- currency + INR

@pytest.mark.parametrize("raw,code", [("INR", "INR"), ("inr", "INR"), ("₹", "INR"), ("Rs", "INR"),
                                      ("Rs.", "INR"), (" usd ", "USD"), ("$", "USD"), ("US$", "USD"),
                                      ("€", "EUR"), ("£", "GBP"), ("aed", "AED"), ("SGD", "SGD")])
def test_currency_aliases(raw, code):
    assert normalize_currency(raw) == code


@pytest.mark.parametrize("raw", ["XYZ", "", "BTC"])
def test_unknown_currency(raw):
    assert normalize_currency(raw) is None


def test_inr_conversion_uses_fixed_rate_and_rounds_half_up_to_paisa():
    assert to_inr(Decimal("10.00"), "USD") == (Decimal("83.00"), Decimal("830.00"))
    assert to_inr(Decimal("0.01"), "AED") == (Decimal("22.6"), Decimal("0.23"))   # 0.226 -> 0.23
    assert to_inr(Decimal("250.50"), "INR")[1] == Decimal("250.50")


# ---------------------------------------------------------------- amount, status, commission

@pytest.mark.parametrize("raw,expected", [("100", Decimal("100")), ("1,250.50", Decimal("1250.50")),
                                          (" -5 ", Decimal("-5")), ("abc", None), ("", None),
                                          ("NaN", None), ("Infinity", None)])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_status_is_case_insensitive_and_unknown_is_rejected():
    assert normalize_status(" failed ") == "FAILED"
    assert normalize_status("Success") == "SUCCESS"
    assert normalize_status("REVERSED") is None


@pytest.mark.parametrize("raw,expected", [("0.025", Decimal("0.025")), ("2.5", Decimal("0.025")),
                                          ("0", Decimal("0")), ("-0.01", None), ("150", None), ("x", None)])
def test_commission_fraction_or_percent(raw, expected):
    assert normalize_commission(raw) == expected


# ---------------------------------------------------------------- one row

def row(**overrides) -> dict:
    base = {"source_row": 2, "txn_ref_no": "T1", "user_id": "U1", "merchant_id": "M001", "amount": "100",
            "currency": "INR", "gateway_status": "SUCCESS", "gateway_response_code": "00",
            "created_at": "2026-10-01T04:30:00Z"}
    return {**base, **overrides}


def test_valid_row_is_cleaned():
    record, reasons = validate_transaction(row(amount="10", currency="$", gateway_status="failed"))
    assert reasons == []
    assert record["amount_original"] == Decimal("10.00") and record["amount_inr"] == Decimal("830.00")
    assert record["currency_original"] == "USD" and record["gateway_status"] == "FAILED"
    assert record["created_at_raw"] == "2026-10-01T04:30:00Z"


@pytest.mark.parametrize("overrides,reason", [
    ({"amount": "0"}, "AMOUNT_NOT_POSITIVE"), ({"amount": "-1"}, "AMOUNT_NOT_POSITIVE"),
    ({"amount": "abc"}, "AMOUNT_NOT_NUMERIC"), ({"amount": "99999999999"}, "AMOUNT_OUT_OF_RANGE"),
    ({"txn_ref_no": ""}, "MISSING_TXN_REF"), ({"txn_ref_no": "   "}, "MISSING_TXN_REF"),
    ({"txn_ref_no": "T" * 65}, "TXN_REF_NO_TOO_LONG"), ({"user_id": ""}, "MISSING_USER_ID"),
    ({"merchant_id": ""}, "MISSING_MERCHANT_ID"), ({"currency": "XYZ"}, "UNKNOWN_CURRENCY"),
    ({"created_at": "not-a-date"}, "BAD_TIMESTAMP"), ({"gateway_status": "REVERSED"}, "UNKNOWN_STATUS"),
])
def test_each_invalid_field_gives_its_reason(overrides, reason):
    record, reasons = validate_transaction(row(**overrides))
    assert record is None and reasons == [reason]


def test_all_reasons_are_reported_not_just_the_first():
    _, reasons = validate_transaction(row(txn_ref_no="", amount="-5", created_at="x"))
    assert reasons == ["MISSING_TXN_REF", "AMOUNT_NOT_POSITIVE", "BAD_TIMESTAMP"]


# ---------------------------------------------------------------- duplicates (A5)

def test_first_valid_copy_wins_and_duplicates_are_classified():
    raw = pd.DataFrame([row(source_row=2), row(source_row=3),                         # exact copy
                        row(source_row=4, gateway_status="FAILED"),                   # conflicting copy
                        row(source_row=5, txn_ref_no="T2")])
    clean, rejected, dups = clean_transactions(raw)
    assert list(clean["txn_ref_no"]) == ["T1", "T2"] and clean.iloc[0]["gateway_status"] == "SUCCESS"
    assert rejected.empty
    assert list(dups["reason"]) == ["DUPLICATE_TXN_REF_EXACT", "DUPLICATE_TXN_REF_CONFLICT"]


def test_an_invalid_first_copy_does_not_block_a_later_valid_one():
    clean, rejected, dups = clean_transactions(pd.DataFrame([row(amount="0"), row(source_row=3)]))
    assert len(clean) == 1 and len(rejected) == 1 and dups.empty


# ---------------------------------------------------------------- merchant rates

def test_rates_convert_percent_and_reject_bad_and_duplicate_merchants():
    raw = pd.DataFrame([{"source_row": 2, "merchant_id": "M1", "tier": "gold", "commission_pct": "0.012"},
                        {"source_row": 3, "merchant_id": "M2", "tier": "", "commission_pct": "2.5"},
                        {"source_row": 4, "merchant_id": "M3", "tier": "X", "commission_pct": "-1"},
                        {"source_row": 5, "merchant_id": "M1", "tier": "X", "commission_pct": "0.5"}])
    clean, rejected = clean_rates(raw)
    assert clean.to_dict("records") == [
        {"merchant_id": "M1", "tier": "GOLD", "commission_pct": Decimal("0.012")},
        {"merchant_id": "M2", "tier": None, "commission_pct": Decimal("0.025")}]
    assert list(rejected["reason"]) == ["BAD_COMMISSION", "DUPLICATE_MERCHANT_ID"]


# ---------------------------------------------------------------- whole file

def test_missing_column_fails_the_whole_file(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("txn_ref_no,amount\nT1,10\n")
    with pytest.raises(SchemaError, match="missing column"):
        read_csv(path, TXN_COLUMNS)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("raw")
    data, rates, manifest = generate(2000, 42)
    write_csv(folder / "raw_payment_dump.csv", data, COLUMNS)
    write_csv(folder / "merchant_rates.csv", rates, ["merchant_id", "tier", "commission_pct"])
    return folder, manifest


def test_pipeline_dry_run_reconciles_with_what_was_planted(sample, tmp_path):
    from app.pipeline import run

    folder, manifest = sample
    s = run(folder / "raw_payment_dump.csv", folder / "merchant_rates.csv", use_db=False,
            run_id="test", processed_dir=tmp_path / "processed", dlq_dir=tmp_path / "dlq", s3_bucket="")

    assert s["read"] == manifest["total_rows"] == s["valid"] + s["rejected"] + s["duplicate"]
    assert s["rejected"] == sum(manifest["planted_invalid"].values())
    assert s["duplicate"] == manifest["exact_duplicates"] + manifest["duplicates_with_changed_status"]
    p = manifest["planted_invalid"]
    assert s["reject_reasons"]["AMOUNT_NOT_POSITIVE"] == p["zero amount"] + p["negative amount"]
    assert s["reject_reasons"]["MISSING_TXN_REF"] == p["missing txn_ref_no"] + p["blank txn_ref_no"]
    assert s["reject_reasons"]["BAD_TIMESTAMP"] == p["invalid timestamp"] + p["impossible date"]
    assert s["rates_valid"] == 40 and s["rates_rejected"] == 0

    clean = pd.read_csv(s["processed_file"], dtype=str)
    assert clean["txn_ref_no"].is_unique and (clean["amount_inr"].astype(float) > 0).all()
    assert set(clean["currency_original"]) <= {"INR", "USD", "EUR", "GBP", "AED", "SGD"}


def test_dlq_file_keeps_the_original_text_and_a_reason(sample, tmp_path):
    from app.pipeline import run

    folder, _ = sample
    s = run(folder / "raw_payment_dump.csv", folder / "merchant_rates.csv", use_db=False, run_id="test",
            processed_dir=tmp_path, dlq_dir=tmp_path / "dlq", s3_bucket="")
    dlq = pd.read_csv(s["dlq_files"][0], dtype=str, keep_default_na=False)
    assert list(dlq.columns) == ["run_id", "source_file", "source_row", *TXN_COLUMNS, "reason"]
    assert (dlq["reason"] != "").all()
    original = pd.read_csv(folder / "raw_payment_dump.csv", dtype=str, keep_default_na=False)
    for _, r in dlq.head(25).iterrows():                         # row text identical to the source line
        assert original.iloc[int(r["source_row"]) - 2][TXN_COLUMNS].tolist() == r[TXN_COLUMNS].tolist()


def test_loader_sends_missing_values_as_sql_null_not_nan():
    from app.database.loader import _records

    df = pd.DataFrame({"tier": ["GOLD", None], "x": [1.0, float("nan")]})
    assert _records(df) == [{"tier": "GOLD", "x": 1.0}, {"tier": None, "x": None}]
