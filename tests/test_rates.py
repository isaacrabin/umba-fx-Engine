"""Tests for the rate provider: formulas, routing, staleness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db as db_module
from app.rates import (
    SPREAD_DEFAULT,
    STUB_MIDS,
    StaleRateError,
    UnresolvablePairError,
    compute_buy_sell,
    get_rate_row,
    resolve_rate,
)


# ── Formula correctness ──────────────────────────────────────────────────────

def test_compute_buy_sell_formula():
    mid = Decimal("100")
    spread = Decimal("0.005")
    buy, sell = compute_buy_sell(mid, spread)
    assert buy == Decimal("99.5")
    assert sell == Decimal("100.5")


def test_refresh_stores_all_stub_pairs(db_path):
    for pair in STUB_MIDS:
        row = get_rate_row(pair)
        assert row is not None, f"missing pair {pair}"
        assert row["buy"] < row["mid"] < row["sell"]


def test_buy_sell_values_match_formula(db_path):
    row = get_rate_row("USD/KES")
    expected_buy, expected_sell = compute_buy_sell(row["mid"], SPREAD_DEFAULT)
    assert row["buy"] == expected_buy
    assert row["sell"] == expected_sell


# ── All 12 pairs resolve without error ──────────────────────────────────────

@pytest.mark.parametrize("from_ccy,to_ccy", [
    ("USD", "KES"), ("USD", "NGN"), ("USD", "EUR"),
    ("EUR", "KES"), ("EUR", "NGN"), ("EUR", "USD"),
    ("KES", "USD"), ("NGN", "USD"), ("KES", "EUR"),
    ("NGN", "EUR"), ("KES", "NGN"), ("NGN", "KES"),
])
def test_all_12_pairs_resolve(db_path, from_ccy, to_ccy):
    r = resolve_rate(from_ccy, to_ccy)
    assert r["rate"] > 0


# ── Route classification ─────────────────────────────────────────────────────

def test_direct_pair_uses_sell_rate(db_path):
    row = get_rate_row("USD/KES")
    r = resolve_rate("USD", "KES")
    assert r["route"] == "direct"
    assert r["rate"] == row["sell"]


def test_inverse_pair_uses_buy_not_mid(db_path):
    row = get_rate_row("USD/KES")
    r = resolve_rate("KES", "USD")
    assert r["route"] == "inverse"
    expected = (Decimal("1") / row["buy"]).quantize(Decimal("0.0000001"))
    wrong_mid = (Decimal("1") / row["mid"]).quantize(Decimal("0.0000001"))
    assert r["rate"] == expected
    assert r["rate"] != wrong_mid


def test_cross_pair_route_and_formula(db_path):
    usd_kes = get_rate_row("USD/KES")
    usd_ngn = get_rate_row("USD/NGN")
    r = resolve_rate("KES", "NGN")
    assert r["route"] == "cross_via_USD"
    expected = ((Decimal("1") / usd_kes["buy"]) * usd_ngn["sell"]).quantize(
        Decimal("0.0000001")
    )
    assert r["rate"] == expected


def test_ngn_kes_cross_route(db_path):
    usd_ngn = get_rate_row("USD/NGN")
    usd_kes = get_rate_row("USD/KES")
    r = resolve_rate("NGN", "KES")
    assert r["route"] == "cross_via_USD"
    expected = ((Decimal("1") / usd_ngn["buy"]) * usd_kes["sell"]).quantize(
        Decimal("0.0000001")
    )
    assert r["rate"] == expected


# ── Staleness ────────────────────────────────────────────────────────────────

def test_stale_rate_raises_error(db_path, monkeypatch):
    """Rate older than STALE_THRESHOLD_SECONDS should raise StaleRateError."""
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    with db_module.atomic() as conn:
        conn.execute("UPDATE rates SET updated_at = ?", (stale_time,))

    with pytest.raises(StaleRateError):
        resolve_rate("USD", "KES")


def test_fresh_rate_does_not_raise(db_path):
    resolve_rate("USD", "KES")  # should not raise


# ── Error cases ───────────────────────────────────────────────────────────────

def test_same_currency_raises(db_path):
    with pytest.raises(ValueError, match="must differ"):
        resolve_rate("USD", "USD")


def test_unsupported_pair_raises(db_path):
    with pytest.raises(UnresolvablePairError):
        resolve_rate("USD", "GBP")
