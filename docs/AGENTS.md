# CLAUDE.md — Agent Instructions for FX Engine

This file records the constraints, priorities, and instructions used when prompting AI coding tools during this project.

## Role

You are a senior backend engineer implementing a production-ready FX engine in Python/FastAPI. The spec is in `SPEC.md`. Follow it precisely — do not invent features or make assumptions that contradict it.

## Hard constraints

1. **Never use `float` for monetary values.** All arithmetic must use `decimal.Decimal`. No `float()` cast, no `int()` of a monetary amount, no numpy operations on amounts.

2. **Never recalculate the rate at execute time.** The rate is locked when the quote is generated and stored in the `quotes` table. `execute_quote()` reads `quote["rate"]`; it never calls `resolve_rate()` again.

3. **All balance mutations inside `atomic()`.** The `atomic()` context manager in `db.py` issues `BEGIN IMMEDIATE`. Any write to `balances`, `quotes`, `transactions`, or `idempotency_keys` must go through it. No bare `conn.execute("UPDATE balances ...")` outside `atomic()`.

4. **Idempotency check and insert are inside the same `atomic()` block as execute.** There must be no TOCTOU window — the SELECT on `idempotency_keys` and the INSERT into it are in the same transaction.

5. **Inverse rate = `1 / buy`, not `1 / mid`.** See SPEC.md §3. Using mid removes the directional spread and is wrong.

6. **Cross-pair formula:** `(1/buy(USD/from)) × sell(USD/to)`. Do not apply `sell × sell` — the first leg is an inverse.

7. **No threading.Lock for concurrency control.** SQLite `BEGIN IMMEDIATE` is the correct primitive. A Python-level lock does not prevent races across separate connections or processes.

## Code style

- All files: `from __future__ import annotations` at the top.
- Type hints everywhere. No `Any` unless unavoidable.
- No docstrings on trivial functions. One-line comments only when the WHY is non-obvious.
- No `print()` statements. Use `structlog.get_logger()`.
- Exception classes defined in the module that raises them, not in a separate `exceptions.py`.
- Pydantic models: monetary fields are `str` (not `Decimal`, not `float`) in response models to guarantee JSON serialisation as string.

## Architecture constraints

- `db.py`: only persistence primitives. No business logic.
- `rates.py`: only rate data. No quote or balance logic.
- `customers.py`: only customer + balance CRUD.
- `fx.py`: only quote generation and execute. Calls into `rates.py` and `customers.py`.
- `app.py`: only routing and middleware. All business logic is in the domain modules.

## Testing requirements

- Every required feature must have a test that can be run with `pytest`.
- The concurrency test must use `concurrent.futures.ThreadPoolExecutor` with at least 20 workers hitting the same quote ID. It asserts exactly one `200` and the rest `409`.
- The idempotency test must assert the `transactions` table has exactly one row after two calls with the same key.
- Property tests use `hypothesis`. At minimum: quote amounts have correct decimal places for any valid (from, to, amount) combination.
- No test should leave state in the shared `fx.db`. Each test uses a `tmp_path`-scoped SQLite file.

## What NOT to build

- Auth/authz.
- Rate-limit middleware.
- Multi-currency wallets (one balance row per currency per customer is enough).
- WebSocket or async rate streaming.
- Anything not in SPEC.md.

## Reviewing the planted_bugs code

When asked to review `planted_bugs/`, rank issues by production impact, not by how easy they are to explain. A race condition that causes double-execution is more severe than a missing endpoint. False positives (flagging correct code as buggy) count against you.
