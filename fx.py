"""FX engine core: quote generation and atomic execution.

Key invariants (enforced here, not in app.py):
- No float arithmetic on monetary values — Decimal only.
- execute_quote() uses the rate stored in the quote row, never recalculates.
- All balance mutations happen inside a single BEGIN IMMEDIATE transaction.
- The idempotency check and insert are inside the same transaction (no TOCTOU).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import structlog

import db
from customers import (
    CustomerNotFoundError,
    InsufficientFundsError,
    credit_balance_conn,
    debit_balance,
    get_customer,
)
from rates import StaleRateError, UnresolvablePairError, resolve_rate

log = structlog.get_logger()

SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}
QUOTE_TTL_SECONDS = 60
_CURRENCY_QUANTUM = Decimal("0.01")


class QuoteNotFoundError(Exception):
    pass


class QuoteExpiredError(Exception):
    pass


class QuoteAlreadyExecutedError(Exception):
    pass


def quantize_amount(amount: Decimal) -> Decimal:
    return amount.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)


def generate_quote(
    customer_id: str,
    from_currency: str,
    to_currency: str,
    from_amount: Decimal,
    path: str | None = None,
) -> dict:
    if from_currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"unsupported currency: {from_currency}")
    if to_currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"unsupported currency: {to_currency}")
    if from_currency == to_currency:
        raise ValueError("from_currency and to_currency must differ")
    if from_amount <= 0:
        raise ValueError("from_amount must be positive")

    get_customer(customer_id, path)  # raises CustomerNotFoundError if missing

    resolved = resolve_rate(from_currency, to_currency, path)
    rate = resolved["rate"]
    to_amount = quantize_amount(from_amount * rate)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=QUOTE_TTL_SECONDS)
    quote_id = str(uuid.uuid4())

    with db.atomic(path) as conn:
        conn.execute(
            """
            INSERT INTO quotes
              (id, customer_id, from_currency, to_currency,
               from_amount, to_amount, rate, route, expires_at, executed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                quote_id,
                customer_id,
                from_currency,
                to_currency,
                str(from_amount),
                str(to_amount),
                str(rate),
                resolved["route"],
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )

    log.info(
        "quote.generated",
        quote_id=quote_id,
        customer_id=customer_id,
        from_currency=from_currency,
        to_currency=to_currency,
        from_amount=str(from_amount),
        to_amount=str(to_amount),
        rate=str(rate),
        route=resolved["route"],
    )

    return {
        "quote_id": quote_id,
        "customer_id": customer_id,
        "from_currency": from_currency,
        "to_currency": to_currency,
        "from_amount": str(from_amount),
        "to_amount": str(to_amount),
        "rate": str(rate),
        "route": resolved["route"],
        "expires_at": expires_at.isoformat(),
    }


def execute_quote(
    quote_id: str,
    customer_id: str,
    idempotency_key: str | None = None,
    path: str | None = None,
) -> tuple[dict, int]:
    """Execute a quote atomically. Returns (response_dict, http_status_code).

    All steps run inside a single BEGIN IMMEDIATE transaction:
      1. Idempotency check (replay if found)
      2. Quote validation (exists, belongs to customer, not expired, not executed)
      3. Balance sufficiency check
      4. Source balance debit
      5. Destination balance credit
      6. Transaction record insert
      7. Quote marked executed
      8. Idempotency record insert

    Raises:
      QuoteNotFoundError, QuoteExpiredError, QuoteAlreadyExecutedError,
      InsufficientFundsError, CustomerNotFoundError
    """
    with db.atomic(path) as conn:
        # Step 1: idempotency replay
        if idempotency_key:
            idem_row = conn.execute(
                "SELECT status_code, response_body FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if idem_row:
                log.info("execute.idempotent_replay", idempotency_key=idempotency_key, quote_id=quote_id)
                return json.loads(idem_row["response_body"]), idem_row["status_code"]

        # Step 2: validate quote
        quote_row = conn.execute(
            "SELECT * FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()

        if quote_row is None:
            raise QuoteNotFoundError(f"quote {quote_id} not found")

        if quote_row["customer_id"] != customer_id:
            raise QuoteNotFoundError(f"quote {quote_id} not found")

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(quote_row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            raise QuoteExpiredError(f"quote {quote_id} expired at {expires_at.isoformat()}")

        if quote_row["executed"]:
            raise QuoteAlreadyExecutedError(f"quote {quote_id} already executed")

        # Step 3: load stored rate — never recalculate
        rate = Decimal(quote_row["rate"])
        from_amount = Decimal(quote_row["from_amount"])
        to_amount = Decimal(quote_row["to_amount"])
        from_currency = quote_row["from_currency"]
        to_currency = quote_row["to_currency"]

        # Step 4: balance sufficiency + debit (raises InsufficientFundsError if short)
        debit_balance(customer_id, from_currency, from_amount, conn)

        # Step 5: credit destination
        credit_balance_conn(customer_id, to_currency, to_amount, conn)

        # Step 6: transaction record
        tx_id = str(uuid.uuid4())
        executed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO transactions
              (id, quote_id, customer_id, from_currency, to_currency,
               from_amount, to_amount, rate, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tx_id, quote_id, customer_id, from_currency, to_currency,
             str(from_amount), str(to_amount), str(rate), executed_at),
        )

        # Step 7: mark quote executed
        conn.execute(
            "UPDATE quotes SET executed = 1 WHERE id = ?", (quote_id,)
        )

        # Step 8: store idempotency response
        result = {
            "transaction_id": tx_id,
            "quote_id": quote_id,
            "customer_id": customer_id,
            "from_currency": from_currency,
            "to_currency": to_currency,
            "from_amount": str(from_amount),
            "to_amount": str(to_amount),
            "rate": str(rate),
            "executed_at": executed_at,
        }
        status_code = 200

        if idempotency_key:
            conn.execute(
                """
                INSERT INTO idempotency_keys (key, status_code, response_body, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (idempotency_key, status_code, json.dumps(result), executed_at),
            )

    log.info(
        "quote.executed",
        transaction_id=tx_id,
        quote_id=quote_id,
        customer_id=customer_id,
        from_currency=from_currency,
        to_currency=to_currency,
        from_amount=str(from_amount),
        to_amount=str(to_amount),
    )
    return result, status_code
