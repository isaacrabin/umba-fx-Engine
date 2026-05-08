"""Tests for customer creation and balance management."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import db as db_module
from app.customers import (
    CustomerNotFoundError,
    InsufficientFundsError,
    create_customer,
    credit_balance,
    debit_balance,
    get_balance_decimal,
    get_balances,
    get_customer,
)

CURRENCIES = ("EUR", "KES", "NGN", "USD")


def test_create_customer_returns_record(db_path):
    c = create_customer("Alice")
    assert c["name"] == "Alice"
    assert c["id"]
    assert c["created_at"]


def test_create_customer_seeds_all_four_balances(db_path):
    c = create_customer("Bob")
    bals = get_balances(c["id"])
    currencies = {b["currency"] for b in bals}
    assert currencies == set(CURRENCIES)
    for b in bals:
        assert b["amount"] == "0.00"


def test_get_customer_not_found_raises(db_path):
    with pytest.raises(CustomerNotFoundError):
        get_customer("nonexistent-id")


def test_credit_balance_increases_correctly(db_path):
    c = create_customer("Charlie")
    credit_balance(c["id"], "USD", Decimal("500.50"))
    bals = {b["currency"]: b["amount"] for b in get_balances(c["id"])}
    assert bals["USD"] == "500.50"


def test_credit_balance_is_cumulative(db_path):
    c = create_customer("Dave")
    credit_balance(c["id"], "KES", Decimal("1000"))
    credit_balance(c["id"], "KES", Decimal("500.25"))
    bals = {b["currency"]: b["amount"] for b in get_balances(c["id"])}
    assert bals["KES"] == "1500.25"


def test_credit_negative_amount_raises(db_path):
    c = create_customer("Eve")
    with pytest.raises(ValueError):
        credit_balance(c["id"], "USD", Decimal("-100"))


def test_credit_zero_amount_raises(db_path):
    c = create_customer("Frank")
    with pytest.raises(ValueError):
        credit_balance(c["id"], "USD", Decimal("0"))


def test_credit_unknown_customer_raises(db_path):
    with pytest.raises(CustomerNotFoundError):
        credit_balance("bad-id", "USD", Decimal("100"))


def test_debit_balance_reduces_correctly(db_path):
    c = create_customer("Grace")
    credit_balance(c["id"], "USD", Decimal("300"))
    with db_module.atomic() as conn:
        debit_balance(c["id"], "USD", Decimal("100"), conn)
    bals = {b["currency"]: b["amount"] for b in get_balances(c["id"])}
    assert bals["USD"] == "200.00"


def test_debit_insufficient_funds_raises(db_path):
    c = create_customer("Hank")
    credit_balance(c["id"], "USD", Decimal("50"))
    with pytest.raises(InsufficientFundsError):
        with db_module.atomic() as conn:
            debit_balance(c["id"], "USD", Decimal("100"), conn)


def test_debit_insufficient_funds_rolls_back(db_path):
    c = create_customer("Iris")
    credit_balance(c["id"], "USD", Decimal("50"))
    try:
        with db_module.atomic() as conn:
            debit_balance(c["id"], "USD", Decimal("100"), conn)
    except InsufficientFundsError:
        pass
    bals = {b["currency"]: b["amount"] for b in get_balances(c["id"])}
    assert bals["USD"] == "50.00"


def test_get_balances_unknown_customer_raises(db_path):
    with pytest.raises(CustomerNotFoundError):
        get_balances("bad-id")
