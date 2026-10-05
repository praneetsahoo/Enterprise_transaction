#!/bin/bash
# Run the pipeline + reports on the server, as the payrecon user, with the deployed settings.
# Logs go to /var/log/payrecon/pipeline.log -> CloudWatch /payrecon/pipeline (ERROR -> alarm).
#
#   sudo /opt/payrecon/app/infra/run_pipeline.sh                                   # sample files in data/raw
#   sudo /opt/payrecon/app/infra/run_pipeline.sh --s3-transactions raw/inbox/x.csv  # a file already in S3
set -euo pipefail
set -a; . /etc/payrecon/payrecon.env; set +a
cd /opt/payrecon/app
PY=/opt/payrecon/venv/bin/python
runuser -u payrecon -- env HOME=/var/lib/payrecon "$PY" -m app.pipeline "$@"
runuser -u payrecon -- env HOME=/var/lib/payrecon "$PY" -m app.analytics.reports > /dev/null
echo "reports refreshed (s3://$S3_BUCKET/processed/reports/)"
