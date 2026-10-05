"""PayRecon dashboard (Streamlit). Shows what the pipeline and the SQL in sql/ produce.

    streamlit run app/dashboard.py

Read-only views over MySQL, plus one action: upload a file and run the SAME pipeline
(app/pipeline.py) the command line uses. No business logic lives here.
"""
from __future__ import annotations

import hmac
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # so `app.*` imports work under streamlit

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.analytics.reports import run_report  # noqa: E402
from app.config import (FRAUD_MAX_FAILURES, FRAUD_STATUS, FRAUD_WINDOW_MINUTES,  # noqa: E402
                        dashboard_password)
from app.database.connection import get_engine  # noqa: E402
from app.preprocessing.cleaning import TXN_COLUMNS  # noqa: E402

st.set_page_config(page_title="PayRecon", page_icon="💳", layout="wide")


@st.cache_resource
def _logging_ready() -> bool:
    from app.pipeline import setup_logging

    setup_logging()                     # stdout + LOG_FILE (shipped to CloudWatch on EC2)
    return True


_logging_ready()


# ---------------------------------------------------------------- data access (cached 60 s)

@st.cache_data(ttl=60, show_spinner=False)
def report(name: str, **params) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return run_report(conn, name, **params)


@st.cache_data(ttl=60, show_spinner=False)
def query(sql: str, **params) -> pd.DataFrame:
    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params)
        return pd.DataFrame(result.mappings().all(), columns=list(result.keys()))


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Neutralise CSV/formula injection in downloads: a text cell starting with = + - @ would run as a
    formula in Excel. Prefix it with ' — but leave real numbers such as -150.00 alone.
    (The S3 / database DLQ keeps the exact original text; only dashboard downloads are changed.)"""
    def fix(v):
        if isinstance(v, str) and v.startswith(_FORMULA_START):
            try:
                float(v.replace(",", ""))
                return v
            except ValueError:
                return "'" + v
        return v
    # pandas 3 stores text as dtype 'str', not object: check "not numeric" instead
    return df.apply(lambda col: col if pd.api.types.is_numeric_dtype(col) else col.map(fix))


def md_escape(value) -> str:
    """Data values shown inside formatted text must not be able to inject markdown (links, images)."""
    return "".join("\\" + ch if ch in "\\`*_{}[]()<>#+-.!|~" else ch for ch in str(value))


def inr(value) -> str:
    return "—" if value is None or pd.isna(value) else f"₹{float(value):,.2f}"


def as_float(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    """MySQL DECIMAL arrives as Python Decimal; charts need floats."""
    out = df.copy()
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


# ---------------------------------------------------------------- login (only when a password is configured)

def require_login() -> None:
    expected = dashboard_password()
    if not expected or st.session_state.get("authenticated"):
        return
    st.title("PayRecon")
    with st.form("login"):
        given = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if hmac.compare_digest(given.encode(), expected.encode()):     # constant-time comparison
                st.session_state["authenticated"] = True
                st.rerun()
            logging.getLogger("payrecon.dashboard").warning("dashboard: failed sign-in")
            time.sleep(1)                                               # slows down password guessing
            st.error("Wrong password.")
    st.stop()


require_login()

# ---------------------------------------------------------------- header + connection check

st.title("PayRecon — Transaction Reconciliation & Fraud Telemetry")
st.caption("Raw payment dump → Python cleaning (UTC, INR, validation, DLQ) → MySQL → SQL settlement "
           "and sliding-window fraud detection")

try:
    runs = query("SELECT run_id, source_file, status, rows_read, rows_valid, rows_rejected, rows_duplicate, "
                 "rows_inserted, error_message, started_at, finished_at FROM pipeline_runs "
                 "ORDER BY started_at DESC LIMIT 50")
except Exception as exc:                                      # no secrets in the message (URL hides password)
    st.error(f"Cannot reach the database: {type(exc).__name__}. Check DB_HOST / DB_URL.")
    st.stop()

tab_overview, tab_upload, tab_settle, tab_fraud, tab_dlq, tab_runs = st.tabs(
    ["Overview", "Upload & run", "Settlement", "Fraud alerts", "Dead-letter queue", "Pipeline runs"])

# ---------------------------------------------------------------- Overview

with tab_overview:
    st.caption("How this works: every uploaded row is validated in Python (UTC time, INR conversion, 10 rules); "
               "valid rows are loaded once (txn_ref_no is the primary key), rejected rows go to the dead-letter "
               "queue with a reason, and the figures below are computed by SQL in MySQL.")
    recon = report("reconciliation")
    if recon.empty:
        st.info("No transactions loaded yet. Use **Upload & run**.")
    else:
        by_state = as_float(recon, "amount_inr").groupby("reconciliation_state")[["txn_count", "amount_inr"]].sum()
        latest = runs[runs["status"] == "SUCCESS"].head(1)
        c = st.columns(5)
        c[0].metric("Transactions loaded", f"{int(recon['txn_count'].sum()):,}")
        c[1].metric("Settled (SUCCESS)", inr(by_state["amount_inr"].get("SETTLED")))
        c[2].metric("Un-reconciled (PENDING/TIMEOUT)", inr(by_state["amount_inr"].get("UNRECONCILED")),
                    help="Customer may be debited but the merchant is not credited yet")
        c[3].metric("Fraud alerts", len(report("fraud_alerts")))
        if not latest.empty:
            r = latest.iloc[0]
            c[4].metric("Last run rejected", f"{int(r['rows_rejected']) + int(r['rows_duplicate'])} / {int(r['rows_read'])}",
                        help="invalid + duplicate rows sent to the DLQ / rows read")

        left, right = st.columns(2)
        with left:
            st.subheader("Reconciliation by status")
            st.dataframe(recon, hide_index=True, width="stretch",
                         column_config={"amount_inr": st.column_config.NumberColumn("amount (INR)", format="localized")})
        with right:
            st.subheader("Why rows were rejected (last run)")
            if not latest.empty:
                dlq = query("SELECT reason FROM dlq_records WHERE run_id = :r", r=latest.iloc[0]["run_id"])
                if dlq.empty:
                    st.success("No rows rejected.")
                else:
                    reasons = dlq["reason"].str.split("|").explode().value_counts().rename_axis("reason").reset_index()
                    st.altair_chart(alt.Chart(reasons).mark_bar().encode(
                        x=alt.X("count:Q", title="rows"), y=alt.Y("reason:N", sort="-x", title=None, axis=alt.Axis(labelLimit=300)),
                        tooltip=["reason", "count"]), width="stretch")

# ---------------------------------------------------------------- Upload & run

with tab_upload:
    st.subheader("Run the pipeline on a new file")
    st.write("Same code as `python -m app.pipeline`: clean → DLQ → S3 copy → batch load. "
             "Already-loaded `txn_ref_no` values are skipped (primary key), so re-uploading is safe.")
    txn_file = st.file_uploader("Transactions CSV (txn_ref_no, user_id, merchant_id, amount, currency, "
                                "gateway_status, gateway_response_code, created_at)", type="csv")
    rate_file = st.file_uploader("Merchant rates CSV (optional — merchant_id, tier, commission_pct). "
                                 "If omitted, the rates already loaded are used.", type="csv")
    if st.button("Run pipeline", type="primary", disabled=txn_file is None):
        from app.pipeline import run as run_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            def save(upload):
                path = Path(tmp) / Path(upload.name).name           # keep only the file name
                path.write_bytes(upload.getvalue())
                return path
            try:
                with st.spinner("Cleaning, validating and loading…"):
                    summary = run_pipeline(save(txn_file), save(rate_file) if rate_file else None)
            except Exception as exc:
                st.error(f"Run FAILED — nothing was loaded. {type(exc).__name__}: {exc}")
            else:
                st.cache_data.clear()
                st.success(f"Run {summary['run_id']} finished.")
                c = st.columns(5)
                for col, key in zip(c, ["read", "valid", "rejected", "duplicate", "inserted"]):
                    col.metric(key.title(), f"{summary[key]:,}")
                st.caption("read = valid + rejected + duplicate. 'Inserted' excludes rows already in the database.")
                if summary["reject_reasons"]:
                    st.write("Rejected / duplicate rows by reason:")
                    st.dataframe(pd.Series(summary["reject_reasons"], name="rows").rename_axis("reason"))

# ---------------------------------------------------------------- Settlement

with tab_settle:
    st.subheader("Merchant settlement — net = amount − amount × commission_pct (SUCCESS only)")
    st.caption("How this works: SQL joins successful transactions to merchant_rates on merchant_id (LEFT JOIN, so a "
               "merchant without a rate is shown and held, never dropped). Commission is rounded per transaction.")
    settle = as_float(report("settlement"), "gross_inr", "commission_inr", "net_settlement_inr", "commission_pct")
    if settle.empty:
        st.info("Nothing to settle yet.")
    else:
        missing = settle[settle["settlement_flag"] == "RATE_MISSING"]
        if not missing.empty:
            st.warning(f"{len(missing)} merchant(s) have no commission rate and are NOT settled: "
                       f"{', '.join(missing['merchant_id'])} ({inr(missing['gross_inr'].sum())} held).")
        ok = settle[settle["settlement_flag"] == "OK"]
        c = st.columns(3)
        c[0].metric("Gross (settled merchants)", inr(ok["gross_inr"].sum()))
        c[1].metric("Commission", inr(ok["commission_inr"].sum()))
        c[2].metric("Net payable", inr(ok["net_settlement_inr"].sum()))
        st.altair_chart(alt.Chart(ok.head(15)).mark_bar().encode(
            x=alt.X("merchant_id:N", sort="-y", title=None), y=alt.Y("net_settlement_inr:Q", title="net payable (INR)"),
            color=alt.Color("tier:N"), tooltip=["merchant_id", "tier", "commission_pct", "txn_count",
                                                 "gross_inr", "commission_inr", "net_settlement_inr"]),
            width="stretch")
        money = {c: st.column_config.NumberColumn(format="localized") for c in
                 ("gross_inr", "commission_inr", "net_settlement_inr")}
        st.dataframe(settle, hide_index=True, width="stretch",
                     column_config={**money, "commission_pct": st.column_config.NumberColumn(format="%.4f")})
        st.download_button("Download settlement CSV", spreadsheet_safe(settle).to_csv(index=False), "settlement.csv",
                           "text/csv")

# ---------------------------------------------------------------- Fraud

with tab_fraud:
    st.subheader("Fraud telemetry — genuine sliding window")
    st.write(f"Rule: more than **{FRAUD_MAX_FAILURES} {FRAUD_STATUS}** transactions by one user inside "
             f"**any {FRAUD_WINDOW_MINUTES}-minute window** (`RANGE BETWEEN INTERVAL … PRECEDING`, not fixed buckets).")
    st.caption("How this works: for every failed payment, SQL counts the same user's failures in the 10 minutes "
               "ending at it; the busiest window always ends on a failure, so every possible window is checked. "
               "A second query (self-join) must agree on every report run.")
    with st.expander("What-if: change the thresholds (does not change the official rule in config)"):
        w = st.slider("Window (minutes)", 1, 60, FRAUD_WINDOW_MINUTES)
        m = st.slider("Flag when failures are more than", 1, 20, FRAUD_MAX_FAILURES)
    alerts = report("fraud_alerts", window_minutes=w, max_failures=m)
    if alerts.empty:
        st.success("No user exceeds the threshold.")
    else:
        st.dataframe(alerts, hide_index=True, width="stretch")
        user = st.selectbox("Show the evidence for user", alerts["user_id"])
        a = alerts.set_index("user_id").loc[user]
        txns = query("SELECT txn_ref_no, merchant_id, amount_inr, gateway_status, gateway_response_code, "
                     "created_at_utc FROM stg_transactions WHERE user_id = :u ORDER BY created_at_utc", u=user)
        txns["in_peak_window"] = (txns["gateway_status"] == FRAUD_STATUS) & \
            txns["created_at_utc"].between(a["window_start"], a["window_end"])
        st.write(f"**{md_escape(user)}**: {int(a['peak_failures_in_window'])} failures between "
                 f"{a['window_start']} and {a['window_end']} UTC "
                 f"({int(a['window_span_seconds']) // 60} min {int(a['window_span_seconds']) % 60} s).")
        band = pd.DataFrame({"start": [a["window_start"]], "end": [a["window_end"]]})
        points = alt.Chart(as_float(txns, "amount_inr")).mark_circle(size=120).encode(
            x=alt.X("created_at_utc:T", title="time (UTC)"), y=alt.Y("gateway_status:N", title=None),
            color=alt.Color("in_peak_window:N", title="in peak window"),
            tooltip=["txn_ref_no", "merchant_id", "amount_inr", "gateway_status", "created_at_utc"])
        window = alt.Chart(band).mark_rect(opacity=0.15, color="red").encode(x="start:T", x2="end:T")
        # fixed 10-minute clock marks: a bucket-based query would split the window at these lines
        lo = pd.Timestamp(a["window_start"]).floor("10min")
        hi = pd.Timestamp(a["window_end"]).ceil("10min")
        marks = pd.DataFrame({"t": pd.date_range(lo, hi, freq="10min")})
        clock = alt.Chart(marks).mark_rule(strokeDash=[4, 4], color="gray").encode(
            x="t:T", tooltip=[alt.Tooltip("t:T", title="fixed 10-min bucket boundary")])
        st.altair_chart(window + clock + points, width="stretch")
        st.caption("Shaded = peak sliding window. Dashed = fixed 10-minute clock buckets: a bucket-based "
                   "query counts each side separately and can miss a burst that crosses a line.")
        st.dataframe(txns, hide_index=True, width="stretch")

# ---------------------------------------------------------------- DLQ

with tab_dlq:
    st.subheader("Dead-letter queue — rejected rows with the original record")
    st.caption("How this works: nothing is silently dropped. Each rejected row keeps its exact original values and "
               "every reason; copies are also kept in S3 (dlq/) and in the data/dlq folder.")
    if runs.empty:
        st.info("No runs yet.")
    else:
        run_id = st.selectbox("Run", runs["run_id"], key="dlq_run")
        dlq = query("SELECT source_file, source_row, txn_ref_no, reason, raw_record FROM dlq_records "
                    "WHERE run_id = :r ORDER BY source_row", r=run_id)
        if dlq.empty:
            st.success("No rows rejected in this run.")
        else:
            reasons = sorted(dlq["reason"].str.split("|").explode().unique())
            pick = st.multiselect("Filter by reason", reasons)
            if pick:
                dlq = dlq[dlq["reason"].apply(lambda r: any(p in r.split("|") for p in pick))]
            original = pd.json_normalize(dlq["raw_record"].map(json.loads).tolist())
            # MySQL JSON stores keys sorted; show them in the source file's column order
            original = original[[c for c in TXN_COLUMNS if c in original.columns] +
                                [c for c in original.columns if c not in TXN_COLUMNS]]
            table = pd.concat([dlq[["source_file", "source_row", "reason"]].reset_index(drop=True), original], axis=1)
            st.caption(f"{len(table)} row(s). Values are exactly as received in the source file.")
            st.dataframe(table, hide_index=True, width="stretch")
            st.download_button("Download DLQ CSV", spreadsheet_safe(table).to_csv(index=False), f"dlq_{run_id}.csv",
                               "text/csv")

# ---------------------------------------------------------------- Runs

with tab_runs:
    st.subheader("Pipeline runs (audit trail)")
    st.caption("Every run is recorded, including failures. rows_read = valid + rejected + duplicate.")
    st.dataframe(runs, hide_index=True, width="stretch")
