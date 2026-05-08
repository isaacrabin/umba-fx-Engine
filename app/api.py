"""FastAPI application: routing, middleware, error handlers, observability."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from app import db
from app import rates as rate_module
from app.customers import (
    CustomerNotFoundError,
    InsufficientFundsError,
    create_customer,
    get_balances,
    get_customer,
    credit_balance,
)
from app.fx import (
    QuoteAlreadyExecutedError,
    QuoteExpiredError,
    QuoteNotFoundError,
    execute_quote,
    generate_quote,
)
from app.models import (
    BalancesResponse,
    BalanceResponse,
    CreateCustomerRequest,
    CreditBalanceRequest,
    CustomerResponse,
    GenerateQuoteRequest,
    HealthResponse,
    QuoteResponse,
    RateRowResponse,
    RatesRefreshResponse,
    TransactionResponse,
)
from app.rates import RateSourceError, StaleRateError, UnresolvablePairError

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

log = structlog.get_logger()

# ── Prometheus metrics ────────────────────────────────────────────────────────

quotes_generated = Counter(
    "fx_quotes_generated_total",
    "Total FX quotes generated",
    ["from_currency", "to_currency", "route"],
)
quotes_executed = Counter(
    "fx_quotes_executed_total",
    "Total FX quote executions",
    ["status"],
)
rate_refresh = Counter(
    "fx_rate_refresh_total",
    "Total rate refresh attempts",
    ["source", "status"],
)
rate_age = Gauge(
    "fx_rate_age_seconds",
    "Age of rate data in seconds",
    ["pair"],
)

APP_VERSION = "1.0.0"


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    try:
        rate_module.refresh_rates()
        rate_refresh.labels(source="stub", status="success").inc()
        log.info("startup.rates_loaded")
    except Exception as exc:
        rate_refresh.labels(source="stub", status="failure").inc()
        log.warning("startup.rates_failed", error=str(exc))
    yield


app = FastAPI(
    title="FX Engine",
    version=APP_VERSION,
    lifespan=lifespan,
)


# ── Middleware ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def correlation_and_logging(request: Request, call_next) -> Response:
    cid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=cid)

    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)

    response.headers["X-Correlation-ID"] = cid
    log.info(
        "http.request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


# ── Exception handlers ────────────────────────────────────────────────────────

def _err(code: int, error: str, detail: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content={"error": error, "detail": detail},
    )


@app.exception_handler(CustomerNotFoundError)
async def _customer_not_found(req, exc):
    return _err(404, "customer_not_found", str(exc))


@app.exception_handler(QuoteNotFoundError)
async def _quote_not_found(req, exc):
    return _err(404, "quote_not_found", str(exc))


@app.exception_handler(QuoteAlreadyExecutedError)
async def _quote_already_executed(req, exc):
    quotes_executed.labels(status="already_executed").inc()
    return _err(409, "quote_already_executed", str(exc))


@app.exception_handler(QuoteExpiredError)
async def _quote_expired(req, exc):
    quotes_executed.labels(status="expired").inc()
    return _err(410, "quote_expired", str(exc))


@app.exception_handler(InsufficientFundsError)
async def _insufficient_funds(req, exc):
    quotes_executed.labels(status="insufficient_funds").inc()
    return _err(422, "insufficient_funds", str(exc))


@app.exception_handler(StaleRateError)
async def _stale_rate(req, exc):
    return _err(503, "rate_stale", str(exc))


@app.exception_handler(UnresolvablePairError)
async def _unresolvable_pair(req, exc):
    return _err(422, "unresolvable_pair", str(exc))


@app.exception_handler(Exception)
async def _unhandled(req, exc):
    log.exception("unhandled_exception", exc=str(exc))
    return _err(500, "internal_error")


# ── Customer endpoints ────────────────────────────────────────────────────────

@app.post("/customers", response_model=CustomerResponse, status_code=201)
async def post_create_customer(body: CreateCustomerRequest):
    return create_customer(body.name)


@app.get("/customers/{customer_id}", response_model=CustomerResponse)
async def get_customer_endpoint(customer_id: str):
    return get_customer(customer_id)


@app.get("/customers/{customer_id}/balances", response_model=BalancesResponse)
async def get_customer_balances(customer_id: str):
    balances = get_balances(customer_id)
    return BalancesResponse(
        customer_id=customer_id,
        balances=[BalanceResponse(**b) for b in balances],
    )


@app.post("/customers/{customer_id}/credit", response_model=BalancesResponse)
async def post_credit_balance(customer_id: str, body: CreditBalanceRequest):
    balances = credit_balance(customer_id, body.currency, body.amount)
    return BalancesResponse(
        customer_id=customer_id,
        balances=[BalanceResponse(**b) for b in balances],
    )


# ── Quote endpoints ───────────────────────────────────────────────────────────

@app.post("/quotes", response_model=QuoteResponse, status_code=201)
async def post_generate_quote(body: GenerateQuoteRequest):
    result = generate_quote(
        body.customer_id,
        body.from_currency,
        body.to_currency,
        body.from_amount,
    )
    quotes_generated.labels(
        from_currency=result["from_currency"],
        to_currency=result["to_currency"],
        route=result["route"],
    ).inc()
    return result


@app.post("/quotes/{quote_id}/execute", response_model=TransactionResponse)
async def post_execute_quote(
    quote_id: str,
    body: dict,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    customer_id = body.get("customer_id")
    if not customer_id:
        return _err(422, "missing_field", "customer_id is required")

    result, status_code = execute_quote(quote_id, customer_id, idempotency_key)
    quotes_executed.labels(status="success").inc()
    return JSONResponse(content=result, status_code=status_code)


# ── Rate endpoints ────────────────────────────────────────────────────────────

@app.post("/rates/refresh", response_model=RatesRefreshResponse)
async def post_refresh_rates():
    try:
        rows = rate_module.refresh_rates()
        rate_refresh.labels(source="stub", status="success").inc()
        return RatesRefreshResponse(
            updated=len(rows),
            source="stub",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except RateSourceError as exc:
        rate_refresh.labels(source="stub", status="failure").inc()
        return _err(503, "rate_source_error", str(exc))


@app.get("/rates", response_model=list[RateRowResponse])
async def get_rates():
    rows = rate_module.get_all_rate_rows()
    return [
        RateRowResponse(
            pair=r["pair"],
            mid=str(r["mid"]),
            buy=str(r["buy"]),
            sell=str(r["sell"]),
            spread=str(r["spread"]),
            source=r["source"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


# ── Observability ─────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    db_ok = True
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False

    rows = rate_module.get_all_rate_rows()
    rates_age: float | None = None
    if rows:
        def _to_utc(ts: str) -> datetime:
            dt = datetime.fromisoformat(ts)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

        oldest = min(
            (datetime.now(timezone.utc) - _to_utc(r["updated_at"])).total_seconds()
            for r in rows
        )
        rates_age = oldest
        for r in rows:
            age = (datetime.now(timezone.utc) - _to_utc(r["updated_at"])).total_seconds()
            rate_age.labels(pair=r["pair"]).set(age)

    status = "ok" if db_ok and rates_age is not None else "degraded"
    http_status = 200 if db_ok else 503

    return JSONResponse(
        status_code=http_status,
        content=HealthResponse(
            status=status,
            db="ok" if db_ok else "error",
            rates_age_seconds=rates_age,
            version=APP_VERSION,
        ).model_dump(),
    )


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(
        generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )
