"""Dead-letter queue: rejected rows are written to a DIRECTORY, never silently dropped.

File: data/dlq/<run_id>__<source>__rejected.csv
Columns: run_id, source_file, source_row, <every original column, text unchanged>, reason
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.config import DLQ_DIR


def write_dlq(rejected: pd.DataFrame, run_id: str, source_file: str, dlq_dir: Path = DLQ_DIR) -> Path | None:
    if rejected.empty:
        return None
    dlq_dir.mkdir(parents=True, exist_ok=True)
    out = rejected.copy()
    out.insert(0, "source_file", source_file)
    out.insert(0, "run_id", run_id)
    path = dlq_dir / f"{run_id}__{Path(source_file).stem}__rejected.csv"
    out.to_csv(path, index=False, encoding="utf-8")
    return path
