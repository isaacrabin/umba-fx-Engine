"""Shared fixtures for all test modules.

Strategy:
- Each test gets an isolated SQLite file via tmp_path.
- db.DB_PATH is monkeypatched so all domain functions (customers, fx, rates)
  resolve to the test DB at call time (they all use `path = path or db.DB_PATH`).
- HTTP-level tests use httpx.Client with ASGITransport; the same DB_PATH patch
  applies because route handlers call domain functions with no explicit path.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, Client

import db as db_module
import rates as rates_module
import customers as customers_module
import fx as fx_module
from app import app
from customers import create_customer, credit_balance


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Per-test SQLite file. Patches db.DB_PATH globally so all modules use it."""
    path = str(tmp_path / "test_fx.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(path)
    rates_module.refresh_rates(path=path)
    return path


@pytest.fixture
def client(db_path):
    """HTTPX synchronous test client backed by the isolated test DB."""
    with Client(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def seeded_customer(db_path):
    """Customer pre-loaded with 10,000 of each currency."""
    c = create_customer("Test User")
    for ccy in ("USD", "EUR", "KES", "NGN"):
        credit_balance(c["id"], ccy, Decimal("10000"))
    return c
