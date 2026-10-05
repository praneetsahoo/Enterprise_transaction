#!/bin/bash
# EC2 first-boot script (user data) for PayRecon — Amazon Linux 2023.
# Installs Python 3.11 + git, clones the public repo, creates a virtualenv, downloads the
# RDS TLS certificate bundle. Contains NO secrets: the DB password is read from SSM at runtime
# by the instance's IAM role.
set -euo pipefail
exec > >(tee -a /var/log/payrecon-bootstrap.log) 2>&1
export HOME=/root
echo "[payrecon] bootstrap started $(date -u +%FT%TZ)"

dnf install -y -q python3.11 python3.11-pip git >/dev/null

APP=/opt/payrecon
mkdir -p "$APP"
git config --global --add safe.directory "$APP/app"
if [ -d "$APP/app/.git" ]; then
  git -C "$APP/app" pull --ff-only
else
  git clone --depth 1 https://github.com/praneetsahoo/Enterprise_transaction.git "$APP/app"
fi

[ -d "$APP/venv" ] || python3.11 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/app/requirements.txt"

# RDS MySQL 8.4 requires TLS; this is AWS's public CA bundle used to verify the server.
curl -fsSL -o "$APP/rds-ca.pem" https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

echo "[payrecon] bootstrap finished $(date -u +%FT%TZ)"
