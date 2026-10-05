"""Cleaning and validation of the raw payment dump and merchant rates.

Every rule is a small pure function (value in -> clean value or None out), so each one is unit
tested on its own. `clean_transactions` applies them row by row and splits the file into:

    clean      -> loaded into MySQL stg_transactions
    rejected   -> DLQ, original record + reason codes (one row can have several reasons)
    duplicates -> DLQ, a later copy of a txn_ref_no that was already accepted (rule A5)

Invariant (checked by tests): rows_read == clean + rejected + duplicates. Nothing is lost.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import CURRENCY_ALIASES, FX_TO_INR, MAX_FILE_MB, SOURCE_TIMEZONE, VALID_STATUSES

TXN_COLUMNS = ["txn_ref_no", "user_id", "merchant_id", "amount", "currency",
               "gateway_status", "gateway_response_code", "created_at"]
RATE_COLUMNS = ["merchant_id", "tier", "commission_pct"]

MAX_LEN = {"txn_ref_no": 64, "user_id": 32, "merchant_id": 32}
MAX_AMOUNT = Decimal("1000000000")          # 100 crore per transaction: anything above is a data error
PAISA = Decimal("0.01")
SOURCE_TZ = ZoneInfo(SOURCE_TIMEZONE)
_EPOCH = re.compile(r"^\d{10}(\d{3})?$")   # 10 digits = seconds, 13 digits = milliseconds
_NAIVE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S")


class SchemaError(ValueError):
    """The file itself is unusable (missing columns) — the whole run fails, nothing is loaded."""


# --------------------------------------------------------------------------- field rules

def parse_amount(value: str) -> Decimal | None:
    """'1,250.50' -> Decimal('1250.50'); None if not a finite number."""
    try:
        amount = Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    return amount if amount.is_finite() else None          # rejects 'NaN', 'Infinity'


def normalize_currency(value: str) -> str | None:
    """'₹', 'Rs.', 'inr' -> 'INR'. Fixed dictionary only; None if unknown."""
    key = value.strip().upper()
    code = CURRENCY_ALIASES.get(key, key)
    return code if code in FX_TO_INR else None


def to_inr(amount: Decimal, currency: str) -> tuple[Decimal, Decimal]:
    """Return (fx_rate, amount_in_inr rounded to the paisa, half-up)."""
    rate = Decimal(str(FX_TO_INR[currency]))
    return rate, (amount * rate).quantize(PAISA, rounding=ROUND_HALF_UP)


def parse_timestamp(value: str) -> datetime | None:
    """Any supported format -> naive datetime in UTC (what MySQL DATETIME stores).

    Supported: epoch seconds / milliseconds, ISO 8601 with 'Z' or an offset, and zone-less
    'YYYY-MM-DD HH:MM:SS' / 'DD/MM/YYYY HH:MM:SS', which are read as IST (rule A1).
    Impossible dates (31/02) and years outside 2000–2100 return None.
    """
    text = value.strip()
    if not text:
        return None
    moment: datetime | None = None
    if _EPOCH.match(text):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(text)            # handles 'Z' and '+05:30'
            moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=SOURCE_TZ)
        except ValueError:
            for fmt in _NAIVE_FORMATS:
                try:
                    moment = datetime.strptime(text, fmt).replace(tzinfo=SOURCE_TZ)
                    break
                except ValueError:
                    continue
    if moment is None or not 2000 <= moment.year <= 2100:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_status(value: str) -> str | None:
    status = value.strip().upper()
    return status if status in VALID_STATUSES else None


def normalize_commission(value: str) -> Decimal | None:
    """Rule A2: 0.025 stays 0.025; 2.5 (a percentage) becomes 0.025. None if invalid."""
    rate = parse_amount(value)
    if rate is None or rate < 0:
        return None
    if rate > 1:
        rate = rate / 100
    return rate.quantize(Decimal("0.000001")) if rate < 1 else None


# --------------------------------------------------------------------------- file level

def read_csv(path, columns: list[str]) -> pd.DataFrame:
    """Read every value as TEXT, exactly as written (no type guessing, no NaN).

    File-level problems (not UTF-8 text, empty, too big, required column missing) raise SchemaError:
    the run fails and nothing is loaded. A single line with the wrong number of fields does NOT
    fail the file: it gets an extra `raw_line` value (the line re-written as CSV) and the cleaners
    send it to the DLQ as MALFORMED_ROW.
    """
    path = Path(path)
    size_mb = path.stat().st_size / 1_000_000
    if size_mb > MAX_FILE_MB:
        raise SchemaError(f"{path.name}: file is {size_mb:,.0f} MB, limit is {MAX_FILE_MB} MB")
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header or not any(h.strip() for h in header):
                raise SchemaError(f"{path.name}: file is empty (no header row)")
            header = [h.strip().lower() for h in header]
            missing = [c for c in columns if c not in header]
            if missing:
                raise SchemaError(f"{path.name}: missing column(s) {missing}")
            rows, malformed = [], False
            for fields in reader:
                if not fields or not any(x.strip() for x in fields):
                    continue                                        # blank line
                record = dict(zip(header, fields + [""] * (len(header) - len(fields))))
                row = {"source_row": reader.line_num, **{c: record[c] for c in columns}}
                if len(fields) != len(header):
                    buf = io.StringIO()
                    csv.writer(buf, lineterminator="").writerow(fields)
                    row["raw_line"] = buf.getvalue()
                    malformed = True
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{path.name}: not a UTF-8 text/CSV file ({exc.reason} at byte {exc.start})") from None
    except csv.Error as exc:
        raise SchemaError(f"{path.name}: unreadable CSV ({exc})") from None
    out_cols = ["source_row", *columns] + (["raw_line"] if malformed else [])
    return pd.DataFrame(rows, columns=out_cols, dtype=object).fillna("")


def validate_transaction(row: dict) -> tuple[dict | None, list[str]]:
    """One raw row -> (clean record, []) or (None, [reason codes])."""
    reasons: list[str] = []
    ref, user, merchant = (row[k].strip() for k in ("txn_ref_no", "user_id", "merchant_id"))
    if not ref:
        reasons.append("MISSING_TXN_REF")
    if not user:
        reasons.append("MISSING_USER_ID")
    if not merchant:
        reasons.append("MISSING_MERCHANT_ID")
    for field, value in (("txn_ref_no", ref), ("user_id", user), ("merchant_id", merchant)):
        if len(value) > MAX_LEN[field]:
            reasons.append(f"{field.upper()}_TOO_LONG")

    amount = parse_amount(row["amount"])
    if amount is None:
        reasons.append("AMOUNT_NOT_NUMERIC")
    elif amount <= 0:
        reasons.append("AMOUNT_NOT_POSITIVE")
    elif amount > MAX_AMOUNT:
        reasons.append("AMOUNT_OUT_OF_RANGE")

    currency = normalize_currency(row["currency"])
    if currency is None:
        reasons.append("UNKNOWN_CURRENCY")
    created = parse_timestamp(row["created_at"])
    if created is None:
        reasons.append("BAD_TIMESTAMP")
    status = normalize_status(row["gateway_status"])
    if status is None:
        reasons.append("UNKNOWN_STATUS")

    if reasons:
        return None, reasons
    fx_rate, amount_inr = to_inr(amount.quantize(PAISA, rounding=ROUND_HALF_UP), currency)
    return {
        "txn_ref_no": ref, "user_id": user, "merchant_id": merchant,
        "amount_original": amount.quantize(PAISA, rounding=ROUND_HALF_UP),
        "currency_original": currency, "fx_rate_to_inr": fx_rate, "amount_inr": amount_inr,
        "gateway_status": status,
        "gateway_response_code": row["gateway_response_code"].strip() or None,
        "created_at_utc": created, "created_at_raw": row["created_at"].strip(),
    }, []


def clean_transactions(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split the raw dump into (clean, rejected, duplicates). Raw rows keep their original text."""
    clean, rejected, duplicates = [], [], []
    accepted: dict[str, dict] = {}                       # txn_ref_no -> original raw row
    for row in raw.to_dict("records"):
        if row.get("raw_line"):                          # wrong number of fields: never guess
            rejected.append({**row, "reason": "MALFORMED_ROW"})
            continue
        record, reasons = validate_transaction(row)
        if reasons:
            rejected.append({**row, "reason": "|".join(reasons)})
        elif record["txn_ref_no"] in accepted:          # A5: first valid copy wins
            first = accepted[record["txn_ref_no"]]
            same = all(first[c] == row[c] for c in TXN_COLUMNS)
            duplicates.append({**row, "reason": "DUPLICATE_TXN_REF_EXACT" if same
                               else "DUPLICATE_TXN_REF_CONFLICT"})
        else:
            accepted[record["txn_ref_no"]] = row
            clean.append({"source_row": row["source_row"], **record})
    dlq_cols = ["source_row", *TXN_COLUMNS, "reason"] + (["raw_line"] if "raw_line" in raw.columns else [])
    # dtype=object keeps Python values as they are (None stays None, Decimal stays Decimal);
    # pandas 3 would otherwise turn None into NaN in text columns.
    clean_cols = ["source_row", "txn_ref_no", "user_id", "merchant_id", "amount_original",
                  "currency_original", "fx_rate_to_inr", "amount_inr", "gateway_status",
                  "gateway_response_code", "created_at_utc", "created_at_raw"]
    return (pd.DataFrame(clean, columns=clean_cols, dtype=object),
            pd.DataFrame(rejected, columns=dlq_cols, dtype=object),
            pd.DataFrame(duplicates, columns=dlq_cols, dtype=object))


def clean_rates(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Merchant rates -> (clean, rejected). Percentages are converted to fractions (A2)."""
    clean, rejected, seen = [], [], set()
    for row in raw.to_dict("records"):
        if row.get("raw_line"):
            rejected.append({**row, "reason": "MALFORMED_ROW"})
            continue
        merchant = row["merchant_id"].strip()
        rate = normalize_commission(row["commission_pct"])
        reasons = ([] if merchant else ["MISSING_MERCHANT_ID"]) + ([] if rate is not None else ["BAD_COMMISSION"])
        if merchant in seen:
            reasons.append("DUPLICATE_MERCHANT_ID")
        if reasons:
            rejected.append({**row, "reason": "|".join(reasons)})
            continue
        seen.add(merchant)
        clean.append({"merchant_id": merchant, "tier": row["tier"].strip().upper() or None,
                      "commission_pct": rate})
    return (pd.DataFrame(clean, columns=["merchant_id", "tier", "commission_pct"], dtype=object),
            pd.DataFrame(rejected, columns=["source_row", *RATE_COLUMNS, "reason"]
                         + (["raw_line"] if "raw_line" in raw.columns else []), dtype=object))
