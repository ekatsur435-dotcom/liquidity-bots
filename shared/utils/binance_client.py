"""
Binance + Bybit Futures API Client
Bybit используется как fallback если Binance заблокирован (HTTP 451)
Bybit не имеет гео-ограничений → работает с Render US серверов
"""

import os
import asyncio
import aiohttp
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime
import time
import random


# Прокси — используются для Binance если он заблокирован напрямую
# Берутся из env PROXY_LIST (через запятую) или из этого списка
DEFAULT_PROXIES = [
    "http://w8S1GP:ps1b8h@186.65.114.244:9094",
    "http://Q9r7eX:ARt51J@163.198.135.24:8000",
    "http://FmE3ov:5yKd4y@161.0.18.201:8000",
    "http://UJDVUJ:rVPPZC@196.16.8.64:9323",
]

@dataclass
class CandleData:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass
class MarketData:
    symbol: str
    price: float
    rsi_1h: Optional[float]
    funding_rate: float
    funding_accumulated: float
    open_interest: float
    oi_change_4d: float
    long_short_ratio: float
    volume_24h: float
    volume_change_24h: float
    price_change_24h: float
    hourly_deltas: List[float]
    last_updated: datetime


FALLBACK_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT",
    "XLMUSDT", "BCHUSDT", "FILUSDT", "AAVEUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "TIAUSDT", "WLDUSDT", "ORDIUSDT", "LDOUSDT",
    "STXUSDT", "RUNEUSDT", "MKRUSDT", "SNXUSDT", "GALAUSDT",
    "SANDUSDT", "MANAUSDT", "AXSUSDT", "APEUSDT", "GMXUSDT",
    "DYDXUSDT", "FTMUSDT", "ALGOUSDT", "FLOWUSDT", "HBARUSDT",
    "QNTUSDT", "EGLDUSDT", "THETAUSDT", "BLURUSDT", "OCEANUSDT",
]


