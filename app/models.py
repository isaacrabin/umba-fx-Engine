"""Pydantic v2 request and response models.

Monetary fields in response models are typed as str to guarantee JSON
serialisation as strings — never floats.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_validator

SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}


# ── Request models ──────────────────────────────────────────────────────────

class CreateCustomerRequest(BaseModel):
    name: str


class GenerateQuoteRequest(BaseModel):
    customer_id: str
    from_currency: str
    to_currency: str
    from_amount: Decimal

    @field_validator("from_currency", "to_currency", mode="before")
    @classmethod
    def currency_must_be_supported(cls, v: str) -> str:
        v = str(v).upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {v}")
        return v

    @field_validator("from_amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("from_amount must be positive")
        return v


class CreditBalanceRequest(BaseModel):
    currency: str
    amount: Decimal

    @field_validator("currency", mode="before")
    @classmethod
    def currency_must_be_supported(cls, v: str) -> str:
        v = str(v).upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {v}")
        return v

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v


# ── Response models ──────────────────────────────────────────────────────────

class CustomerResponse(BaseModel):
    id: str
    name: str
    created_at: str


class BalanceResponse(BaseModel):
    currency: str
    amount: str


class BalancesResponse(BaseModel):
    customer_id: str
    balances: list[BalanceResponse]


class QuoteResponse(BaseModel):
    quote_id: str
    customer_id: str
    from_currency: str
    to_currency: str
    from_amount: str
    to_amount: str
    rate: str
    route: str
    expires_at: str


class TransactionResponse(BaseModel):
    transaction_id: str
    quote_id: str
    customer_id: str
    from_currency: str
    to_currency: str
    from_amount: str
    to_amount: str
    rate: str
    executed_at: str


class RatesRefreshResponse(BaseModel):
    updated: int
    source: str
    timestamp: str


class RateRowResponse(BaseModel):
    pair: str
    mid: str
    buy: str
    sell: str
    spread: str
    source: str
    updated_at: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "error"]
    rates_age_seconds: float | None
    version: str


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    correlation_id: str | None = None
