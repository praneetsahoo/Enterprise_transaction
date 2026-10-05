#!/bin/bash
# Deploy / update PayRecon on the EC2 instance. Idempotent: safe to run again after every git push.
# Run as root (via SSM Run Command):
#   DB_HOST=<rds endpoint> S3_BUCKET=<bucket> bash /opt/payrecon/app/infra/deploy.sh
# Never deletes data, never touches the database schema destructively, holds no secrets
# (passwords are read from SSM Parameter Store at runtime by the instance role).
set -euo pipefail
: "${DB_HOST:?set DB_HOST}" "${S3_BUCKET:?set S3_BUCKET}"
export HOME=/root
APP=/opt/payrecon
echo "[deploy] $(date -u +%FT%TZ) start"

# 1. code + dependencies
git config --global --add safe.directory "$APP/app"
git -C "$APP/app" pull --ff-only
"$APP/venv/bin/pip" install -q -r "$APP/app/requirements.txt"
echo "[deploy] code at $(git -C "$APP/app" rev-parse --short HEAD)"

# 2. service user (no login shell) + folders it may write
id payrecon >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/payrecon --shell /sbin/nologin payrecon
install -d -o payrecon -g payrecon -m 750 /var/log/payrecon
chown -R payrecon:payrecon "$APP/app/data"

# 3. settings (no secrets) — readable by root and the payrecon group only
install -d -o root -g payrecon -m 750 /etc/payrecon
cat > /etc/payrecon/payrecon.env.tmp <<ENV
AWS_REGION=${AWS_REGION:-ap-southeast-2}
DB_HOST=$DB_HOST
S3_BUCKET=$S3_BUCKET
LOG_FILE=/var/log/payrecon/pipeline.log
DASHBOARD_PASSWORD_PARAM=/payrecon/dashboard/password
ENV
install -o root -g payrecon -m 640 /etc/payrecon/payrecon.env.tmp /etc/payrecon/payrecon.env
rm -f /etc/payrecon/payrecon.env.tmp
chmod 755 "$APP/app/infra/run_pipeline.sh"

# 4. schema (CREATE TABLE IF NOT EXISTS only)
runuser -u payrecon -- env $(grep -v '^#' /etc/payrecon/payrecon.env | xargs) HOME=/var/lib/payrecon \
  bash -c "cd $APP/app && $APP/venv/bin/python -m app.database.schema"

# 5. CloudWatch agent: ship /var/log/payrecon/*.log
rpm -q amazon-cloudwatch-agent >/dev/null 2>&1 || dnf install -y -q amazon-cloudwatch-agent
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
  -c "file:$APP/app/infra/cloudwatch-agent.json" >/dev/null
echo "[deploy] cloudwatch agent: $(/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status | grep -o '"status": "[a-z]*"' | head -1)"

# 6. dashboard service
install -m 644 "$APP/app/infra/systemd/payrecon-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q payrecon-dashboard
systemctl restart payrecon-dashboard
for i in $(seq 1 30); do
  curl -fsS -o /dev/null http://127.0.0.1/_stcore/health 2>/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1/_stcore/health >/dev/null && echo "[deploy] dashboard healthy on port 80" \
  || { echo "[deploy] ERROR dashboard not healthy"; journalctl -u payrecon-dashboard -n 30 --no-pager; exit 1; }
echo "[deploy] $(date -u +%FT%TZ) done"
