"""Generate realistic, deliberately messy sample input files — deterministically.

Same seed -> byte-identical files every time (so tests and demos are repeatable).

Writes:
  data/raw/raw_payment_dump.csv   transactions with mixed formats and planted problems
  data/raw/merchant_rates.csv     merchant commission rates
  data/raw/planted_cases.json     what was planted, so tests can check the pipeline finds it

Planted problems:  mixed timestamp formats/time zones, mixed currency spellings, amount <= 0,
non-numeric amounts, missing txn_ref_no, unknown currency, invalid timestamp, unknown status,
missing user_id, exact duplicates, same txn_ref_no with a different status, a merchant with
no rate, commission given as a percentage instead of a fraction.

Planted fraud (the three cases from the brief + a bucket trap):
  U9001  6 FAILED within 8 minutes                 -> must be flagged
  U9002  exactly 5 FAILED within 10 minutes        -> must NOT be flagged
  U9003  6 FAILED spread over 50 minutes           -> must NOT be flagged
  U9004  3 FAILED at :08 + 3 at :11 (crosses a 10-minute clock bucket) -> must be flagged

Usage:  python scripts/generate_sample_data.py [--rows 2000] [--seed 42]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
COLUMNS = ["txn_ref_no", "user_id", "merchant_id", "amount", "currency",
           "gateway_status", "gateway_response_code", "created_at"]

CURRENCY_SPELLINGS = {   # how each currency shows up in a messy dump
    "INR": ["INR", "inr", "₹", "Rs", "Rs."],
    "USD": ["USD", "usd", "$", "US$"],
    "EUR": ["EUR", "eur", "€"],
    "GBP": ["GBP", "£"],
    "AED": ["AED", "aed"],
    "SGD": ["SGD"],
}
CURRENCY_WEIGHTS = {"INR": 0.80, "USD": 0.08, "EUR": 0.04, "GBP": 0.03, "AED": 0.03, "SGD": 0.02}
STATUS_WEIGHTS = {"SUCCESS": 0.80, "FAILED": 0.12, "PENDING": 0.04, "TIMEOUT": 0.04}
RESPONSE_CODES = {"SUCCESS": "00", "FAILED": "U30", "PENDING": "01", "TIMEOUT": "U68"}


def pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def format_timestamp(rng: random.Random, moment: datetime) -> str:
    """Render one UTC moment in one of six real-world formats."""
    style = rng.choice(["iso_z", "iso_offset", "naive_ist", "ddmmyyyy_ist", "epoch_s", "epoch_ms"])
    ist = moment.astimezone(IST)
    if style == "iso_z":
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    if style == "iso_offset":
        return ist.isoformat()                                   # 2026-10-01T09:15:00+05:30
    if style == "naive_ist":
        return ist.strftime("%Y-%m-%d %H:%M:%S")                 # no zone -> IST by rule A1
    if style == "ddmmyyyy_ist":
        return ist.strftime("%d/%m/%Y %H:%M:%S")
    if style == "epoch_s":
        return str(int(moment.timestamp()))
    return str(int(moment.timestamp() * 1000))                   # epoch milliseconds


def make_row(rng, n: int, user: str, merchant: str, status: str, moment: datetime) -> dict:
    currency = pick(rng, CURRENCY_WEIGHTS)
    amount = round(rng.uniform(20, 5000) if currency == "INR" else rng.uniform(1, 80), 2)
    return {
        "txn_ref_no": f"TXN{n:010d}",
        "user_id": user,
        "merchant_id": merchant,
        "amount": f"{amount:.2f}",
        "currency": rng.choice(CURRENCY_SPELLINGS[currency]),
        "gateway_status": rng.choice([status, status.lower(), status.title()]),
        "gateway_response_code": RESPONSE_CODES[status],
        "created_at": format_timestamp(rng, moment),
    }


# Each breaker damages one valid row in a way the pipeline must catch.
BREAKERS = [
    ("missing txn_ref_no", lambda r: r.update(txn_ref_no="")),
    ("blank txn_ref_no", lambda r: r.update(txn_ref_no="   ")),
    ("zero amount", lambda r: r.update(amount="0")),
    ("negative amount", lambda r: r.update(amount="-150.00")),
    ("non-numeric amount", lambda r: r.update(amount="abc")),
    ("unknown currency", lambda r: r.update(currency="XYZ")),
    ("invalid timestamp", lambda r: r.update(created_at="not-a-date")),
    ("impossible date", lambda r: r.update(created_at="31/02/2026 10:00:00")),
    ("unknown status", lambda r: r.update(gateway_status="REVERSED")),
    ("missing user_id", lambda r: r.update(user_id="")),
]


def fraud_rows(rng, start_n: int) -> tuple[list[dict], dict]:
    """The planted sliding-window cases (all rows themselves are valid)."""
    base = START + timedelta(days=1, hours=5)
    plans = {
        "U9001": [0, 1, 3, 4, 6, 8],              # 6 failures inside 8 minutes
        "U9002": [0, 2, 4, 6, 9],                  # exactly 5 inside 10 minutes
        "U9003": [0, 10, 20, 30, 40, 50],          # 6 failures, 10 minutes apart
        "U9004": [8, 8.5, 9, 11, 11.5, 12],        # 3 before 10:10, 3 after: crosses a bucket
    }
    rows, n = [], start_n
    for user, minutes in plans.items():
        for m in minutes:
            rows.append(make_row(rng, n, user, f"M{rng.randint(1, 40):03d}", "FAILED",
                                 base + timedelta(minutes=m)))
            n += 1
    expected = {"flagged": ["U9001", "U9004"], "not_flagged": ["U9002", "U9003"]}
    return rows, expected


def generate(rows: int, seed: int) -> tuple[list[dict], list[dict], dict]:
    rng = random.Random(seed)
    users = [f"U{i:04d}" for i in range(1, 301)]
    merchants = [f"M{i:03d}" for i in range(1, 41)] + ["M099"]        # M099 has no rate
    data = []
    for n in range(1, rows + 1):
        moment = START + timedelta(seconds=rng.randint(0, 3 * 24 * 3600))
        # ordinary users fail rarely and far apart, so only planted users trip the fraud rule
        data.append(make_row(rng, n, rng.choice(users), rng.choice(merchants),
                             pick(rng, STATUS_WEIGHTS), moment))

    broken = {}
    for idx in rng.sample(range(len(data)), k=int(rows * 0.05)):          # ~5% invalid rows
        label, damage = BREAKERS[rng.randrange(len(BREAKERS))]
        damage(data[idx])
        broken[label] = broken.get(label, 0) + 1

    fraud, fraud_expected = fraud_rows(rng, rows + 1)
    data += fraud

    exact_dups = [dict(data[i]) for i in rng.sample(range(rows), k=int(rows * 0.01))]
    changed_dups = []
    for i in rng.sample(range(rows), k=int(rows * 0.005)):
        dup = dict(data[i])
        dup["gateway_status"], dup["gateway_response_code"] = "SUCCESS", "00"   # a later retry
        changed_dups.append(dup)
    data += exact_dups + changed_dups
    rng.shuffle(data)

    rates = []
    for i in range(1, 41):
        tier = rng.choice(["GOLD", "SILVER", "BRONZE"])
        pct = {"GOLD": rng.uniform(0.010, 0.015), "SILVER": rng.uniform(0.018, 0.022),
               "BRONZE": rng.uniform(0.025, 0.030)}[tier]
        value = f"{pct:.4f}" if i % 13 else f"{pct * 100:.2f}"                # a few as percent
        rates.append({"merchant_id": f"M{i:03d}", "tier": tier, "commission_pct": value})

    manifest = {
        "seed": seed, "base_rows": rows, "total_rows": len(data),
        "planted_invalid": broken, "exact_duplicates": len(exact_dups),
        "duplicates_with_changed_status": len(changed_dups),
        "merchant_without_rate": "M099",
        "rates_given_as_percent": [r["merchant_id"] for r in rates if float(r["commission_pct"]) > 1],
        "fraud": fraud_expected,
    }
    return data, rates, manifest


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=RAW_DIR)
    args = parser.parse_args()

    data, rates, manifest = generate(args.rows, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "raw_payment_dump.csv", data, COLUMNS)
    write_csv(args.out / "merchant_rates.csv", rates, ["merchant_id", "tier", "commission_pct"])
    (args.out / "planted_cases.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {len(data)} transactions and {len(rates)} merchant rates to {args.out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
