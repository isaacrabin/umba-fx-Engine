"""FX engine core: quote generation, execution, and rate math.

All financial calculations use Decimal with ROUND_HALF_UP rounding,
quantized to 2 decimal places at the final step.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from db import get_db
from rates import RateProvider

QUOTE_TTL_SECONDS = 60
QUANTUM = Decimal("0.01")

_execute_lock = threading.Lock()


@dataclass
class Quote:
    id: str
    from_currency: str
    to_currency: str
    amount: Decimal
    rate: Decimal
    final_amount: Decimal
    created_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict:
        return {
            "quote_id": self.id,
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "amount": str(self.amount),
            "rate": str(self.rate),
            "final_amount": str(self.final_amount),
            "expires_at": self.expires_at.isoformat(),
        }


class FXEngine:
    def __init__(self, rate_provider: RateProvider):
        self.rates = rate_provider

    def generate_quote(
        self, from_ccy: str, to_ccy: str, amount: Decimal
    ) -> Quote:
        if amount <= 0:
            raise ValueError("amount must be positive")
        if from_ccy == to_ccy:
            raise ValueError("from and to currencies must differ")

        rate = self._effective_rate(from_ccy, to_ccy)
        final = float(amount) * float(rate)
        final_decimal = Decimal(str(final)).quantize(
            QUANTUM, rounding=ROUND_HALF_UP
        )

        now = datetime.now(timezone.utc)
        quote = Quote(
            id=str(uuid.uuid4()),
            from_currency=from_ccy,
            to_currency=to_ccy,
            amount=amount,
            rate=rate,
            final_amount=final_decimal,
            created_at=now,
            expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
        )

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO quotes
                  (id, from_currency, to_currency, amount, rate,
                   final_amount, created_at, expires_at, executed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    quote.id,
                    from_ccy,
                    to_ccy,
                    str(amount),
                    str(rate),
                    str(final_decimal),
                    now.isoformat(),
                    quote.expires_at.isoformat(),
                ),
            )

        return quote

    def execute_quote(
        self, quote_id: str, idempotency_key: Optional[str] = None
    ) -> dict:
        if idempotency_key:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT response FROM idempotency WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    import json
                    return json.loads(row["response"])

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM quotes WHERE id = ?", (quote_id,)
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")

            now = datetime.now(timezone.utc)
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at < now:
                raise ValueError("quote expired")
            if row["executed"]:
                raise ValueError("quote already executed")

            current_rate = self._effective_rate(
                row["from_currency"], row["to_currency"]
            )
            amount = Decimal(row["amount"])
            final = (amount * current_rate).quantize(
                QUANTUM, rounding=ROUND_HALF_UP
            )

            with _execute_lock:
                conn.execute(
                    "UPDATE quotes SET executed = 1, executed_at = ? "
                    "WHERE id = ?",
                    (now.isoformat(), quote_id),
                )

                tx_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO transactions
                      (id, quote_id, from_currency, to_currency, amount,
                       final_amount, rate, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx_id,
                        quote_id,
                        row["from_currency"],
                        row["to_currency"],
                        row["amount"],
                        str(final),
                        str(current_rate),
                        now.isoformat(),
                    ),
                )

            response = {
                "transaction_id": tx_id,
                "quote_id": quote_id,
                "from_currency": row["from_currency"],
                "to_currency": row["to_currency"],
                "amount": row["amount"],
                "final_amount": str(final),
                "rate": str(current_rate),
                "executed_at": now.isoformat(),
            }

            if idempotency_key:
                import json
                conn.execute(
                    "INSERT INTO idempotency (key, response) VALUES (?, ?)",
                    (idempotency_key, json.dumps(response)),
                )

            return response

    def _effective_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Return the effective sell rate (bank sells `to_ccy` to customer)."""
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["sell"]

        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            mid = (inverse["buy"] + inverse["sell"]) / 2
            return Decimal("1") / mid

        # Cross via USD.
        leg1 = self.rates.get(f"{from_ccy}/USD") or self.rates.get(
            f"USD/{from_ccy}"
        )
        leg2 = self.rates.get(f"USD/{to_ccy}") or self.rates.get(
            f"{to_ccy}/USD"
        )
        if leg1 and leg2:
            return leg1["sell"] * leg2["sell"]

        raise ValueError(f"no rate available for {from_ccy}/{to_ccy}")
