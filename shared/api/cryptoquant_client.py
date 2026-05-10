"""
CryptoQuant API Client — On-chain liquidation & exchange flow data
Docs: https://cryptoquant.com/docs
Free tier: 100 req/day, endpoints: /v1/btc/exchange-flows/*, derivatives/*

Used as fallback for liquidation data when Binance 418/400 & OKX unavailable.
"""

import os
import asyncio
import aiohttp
import time
from typing import Optional, Dict, List
from datetime import datetime


class CryptoQuantClient:
    BASE_URL = "https://api.cryptoquant.com/v1"

    # circuit breaker: pause for 1h after 3 consecutive 429/403 errors
    _cb_failures: int = 0
    _cb_blocked_until: float = 0.0
    CB_THRESHOLD = 3
    CB_COOLDOWN = 3600  # 1 hour

    # CryptoQuant symbol map: Bybit/Binance symbol → CQ exchange slug
    EXCHANGE_SLUG = "all_exchange"  # aggregated across all exchanges
    SYMBOL_MAP = {
        "BTCUSDT":  "btc",
        "ETHUSDT":  "eth",
        "SOLUSDT":  "sol",
        "XRPUSDT":  "xrp",
        "BNBUSDT":  "bnb",
        "DOGEUSDT": "doge",
        "ADAUSDT":  "ada",
        "AVAXUSDT": "avax",
        "LINKUSDT": "link",
        "DOTUSDT":  "dot",
        "LTCUSDT":  "ltc",
        "MATICUSDT":"matic",
        "UNIUSDT":  "uni",
        "ATOMUSDT": "atom",
        "BCHUSDT":  "bch",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("CRYPTOQUANT_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None

    def _symbol_to_cq(self, symbol: str) -> Optional[str]:
        """Convert BTCUSDT → btc. Returns None if unsupported."""
        return self.SYMBOL_MAP.get(symbol.upper())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
                connector=aiohttp.TCPConnector(ssl=False),
            )
        return self._session

    async def _get(self, path: str, params: Dict = None) -> Optional[Dict]:
        """Single GET with circuit breaker."""
        if not self.api_key:
            return None

        # circuit breaker
        if time.time() < CryptoQuantClient._cb_blocked_until:
            return None

        try:
            session = await self._get_session()
            async with session.get(
                f"{self.BASE_URL}{path}",
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    CryptoQuantClient._cb_failures = 0
                    return await resp.json()
                if resp.status in (429, 403, 401):
                    CryptoQuantClient._cb_failures += 1
                    if CryptoQuantClient._cb_failures >= self.CB_THRESHOLD:
                        CryptoQuantClient._cb_blocked_until = time.time() + self.CB_COOLDOWN
                        print(f"🔒 [CryptoQuant] Circuit breaker OPEN "
                              f"(status={resp.status}, will retry in 1h)")
                    else:
                        print(f"⚠️ [CryptoQuant] {path} HTTP {resp.status} "
                              f"(failure {CryptoQuantClient._cb_failures}/{self.CB_THRESHOLD})")
                return None
        except asyncio.TimeoutError:
            print(f"⚠️ [CryptoQuant] {path} timeout")
            return None
        except Exception as e:
            print(f"⚠️ [CryptoQuant] {path} error: {e}")
            return None

    async def get_liquidations(self, symbol: str, window: str = "h1") -> Optional[Dict]:
        """
        Get exchange liquidation volume for a symbol.
        window: h1 | h4 | d1

        Returns: {
            "total_usd": float,
            "long_liq_usd": float,
            "short_liq_usd": float,
            "dominant_side": "LONG" | "SHORT",
            "source": "cryptoquant"
        }
        Only supported for major coins in SYMBOL_MAP.
        """
        cq_sym = self._symbol_to_cq(symbol)
        if not cq_sym:
            return None  # unsupported altcoin → skip silently

        # CryptoQuant derivatives liquidation endpoint
        # GET /v1/{asset}/derivatives/liquidations
        path = f"/{cq_sym}/derivatives/liquidations"
        params = {
            "exchange": self.EXCHANGE_SLUG,
            "window": window,
            "limit": 1,
            "format": "json",
        }
        data = await self._get(path, params)
        if not data:
            return None

        try:
            result = data.get("result", {})
            rows = result.get("data", [])
            if not rows:
                return None

            row = rows[-1]  # most recent
            # CryptoQuant fields: liquidations_long_usd, liquidations_short_usd
            long_liq = float(row.get("liquidations_long_usd", 0))
            short_liq = float(row.get("liquidations_short_usd", 0))
            total = long_liq + short_liq

            if total <= 0:
                return None

            dominant = "LONG" if long_liq > short_liq else "SHORT"
            print(f"   ✅ [CryptoQuant] Liquidations {symbol}: "
                  f"${total:,.0f} ({dominant} dominant)")
            return {
                "total_usd": total,
                "long_liq_usd": long_liq,
                "short_liq_usd": short_liq,
                "dominant_side": dominant,
                "source": "cryptoquant",
            }
        except Exception as e:
            print(f"⚠️ [CryptoQuant] parse liquidations {symbol}: {e}")
            return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get predicted/current funding rate from CryptoQuant. Fallback use."""
        cq_sym = self._symbol_to_cq(symbol)
        if not cq_sym:
            return None
        path = f"/{cq_sym}/derivatives/funding-rates"
        params = {"exchange": "binance", "window": "h1", "limit": 1, "format": "json"}
        data = await self._get(path, params)
        if not data:
            return None
        try:
            rows = data.get("result", {}).get("data", [])
            if rows:
                return float(rows[-1].get("funding_rate", 0))
        except Exception:
            pass
        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ── Singleton ─────────────────────────────────────────────────────────────
_cq_client: Optional[CryptoQuantClient] = None


def get_cryptoquant_client() -> CryptoQuantClient:
    global _cq_client
    if _cq_client is None:
        _cq_client = CryptoQuantClient()
    return _cq_client
