# FX Engine — Technical Specification

## 1. Supported Currencies

| Currency | ISO Code | Minor unit | Decimal places |
|----------|----------|-----------|----------------|
| US Dollar | USD | cent | 2 |
| Euro | EUR | cent | 2 |
| Kenyan Shilling | KES | cent | 2 |
| Nigerian Naira | NGN | kobo | 2 |

All amounts are stored as TEXT in SQLite and serialised as strings in JSON responses. No floating-point type ever holds a monetary value.

## 2. Supported Currency Pairs

| From | To | Route | Rate formula |
|------|----|-------|-------------|
| USD | KES | direct | sell(USD/KES) |
| USD | NGN | direct | sell(USD/NGN) |
| USD | EUR | direct | sell(USD/EUR) |
| EUR | KES | direct | sell(EUR/KES) |
| EUR | NGN | direct | sell(EUR/NGN) |
| EUR | USD | inverse | 1 / buy(USD/EUR) |
| KES | USD | inverse | 1 / buy(USD/KES) |
| NGN | USD | inverse | 1 / buy(USD/NGN) |
| KES | EUR | inverse | 1 / buy(EUR/KES) |
| NGN | EUR | inverse | 1 / buy(EUR/NGN) |
| KES | NGN | cross via USD | (1/buy(USD/KES)) × sell(USD/NGN) |
| NGN | KES | cross via USD | (1/buy(USD/NGN)) × sell(USD/KES) |

**Routing rule:** Resolve direct pair first; fall back to inverse; fall back to cross via USD. EUR-based cross pairs (e.g. KES/NGN routing via EUR) are not used — USD routing is preferred because more emerging-market pairs quote against USD.

**How spreads compound on cross pairs:** Each leg applies one half-spread (0.5%). The customer pays spread on KES→USD (via the buy side of USD/KES) and then again on USD→NGN (via the sell side of USD/NGN). The effective two-leg spread is approximately 2 × 0.5% = 1.0%, compounding slightly above 1%.

## 3. Rate Model

Base rates are sourced from a stub (or optionally exchangeratesapi.io). All stored rates are mid-rates. Buy and sell rates are derived at refresh time:

```
buy  = mid × (1 − spread)   # bank buys quote currency at a discount
sell = mid × (1 + spread)   # bank sells quote currency at a premium
```

**Current spread:** 0.5% (50 bps) per side, configurable as `SPREAD_DEFAULT` in `rates.py`.

**Directional convention:**
- `sell(A/B)` is used when the customer converts A → B (bank sells B).
- `1/buy(A/B)` is used when the customer converts B → A (bank buys B from customer at the lower buy rate, so the customer receives fewer A).

Using `1/mid` for the inverse (as the planted_bugs code does) incorrectly gives the customer a spread-free rate, which is a revenue leak.

## 4. Decimal Precision

**Rounding mode:** `ROUND_HALF_UP` (Python `decimal.ROUND_HALF_UP`) for all monetary calculations.

**Quantisation:** Applied once, at the final step, to the destination currency's minor unit (2 decimal places for all four currencies). Intermediate calculations use full Decimal precision and are never quantised early.

**No float intermediaries:** `float()` is never called on any monetary value. All arithmetic is performed with `decimal.Decimal`. JSON amounts are serialised as strings.

## 5. Quote Lifecycle

1. Client calls `POST /quotes` with `customer_id`, `from_currency`, `to_currency`, `from_amount`.
2. Engine resolves the effective rate using the current spread-adjusted rates.
3. `to_amount = quantize(from_amount × rate, to_currency)`.
4. Quote is persisted with `executed = 0` and `expires_at = now + 60s`.
5. The resolved rate is stored in the quote row; it is **never recalculated** at execution time.
6. Client calls `POST /quotes/{quote_id}/execute` before `expires_at`.
7. Engine atomically debits `from_amount` from the customer's `from_currency` balance and credits `to_amount` to their `to_currency` balance.
8. Quote is marked `executed = 1`. A transaction record is written.

A quote may be executed exactly once. Attempts to re-execute return `409 Conflict`. Expired quotes return `410 Gone`.

## 6. Transaction Atomicity

Execute uses a single `BEGIN IMMEDIATE` SQLite transaction that wraps, in order:

