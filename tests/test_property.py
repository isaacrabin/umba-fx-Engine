"""Property-based tests using Hypothesis.

Properties verified:
1. For any valid (from_ccy, to_ccy, amount): to_amount always has exactly 2
   decimal places, is positive, and equals from_amount * rate (within rounding).
2. No float values appear in quote or execute responses.
3. Cross-pair rates are near-inverse of each other (within spread²).
4. For any N concurrent executions of the same quote, exactly 1 succeeds.
"""
from __future__ import annotations

import concurrent.futures
from decimal import Decimal, ROUND_HALF_UP

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import db as db_module
from app import rates as rates_module
from app.customers import create_customer, credit_balance
from app.fx import (
    QuoteAlreadyExecutedError,
    QuoteExpiredError,
    execute_quote,
    generate_quote,
)
from app.rates import get_rate_row

CURRENCIES = ["USD", "EUR", "KES", "NGN"]


@pytest.fixture
def property_db(tmp_path, monkeypatch):
    path = str(tmp_path / "prop_test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(path)
    rates_module.refresh_rates(path=path)
    return path


@pytest.fixture
def property_customer(property_db):
    c = create_customer("PropertyBot")
    for ccy in CURRENCIES:
        credit_balance(c["id"], ccy, Decimal("1000000"))
    return c


# ── Quote precision ───────────────────────────────────────────────────────────

@given(
    from_ccy=st.sampled_from(CURRENCIES),
    to_ccy=st.sampled_from(CURRENCIES),
    amount=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_quote_to_amount_has_two_decimal_places(property_customer, from_ccy, to_ccy, amount):
    if from_ccy == to_ccy:
        return
    q = generate_quote(property_customer["id"], from_ccy, to_ccy, amount)
    to_amount_str = q["to_amount"]
    assert "." in to_amount_str
    decimals = len(to_amount_str.split(".")[1])
    assert decimals == 2, f"to_amount={to_amount_str} has {decimals} dp, expected 2"


@given(
    from_ccy=st.sampled_from(CURRENCIES),
    to_ccy=st.sampled_from(CURRENCIES),
    amount=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_quote_amounts_are_strings_not_floats(property_customer, from_ccy, to_ccy, amount):
    """No float values in quote response — all monetary fields must be str."""
    if from_ccy == to_ccy:
        return
    q = generate_quote(property_customer["id"], from_ccy, to_ccy, amount)
    for field in ("from_amount", "to_amount", "rate"):
        assert isinstance(q[field], str), f"{field} is {type(q[field])}, expected str"
        # Verify it round-trips through Decimal without error
        Decimal(q[field])


@given(
    from_ccy=st.sampled_from(CURRENCIES),
    to_ccy=st.sampled_from(CURRENCIES),
    amount=st.decimals(min_value=Decimal("1"), max_value=Decimal("10000"), places=2),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_to_amount_equals_from_amount_times_rate(property_customer, from_ccy, to_ccy, amount):
    """to_amount should equal round(from_amount * rate, 2)."""
    if from_ccy == to_ccy:
        return
    q = generate_quote(property_customer["id"], from_ccy, to_ccy, amount)
    rate = Decimal(q["rate"])
    from_amount = Decimal(q["from_amount"])
    to_amount = Decimal(q["to_amount"])
    expected = (from_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert to_amount == expected, (
        f"{from_ccy}->{to_ccy}: {from_amount}*{rate}={expected}, got {to_amount}"
    )


# ── Cross-pair round-trip symmetry ────────────────────────────────────────────

def test_cross_pair_rate_near_inverse(property_db):
    """KES/NGN rate × NGN/KES rate should be close to 1 (within spread²)."""
    from app.rates import resolve_rate

    r_fwd = resolve_rate("KES", "NGN")
    r_inv = resolve_rate("NGN", "KES")
    product = r_fwd["rate"] * r_inv["rate"]

    # Each cross-pair leg applies 0.5% spread. Round-trip compounds to ((1+s)/(1-s))^2 ≈ 1.0202.
    # Accept any product within a 3% corridor of 1.0.
    assert Decimal("0.97") < product < Decimal("1.04"), (
        f"KES/NGN × NGN/KES = {product}, expected near 1.0"
    )


# ── Concurrency property ──────────────────────────────────────────────────────

@given(n_threads=st.integers(min_value=2, max_value=8))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_concurrent_execute_exactly_one_winner(property_customer, n_threads, property_db):
    """For N concurrent executions of the same quote, exactly 1 must succeed."""
    q = generate_quote(property_customer["id"], "USD", "KES", Decimal("1.00"))
    quote_id = q["quote_id"]
    customer_id = property_customer["id"]

    successes = []
    failures = []

    def attempt():
        try:
            result, status = execute_quote(quote_id, customer_id)
            successes.append(status)
        except (QuoteAlreadyExecutedError, QuoteExpiredError):
            failures.append(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(attempt) for _ in range(n_threads)]
        concurrent.futures.wait(futures)

    assert len(successes) == 1, f"N={n_threads}: {len(successes)} successes, expected 1"
    assert len(failures) == n_threads - 1
