"""Flask app exposing the FX engine."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from db import init_db
from fx import FXEngine
from rates import RateProvider

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)
rate_provider = RateProvider()
engine = FXEngine(rate_provider)
init_db()


@app.before_request
def _attach_correlation_id():
    request.environ["correlation_id"] = (
        request.headers.get("X-Request-Id") or str(uuid.uuid4())
    )


@app.errorhandler(Exception)
def _handle_unexpected(exc):
    if isinstance(exc, HTTPException):
        return exc
    cid = request.environ.get("correlation_id", "-")
    log.exception("unhandled exception cid=%s", cid)
    return jsonify({"error": "internal_error", "correlation_id": cid}), 500


@app.post("/quotes")
def create_quote():
    data = request.get_json() or {}
    try:
        from_ccy = data["from_currency"].upper()
        to_ccy = data["to_currency"].upper()
        amount = Decimal(str(data["amount"]))
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"invalid request: {e}"}), 400

    try:
        quote = engine.generate_quote(from_ccy, to_ccy, amount)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info(
        "created quote %s %s->%s amount=%s",
        quote.id, from_ccy, to_ccy, amount,
    )
    return jsonify(quote.to_dict()), 201


@app.post("/quotes/<quote_id>/execute")
def execute_quote(quote_id):
    idempotency_key = request.headers.get("Idempotency-Key")
    try:
        result = engine.execute_quote(
            quote_id, idempotency_key=idempotency_key
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info("executed quote %s", quote_id)
    return jsonify(result), 200


@app.post("/rates/refresh")
def refresh_rates():
    rate_provider.refresh()
    return (
        jsonify(
            {"status": "ok", "updated_at": rate_provider.last_updated_iso()}
        ),
        200,
    )


@app.get("/rates")
def get_rates():
    return jsonify(rate_provider.snapshot()), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
