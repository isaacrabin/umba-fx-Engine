# DECISIONS.md

## Architecture and library choices

### FastAPI over Flask
I chose FastAPI for three reasons: Pydantic v2 validates and coerces request
bodies with field-level validators (no manual `try/except KeyError` in routes),
the lifespan hook makes DB and rate initialisation at startup clean and
testable, and the automatic OpenAPI docs are useful for a reviewer running
the service. Flask would have worked but required more boilerplate to get
the same validation guarantees.

Trade-off: FastAPI's async runtime adds an ASGI layer that is unnecessary
when all our I/O is synchronous SQLite. I accepted this because the
simplicity benefit outweighs the mild overhead.

### SQLite with WAL over Postgres
The assignment says SQLite is fine. I chose it because there is no setup
friction — tests create isolated SQLite files in `tmp_path` — and WAL mode
gives good concurrent read performance for a single-process service. I
documented the Postgres migration path in SPEC.md (§11, out of scope).

### `BEGIN IMMEDIATE` over threading.Lock
The planted_bugs code uses `threading.Lock` to attempt concurrency control.
This was the wrong primitive: it prevents Python threads from interleaving
their UPDATE statements, but both threads can read `executed = 0` before
either acquires the lock. Worse, a Python lock provides no protection across
multiple processes or when the service is restarted mid-execution.

`BEGIN IMMEDIATE` is the right primitive. SQLite acquires a write reservation
at the transaction boundary; subsequent callers block at the database level
until the first caller commits or rolls back. This works regardless of
threads, processes, or service restarts.

### Stored rate at execution, never recalculated
This was a deliberate decision I made before writing code and recorded in
CLAUDE.md as a hard constraint. A quote is a promise: the customer sees a
rate, decides to proceed, and calls execute. If the rate is recalculated at
execution time and markets have moved in the 0–60 seconds between quote and
execute, the customer receives a different amount than promised. The planted
code recalculates — I caught this while writing the spec and it became the
invariant "never recalculate the rate at execute time." Tests verify this:
`test_execute_uses_stored_rate_not_current` mutates the rates table between
generate and execute and asserts the outcome matches the original quote.

### `1/buy` for inverse pairs, not `1/mid`
The planted code computes `1/mid` for inverse pairs. This was subtle: the
code is not obviously wrong, and both produce a plausible-looking number.
I caught it by working through the spread arithmetic in SPEC.md §3 before
writing a single line of code. When the customer sells KES to the bank, the
bank applies its buy rate (it is buying KES). Using the mid rate gives the
customer the spread for free — a revenue leak on five of the twelve pairs.

### No `path` parameter default to `db.DB_PATH` at definition time
The initial implementation used `path: str = db.DB_PATH` on every function.
Python evaluates default parameters at import time, so monkeypatching
`db.DB_PATH` in tests had no effect on the default value. I refactored to
`path: str | None = None` with `path = path or DB_PATH` inside each function,
which resolves the module-level variable at call time. This is one thing the
AI introduced that I caught and fixed before the test suite ran — the tests
were failing silently because they were all hitting the same shared database.

## What I delegated to the AI vs. owned

**Owned (designed before prompting):**
- The atomicity model: `BEGIN IMMEDIATE` wrapping all 8 steps of execute
- The rate routing table (all 12 pairs with exact formulas)
- The `1/buy` vs `1/mid` distinction for inverse pairs
- The cross-pair formula: `(1/buy(USD/from)) × sell(USD/to)`
- The idempotency model: check and insert in the same transaction (no TOCTOU)
- The `path=None` late-binding pattern (added after initial generation)

**Delegated:**
- Boilerplate: schema DDL, Pydantic model fields, FastAPI route signatures
- Structlog configuration
- Prometheus counter registration
- Test fixture structure (seeded_customer, db_path, client)
- requirements.txt

## What I accepted, rejected, or overrode from AI suggestions

**Accepted:**
- The `atomic()` context manager using `isolation_level=None` + manual
  `BEGIN IMMEDIATE` / COMMIT / ROLLBACK. Clean and correct.
- Using `ON CONFLICT DO NOTHING` for the idempotency insert instead of
  catching IntegrityError. This handles the race where two requests insert
  concurrently: one succeeds, one is silently ignored.
- The conftest fixture pattern with `monkeypatch.setattr(db_module, "DB_PATH", path)`.

**Rejected / overrode:**
- Initial suggestion to use `threading.Lock` in `execute_quote`. I replaced
  this with `BEGIN IMMEDIATE` (see above).
- `path: str = db.DB_PATH` default parameters. Changed to `path: str | None = None`.
- Initial test for atomicity patched `customers.credit_balance_conn` but
  `fx.py` does `from customers import credit_balance_conn`, so the reference
  was already bound. Fixed to patch `fx.credit_balance_conn`.
- The initial property test used `(amount * rate).quantize(Decimal("0.01"))`
  (default `ROUND_HALF_EVEN`) but the production code uses `ROUND_HALF_UP`.
  Fixed to add explicit rounding mode to the expected-value calculation.

## One thing the AI got wrong and how I caught it

The AI generated `path: str = db.DB_PATH` as default parameters on all
domain functions. I caught this by running the test suite: every test was
using the same `fx.db` file instead of the per-test `tmp_path` SQLite.
Tests were passing individually but failing when run in parallel because
they shared state. I traced the issue to the early-binding default, refactored
to `None` defaults, and confirmed that monkeypatching `db.DB_PATH` now
propagates correctly to all call sites.

## What I did not trust without verifying

- **Decimal arithmetic in tests**: I verified that the expected-value
  calculation in `test_to_amount_equals_from_amount_times_rate` uses the
  same rounding mode as the production code (ROUND_HALF_UP). The AI-generated
  test initially used the default context, which uses ROUND_HALF_EVEN.
- **Cross-pair formula**: I derived `(1/buy(USD/from)) × sell(USD/to)` on
  paper before accepting the implementation, and wrote a parametric test
  that computes the expected value independently from the formula and
  compares it to the resolve_rate output.
- **Concurrency test**: I ran it several times with N=20 to confirm it
  reliably catches races. The planted code fails this test.
- **Idempotency atomicity**: I reviewed the transaction boundary manually
  to confirm the idempotency check and insert are inside the same
  `BEGIN IMMEDIATE` block, not in a separate connection.

## What I'd do with another day

- Add a real rate-source adapter (exchangeratesapi.io) behind the stub,
  with a circuit breaker (tenacity) and a configurable staleness threshold.
- Migrate to Postgres with row-level locking (`SELECT ... FOR UPDATE`) to
  support horizontal scaling.
- Add request-level rate limiting (slowapi) and a circuit breaker on the
  `/quotes/{id}/execute` route.
- Add OpenTelemetry tracing so quote → execute spans are visible in Jaeger/Tempo.
- Add a background task that refreshes rates every 60 seconds and exposes
  the last-refresh timestamp in `/healthz`.
- Write load tests (Locust) to verify the concurrency guarantees under
  realistic TPS.
