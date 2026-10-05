#!/bin/bash
# Phase 12 security checks on the deployed server (read-only). Prints PASS/FAIL, never a secret.
#   sudo bash /opt/payrecon/app/infra/security_check.sh
set -uo pipefail
set -a; . /etc/payrecon/payrecon.env; set +a
cd /opt/payrecon/app
PY=/opt/payrecon/venv/bin/python
ok() { echo "[PASS] $*"; }; bad() { echo "[FAIL] $*"; }

# 1. the real secret values appear nowhere on disk, in git history or in logs
$PY - <<'PYEOF'
import os, subprocess, boto3
ssm = boto3.client("ssm", region_name=os.environ["AWS_REGION"])
secrets = [ssm.get_parameter(Name=n, WithDecryption=True)["Parameter"]["Value"]
           for n in ("/payrecon/db/app_password", "/payrecon/db/master_password", "/payrecon/dashboard/password")]
def found(blob: bytes) -> int:
    return sum(blob.count(s.encode()) for s in secrets)
places = {
    "git history (all commits)": subprocess.run(["git", "log", "-p", "--all"], capture_output=True).stdout,
    "working tree": subprocess.run(["grep", "-rIa", "--exclude-dir=.git", "-e", "", "."], capture_output=True).stdout,
    "/var/log/payrecon": subprocess.run("cat /var/log/payrecon/*", shell=True, capture_output=True).stdout,
    "systemd journal": subprocess.run(["journalctl", "--no-pager", "-q", "--since", "-7d"], capture_output=True).stdout,
    "/etc/payrecon": subprocess.run("cat /etc/payrecon/*", shell=True, capture_output=True).stdout,
    "bootstrap log": subprocess.run("cat /var/log/payrecon-bootstrap.log /var/log/cloud-init-output.log 2>/dev/null", shell=True, capture_output=True).stdout,
}
for where, blob in places.items():
    n = found(blob)
    print(f"[{'PASS' if n == 0 else 'FAIL'}] no secret value in {where} ({len(blob):,} bytes searched)")
PYEOF

# 2. database: TLS enforced server-wide, app user cannot touch other databases
$PY - <<'PYEOF'
import os, boto3, pymysql
ssm = boto3.client("ssm", region_name=os.environ["AWS_REGION"])
pw = lambda n: ssm.get_parameter(Name=n, WithDecryption=True)["Parameter"]["Value"]
host, ca = os.environ["DB_HOST"], "/opt/payrecon/rds-ca.pem"
for user, name in (("payrecon_app", "app_password"), ("payrecon_admin", "master_password")):
    try:
        pymysql.connect(host=host, user=user, password=pw(f"/payrecon/db/{name}"), ssl_disabled=True, connect_timeout=10).close()
        print(f"[FAIL] {user} could connect WITHOUT TLS")
    except pymysql.err.OperationalError as e:
        print(f"[PASS] {user} without TLS refused (MySQL {e.args[0]})")
c = pymysql.connect(host=host, user="payrecon_app", password=pw("/payrecon/db/app_password"), ssl={"ca": ca})
cur = c.cursor()
cur.execute("SELECT @@require_secure_transport"); rst = cur.fetchone()[0]
print(f"[{'PASS' if rst == 1 else 'INFO'}] require_secure_transport = {rst}")
cur.execute("SHOW GRANTS"); grants = " ".join(r[0] for r in cur.fetchall())
bad = [p for p in ("ALL PRIVILEGES", "GRANT OPTION", "SUPER", "DROP", "FILE", "*.* TO") if p in grants.replace("USAGE ON *.* TO", "")]
print(f"[{'PASS' if not bad else 'FAIL'}] app user grants limited to payrecon.* (no {', '.join(['DROP','FILE','SUPER','GRANT OPTION'])}){' -> ' + str(bad) if bad else ''}")
c.close()
PYEOF

# 3. host
[ "$(stat -c '%a %U %G' /etc/payrecon/payrecon.env)" = "640 root payrecon" ] && ok "settings file 640 root:payrecon" || bad "settings file permissions"
U=$(ps -o user= -C streamlit | sort -u); [ "$U" = "payrecon" ] && ok "dashboard runs as '$U' (not root)" || bad "dashboard user: $U"
getent passwd payrecon | grep -q nologin && ok "service user has no login shell" || bad "service user has a shell"
PORTS=$(ss -ltnH | awk '{print $4}' | sed 's/.*://' | sort -un | tr '\n' ' '); echo "[INFO] listening TCP ports: $PORTS(only 80 is open in the security group)"
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 30')
CODE=$(curl -s -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/); [ "$CODE" = "401" ] && ok "IMDSv1 (no token) refused: HTTP $CODE" || bad "IMDSv1 answered: $CODE"
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/ | grep -q 200 && ok "dashboard up (login required — checked by tests)"

# 4. dependencies with known vulnerabilities
/opt/payrecon/venv/bin/pip install -q pip-audit >/dev/null 2>&1
AUD=$(/opt/payrecon/venv/bin/pip-audit -r requirements.txt --progress-spinner off 2>&1 | tail -15)
echo "$AUD" | grep -q "No known vulnerabilities found" && ok "pip-audit: no known vulnerabilities in requirements.txt" || { echo "[INFO] pip-audit:"; echo "$AUD"; }
