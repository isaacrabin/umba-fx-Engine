# FX Engine (AI-generated baseline)

This is an AI-generated implementation of the FX engine described in
`ASSIGNMENT.md`. Treat it as a PR a teammate just opened.

Your review goes in `REVIEW.md` at the root of your submission. See
Part 3 of the assignment for instructions.

## Running it

```bash
cd planted_bugs
pip install -r requirements.txt
pytest
python app.py
```

Endpoints:

- `POST /quotes` — body `{"from_currency": "USD", "to_currency": "KES", "amount": "100"}`
- `POST /quotes/<quote_id>/execute` — header `Idempotency-Key: <key>` (optional)
- `POST /rates/refresh`
- `GET /rates`
