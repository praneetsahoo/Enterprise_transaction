"""Phase 3 connectivity test — run ON the EC2 instance (via SSM Run Command).

Proves each connection in the architecture works, and that the security controls actually
block what they should:

  1. SSM        the instance role can read the app DB password
  2. RDS        TLS connection as the least-privilege user; MySQL 8 sliding-window syntax works
  3. RDS        a connection WITHOUT TLS is refused (REQUIRE SSL)
  4. RDS        endpoint resolves to a PRIVATE address (10.30.x.x) — not reachable from the internet
  5. S3         write + read inside raw/ works
  6. S3         write OUTSIDE raw/processed/dlq is DENIED (least privilege)
  7. CloudWatch the instance can write to /payrecon/pipeline

    DB_HOST=<endpoint> S3_BUCKET=<bucket> python infra/check_connectivity.py
"""
from __future__ import annotations

import os
import socket
import sys
import time

import boto3
import pymysql
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_REGION", "ap-southeast-2")
HOST = os.environ["DB_HOST"]
BUCKET = os.environ["S3_BUCKET"]
CA = os.getenv("DB_SSL_CA", "/opt/payrecon/rds-ca.pem")
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}", flush=True)


def main() -> int:
    # 1. SSM
    password = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/payrecon/db/app_password", WithDecryption=True)["Parameter"]["Value"]
    check(len(password) == 32, "SSM: app DB password readable by the instance role (value not printed)")

    # 2. RDS over TLS as the app user + MySQL 8 sliding-window syntax
    conn = pymysql.connect(host=HOST, user="payrecon_app", password=password, database="payrecon",
                           ssl={"ca": CA}, connect_timeout=15)
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_USER(), VERSION()")
        user, version = cur.fetchone()
        cur.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        cipher = cur.fetchone()[1]
        cur.execute("""
            SELECT t, COUNT(*) OVER (ORDER BY t RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW)
            FROM (SELECT TIMESTAMP('2026-10-01 10:00:00') AS t
                  UNION ALL SELECT TIMESTAMP('2026-10-01 10:09:00')
                  UNION ALL SELECT TIMESTAMP('2026-10-01 10:20:00')) x ORDER BY t""")
        window_counts = [row[1] for row in cur.fetchall()]
    conn.close()
    check(user.startswith("payrecon_app@") and bool(cipher),
          f"RDS: connected as {user} over TLS ({cipher}), MySQL {version}")
    check(window_counts == [1, 2, 1],
          f"RDS: time-based sliding window (RANGE ... INTERVAL 10 MINUTE) works -> {window_counts}")

    # 3. RDS without TLS must be refused
    try:
        pymysql.connect(host=HOST, user="payrecon_app", password=password, database="payrecon",
                        ssl_disabled=True, connect_timeout=15).close()
        check(False, "RDS: connection without TLS was ALLOWED (should be refused)")
    except pymysql.err.OperationalError as exc:
        check(exc.args[0] in (1045, 3159), f"RDS: connection without TLS refused (MySQL error {exc.args[0]})")

    # 4. RDS endpoint is private
    ip = socket.gethostbyname(HOST)
    check(ip.startswith("10.30."), f"RDS: endpoint resolves to private address {ip}")

    # 5./6. S3 least privilege
    s3 = boto3.client("s3", region_name=REGION)
    key = "raw/_connectivity_check/hello.txt"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"payrecon connectivity check", ServerSideEncryption="AES256")
    check(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"payrecon connectivity check",
          f"S3: write + read in raw/ works (s3://{BUCKET}/{key})")
    try:
        s3.put_object(Bucket=BUCKET, Key="not-allowed/test.txt", Body=b"x")
        check(False, "S3: write outside allowed folders was ALLOWED (should be denied)")
    except ClientError as exc:
        check(exc.response["Error"]["Code"] == "AccessDenied", "S3: write outside raw/processed/dlq is denied")

    # 7. CloudWatch Logs
    logs = boto3.client("logs", region_name=REGION)
    stream = f"connectivity-{int(time.time())}"
    logs.create_log_stream(logGroupName="/payrecon/pipeline", logStreamName=stream)
    logs.put_log_events(logGroupName="/payrecon/pipeline", logStreamName=stream,
                        logEvents=[{"timestamp": int(time.time() * 1000), "message": "INFO connectivity check passed"}])
    check(True, f"CloudWatch: wrote to /payrecon/pipeline (stream {stream})")

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
