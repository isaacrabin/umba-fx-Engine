# Code Review: planted_bugs/

Reviewed as a PR from a teammate. Bugs ranked by production impact — a race
condition that silently double-charges a customer ranks higher than a missing
endpoint. False positives are labelled as such.

**Method:** I read all source files, ran the provided test suite, wrote
additional concurrent and edge-case tests, and used my own FX engine
(SPEC.md) as the canonical reference for correctness.

---

## BLOCKER

### BUG-1 — Race condition: execute can run the same quote twice

**File:** `fx.py`, `execute_quote()`  
**Severity:** Blocker

The `if row["executed"]` check reads from the database *outside* the
`_execute_lock`. Two threads can both read `executed = 0`, both pass the
check, and both enter the `with _execute_lock` block. The Python-level
lock only prevents the UPDATE statements from interleaving; it does not
make the read-check-write sequence atomic. With N=20 concurrent callers
(a realistic retry storm after a timeout) this race is reliably triggered.

**Production impact:** The same quote executes twice. The customer is
debited twice from the source-currency balance — except there are no
balance tables (see BUG-2), so the phantom double-transaction goes
undetected. When balances are added, this will immediately cause silent
double-charges.

**Fix:** Use a single `BEGIN IMMEDIATE` SQLite transaction that wraps the
SELECT, the executed check, and the UPDATE. `BEGIN IMMEDIATE` acquires a
write reservation immediately, so concurrent callers block at the
transaction boundary, not at a Python lock that can be bypassed.

---

### BUG-2 — Missing customer balances: execute never moves money

**File:** `fx.py`, `execute_quote()`; `db.py`, schema  
**Severity:** Blocker

There are no `customers` or `balances` tables in the schema. The
assignment requires "debit the source-currency balance and credit the
destination-currency balance for the customer … atomic: both legs succeed
or neither." The current code marks the quote executed and records a
`transactions` row, but no actual accounting occurs. Every execution is a
phantom transaction.

**Production impact:** Any deployed version of this code would process FX
transactions without moving money. Balances would never change. This is a
complete functional failure of the core product requirement.

**Fix:** Add `customers` and `balances` tables. Inside the same
`BEGIN IMMEDIATE` transaction: verify the customer has sufficient source
balance, debit it, credit the destination balance.

---

### BUG-3 — No balance check: overdraft silently allowed

**File:** `fx.py`, `execute_quote()`  
**Severity:** Blocker

Even if balances existed, there is no check that the customer has
sufficient funds before debiting. A customer with zero balance could
execute a $1,000,000 FX quote.

**Production impact:** Silent overdraft. In a settlement system this
creates real liabilities. In a B2C system it is a fraud vector.

**Fix:** Inside the execute transaction, read `current_balance` and raise
`InsufficientFundsError` (→ HTTP 422) before any mutation if
`from_amount > current_balance`.

---

## HIGH

### BUG-4 — Rate recalculated at execution, not taken from quote

**File:** `fx.py`, `execute_quote()`, line ~88  
**Severity:** High

```python
current_rate = self._effective_rate(row["from_currency"], row["to_currency"])
```

The execute function re-derives the effective rate from the *current*
market rates rather than reading the rate stored in `row["rate"]`. A quote
is a contractual promise: "give me 100 USD and you will receive 12,900 KES
for the next 60 seconds." If rates move between quote generation and
execution (realistic for volatile pairs like NGN), the customer receives a
different amount than agreed.

**Production impact:** Customer receives a different `final_amount` than
`quotes.final_amount`. This is a contractual violation and will produce
support tickets. For large NGN or KES amounts, the discrepancy is material.

**Fix:** Read `rate = Decimal(row["rate"])` and compute
`final = (amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)`. The
`current_rate` variable should not exist in this function.

---

### BUG-5 — Idempotency TOCTOU: concurrent retries can double-execute

**File:** `fx.py`, `execute_quote()`  
**Severity:** High

The idempotency check and the idempotency insert run in separate database
connections:

```python
# Connection 1: check
with get_db() as conn:
    row = conn.execute("SELECT response FROM idempotency WHERE key = ?", ...).fetchone()
    if row:
        return json.loads(row["response"])

# ... execution logic in Connection 2 ...

# Connection 2: insert
conn.execute("INSERT INTO idempotency (key, response) ...", ...)
```

Two concurrent requests with the same `Idempotency-Key` can both pass the
check (both see no existing record), both execute the underlying quote (the
second will fail at the `executed` check — if it hasn't already raced past
it per BUG-1), and both attempt to insert the idempotency record.

**Production impact:** In a retry storm (client retries 3× on timeout),
all three requests may arrive before the first commits. Combined with
BUG-1, this can produce multiple executions for the same idempotency key —
directly violating the idempotency guarantee.

**Fix:** Include both the idempotency SELECT and INSERT inside the same
`BEGIN IMMEDIATE` transaction as the rest of the execute logic.

---

### BUG-6 — Cross-pair spread direction wrong: KES→NGN uses sell instead of 1/buy

**File:** `fx.py`, `_effective_rate()`, lines ~142–149  
**Severity:** High

```python
leg1 = self.rates.get(f"{from_ccy}/USD") or self.rates.get(f"USD/{from_ccy}")
leg2 = self.rates.get(f"USD/{to_ccy}") or self.rates.get(f"{to_ccy}/USD")
if leg1 and leg2:
    return leg1["sell"] * leg2["sell"]
```

