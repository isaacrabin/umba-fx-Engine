"""SQLite persistence layer.

Provides two context managers:
- get_conn(): read-only / autocommit queries
- atomic(): BEGIN IMMEDIATE transaction for all writes
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DB_PATH = str(Path(__file__).parent.parent / "fx.db")

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS customers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
    customer_id TEXT NOT NULL REFERENCES customers(id),
    currency    TEXT NOT NULL,
    amount      TEXT NOT NULL DEFAULT '0.00',
    PRIMARY KEY (customer_id, currency)
);

CREATE TABLE IF NOT EXISTS rates (
    pair       TEXT PRIMARY KEY,
    mid        TEXT NOT NULL,
    spread     TEXT NOT NULL,
    buy        TEXT NOT NULL,
    sell       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'stub',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    from_currency TEXT NOT NULL,
    to_currency   TEXT NOT NULL,
    from_amount   TEXT NOT NULL,
    to_amount     TEXT NOT NULL,
    rate          TEXT NOT NULL,
    route         TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    executed      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    quote_id      TEXT NOT NULL REFERENCES quotes(id),
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    from_currency TEXT NOT NULL,
    to_currency   TEXT NOT NULL,
    from_amount   TEXT NOT NULL,
    to_amount     TEXT NOT NULL,
    rate          TEXT NOT NULL,
    executed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT PRIMARY KEY,
    status_code   INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quotes_customer    ON quotes(customer_id);
CREATE INDEX IF NOT EXISTS idx_transactions_quote ON transactions(quote_id);
CREATE INDEX IF NOT EXISTS idx_transactions_cust  ON transactions(customer_id);
"""


def init_db(path: str | None = None) -> None:
    path = path or DB_PATH
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(_DDL)
    finally:
        conn.close()


@contextmanager
def get_conn(path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = path or DB_PATH
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def atomic(path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Exclusive write transaction using BEGIN IMMEDIATE.

    Guarantees that the read-check-write sequence inside is atomic:
    no other writer can interleave between our SELECT and UPDATE.
    All balance and quote mutations must go through this context manager.
    """
    path = path or DB_PATH
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
