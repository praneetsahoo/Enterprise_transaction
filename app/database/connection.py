"""One SQLAlchemy engine for the whole app (connection pool, reconnect check)."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine

from app.config import database_url


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping: test a pooled connection before use, so an RDS restart or idle timeout
    # gives a fresh connection instead of a "MySQL server has gone away" error.
    return create_engine(database_url(), pool_pre_ping=True, pool_recycle=1800, future=True)
