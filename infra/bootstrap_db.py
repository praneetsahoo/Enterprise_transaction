"""Create the least-privilege MySQL user the pipeline connects as (run once, on EC2).

RDS creates the `payrecon` database and an admin user. The pipeline must NOT use the admin, so
this script creates `payrecon_app`, which:
  * can only touch the `payrecon` database (not the whole server),
  * must connect over TLS (REQUIRE SSL).
It also sets REQUIRE SSL on the admin user itself (found in the Phase 12 security review).
Both passwords are read from SSM Parameter Store with the EC2 IAM role and never printed.

    DB_HOST=<rds endpoint> python infra/bootstrap_db.py
"""
from __future__ import annotations

import os
import sys

import boto3
import pymysql

REGION = os.getenv("AWS_REGION", "ap-southeast-2")
HOST = os.environ["DB_HOST"]
CA = os.getenv("DB_SSL_CA", "/opt/payrecon/rds-ca.pem")
APP_USER = "payrecon_app"
PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW"


def secret(name: str) -> str:
    return boto3.client("ssm", region_name=REGION).get_parameter(
        Name=f"/payrecon/db/{name}", WithDecryption=True)["Parameter"]["Value"]


def main() -> int:
    conn = pymysql.connect(host=HOST, user="payrecon_admin", password=secret("master_password"),
                           ssl={"ca": CA}, connect_timeout=15, autocommit=True)
    app_password = secret("app_password")
    with conn.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS payrecon CHARACTER SET utf8mb4")
        cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s REQUIRE SSL", (APP_USER, app_password))
        cur.execute("ALTER USER %s@'%%' IDENTIFIED BY %s REQUIRE SSL", (APP_USER, app_password))
        cur.execute(f"GRANT {PRIVILEGES} ON payrecon.* TO %s@'%%'", (APP_USER,))
        # the admin too must use TLS (RDS default parameter group has require_secure_transport = 0)
        cur.execute("ALTER USER CURRENT_USER() REQUIRE SSL")
        cur.execute("SHOW GRANTS FOR %s@'%%'", (APP_USER,))
        grants = [row[0] for row in cur.fetchall()]
    conn.close()
    print("payrecon_app ready (TLS required). Grants:", *grants, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
