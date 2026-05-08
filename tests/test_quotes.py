"""Tests for quote generation."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import db as db_module
from app.customers import create_customer, credit_balance, CustomerNotFoundError
from app.fx import generate_quote
from app.rates import get_rate_row, resolve_rate


@pytest.fixture
def customer(db_path):
    c = create_customer("Quobot")
    credit_balance(c["id"], "USD", Decimal("100000"))
    credit_balance(c["id"], "EUR", Decimal("100000"))
    credit_balance(c["id"], "KES", Decimal("100000"))
    credit_balance(c["id"], "NGN", Decimal("100000"))
    return c


def test_quote_shape(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    assert q["from_currency"] == "USD"
    assert q["to_currency"] == "KES"
    assert q["from_amount"] == "100"
    assert q["route"] == "direct"
    assert q["quote_id"]
    assert q["expires_at"]


def test_quote_expires_60s_from_now(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    created = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(q["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delta = (expires - created).total_seconds()
    assert 58 <= delta <= 62


def test_direct_pair_uses_sell_rate(customer, db_path):
    row = get_rate_row("USD/KES")
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    assert Decimal(q["rate"]) == row["sell"]
    expected = (Decimal("100") * row["sell"]).quantize(Decimal("0.01"))
    assert Decimal(q["to_amount"]) == expected


def test_inverse_pair_rate_correct(customer, db_path):
    row = get_rate_row("USD/EUR")
    q = generate_quote(customer["id"], "EUR", "USD", Decimal("100"))
    assert q["route"] == "inverse"
    expected_rate = (Decimal("1") / row["buy"]).quantize(Decimal("0.0000001"))
    assert Decimal(q["rate"]) == expected_rate


def test_cross_pair_route(customer, db_path):
    q = generate_quote(customer["id"], "KES", "NGN", Decimal("1000"))
    assert q["route"] == "cross_via_USD"
    assert Decimal(q["to_amount"]) > 0


def test_all_12_pairs_generate_quotes(customer, db_path):
    pairs = [
        ("USD", "KES"), ("USD", "NGN"), ("USD", "EUR"),
        ("EUR", "KES"), ("EUR", "NGN"), ("EUR", "USD"),
        ("KES", "USD"), ("NGN", "USD"), ("KES", "EUR"),
        ("NGN", "EUR"), ("KES", "NGN"), ("NGN", "KES"),
    ]
    for from_ccy, to_ccy in pairs:
        q = generate_quote(customer["id"], from_ccy, to_ccy, Decimal("100"))
        assert Decimal(q["to_amount"]) > 0, f"zero to_amount for {from_ccy}/{to_ccy}"


def test_to_amount_has_2_decimal_places(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100.33"))
    parts = q["to_amount"].split(".")
    assert len(parts) == 2 and len(parts[1]) == 2


def test_no_float_in_response(customer, db_path):
    q = generate_quote(customer["id"], "KES", "NGN", Decimal("7777.77"))
    for key in ("from_amount", "to_amount", "rate"):
        assert isinstance(q[key], str), f"{key} should be a string, got {type(q[key])}"


def test_stored_rate_not_recalculated(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    original_rate = q["rate"]
    with db_module.atomic() as conn:
        conn.execute("UPDATE rates SET sell = '999.99', buy = '998.00' WHERE pair = 'USD/KES'")
    with db_module.get_conn() as conn:
        row = conn.execute("SELECT rate FROM quotes WHERE id = ?", (q["quote_id"],)).fetchone()
    assert row["rate"] == original_rate


def test_quote_rejects_unsupported_currency(customer, db_path):
    with pytest.raises(ValueError):
        generate_quote(customer["id"], "USD", "GBP", Decimal("100"))


def test_quote_rejects_same_currency(customer, db_path):
    with pytest.raises(ValueError):
        generate_quote(customer["id"], "USD", "USD", Decimal("100"))


def test_quote_rejects_zero_amount(customer, db_path):
    with pytest.raises(ValueError):
        generate_quote(customer["id"], "USD", "KES", Decimal("0"))


def test_quote_rejects_negative_amount(customer, db_path):
    with pytest.raises(ValueError):
        generate_quote(customer["id"], "USD", "KES", Decimal("-1"))


def test_quote_unknown_customer_raises(db_path):
    with pytest.raises(CustomerNotFoundError):
        generate_quote("bad-id", "USD", "KES", Decimal("100"))
