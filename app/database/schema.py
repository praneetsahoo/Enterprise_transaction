"""Apply sql/*.sql files in name order. Idempotent and non-destructive.

    python -m app.database.schema           # create missing tables, then list them
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from sqlalchemy import Engine, text

from app.config import SQL_DIR

SCHEMA_FILES = ("01_schema.sql",)
EXPECTED_TABLES = {"merchant_rates", "stg_transactions", "dlq_records", "pipeline_runs"}
_FORBIDDEN = re.compile(r"\b(DROP|TRUNCATE)\b|\bDELETE\s+FROM\b", re.IGNORECASE)


def split_statements(sql: str) -> list[str]:
    """Strip `--` comments and split on `;`. Our schema has no semicolons inside strings."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def load_statements(files: tuple[str, ...] = SCHEMA_FILES, sql_dir: Path = SQL_DIR) -> list[str]:
    statements: list[str] = []
    for name in files:
        for stmt in split_statements((sql_dir / name).read_text(encoding="utf-8")):
            if _FORBIDDEN.search(stmt):
                raise ValueError(f"{name}: destructive statement refused: {stmt[:60]}...")
            statements.append(stmt)
    return statements


def apply_schema(engine: Engine) -> list[str]:
    """Create any missing tables; return the table names now present."""
    with engine.begin() as conn:
        for stmt in load_statements():
            conn.execute(text(stmt))
        rows = conn.execute(text("SHOW TABLES")).fetchall()
    return sorted(r[0] for r in rows)


def main() -> int:
    from app.database.connection import get_engine

    tables = apply_schema(get_engine())
    missing = EXPECTED_TABLES - set(tables)
    print("tables:", ", ".join(tables))
    if missing:
        print("MISSING:", ", ".join(sorted(missing)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
