"""Rate provider — fetches, stores, and resolves FX rates with buy/sell spreads.

Resolution order for a given (from, to) pair:
  1. Direct lookup: pair "from/to" exists in rates table → use sell rate
  2. Inverse lookup: pair "to/from" exists → rate = 1 / buy("to/from")
  3. Cross via USD: route from→USD→to using (1/buy(USD/from)) × sell(USD/to)

Using 1/buy (not 1/mid) for inverse pairs preserves the directional spread:
the bank buys the from-currency at a discount, so the customer gets fewer
destination units than the mid-rate would imply.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import TypedDict

import structlog

import db

log = structlog.get_logger()

SPREAD_DEFAULT = Decimal("0.005")   # 0.5% per side
STALE_THRESHOLD_SECONDS = 300
_RATE_PRECISION = Decimal("0.0000001")

STUB_MIDS: dict[str, Decimal] = {
    "USD/KES": Decimal("129.50"),
    "USD/NGN": Decimal("1580.00"),
    "USD/EUR": Decimal("0.9200"),
    "EUR/KES": Decimal("140.76"),
    "EUR/NGN": Decimal("1717.39"),
}


class RateSourceError(Exception):
    pass


class UnresolvablePairError(Exception):
    pass


class StaleRateError(Exception):
    pass


class RateRow(TypedDict):
    pair: str
    mid: Decimal
    spread: Decimal
    buy: Decimal
    sell: Decimal
    source: str
    updated_at: str


class ResolvedRate(TypedDict):
    rate: Decimal
    route: str


def compute_buy_sell(mid: Decimal, spread: Decimal) -> tuple[Decimal, Decimal]:
    buy = mid * (Decimal("1") - spread)
    sell = mid * (Decimal("1") + spread)
    return buy, sell


def refresh_rates(source: str = "stub", path: str = db.DB_PATH) -> list[RateRow]:
    if source != "stub":
        raise RateSourceError(f"unsupported source: {source}")

    now = datetime.now(timezone.utc).isoformat()
    rows: list[RateRow] = []

    with db.atomic(path) as conn:
        for pair, mid in STUB_MIDS.items():
            buy, sell = compute_buy_sell(mid, SPREAD_DEFAULT)
            conn.execute(
                """
                INSERT INTO rates (pair, mid, spread, buy, sell, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair) DO UPDATE SET
                    mid=excluded.mid, spread=excluded.spread,
                    buy=excluded.buy, sell=excluded.sell,
                    source=excluded.source, updated_at=excluded.updated_at
                """,
                (pair, str(mid), str(SPREAD_DEFAULT), str(buy), str(sell), source, now),
            )
            rows.append(RateRow(
                pair=pair, mid=mid, spread=SPREAD_DEFAULT,
                buy=buy, sell=sell, source=source, updated_at=now,
            ))

    log.info("rates.refreshed", count=len(rows), source=source)
    return rows


def get_rate_row(pair: str, path: str = db.DB_PATH) -> RateRow | None:
    with db.get_conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM rates WHERE pair = ?", (pair,)
        ).fetchone()
    if row is None:
        return None
    return RateRow(
        pair=row["pair"],
        mid=Decimal(row["mid"]),
        spread=Decimal(row["spread"]),
        buy=Decimal(row["buy"]),
        sell=Decimal(row["sell"]),
        source=row["source"],
        updated_at=row["updated_at"],
    )


def get_all_rate_rows(path: str = db.DB_PATH) -> list[RateRow]:
    with db.get_conn(path) as conn:
        rows = conn.execute("SELECT * FROM rates ORDER BY pair").fetchall()
    return [
        RateRow(
            pair=r["pair"],
            mid=Decimal(r["mid"]),
            spread=Decimal(r["spread"]),
            buy=Decimal(r["buy"]),
            sell=Decimal(r["sell"]),
            source=r["source"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def _check_staleness(row: RateRow) -> None:
    updated = datetime.fromisoformat(row["updated_at"])
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    if age > STALE_THRESHOLD_SECONDS:
        raise StaleRateError(
            f"rate for {row['pair']} is {age:.0f}s old (threshold {STALE_THRESHOLD_SECONDS}s)"
        )


def resolve_rate(
    from_currency: str,
    to_currency: str,
    path: str = db.DB_PATH,
) -> ResolvedRate:
    """Resolve the effective rate for a (from, to) conversion.

    Returns a ResolvedRate with the customer-facing rate and the resolution route.
    Raises UnresolvablePairError if no route exists.
    Raises StaleRateError if the underlying rate data is too old.
    """
    if from_currency == to_currency:
        raise ValueError("from_currency and to_currency must differ")

    # 1. Direct pair
    direct_key = f"{from_currency}/{to_currency}"
    row = get_rate_row(direct_key, path)
    if row is not None:
        _check_staleness(row)
        return ResolvedRate(rate=row["sell"], route="direct")

    # 2. Inverse pair — use 1/buy, not 1/mid
    inverse_key = f"{to_currency}/{from_currency}"
    row = get_rate_row(inverse_key, path)
    if row is not None:
        _check_staleness(row)
        rate = (Decimal("1") / row["buy"]).quantize(_RATE_PRECISION, rounding=ROUND_HALF_UP)
        return ResolvedRate(rate=rate, route="inverse")

    # 3. Cross via USD
    if from_currency == "USD" or to_currency == "USD":
        raise UnresolvablePairError(f"no route for {from_currency}/{to_currency}")

    usd_from_key = f"USD/{from_currency}"
    usd_to_key = f"USD/{to_currency}"

    row_from = get_rate_row(usd_from_key, path)
    row_to = get_rate_row(usd_to_key, path)

    if row_from is None or row_to is None:
        raise UnresolvablePairError(
            f"cannot route {from_currency}/{to_currency}: "
            f"missing {usd_from_key} or {usd_to_key}"
        )

    _check_staleness(row_from)
    _check_staleness(row_to)

    # Customer sells from_currency → gets USD at 1/buy(USD/from)
    # then buys to_currency with USD at sell(USD/to)
    leg1 = Decimal("1") / row_from["buy"]
    leg2 = row_to["sell"]
    rate = (leg1 * leg2).quantize(_RATE_PRECISION, rounding=ROUND_HALF_UP)
    return ResolvedRate(rate=rate, route="cross_via_USD")
