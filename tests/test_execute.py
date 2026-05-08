"""Tests for quote execution: happy path, atomicity, concurrency, idempotency."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db as db_module
from app.customers import (
    InsufficientFundsError,
    create_customer,
    credit_balance,
    get_balance_decimal,
    get_balances,
)
from app.fx import (
    QuoteAlreadyExecutedError,
    QuoteExpiredError,
    QuoteNotFoundError,
    execute_quote,
    generate_quote,
)


@pytest.fixture
def customer(db_path):
    c = create_customer("Exec Tester")
    credit_balance(c["id"], "USD", Decimal("50000"))
    credit_balance(c["id"], "EUR", Decimal("50000"))
    credit_balance(c["id"], "KES", Decimal("50000"))
    credit_balance(c["id"], "NGN", Decimal("50000"))
    return c


# ── Happy path ────────────────────────────────────────────────────────────────

def test_execute_happy_path(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    result, status = execute_quote(q["quote_id"], customer["id"])
    assert status == 200
    assert result["from_currency"] == "USD"
    assert result["to_currency"] == "KES"
    assert result["transaction_id"]


def test_execute_debits_source_and_credits_destination(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    to_amount = Decimal(q["to_amount"])

    execute_quote(q["quote_id"], customer["id"])

    bals = {b["currency"]: Decimal(b["amount"]) for b in get_balances(customer["id"])}
    assert bals["USD"] == Decimal("49900.00")
    assert bals["KES"] == Decimal("50000") + to_amount


def test_execute_uses_stored_rate_not_current(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("200"))
    original_to_amount = q["to_amount"]

    # Change the rate in DB after quote was generated
    with db_module.atomic() as conn:
        conn.execute("UPDATE rates SET sell = '1.00', buy = '0.99' WHERE pair = 'USD/KES'")

    result, _ = execute_quote(q["quote_id"], customer["id"])
    # Must use original to_amount from quote, not the mutated rate
    assert result["to_amount"] == original_to_amount
    assert result["rate"] == q["rate"]


def test_execute_marks_quote_executed(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("50"))
    execute_quote(q["quote_id"], customer["id"])
    with db_module.get_conn() as conn:
        row = conn.execute("SELECT executed FROM quotes WHERE id = ?", (q["quote_id"],)).fetchone()
    assert row["executed"] == 1


def test_execute_creates_transaction_record(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("50"))
    result, _ = execute_quote(q["quote_id"], customer["id"])
    with db_module.get_conn() as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (result["transaction_id"],)).fetchone()
    assert tx is not None
    assert tx["quote_id"] == q["quote_id"]


# ── Error cases ───────────────────────────────────────────────────────────────

def test_execute_quote_not_found(customer, db_path):
    with pytest.raises(QuoteNotFoundError):
        execute_quote("bad-quote-id", customer["id"])


def test_execute_wrong_customer(customer, db_path):
    other = create_customer("Other")
    q = generate_quote(customer["id"], "USD", "KES", Decimal("50"))
    with pytest.raises(QuoteNotFoundError):
        execute_quote(q["quote_id"], other["id"])


def test_execute_expired_quote(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("50"))
    # Backdate the expires_at
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with db_module.atomic() as conn:
        conn.execute("UPDATE quotes SET expires_at = ? WHERE id = ?", (past, q["quote_id"]))
    with pytest.raises(QuoteExpiredError):
        execute_quote(q["quote_id"], customer["id"])


def test_execute_already_executed_raises(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("50"))
    execute_quote(q["quote_id"], customer["id"])
    with pytest.raises(QuoteAlreadyExecutedError):
        execute_quote(q["quote_id"], customer["id"])


def test_execute_insufficient_funds_raises(db_path):
    c = create_customer("Broke")
    # No credit — balance stays at 0.00
    q = generate_quote(c["id"], "USD", "KES", Decimal("100"))
    with pytest.raises(InsufficientFundsError):
        execute_quote(q["quote_id"], c["id"])


def test_execute_insufficient_funds_no_balance_change(db_path):
    c = create_customer("AlmostBroke")
    credit_balance(c["id"], "USD", Decimal("10"))
    q = generate_quote(c["id"], "USD", "KES", Decimal("100"))
    try:
        execute_quote(q["quote_id"], c["id"])
    except InsufficientFundsError:
        pass
    bals = {b["currency"]: b["amount"] for b in get_balances(c["id"])}
    assert bals["USD"] == "10.00"   # unchanged after rollback
    assert bals["KES"] == "0.00"    # never credited


# ── Atomicity: both legs or neither ──────────────────────────────────────────

def test_atomic_debit_rolls_back_on_credit_failure(customer, db_path, monkeypatch):
    """If credit_balance_conn raises, the debit must also be rolled back."""
    from app import fx as fx_module  # patch on app.fx since it does `from app.customers import credit_balance_conn`

    def failing_credit(*args, **kwargs):
        raise RuntimeError("simulated credit failure")

    monkeypatch.setattr(fx_module, "credit_balance_conn", failing_credit)

    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    with pytest.raises(RuntimeError):
        execute_quote(q["quote_id"], customer["id"])

    # Debit must have been rolled back
    bals = {b["currency"]: b["amount"] for b in get_balances(customer["id"])}
    assert bals["USD"] == "50000.00"  # unchanged
    # Quote must still be executable (not marked as executed)
    with db_module.get_conn() as conn:
        row = conn.execute("SELECT executed FROM quotes WHERE id = ?", (q["quote_id"],)).fetchone()
    assert row["executed"] == 0


# ── Concurrency: exactly one winner ──────────────────────────────────────────

def test_concurrency_exactly_one_succeeds(customer, db_path):
    """N threads execute the same quote simultaneously. Exactly 1 must succeed."""
    N = 20
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    quote_id = q["quote_id"]
    customer_id = customer["id"]
    from_amount = Decimal(q["from_amount"])

    results = []
    errors = []

    def attempt():
        try:
            result, status = execute_quote(quote_id, customer_id)
            results.append(status)
        except (QuoteAlreadyExecutedError, QuoteExpiredError) as e:
            errors.append(type(e).__name__)
        except Exception as e:
            errors.append(f"unexpected: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as pool:
        futures = [pool.submit(attempt) for _ in range(N)]
        concurrent.futures.wait(futures)

    assert len(results) == 1, f"expected 1 success, got {len(results)}: {results}"
    assert results[0] == 200
    assert len(errors) == N - 1

    # Balance must have been debited exactly once
    bals = {b["currency"]: Decimal(b["amount"]) for b in get_balances(customer_id)}
    assert bals["USD"] == Decimal("50000") - from_amount

    # Exactly 1 transaction record
    with db_module.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = ?", (quote_id,)
        ).fetchone()[0]
    assert count == 1


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_idempotency_returns_same_response(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    first, s1 = execute_quote(q["quote_id"], customer["id"], idempotency_key="key-abc")
    second, s2 = execute_quote(q["quote_id"], customer["id"], idempotency_key="key-abc")
    assert s1 == s2 == 200
    assert first["transaction_id"] == second["transaction_id"]
    assert first == second


def test_idempotency_exactly_one_transaction(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    execute_quote(q["quote_id"], customer["id"], idempotency_key="idem-xyz")
    execute_quote(q["quote_id"], customer["id"], idempotency_key="idem-xyz")

    with db_module.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = ?", (q["quote_id"],)
        ).fetchone()[0]
    assert count == 1


def test_idempotency_balance_debited_once(customer, db_path):
    q = generate_quote(customer["id"], "USD", "KES", Decimal("200"))
    execute_quote(q["quote_id"], customer["id"], idempotency_key="once-only")
    execute_quote(q["quote_id"], customer["id"], idempotency_key="once-only")

    bals = {b["currency"]: Decimal(b["amount"]) for b in get_balances(customer["id"])}
    assert bals["USD"] == Decimal("49800.00")  # debited exactly once


def test_idempotency_concurrent_same_key(customer, db_path):
    """Concurrent requests with the same key should only execute once."""
    N = 10
    q = generate_quote(customer["id"], "USD", "KES", Decimal("100"))
    key = "concurrent-idem-key"

    tx_ids = []

    def attempt():
        try:
            result, _ = execute_quote(q["quote_id"], customer["id"], idempotency_key=key)
            tx_ids.append(result["transaction_id"])
        except (QuoteAlreadyExecutedError,):
            tx_ids.append(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as pool:
        futures = [pool.submit(attempt) for _ in range(N)]
        concurrent.futures.wait(futures)

    non_none = [t for t in tx_ids if t is not None]
    assert len(set(non_none)) == 1, "all successful replies must return the same transaction_id"