class BinanceFuturesClient:
    BINANCE_URL = "https://fapi.binance.com"
    BYBIT_URL = "https://api.bybit.com"

    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_request_time = 0.0
        self.min_request_interval = 0.05
        self._cache: Dict = {}
        self._cache_ttl = 60
        self._use_bybit = False
        self._source_checked = False
        # Прокси: из env или дефолтный список
        proxy_env = os.getenv("PROXY_LIST", "")
        self._proxies = [p.strip() for p in proxy_env.split(",") if p.strip()] or DEFAULT_PROXIES
        self._proxy_idx = 0

    def _next_proxy(self) -> Optional[str]:
        """Следующий прокси в ротации"""
        if not self._proxies:
            return None
        proxy = self._proxies[self._proxy_idx % len(self._proxies)]
        self._proxy_idx += 1
        return proxy

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _check_source(self):
        """Один раз определяем: Binance напрямую, Binance через прокси, или Bybit"""
        if self._source_checked:
            return
        self._source_checked = True

        # 1. Пробуем Binance напрямую
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.BINANCE_URL}/fapi/v1/time",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    self._use_bybit = False
                    print("✅ Data source: Binance Futures (direct)")
                    return
        except Exception:
            pass

        # 2. Пробуем Binance через прокси
        for proxy in self._proxies[:2]:  # Проверяем первые 2 прокси
            try:
                session = await self._get_session()
                async with session.get(
                    f"{self.BINANCE_URL}/fapi/v1/time",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    if resp.status == 200:
                        self._use_bybit = False
                        self._active_proxy = proxy
                        print(f"✅ Data source: Binance Futures (proxy {proxy.split('@')[-1]})")
                        return
            except Exception:
                continue

        # 3. Fallback: Bybit (нет гео-ограничений)
        self._use_bybit = True
        print("⚠️ Binance unavailable. Using Bybit as data source.")

    async def _binance(self, endpoint: str, params: Dict = None) -> Optional[Any]:
        """Запрос к Binance — сначала напрямую, потом через прокси"""
        await self._rate_limit()
        proxy = getattr(self, '_active_proxy', None)
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.BINANCE_URL}{endpoint}",
                params=params or {},
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 451 and self._proxies:
                    # Пробуем следующий прокси
                    next_proxy = self._next_proxy()
                    async with session.get(
                        f"{self.BINANCE_URL}{endpoint}",
                        params=params or {},
                        proxy=next_proxy,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp2:
                        if resp2.status == 200:
                            self._active_proxy = next_proxy
                            return await resp2.json()
                return None
        except Exception:
            return None

    async def _bybit(self, endpoint: str, params: Dict = None) -> Optional[Any]:
        await self._rate_limit()
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.BYBIT_URL}{endpoint}",
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("retCode") == 0:
                        return data.get("result")
                return None
        except Exception:
            return None

    # =========================================================================
    # SYMBOLS
    # =========================================================================

    async def get_all_symbols(self, min_volume_usdt: float = 10_000_000) -> List[str]:
        await self._check_source()
        if self._use_bybit:
            return await self._symbols_bybit(min_volume_usdt)
        symbols = await self._symbols_binance(min_volume_usdt)
        return symbols if symbols else await self._symbols_bybit(min_volume_usdt)

    async def _symbols_binance(self, min_vol: float) -> List[str]:
        try:
            info = await self._binance("/fapi/v1/exchangeInfo")
            if not info:
                return []
            syms = [s["symbol"] for s in info.get("symbols", [])
                    if s.get("symbol", "").endswith("USDT")
                    and s.get("status") == "TRADING"
                    and s.get("contractType") == "PERPETUAL"]
            filtered = []
            for sym in syms[:60]:
                t = await self._binance("/fapi/v1/ticker/24hr", {"symbol": sym})
                if t and float(t.get("quoteVolume", 0)) >= min_vol:
                    filtered.append(sym)
                if len(filtered) >= 50:
                    break
            return filtered or syms[:50]
        except Exception:
            return []

    async def _symbols_bybit(self, min_vol: float) -> List[str]:
        try:
            result = await self._bybit("/v5/market/tickers", {"category": "linear"})
            if not result:
                print(f"⚠️ Bybit also unavailable. Using fallback watchlist.")
                return FALLBACK_WATCHLIST
            tickers = result.get("list", [])
            syms = [t["symbol"] for t in tickers
                    if t.get("symbol", "").endswith("USDT")
                    and float(t.get("turnover24h", 0)) >= min_vol]
            print(f"✅ Bybit watchlist: {len(syms[:50])} symbols")
            return syms[:50] if syms else FALLBACK_WATCHLIST
        except Exception:
            return FALLBACK_WATCHLIST

    # =========================================================================
    # PRICE / KLINES
    # =========================================================================

    async def get_price(self, symbol: str) -> Optional[float]:
        await self._check_source()
        if not self._use_bybit:
            d = await self._binance("/fapi/v1/ticker/price", {"symbol": symbol})
            if d:
                return float(d["price"])
        result = await self._bybit("/v5/market/tickers",
                                   {"category": "linear", "symbol": symbol})
        if result:
            items = result.get("list", [])
            if items:
                p = items[0].get("lastPrice")
                return float(p) if p else None
        return None

    async def get_klines(self, symbol: str, interval: str = "1h",
                         limit: int = 100) -> List[CandleData]:
        await self._check_source()
        if not self._use_bybit:
            return await self._klines_binance(symbol, interval, limit)
        return await self._klines_bybit(symbol, interval, limit)

    async def _klines_binance(self, symbol, interval, limit) -> List[CandleData]:
        data = await self._binance("/fapi/v1/klines",
                                   {"symbol": symbol, "interval": interval, "limit": limit})
        if not data:
            return []
        return [CandleData(int(c[0]), float(c[1]), float(c[2]),
                           float(c[3]), float(c[4]), float(c[5]), float(c[7]))
                for c in data]

    async def _klines_bybit(self, symbol, interval, limit) -> List[CandleData]:
        imap = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                "1h": "60", "2h": "120", "4h": "240", "1d": "D"}
        bi = imap.get(interval, "60")
        result = await self._bybit("/v5/market/kline",
                                   {"category": "linear", "symbol": symbol,
                                    "interval": bi, "limit": limit})
        if not result:
            return []
        candles = [CandleData(
            int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]),
            float(c[5]), float(c[6]) if len(c) > 6 else 0.0
        ) for c in result.get("list", [])]
        candles.reverse()  # Bybit: новые → старые, нам нужно старые → новые
        return candles

    async def get_24h_ticker(self, symbol: Optional[str] = None) -> Optional[Dict]:
        await self._check_source()
        if not self._use_bybit:
            params = {"symbol": symbol} if symbol else {}
            return await self._binance("/fapi/v1/ticker/24hr", params)
        if symbol:
            result = await self._bybit("/v5/market/tickers",
                                       {"category": "linear", "symbol": symbol})
            if result:
                items = result.get("list", [])
                if items:
                    t = items[0]
                    return {
                        "quoteVolume": t.get("turnover24h", 0),
                        "priceChangePercent": float(t.get("price24hPcnt", 0)) * 100
                    }
        return None

    # =========================================================================
    # FUNDING
    # =========================================================================

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        await self._check_source()
        if not self._use_bybit:
            d = await self._binance("/fapi/v1/fundingRate",
                                    {"symbol": symbol, "limit": 1})
            if d and len(d) > 0:
                return float(d[0].get("fundingRate", 0))
        result = await self._bybit("/v5/market/tickers",
                                   {"category": "linear", "symbol": symbol})
        if result:
            items = result.get("list", [])
            if items:
                return float(items[0].get("fundingRate", 0))
        return None

    async def get_funding_history(self, symbol: str, limit: int = 12) -> List[Dict]:
        await self._check_source()
        if not self._use_bybit:
            d = await self._binance("/fapi/v1/fundingRate",
                                    {"symbol": symbol, "limit": limit})
            return d or []
        result = await self._bybit("/v5/market/funding/history",
                                   {"category": "linear", "symbol": symbol, "limit": limit})
        if result:
            return [{"fundingRate": item.get("fundingRate", 0)}
                    for item in result.get("list", [])]
        return []

    async def get_accumulated_funding(self, symbol: str, days: int = 4) -> float:
        history = await self.get_funding_history(symbol, days * 3)
        if not history:
            return 0.0
        return round(sum(float(h.get("fundingRate", 0)) for h in history) * 100, 4)

    # =========================================================================
    # OPEN INTEREST
    # =========================================================================

    async def get_open_interest(self, symbol: str) -> Optional[float]:
        await self._check_source()
        if not self._use_bybit:
            d = await self._binance("/fapi/v1/openInterest", {"symbol": symbol})
            if d:
                return float(d.get("openInterest", 0))
        result = await self._bybit("/v5/market/tickers",
                                   {"category": "linear", "symbol": symbol})
        if result:
            items = result.get("list", [])
            if items:
                oi = items[0].get("openInterest")
                return float(oi) if oi else None
        return None

    async def get_open_interest_history(self, symbol: str,
                                         period: str = "1h", limit: int = 5) -> List[Dict]:
        await self._check_source()
        if not self._use_bybit:
            d = await self._binance("/fapi/v1/openInterestHist",
                                    {"symbol": symbol, "period": period, "limit": limit})
            return d or []
        imap = {"5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1d"}
        result = await self._bybit("/v5/market/open-interest",
                                   {"category": "linear", "symbol": symbol,
                                    "intervalTime": imap.get(period, "1h"), "limit": limit})
        if result:
            return [{"sumOpenInterest": item.get("openInterest", 0)}
                    for item in result.get("list", [])]
        return []

    async def get_oi_change(self, symbol: str, days: int = 4) -> float:
        history = await self.get_open_interest_history(symbol, "1d", days + 1)
        if not history or len(history) < 2:
            return 0.0
        old = float(history[0].get("sumOpenInterest", 0))
        new = float(history[-1].get("sumOpenInterest", 0))
        return round((new - old) / old * 100, 2) if old else 0.0

    # =========================================================================
    # LONG/SHORT RATIO
    # =========================================================================

    async def get_long_short_ratio(self, symbol: str, period: str = "1h") -> Optional[float]:
        await self._check_source()
        if not self._use_bybit:
            d = await self._binance("/futures/data/topLongShortAccountRatio",
                                    {"symbol": symbol, "period": period, "limit": 1})
            if d and len(d) > 0:
                return float(d[0].get("longAccount", 0))
        result = await self._bybit("/v5/market/account-ratio",
                                   {"category": "linear", "symbol": symbol,
                                    "period": period, "limit": 1})
        if result:
            items = result.get("list", [])
            if items:
                return float(items[0].get("buyRatio", 0.5)) * 100
        return 50.0

    # =========================================================================
    # VOLUME PROFILE
    # =========================================================================

    async def get_agg_trades(self, symbol: str, start_time=None,
                             end_time=None, limit: int = 500) -> List[Dict]:
        if self._use_bybit:
            return []
        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._binance("/fapi/v1/aggTrades", params) or []

    async def get_hourly_volume_profile(self, symbol: str, hours: int = 7) -> List[float]:
        """Берём объёмы из часовых свечей — быстрее aggTrades"""
        try:
            candles = await self.get_klines(symbol, "1h", hours + 1)
            if candles and len(candles) >= hours:
                return [c.quote_volume for c in candles[-hours:]]
        except Exception:
            pass
        return [0.0] * hours

    # =========================================================================
    # COMPLETE MARKET DATA
    # =========================================================================

    async def get_complete_market_data(self, symbol: str) -> Optional[MarketData]:
        try:
            await self._check_source()

            price, funding, oi, ratio, ticker, klines = await asyncio.gather(
                self.get_price(symbol),
                self.get_funding_rate(symbol),
                self.get_open_interest(symbol),
                self.get_long_short_ratio(symbol),
                self.get_24h_ticker(symbol),
                self.get_klines(symbol, "1h", 100),
                return_exceptions=True
            )

            if isinstance(price, Exception) or not price:
                return None
            if isinstance(klines, Exception) or not klines or len(klines) < 20:
                return None

            funding = None if isinstance(funding, Exception) else funding
            oi = None if isinstance(oi, Exception) else oi
            ratio = None if isinstance(ratio, Exception) else ratio
            ticker = None if isinstance(ticker, Exception) else ticker

            rsi = self._calculate_rsi([c.close for c in klines])
            funding_acc = await self.get_accumulated_funding(symbol, 4)
            oi_change = await self.get_oi_change(symbol, 4)
            hourly_vols = await self.get_hourly_volume_profile(symbol, 7)

            return MarketData(
                symbol=symbol,
                price=float(price),
                rsi_1h=rsi,
                funding_rate=round(float(funding) * 100, 4) if funding else 0.0,
                funding_accumulated=funding_acc,
                open_interest=float(oi) if oi else 0.0,
                oi_change_4d=oi_change,
                long_short_ratio=float(ratio) if ratio else 50.0,
                volume_24h=float(ticker.get("quoteVolume", 0)) if isinstance(ticker, dict) else 0.0,
                volume_change_24h=float(ticker.get("priceChangePercent", 0)) if isinstance(ticker, dict) else 0.0,
                price_change_24h=float(ticker.get("priceChangePercent", 0)) if isinstance(ticker, dict) else 0.0,
                hourly_deltas=hourly_vols,
                last_updated=datetime.utcnow()
            )
        except Exception as e:
            print(f"Error market data {symbol}: {e}")
            return None

    # =========================================================================
    # RSI
    # =========================================================================

    def _calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        recent = deltas[-period:]
        avg_gain = sum(d for d in recent if d > 0) / period
        avg_loss = sum(-d for d in recent if d < 0) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


# ============================================================================
# SINGLETON
# ============================================================================

_binance_client = None

def get_binance_client() -> BinanceFuturesClient:
    global _binance_client
    if _binance_client is None:
        _binance_client = BinanceFuturesClient()
    return _binance_client
