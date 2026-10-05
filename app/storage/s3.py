"""S3 helpers. Credentials come from the EC2 IAM role (never from code).

Layout:  raw/<run_id>/<file>   processed/<run_id>/<file>   dlq/<run_id>/<file>
The IAM role can only write to these three prefixes.
"""
from __future__ import annotations

from pathlib import Path

import boto3

from app.config import AWS_REGION


def upload(bucket: str, local_path: Path, prefix: str, run_id: str) -> str:
    key = f"{prefix}/{run_id}/{Path(local_path).name}"
    boto3.client("s3", region_name=AWS_REGION).upload_file(
        str(local_path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    return key


def download(bucket: str, key: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(key).name
    boto3.client("s3", region_name=AWS_REGION).download_file(bucket, key, str(dest))
    return dest