For KES→NGN: `rates.get("KES/USD")` returns None, so `leg1` falls back to
`rates.get("USD/KES")`. `leg1["sell"]` is then `sell(USD/KES)` = 130.15
(KES per USD). But the customer is *selling* KES to acquire USD — the bank
*buys* KES at the buy rate (128.85 KES/USD). The correct leg-1 rate is
`1 / buy(USD/KES)` = 1/128.85 ≈ 0.00776 USD per KES.

Using `sell` for leg-1 returns `130.15 × sell(USD/NGN)` ≈ 130.15 ×
1587.9 ≈ **206,617 NGN per KES**, which is off by a factor of ~10,000.

**Production impact:** Every KES→NGN or NGN→KES transaction would use a
rate that is orders of magnitude wrong. Customers would receive wildly
incorrect amounts. This is an immediate critical P0 on any deployment.

**Fix:**
```python
leg1_rate = Decimal("1") / buy_rate_of(f"USD/{from_ccy}")   # selling from_ccy for USD
leg2_rate = sell_rate_of(f"USD/{to_ccy}")                    # buying to_ccy with USD
return leg1_rate * leg2_rate
```

---

## MEDIUM

### BUG-7 — Float conversion loses Decimal precision

**File:** `fx.py`, `generate_quote()`, lines 54–57  
**Severity:** Medium

```python
final = float(amount) * float(rate)
final_decimal = Decimal(str(final)).quantize(QUANTUM, rounding=ROUND_HALF_UP)
```

IEEE-754 double precision has ~15–16 significant decimal digits. For large
NGN amounts (mid-rate ~1,480 NGN/USD, amounts up to 1M NGN), the product
has 10+ digits before the decimal point, leaving fewer than 6 significant
fractional digits — enough for this test data, but not for production where
amounts can be much larger. The float → str → Decimal round-trip also
introduces silent truncation at the float representation boundary.

Concretely: `float(Decimal("7892.35")) * float(Decimal("1587.90000"))` may
not equal `Decimal("7892.35") * Decimal("1587.90000")` due to float's
binary rounding.

**Production impact:** Rounding errors in `final_amount`. The customer
could receive 1 kobo more or less than the exact calculation. At scale this
accumulates. Audits will show discrepancies between the agreed `final_amount`
and the actual received amount.

**Fix:** Use `Decimal` arithmetic throughout:
```python
final_decimal = (amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)
```

---

### BUG-8 — Inverse pair uses 1/mid instead of 1/buy: spread-free rate given to customer

**File:** `fx.py`, `_effective_rate()`, inverse branch  
**Severity:** Medium

```python
inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
if inverse is not None:
    mid = (inverse["buy"] + inverse["sell"]) / 2
    return Decimal("1") / mid
```

For KES→USD (using USD/KES rates): `mid = (128.85 + 130.15) / 2 = 129.50`.
Rate = 1/129.50 = 0.007722 USD/KES.

Correct: rate = 1/buy(USD/KES) = 1/128.85 = 0.007761 USD/KES.

The customer is selling KES to the bank; the bank pays them in USD at the
*bank's buy rate for KES* (buy side of USD/KES). Using the mid rate gives
the customer a spread-free inverse — 0.5% revenue per transaction is silently
given away.

**Production impact:** Systematic revenue leak on all inverse-pair transactions
(EUR→USD, KES→USD, NGN→USD, KES→EUR, NGN→EUR — five of the twelve pairs).
At 1,000 transactions/day this is a material P&L impact.

**Fix:** `return Decimal("1") / inverse["buy"]`

---

## LOW

### BUG-9 — SQLite without WAL mode: concurrent reads blocked by writes

**File:** `db.py`  
**Severity:** Low

`sqlite3.connect(DB_PATH)` uses the default DELETE/ROLLBACK journal mode.
In this mode, any write (even a brief one to mark a quote executed) holds a
PENDING/EXCLUSIVE lock that blocks all concurrent readers. Under the
concurrent execution test (`test_fx.py` only has 1–2 threads), this is not
visible, but under production traffic the `/rates` and `/healthz` endpoints
will stall during every execute.

**Production impact:** Request queuing and elevated latency for read
endpoints during write bursts. Under 10+ concurrent executes, readers
experience > 100ms delays.

**Fix:** `conn.execute("PRAGMA journal_mode=WAL")` at startup. WAL allows
concurrent readers and one writer.

---

### BUG-10 — Missing /healthz and /metrics endpoints

**File:** `app.py`  
**Severity:** Low

The assignment spec and standard production practice require both. Absent
`/healthz`, load balancers and orchestrators (ECS, Kubernetes) cannot
perform health checks, and the service cannot be safely deployed behind any
proxy. Absent `/metrics`, there is no observability into quote generation
rates, execute failure modes, or rate staleness.

**Fix:** Add both endpoints. `/healthz` should run `SELECT 1` against the
DB and check rate age. `/metrics` should emit Prometheus counters for quotes
generated/executed and rate refresh outcomes.

---

## Not flagged

- The `SPREAD_BPS` naming (`BPS = "basis points"` but the value is `0.005
  = 0.5% = 50 bps`) is a naming inconsistency but does not affect correctness.
- `import json` inside function bodies is a minor style issue, not a bug.
- The `EUR/USD` seed mid of 1.087 is inconsistent with `USD/EUR` mid of 0.92
  (1/0.92 ≈ 1.087 — this is actually consistent when accounting for a spread).
  I considered flagging this but after calculating the spread arithmetic it
  is internally consistent, so I am not flagging it.
