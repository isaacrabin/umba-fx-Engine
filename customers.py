"""Customer and balance management."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog

import db

log = structlog.get_logger()

SUPPORTED_CURRENCIES = ("USD", "EUR", "KES", "NGN")


class CustomerNotFoundError(Exception):
    pass


class InsufficientFundsError(Exception):
    pass


def create_customer(name: str, path: str | None = None) -> dict:
    customer_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with db.atomic(path) as conn:
        conn.execute(
            "INSERT INTO customers (id, name, created_at) VALUES (?, ?, ?)",
            (customer_id, name, now),
        )
        for currency in SUPPORTED_CURRENCIES:
            conn.execute(
                "INSERT INTO balances (customer_id, currency, amount) VALUES (?, ?, '0.00')",
                (customer_id, currency),
            )

    log.info("customer.created", customer_id=customer_id, name=name)
    return {"id": customer_id, "name": name, "created_at": now}


def get_customer(customer_id: str, path: str | None = None) -> dict:
    with db.get_conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
    if row is None:
        raise CustomerNotFoundError(f"customer {customer_id} not found")
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}


def get_balances(customer_id: str, path: str | None = None) -> list[dict]:
    get_customer(customer_id, path)  # raises if not found
    with db.get_conn(path) as conn:
        rows = conn.execute(
            "SELECT currency, amount FROM balances WHERE customer_id = ? ORDER BY currency",
            (customer_id,),
        ).fetchall()
    return [{"currency": r["currency"], "amount": r["amount"]} for r in rows]


def get_balance_decimal(customer_id: str, currency: str, conn) -> Decimal:
    """Read balance as Decimal within an existing connection (inside atomic())."""
    row = conn.execute(
        "SELECT amount FROM balances WHERE customer_id = ? AND currency = ?",
        (customer_id, currency),
    ).fetchone()
    return Decimal(row["amount"]) if row else Decimal("0")


def credit_balance(
    customer_id: str,
    currency: str,
    amount: Decimal,
    path: str | None = None,
) -> list[dict]:
    """Credit `amount` to the customer's `currency` balance. Returns updated balances."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    get_customer(customer_id, path)

    with db.atomic(path) as conn:
        current = get_balance_decimal(customer_id, currency, conn)
        new_amount = current + amount
        conn.execute(
            """
            INSERT INTO balances (customer_id, currency, amount)
            VALUES (?, ?, ?)
            ON CONFLICT(customer_id, currency) DO UPDATE SET amount = excluded.amount
            """,
            (customer_id, currency, str(new_amount)),
        )

    log.info("balance.credited", customer_id=customer_id, currency=currency, amount=str(amount))
    return get_balances(customer_id, path)


def debit_balance(
    customer_id: str,
    currency: str,
    amount: Decimal,
    conn,
) -> None:
    """Debit `amount` from balance. Must be called inside an atomic() block.

    Raises InsufficientFundsError if balance < amount.
    Does not commit — the caller's atomic() block commits.
    """
    current = get_balance_decimal(customer_id, currency, conn)
    if amount > current:
        raise InsufficientFundsError(
            f"need {amount} {currency}, have {current}"
        )
    new_amount = current - amount
    conn.execute(
        "UPDATE balances SET amount = ? WHERE customer_id = ? AND currency = ?",
        (str(new_amount), customer_id, currency),
    )


def credit_balance_conn(
    customer_id: str,
    currency: str,
    amount: Decimal,
    conn,
) -> None:
    """Credit balance within an existing atomic() connection."""
    current = get_balance_decimal(customer_id, currency, conn)
    new_amount = current + amount
    conn.execute(
        """
        INSERT INTO balances (customer_id, currency, amount)
        VALUES (?, ?, ?)
        ON CONFLICT(customer_id, currency) DO UPDATE SET amount = excluded.amount
        """,
        (customer_id, currency, str(new_amount)),
    )
