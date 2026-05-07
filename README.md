# FX Engine

A production-ready foreign exchange engine supporting USD, EUR, KES, and NGN
with per-customer balance accounts.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run the server

```bash
python main.py
# or: uvicorn app:app --host 0.0.0.0 --port 8000
```

The server initialises the SQLite database and loads stub rates on startup.

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/customers` | Create a customer |
| GET | `/customers/{id}` | Get customer record |
| GET | `/customers/{id}/balances` | List balances for all currencies |
| POST | `/customers/{id}/credit` | Credit a balance (test fixture) |
| POST | `/quotes` | Generate a quote (locks rate for 60 s) |
| POST | `/quotes/{id}/execute` | Execute a quote atomically |
| POST | `/rates/refresh` | Reload rates from source |
| GET | `/rates` | List current rates with buy/sell spreads |
| GET | `/healthz` | Health check |
| GET | `/metrics` | Prometheus metrics |

Interactive docs: http://localhost:8000/docs

### Quick start

```bash
# Create a customer
curl -s -X POST http://localhost:8000/customers \
  -H "Content-Type: application/json" \
  -d '{"name": "Alice"}' | python -m json.tool

# Credit USD balance
curl -s -X POST http://localhost:8000/customers/<id>/credit \
  -H "Content-Type: application/json" \
  -d '{"currency": "USD", "amount": "1000.00"}' | python -m json.tool

# Generate a quote
curl -s -X POST http://localhost:8000/quotes \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "<id>", "from_currency": "USD", "to_currency": "KES", "from_amount": "100.00"}' \
  | python -m json.tool

# Execute the quote (with idempotency key)
curl -s -X POST http://localhost:8000/quotes/<quote_id>/execute \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"customer_id": "<id>"}' | python -m json.tool
```

## Tests

```bash
# Full suite (71 tests: unit, integration, concurrency, property-based)
pytest tests/ -v

# Concurrency test: 20 threads on the same quote_id → exactly 1 succeeds
pytest tests/test_execute.py::test_concurrency_exactly_one_succeeds -v

# Idempotency tests
pytest tests/test_execute.py -k idempotency -v

# Property-based tests (Hypothesis, ~200 examples)
pytest tests/test_property.py -v

# Atomicity test: credit failure rolls back debit
pytest tests/test_execute.py::test_atomic_debit_rolls_back_on_credit_failure -v
```

## Example log output (structured JSON)

```json
{"count": 5, "source": "stub", "event": "rates.refreshed", "timestamp": "2026-05-07T22:30:42Z", "level": "info"}
{"customer_id": "a50ac19d", "name": "Alice", "event": "customer.created", "timestamp": "2026-05-07T22:30:42Z", "level": "info"}
{"quote_id": "3eb31901", "customer_id": "a50ac19d", "from_currency": "USD", "to_currency": "KES", "from_amount": "100", "to_amount": "13014.75", "rate": "130.14750", "route": "direct", "event": "quote.generated", "timestamp": "2026-05-07T22:30:42Z", "level": "info"}
{"transaction_id": "150a3d2a", "quote_id": "3eb31901", "customer_id": "a50ac19d", "from_currency": "USD", "to_currency": "KES", "from_amount": "100", "to_amount": "13014.75", "event": "quote.executed", "timestamp": "2026-05-07T22:30:42Z", "level": "info"}
{"method": "POST", "path": "/quotes/3eb31901/execute", "status": 200, "duration_ms": 3.4, "correlation_id": "f7b2e4a1", "event": "http.request", "timestamp": "2026-05-07T22:30:42Z", "level": "info"}
```

Every response includes an `X-Correlation-ID` header. Pass `X-Request-Id` to propagate your own trace ID.

## Example /metrics output

```
# HELP fx_quotes_generated_total Total FX quotes generated
# TYPE fx_quotes_generated_total counter
fx_quotes_generated_total{from_currency="USD",route="direct",to_currency="KES"} 42.0
fx_quotes_generated_total{from_currency="KES",route="cross_via_USD",to_currency="NGN"} 7.0
# HELP fx_quotes_executed_total Total FX quote executions
# TYPE fx_quotes_executed_total counter
fx_quotes_executed_total{status="success"} 38.0
fx_quotes_executed_total{status="already_executed"} 4.0
fx_quotes_executed_total{status="insufficient_funds"} 2.0
# HELP fx_rate_age_seconds Age of rate data in seconds
# TYPE fx_rate_age_seconds gauge
fx_rate_age_seconds{pair="USD/KES"} 37.2
```

## Architecture notes

- **Concurrency model:** All balance mutations run inside a `BEGIN IMMEDIATE`
  SQLite transaction (`db.atomic()`). This prevents the read-check-write race
  that would allow a quote to execute twice. No threading locks anywhere.
- **Decimal precision:** No `float` is ever used in monetary arithmetic.
  All amounts stored as TEXT in SQLite, serialised as strings in JSON.
- **Rate locking:** The rate is resolved at quote generation and stored in
  the `quotes` table. `execute_quote` reads `quote["rate"]` — it never calls
  `resolve_rate()` again.
- **Idempotency:** The idempotency key check and store are inside the same
  `BEGIN IMMEDIATE` transaction as the execution. There is no TOCTOU window.
- **Cross-pairs:** KES/NGN and NGN/KES route via USD.
  Formula: `(1/buy(USD/from)) × sell(USD/to)`. Spread compounds to ~1% for
  the two-leg round trip.

## Known limitations

- SQLite is single-writer. For horizontal scale (multiple processes), migrate
  to Postgres with `SELECT ... FOR UPDATE` for balance rows.
- Rate source is a stub. A production adapter would call exchangeratesapi.io
  (or similar) and implement circuit-breaking on failure.
- No authentication or authorisation. Any caller can credit any customer's
  balance via the `/credit` endpoint.
- No quote cancellation endpoint.
- Idempotency keys are stored indefinitely. A TTL cleanup job is needed.

## What I'd do with another day

- Real rate-source adapter (exchangeratesapi.io) with circuit breaker and
  background 60-second refresh.
- Postgres migration with row-level locking for horizontal scale.
- OpenTelemetry tracing (quote → execute spans in Jaeger).
- Locust load test to verify concurrency guarantees at production TPS.
- Rate limiting on execute endpoint (slowapi).

## Estimated time

- Active engagement: ~6 hours
- Wall clock: ~8 hours (including thinking time and iteration)
