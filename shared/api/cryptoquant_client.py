"""
CryptoQuant API Client v1.0
Использует CryptoQuant API для получения данных о ликвидациях.
API Key задаётся через ENV: CRYPTOQUANT_API_KEY

Docs: https://cryptoquant.com/settings/api
"""

import os
import asyncio
import aiohttp
from typing import Optional, Dict

_CRYPTOQUANT_BASE = "https://api.cryptoquant.com/v1"

# symbol mapping Bybit → CryptoQuant slug
_SYMBOL_MAP = {
    "BTCUSDT": "btc", "ETHUSDT": "eth", "BNBUSDT": "bnb", "XRPUSDT": "xrp",
    "SOLUSDT": "sol", "ADAUSDT": "ada", "DOTUSDT": "dot", "MATICUSDT": "matic",
    "LINKUSDT": "link", "LTCUSDT": "ltc", "ARUSDT": "ar",
}


def _slug(symbol: str) -> str:
    """Convert BTCUSDT → btc"""
    base = symbol.replace("USDT", "").replace("BUSD", "").replace("PERP", "")
    return _SYMBOL_MAP.get(symbol, base.lower())


class CryptoQuantClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("CRYPTOQUANT_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        return self._session

    async def get_oi_history(self, symbol: str, period: str = "1h",
                              limit: int = 5) -> list:
        """
        Получить историю Open Interest через CryptoQuant.
        Returns: [{"sumOpenInterest": float, "timestamp": int}, ...]
        """
        if not self.api_key:
            return []
        try:
            slug = _slug(symbol)
            # Маппинг period → CryptoQuant window
            window_map = {
                "5m": "5m", "15m": "15m", "30m": "30m",
                "1h": "1h", "4h": "4h", "1d": "1d",
            }
            window = window_map.get(period.lower(), "1h")
            url = f"{_CRYPTOQUANT_BASE}/futures/{slug}/open-interest"
            params = {"window": window, "limit": limit}
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                rows = data.get("result", {}).get("data", [])
                if not rows:
                    return []
                result = []
                for r in rows:
                    oi = float(r.get("open_interest_usd", r.get("openInterest", 0)) or 0)
                    ts = int(r.get("date", r.get("ts", 0)) or 0)
                    if oi > 0:
                        result.append({"sumOpenInterest": oi, "timestamp": ts})
                result.sort(key=lambda x: x["timestamp"])
                return result
        except Exception as e:
            print(f"   ⚠️ CryptoQuant OI error ({symbol}): {e}")
            return []

    async def get_liquidations(self, symbol: str, hours: int = 1) -> Optional[Dict]:        """
        Получить объём ликвидаций за последний час.
        Returns: {"total_usd": float, "long_liq_usd": float, "short_liq_usd": float,
                  "dominant_side": "LONG"|"SHORT"}
        """
        if not self.api_key:
            return None
        try:
            slug = _slug(symbol)
            # CryptoQuant: /v1/futures/{slug}/liquidations
            # interval=1h, window=1 → последний 1ч
            url = f"{_CRYPTOQUANT_BASE}/futures/{slug}/liquidations"
            params = {"window": "1h", "limit": 1}
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                result = data.get("result", {})
                rows = result.get("data", [])
                if not rows:
                    return None
                row = rows[-1]
                # Field names vary; try common patterns
                long_liq = float(row.get("long_liquidations_usd", row.get("longLiquidationsUsd", 0)) or 0)
                short_liq = float(row.get("short_liquidations_usd", row.get("shortLiquidationsUsd", 0)) or 0)
                total = long_liq + short_liq
                if total <= 0:
                    return None
                print(f"   ✅ Liquidations from CryptoQuant ({slug}): ${total:,.0f}")
                return {
                    "total_usd": total,
                    "long_liq_usd": long_liq,
                    "short_liq_usd": short_liq,
                    "dominant_side": "LONG" if long_liq > short_liq else "SHORT",
                }
        except Exception as e:
            print(f"   ⚠️ CryptoQuant liquidation error ({symbol}): {e}")
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


_instance: Optional[CryptoQuantClient] = None


def get_cryptoquant_client() -> CryptoQuantClient:
    global _instance
    if _instance is None:
        _instance = CryptoQuantClient()
    return _instance
