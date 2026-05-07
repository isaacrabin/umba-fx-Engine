"""Tests for the FX engine."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import reset_db
from fx import FXEngine
from rates import RateProvider


@pytest.fixture(autouse=True)
def fresh_db():
    reset_db()
    yield


@pytest.fixture
def engine():
    return FXEngine(RateProvider())


def test_generate_quote_returns_expected_shape(engine):
    quote = engine.generate_quote("USD", "KES", Decimal("100"))
    assert quote.from_currency == "USD"
    assert quote.to_currency == "KES"
    assert quote.amount == Decimal("100")
    assert quote.final_amount > 0
    assert quote.expires_at > quote.created_at


def test_generate_quote_rejects_zero_amount(engine):
    with pytest.raises(ValueError):
        engine.generate_quote("USD", "KES", Decimal("0"))


def test_generate_quote_rejects_same_currency(engine):
    with pytest.raises(ValueError):
        engine.generate_quote("USD", "USD", Decimal("100"))


def test_execute_quote_succeeds(engine):
    quote = engine.generate_quote("USD", "KES", Decimal("100"))
    result = engine.execute_quote(quote.id)
    assert result["quote_id"] == quote.id
    assert result["from_currency"] == "USD"
    assert result["to_currency"] == "KES"


def test_execute_quote_unknown_id_raises(engine):
    with pytest.raises(ValueError):
        engine.execute_quote("does-not-exist")


def test_execute_with_idempotency_key_returns_cached(engine):
    quote = engine.generate_quote("USD", "EUR", Decimal("50"))
    first = engine.execute_quote(quote.id, idempotency_key="abc")
    second = engine.execute_quote(quote.id, idempotency_key="abc")
    assert first["transaction_id"] == second["transaction_id"]


def test_rate_lookup_uses_provider():
    """Engine asks the rate provider for the right pair."""
    provider = MagicMock(spec=RateProvider)
    provider.get.return_value = {
        "buy": Decimal("100"),
        "sell": Decimal("101"),
    }
    eng = FXEngine(provider)
    rate = eng._effective_rate("USD", "KES")
    assert rate == Decimal("101")
    provider.get.assert_any_call("USD/KES")


def test_inverse_pair_calculation():
    """KES->USD should derive from USD/KES."""
    provider = MagicMock(spec=RateProvider)
    provider.get.side_effect = lambda pair: {
        "USD/KES": {"buy": Decimal("129"), "sell": Decimal("130")},
    }.get(pair)
    eng = FXEngine(provider)
    rate = eng._effective_rate("KES", "USD")
    assert rate > 0
