"""Phase 6: settlement + sliding-window fraud SQL.

Live tests insert their own test rows (user / merchant ids start with 'PYT_') inside a
transaction that is rolled back, so they are independent of whatever data is loaded.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.analytics.reports import PARAMS, REPORTS, fraud_users, run_report, sql_for

T0 = datetime(2026, 10, 2, 5, 0, 0)

# ---------------------------------------------------------------- static

def test_analytics_sql_is_read_only():
    for name in REPORTS:
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b", sql_for(name), re.I), name


def test_fraud_query_is_a_time_based_sliding_window_not_buckets():
    sql = sql_for("fraud_alerts")
    assert re.search(r"RANGE BETWEEN INTERVAL :window_minutes MINUTE PRECEDING AND CURRENT ROW", sql)
    assert "PARTITION BY user_id" in sql and "ORDER BY created_at_utc" in sql
    assert not re.search(r"\bROWS BETWEEN\b|DATE\(|FLOOR\(|DATE_FORMAT\(", sql)   # no row-count or clock buckets


def test_thresholds_come_from_config():
    assert PARAMS["window_minutes"] == 10 and PARAMS["max_failures"] == 5 and PARAMS["fraud_status"] == "FAILED"


# ---------------------------------------------------------------- live helpers

live = pytest.mark.live
_n = iter(range(10**6))


def add_txn(conn, user, minutes, status="FAILED", merchant="PYT_M1", amount="100.00"):
    conn.execute(text("""
        INSERT INTO stg_transactions (txn_ref_no, user_id, merchant_id, amount_original, currency_original,
            fx_rate_to_inr, amount_inr, gateway_status, created_at_utc, created_at_raw, run_id)
        VALUES (:ref, :user, :merchant, :amt, 'INR', 1, :amt, :status, :ts, 'test', 'pytest')"""),
        {"ref": f"PYT-{next(_n):07d}", "user": user, "merchant": merchant, "amt": Decimal(amount),
         "status": status, "ts": T0 + timedelta(minutes=minutes)})


def flagged(conn) -> dict[str, int]:
    df = run_report(conn, "fraud_alerts")
    df = df[df["user_id"].str.startswith("PYT_")]
    return dict(zip(df["user_id"], df["peak_failures_in_window"]))


# ---------------------------------------------------------------- fraud: the brief's three cases + traps

@live
def test_six_failures_within_ten_minutes_is_flagged(conn):
    for m in [0, 1, 3, 4, 6, 8]:
        add_txn(conn, "PYT_SIX", m)
    assert flagged(conn) == {"PYT_SIX": 6}


@live
def test_exactly_five_failures_is_not_flagged(conn):
    for m in [0, 2, 4, 6, 9]:
        add_txn(conn, "PYT_FIVE", m)
    assert flagged(conn) == {}


@live
def test_six_failures_spread_beyond_ten_minutes_is_not_flagged(conn):
    for m in [0, 10.5, 21, 31.5, 42, 52.5]:
        add_txn(conn, "PYT_SPREAD", m)
    assert flagged(conn) == {}


@live
def test_window_crossing_a_clock_bucket_is_still_flagged(conn):
    # 3 failures before xx:10 and 3 after: fixed 10-minute buckets would see 3 + 3 and miss it
    for m in [8, 8.5, 9, 11, 11.5, 12]:
        add_txn(conn, "PYT_BUCKET", m)
    assert flagged(conn) == {"PYT_BUCKET": 6}


@live
def test_window_boundary_is_inclusive_rule_a6(conn):
    for m in [0, 2, 4, 6, 8, 10]:              # first and last exactly 10:00 apart
        add_txn(conn, "PYT_EDGE_IN", m)
    for m in [0, 2, 4, 6, 8, 10 + 1 / 60]:     # last one 1 second outside
        add_txn(conn, "PYT_EDGE_OUT", m)
    assert flagged(conn) == {"PYT_EDGE_IN": 6}


@live
def test_only_failed_counts_and_users_are_separate(conn):
    for m in [0, 1, 2, 3, 4]:
        add_txn(conn, "PYT_MIX", m)
    add_txn(conn, "PYT_MIX", 5, status="TIMEOUT")              # 6 events, only 5 FAILED
    add_txn(conn, "PYT_MIX", 6, status="SUCCESS")
    for m in [0, 1, 2]:
        add_txn(conn, "PYT_OTHER", m)                          # other user's failures don't add up
    assert flagged(conn) == {}


@live
def test_same_second_failures_are_all_counted(conn):
    for _ in range(6):
        add_txn(conn, "PYT_BURST", 0)                          # a bot firing 6 at once
    assert flagged(conn) == {"PYT_BURST": 6}


@live
def test_report_gives_the_peak_window(conn):
    for m in [0, 1, 2, 3, 4, 5, 6, 30]:
        add_txn(conn, "PYT_PEAK", m)
    row = run_report(conn, "fraud_alerts").set_index("user_id").loc["PYT_PEAK"]
    assert row["peak_failures_in_window"] == 7 and row["total_failures"] == 8
    assert row["window_start"] == T0 and row["window_end"] == T0 + timedelta(minutes=6)


@live
def test_window_query_matches_selfjoin_and_brute_force_on_random_data(conn):
    rng = random.Random(7)
    events: dict[str, list[float]] = {}
    for u in range(60):
        user = f"PYT_R{u:02d}"
        events[user] = sorted(round(rng.uniform(0, 30), 2) for _ in range(rng.randint(3, 14)))
        for m in events[user]:
            add_txn(conn, user, m)

    def brute(times):     # for each failure, count failures in the 10 minutes up to it (whole seconds)
        secs = [round(t * 60) for t in times]
        return max(sum(1 for b in secs if a - 600 <= b <= a) for a in secs)

    expected = sorted(u for u, t in events.items() if brute(t) > 5)
    window, selfjoin = fraud_users(conn)
    window = [u for u in window if u.startswith("PYT_")]
    selfjoin = [u for u in selfjoin if u.startswith("PYT_")]
    assert window == selfjoin == expected
    assert 0 < len(expected) < 60                               # the random data really tests both sides


# ---------------------------------------------------------------- settlement

@live
def test_settlement_formula_success_only_and_missing_rate(conn):
    conn.execute(text("INSERT INTO merchant_rates (merchant_id, tier, commission_pct) "
                      "VALUES ('PYT_M1', 'GOLD', 0.025)"))
    add_txn(conn, "PYT_U", 0, "SUCCESS", "PYT_M1", "1000.00")
    add_txn(conn, "PYT_U", 1, "SUCCESS", "PYT_M1", "333.33")   # commission 8.33325 -> 8.33
    add_txn(conn, "PYT_U", 2, "FAILED", "PYT_M1", "5000.00")   # not settled (A3)
    add_txn(conn, "PYT_U", 3, "PENDING", "PYT_M1", "7000.00")  # not settled (A3)
    add_txn(conn, "PYT_U", 4, "SUCCESS", "PYT_NORATE", "200.00")

    df = run_report(conn, "settlement").set_index("merchant_id")
    m1 = df.loc["PYT_M1"]
    assert m1["txn_count"] == 2 and m1["gross_inr"] == Decimal("1333.33")
    assert m1["commission_inr"] == Decimal("33.33")            # 25.00 + 8.33
    assert m1["net_settlement_inr"] == Decimal("1300.00")      # 1333.33 - 33.33
    assert m1["settlement_flag"] == "OK"

    missing = df.loc["PYT_NORATE"]
    assert missing["settlement_flag"] == "RATE_MISSING" and missing["gross_inr"] == Decimal("200.00")
    assert missing["net_settlement_inr"] is None                # never pay out on a guessed rate


@live
def test_reconciliation_summary_marks_pending_and_timeout_unreconciled(conn):
    df = run_report(conn, "reconciliation").set_index("gateway_status")
    for status, state in {"SUCCESS": "SETTLED", "FAILED": "NOT_SETTLED",
                          "PENDING": "UNRECONCILED", "TIMEOUT": "UNRECONCILED"}.items():
        if status in df.index:
            assert df.loc[status, "reconciliation_state"] == state


# ---------------------------------------------------------------- report runner

def test_report_runner_fails_loudly_if_the_two_fraud_methods_disagree(tmp_path, caplog):
    from unittest import mock

    import pandas as pd

    from app.analytics import reports

    def fake(conn, name, **_):
        users = {"fraud_alerts": ["U1", "U2"], "fraud_crosscheck": ["U1"]}.get(name, [])
        return pd.DataFrame({"user_id": users})

    engine = mock.MagicMock()
    with mock.patch.object(reports, "run_report", side_effect=fake), pytest.raises(RuntimeError, match="mismatch"):
        reports.run_all(engine, out_dir=tmp_path, s3_bucket="")
    assert any(r.levelname == "ERROR" and "MISMATCH" in r.message for r in caplog.records)
    assert not list(tmp_path.iterdir())                           # no report files written


@live
def test_report_runner_end_to_end(engine, tmp_path):
    from app.analytics.reports import run_all

    out = run_all(engine, out_dir=tmp_path, s3_bucket="")
    assert sorted(p.name.split("__")[1] for p in tmp_path.iterdir()) == \
        ["fraud_alerts.csv", "reconciliation.csv", "settlement.csv"]
    assert out["fraud_users"] == sorted(out["frames"]["fraud_crosscheck"]["user_id"])
