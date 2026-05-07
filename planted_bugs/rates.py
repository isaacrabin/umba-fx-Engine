"""Rate provider — fetches and caches FX rates with buy/sell spreads."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional


# Stub mid-rates against USD. In production this would hit exchangeratesapi.io.
_SEED_MID = {
    "USD/EUR": Decimal("0.92"),
    "USD/KES": Decimal("129.50"),
    "USD/NGN": Decimal("1480.00"),
    "EUR/USD": Decimal("1.087"),
    "EUR/KES": Decimal("140.75"),
    "EUR/NGN": Decimal("1608.50"),
}

SPREAD_BPS = Decimal("0.005")  # 50 bps each side


def _with_spread(mid: Decimal) -> Dict[str, Decimal]:
    return {
        "buy": mid * (Decimal("1") - SPREAD_BPS),
        "sell": mid * (Decimal("1") + SPREAD_BPS),
    }


class RateProvider:
    def __init__(self):
        self._rates: Dict[str, Dict[str, Decimal]] = {
            pair: _with_spread(mid) for pair, mid in _SEED_MID.items()
        }
        self._last_updated = datetime.now(timezone.utc)

    def refresh(self):
        """Refresh rates from upstream. (Stubbed: re-applies the seed.)"""
        for pair, mid in _SEED_MID.items():
            self._rates[pair] = _with_spread(mid)
        self._last_updated = datetime.now(timezone.utc)

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        return {
            pair: {"buy": str(v["buy"]), "sell": str(v["sell"])}
            for pair, v in self._rates.items()
        }

    def get(self, pair: str) -> Optional[Dict[str, Decimal]]:
        return self._rates.get(pair)

    def last_updated_iso(self) -> str:
        return self._last_updated.isoformat()