1. Idempotency check (read)
2. Quote existence + expiry + executed check (read)
3. Source balance sufficiency check (read)
4. Source balance debit (write)
5. Destination balance credit (write)
6. Transaction record insert (write)
7. Quote `executed = 1` update (write)
8. Idempotency record insert (write)

**If step 3 fails (insufficient funds):** `ROLLBACK`. No balances change. Quote remains executable until expiry.

**If step 5 would push destination balance negative:** Not possible — credit only adds to balance.

**If process is interrupted mid-execute:** SQLite WAL + `BEGIN IMMEDIATE` guarantees the transaction either fully commits or is fully rolled back. Partial writes are impossible.

**Concurrency:** `BEGIN IMMEDIATE` acquires a write reservation immediately. Concurrent callers block until the reservation is released. The first caller to commit sets `executed = 1`; all subsequent callers at step 2 read that value and get `409`.

## 7. Idempotency

- Header: `Idempotency-Key: <client-generated string>`
- Scope: `POST /quotes/{quote_id}/execute` only.
- Behaviour: the first successful (or deterministically failed) execution stores the response in `idempotency_keys`. Any subsequent request with the same key returns the stored response verbatim, with the original HTTP status code.
- The idempotency check and the idempotency record insert are both inside the same `BEGIN IMMEDIATE` transaction. There is no TOCTOU window.
- Keys are not namespaced by customer; clients must generate globally unique keys (e.g. UUID v4).

## 8. Rate Staleness Policy

- **Threshold:** 300 seconds (5 minutes).
- **At quote generation:** if the most recently updated rate for any required pair is older than the threshold, `StaleRateError` is raised and the API returns `503 Service Unavailable` with `{"error": "rate_stale"}`.
- **At `/rates/refresh`:** if the upstream source fails, the error is logged at ERROR level, `503` is returned, and the existing (stale) rates remain in the database. The engine continues to serve quotes if rates are within the staleness threshold.
- **At startup:** `refresh_rates()` is called in the FastAPI lifespan hook. If it fails, startup continues with stale/empty rates. The `/healthz` endpoint reports `degraded` status.

## 9. Customer Balances

- Each customer has one balance row per supported currency (`USD`, `EUR`, `KES`, `NGN`), created with `amount = "0.00"` at customer creation.
- Balances are debited and credited exclusively inside the `BEGIN IMMEDIATE` execute transaction.
- Balances may not go negative. Any execution that would produce a negative source balance is rejected with `422 Unprocessable Entity` and error code `insufficient_funds`.
- The `/customers/{id}/credit` endpoint is a test fixture for seeding balances. It is not protected by idempotency.

## 10. Observability

### Prometheus metrics (`GET /metrics`)

| Metric | Type | Labels |
|--------|------|--------|
| `fx_quotes_generated_total` | counter | `from_currency`, `to_currency`, `route` |
| `fx_quotes_executed_total` | counter | `status` (success / expired / already_executed / insufficient_funds / not_found) |
| `fx_rate_refresh_total` | counter | `source`, `status` (success / failure) |
| `fx_rate_age_seconds` | gauge | `pair` |

### Health (`GET /healthz`)

Returns `200 OK` if DB is reachable and at least one rate exists. Returns `503 Service Unavailable` if DB is unreachable. Reports `rates_age_seconds` (age of oldest rate row) and `status` (`ok` or `degraded`).

### Structured logging

Every log line is a JSON object with fields: `timestamp`, `level`, `event`, `correlation_id`, `duration_ms` (on response), plus request-specific fields (`customer_id`, `quote_id`, etc.). Implemented via `structlog`.

### Correlation IDs

Every HTTP request is assigned a `correlation_id` (from `X-Request-Id` header if provided, otherwise a new UUID v4). It is bound to the structlog context for the duration of the request and returned in the `X-Correlation-ID` response header. Quote and execute log lines both include the `quote_id` so a full quote-to-execute trace can be constructed from logs.

## 11. Out of Scope

- Authentication and authorisation.
- Multi-process / multi-node deployments (SQLite is single-writer; Postgres + row-level locking would be required for horizontal scale).
- Real-time rate streaming or WebSocket support.
- Partial fills or split executions.
- Currency conversion fees beyond the spread.
- Audit log retention policy.
- Rate-limit and circuit-breaker for upstream rate provider.
