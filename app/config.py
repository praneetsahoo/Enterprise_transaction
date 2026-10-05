"""PayRecon configuration: business rules (assumptions) and environment settings.

Every rule the SME might change lives here, in ONE place, as a named constant.
Environment-specific values (bucket, database host) come from environment variables,
so no credential or server address is ever written in code.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Business rules (hackathon assumptions A1–A6 from the Phase 0 analysis)
# ---------------------------------------------------------------------------

# A1: timestamps that carry no time zone are Indian Standard Time (UPI data).
SOURCE_TIMEZONE = "Asia/Kolkata"

# Fixed conversion dictionary (the brief forbids an external currency API).
# Value = how many INR one unit of that currency is worth. Hackathon rates, deterministic.
FX_TO_INR: dict[str, float] = {
    "INR": 1.00,
    "USD": 83.00,
    "EUR": 90.00,
    "GBP": 105.00,
    "AED": 22.60,
    "SGD": 61.50,
}

# How messy currency values in the dump map onto the codes above.
CURRENCY_ALIASES: dict[str, str] = {
    "₹": "INR", "RS": "INR", "RS.": "INR", "RUPEE": "INR", "RUPEES": "INR",
    "$": "USD", "US$": "USD", "DOLLAR": "USD",
    "€": "EUR", "EURO": "EUR",
    "£": "GBP", "POUND": "GBP",
    "DIRHAM": "AED",
}

# Gateway statuses we accept (after upper-casing).
VALID_STATUSES = {"SUCCESS", "FAILED", "PENDING", "TIMEOUT"}

# A3: only successful payments are settled to merchants.
SETTLEMENT_STATUS = "SUCCESS"

# A4: the fraud rule counts FAILED only; PENDING/TIMEOUT are "un-reconciled" (debited, not credited).
FRAUD_STATUS = "FAILED"
FRAUD_MAX_FAILURES = 5            # flag when failures are MORE THAN this number ...
FRAUD_WINDOW_MINUTES = 10         # ... inside any window of this many minutes

# Batch size for inserts into MySQL.
BATCH_SIZE = 1000

# ---------------------------------------------------------------------------
# Local folders (used for development and as a local copy of the DLQ)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DLQ_DIR = DATA_DIR / "dlq"
SQL_DIR = PROJECT_ROOT / "sql"

# ---------------------------------------------------------------------------
# Environment settings (set on EC2 by the deployment; never hard-coded)
# ---------------------------------------------------------------------------
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
S3_BUCKET = os.getenv("S3_BUCKET", "")          # empty = run without S3 (local development)
LOG_FILE = os.getenv("LOG_FILE", "")            # set on EC2; shipped to CloudWatch


@lru_cache
def database_url():
    """Where the MySQL database is.

    Local development: DB_URL, e.g. mysql+pymysql://user:pass@127.0.0.1/payrecon
    On AWS: DB_HOST + DB_PASSWORD_PARAM — the password is read from SSM Parameter Store
    at runtime using the EC2 instance's IAM role, so it never appears in code or files.
    """
    if os.getenv("DB_URL"):
        return os.environ["DB_URL"]
    if not os.getenv("DB_HOST"):
        raise RuntimeError("Database not configured: set DB_URL (local) or DB_HOST (AWS).")

    import boto3
    from sqlalchemy.engine import URL

    password = boto3.client("ssm", region_name=AWS_REGION).get_parameter(
        Name=os.environ["DB_PASSWORD_PARAM"], WithDecryption=True)["Parameter"]["Value"]
    return URL.create(
        "mysql+pymysql",
        username=os.getenv("DB_USER", "payrecon_app"),
        password=password,                      # URL.create escapes special characters
        host=os.environ["DB_HOST"],
        port=3306,
        database=os.getenv("DB_NAME", "payrecon"),
        query={"ssl_ca": os.getenv("DB_SSL_CA", "/opt/payrecon/rds-ca.pem")},
    )
