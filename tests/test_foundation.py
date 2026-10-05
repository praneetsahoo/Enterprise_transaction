"""Phase 2 checks: configuration is sane and the sample data is deterministic and well-formed."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "generate_sample_data.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def test_fx_dictionary_is_fixed_and_valid():
    assert config.FX_TO_INR["INR"] == 1.0
    assert all(rate > 0 for rate in config.FX_TO_INR.values())
    # every alias points at a currency we can convert
    assert set(config.CURRENCY_ALIASES.values()) <= set(config.FX_TO_INR)


def test_business_rules_match_the_brief():
    assert config.FRAUD_MAX_FAILURES == 5          # "more than 5"
    assert config.FRAUD_WINDOW_MINUTES == 10
    assert config.FRAUD_STATUS == "FAILED"
    assert config.SETTLEMENT_STATUS in config.VALID_STATUSES


def test_database_url_requires_configuration(monkeypatch):
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    config.database_url.cache_clear()
    with pytest.raises(RuntimeError, match="Database not configured"):
        config.database_url()
    config.database_url.cache_clear()


def _file_hashes(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


def test_generator_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        data, rates, manifest = gen.generate(500, seed=7)
        out.mkdir()
        gen.write_csv(out / "raw_payment_dump.csv", data, gen.COLUMNS)
        gen.write_csv(out / "merchant_rates.csv", rates, ["merchant_id", "tier", "commission_pct"])
        (out / "planted.json").write_text(json.dumps(manifest))
    assert _file_hashes(a) == _file_hashes(b)


def test_generated_files_have_the_columns_from_the_brief(tmp_path):
    data, rates, _ = gen.generate(200, seed=1)
    gen.write_csv(tmp_path / "raw.csv", data, gen.COLUMNS)
    gen.write_csv(tmp_path / "rates.csv", rates, ["merchant_id", "tier", "commission_pct"])
    with (tmp_path / "raw.csv").open(encoding="utf-8") as f:
        assert next(csv.reader(f)) == ["txn_ref_no", "user_id", "merchant_id", "amount", "currency",
                                       "gateway_status", "gateway_response_code", "created_at"]
    with (tmp_path / "rates.csv").open(encoding="utf-8") as f:
        assert next(csv.reader(f)) == ["merchant_id", "tier", "commission_pct"]


def test_planted_fraud_cases_are_present():
    data, _, manifest = gen.generate(300, seed=3)
    failed_by_user = {}
    for row in data:
        if row["user_id"].startswith("U90"):
            failed_by_user[row["user_id"]] = failed_by_user.get(row["user_id"], 0) + 1
    assert failed_by_user == {"U9001": 6, "U9002": 5, "U9003": 6, "U9004": 6}
    assert manifest["fraud"] == {"flagged": ["U9001", "U9004"], "not_flagged": ["U9002", "U9003"]}
