#!/bin/bash
# Phase 11 failure drills on the deployed server. Each drill breaks ONE setting for ONE run
# (environment override) — nothing in AWS is changed. Expectation for every drill:
#   exit code 1 (except D7), an ERROR line in the log (-> CloudWatch alarm), staging row count unchanged.
#   sudo bash /opt/payrecon/app/infra/failure_drills.sh
set -uo pipefail
set -a; . /etc/payrecon/payrecon.env; set +a
cd /opt/payrecon/app
PY=/opt/payrecon/venv/bin/python
as_app() { runuser -u payrecon -- env HOME=/var/lib/payrecon "$@"; }
count() { as_app env $(grep -v '^#' /etc/payrecon/payrecon.env | xargs) $PY -c "
from sqlalchemy import text; from app.database.connection import get_engine
with get_engine().connect() as c: print(c.execute(text('SELECT COUNT(*) FROM stg_transactions')).scalar())"; }
SAMPLE=data/raw/raw_payment_dump.csv
TMP=$(mktemp -d); chmod 755 "$TMP"
printf '\x89PNG\r\n\x1a\n\x00\xff garbage' > "$TMP/garbage.csv"
{ head -20 "$SAMPLE"; echo 'TXN9999999999,U1,M001,100,INR,SUCCESS,00,2026-10-01T04:30:00Z,EXTRA_FIELD'; } > "$TMP/one_bad_line.csv"
chmod 644 "$TMP"/*.csv

drill() {   # name, expected exit, env overrides..., -- pipeline args...
  local name=$1 expect=$2; shift 2
  local envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  local out code
  out=$(as_app env $(grep -v '^#' /etc/payrecon/payrecon.env | xargs) "${envs[@]}" $PY -m app.pipeline "$@" 2>&1); code=$?
  local err; err=$(echo "$out" | grep -m1 -E ' ERROR ' | sed -E 's/^[0-9-]+ [0-9:,]+ //' | cut -c1-150)
  [ "$code" = "$expect" ] && r=PASS || r=FAIL
  echo "[$r] $name -> exit $code | ${err:-$(echo "$out" | grep -m1 -oE 'MALFORMED_ROW[^,]*|"inserted": [0-9]+' | head -1)}"
}

BEFORE=$(count); echo "staging rows before drills: $BEFORE"
drill "D1 wrong database host (DNS)"            1 DB_HOST=payrecon-db-wrong.c10guuuqcerb.ap-southeast-2.rds.amazonaws.com -- --transactions $SAMPLE --rates data/raw/merchant_rates.csv
drill "D2 database not reachable (no route)"    1 DB_HOST=10.30.11.250                                   -- --transactions $SAMPLE
drill "D3 wrong database password"              1 "DB_URL=mysql+pymysql://payrecon_app:wrong-password@$DB_HOST/payrecon?ssl_ca=/opt/payrecon/rds-ca.pem" -- --transactions $SAMPLE
drill "D4 IAM: other project's SSM secret"      1 DB_PASSWORD_PARAM=/opsintel2/db/app_password             -- --transactions $SAMPLE
drill "D5 IAM: write to another bucket"         1 S3_BUCKET=opsintel-data-748348797173                     -- --transactions $SAMPLE
drill "D6 corrupt (binary) upload"              1                                                          -- --transactions "$TMP/garbage.csv"
drill "D7 one malformed line in a good file"    0                                                          -- --transactions "$TMP/one_bad_line.csv"
drill "D8 input file missing in S3"             1                                                          -- --s3-transactions raw/does-not-exist.csv
AFTER=$(count); echo "staging rows after drills:  $AFTER"
[ "$BEFORE" = "$AFTER" ] && echo "[PASS] no rows added or lost by any drill" || echo "[FAIL] staging changed: $BEFORE -> $AFTER"
rm -rf "$TMP"
