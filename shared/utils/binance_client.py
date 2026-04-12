"""
Market Data Client: Bybit (основной) + Binance через прокси (опционально)

Bybit: нет гео-ограничений, не нужен прокси, работает с Render US
Binance: включается через USE_BINANCE=true + PROXY_LIST в env

ФИКСЫ:
- Bybit по умолчанию (быстрый старт, нет TLS-in-TLS проблем)
- Watchlist фильтр снижен до $5M (больше символов)
- Startup не блокируется долгими запросами
- Прокси используются только для Binance если USE_BINANCE=true
"""

import os
import asyncio
import aiohttp
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime
import time


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
    """
    Клиент рыночных данных.
    USE_BINANCE=false (default) → Bybit, без прокси, без ограничений
    USE_BINANCE=true            → Binance через прокси из PROXY_LIST
    """

    BYBIT_URL = "https://api.bybit.com"
    BINANCE_URL = "https://fapi.binance.com"

    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_request_time = 0.0
        self.min_request_interval = 0.05

        # USE_BINANCE=true → пробуем Binance через прокси
        use_binance_env = os.getenv("USE_BINANCE", "false").lower()
        self._try_binance = use_binance_env == "true"
        self._use_binance = False  # установится после проверки

        # Прокси: читаем из PROXY_LIST
        proxy_env = os.getenv("PROXY_LIST", "")
        self._proxies = [p.strip() for p in proxy_env.split(",") if p.strip()]
        self._proxy_idx = 0
        self._active_proxy: Optional[str] = None

        print(f"🔧 Market client: {'Binance+proxy' if self._try_binance else 'Bybit'} mode")

    def _next_proxy(self) -> Optional[str]:
        if not self._proxies:
            return None
        p = self._proxies[self._proxy_idx % len(self._proxies)]
        self._proxy_idx += 1
        return p

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            # Используем TCPConnector с ssl=False для прокси
            connector = aiohttp.TCPConnector(ssl=False)
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    async def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _init_source(self):
        """Определяем источник данных один раз при первом запросе"""
        if hasattr(self, '_source_ready'):
            return
        self._source_ready = True

        if not self._try_binance:
            self._use_binance = False
            print("✅ Data source: Bybit (default)")
            return

        # Пробуем Binance через прокси
        for proxy in self._proxies[:3]:
            try:
                session = await self._get_session()
                async with session.get(
                    f"{self.BINANCE_URL}/fapi/v1/time",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=6),
                    ssl=False
                ) as resp:
                    if resp.status == 200:
                        self._use_binance = True
                        self._active_proxy = proxy
                        host = proxy.split('@')[-1] if '@' in proxy else proxy
                        print(f"✅ Data source: Binance via proxy ({host})")
                        return
            except Exception:
                continue

        self._use_binance = False
        print("⚠️ Binance unavailable via proxy. Falling back to Bybit.")

    # =========================================================================
    # BYBIT REQUESTS
    # =========================================================================

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
        except Exception as e:
            return None

    # =========================================================================
    # BINANCE REQUESTS (через прокси)
    # =========================================================================

    async def _binance(self, endpoint: str, params: Dict = None) -> Optional[Any]:
        await self._rate_limit()
        proxy = self._active_proxy or self._next_proxy()
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.BINANCE_URL}{endpoint}",
                params=params or {},
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception:
            return None

    async def _req(self, binance_ep: str, bybit_ep: str,
                   binance_params: Dict = None,
                   bybit_params: Dict = None) -> Optional[Any]:
        """Универсальный запрос: Binance или Bybit в зависимости от конфига"""
        await self._init_source()
        if self._use_binance:
            return await self._binance(binance_ep, binance_params)
        return await self._bybit(bybit_ep, bybit_params)

    # =========================================================================
    # SYMBOLS
    # =========================================================================

    async def get_all_symbols(self, min_volume_usdt: float = 5_000_000) -> List[str]:
        """Получить список символов. Быстрый старт — сначала пробуем Bybit."""
        await self._init_source()

        if self._use_binance:
            syms = await self._symbols_binance(min_volume_usdt)
            if syms:
                return syms

        syms = await self._symbols_bybit(min_volume_usdt)
        return syms if syms else FALLBACK_WATCHLIST

    async def _symbols_bybit(self, min_vol: float) -> List[str]:
        try:
            result = await self._bybit("/v5/market/tickers", {"category": "linear"})
            if not result:
                return FALLBACK_WATCHLIST
            tickers = result.get("list", [])
            syms = [
                t["symbol"] for t in tickers
                if t.get("symbol", "").endswith("USDT")
                and float(t.get("turnover24h", 0)) >= min_vol
            ]
            # Сортируем по объёму (самые ликвидные первые)
            vols = {t["symbol"]: float(t.get("turnover24h", 0)) for t in tickers}
            syms.sort(key=lambda s: vols.get(s, 0), reverse=True)
            result_list = syms[:200]
            print(f"✅ Bybit watchlist: {len(result_list)} symbols")
            return result_list if result_list else FALLBACK_WATCHLIST
        except Exception as e:
            print(f"Bybit symbols error: {e}")
            return FALLBACK_WATCHLIST

    async def _symbols_binance(self, min_vol: float) -> List[str]:
        try:
            info = await self._binance("/fapi/v1/exchangeInfo")
            if not info:
                return []
            syms = [
                s["symbol"] for s in info.get("symbols", [])
                if s.get("symbol", "").endswith("USDT")
                and s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
            ]
            # Фильтр по объёму (проверяем первые 60)
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

    # =========================================================================
    # PRICE
    # =========================================================================

    async def get_price(self, symbol: str) -> Optional[float]:
        await self._init_source()
        if self._use_binance:
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

    # =========================================================================
    # KLINES
    # =========================================================================

    async def get_klines(self, symbol: str, interval: str = "1h",
                         limit: int = 100) -> List[CandleData]:
        await self._init_source()
        if self._use_binance:
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
        result = await self._bybit("/v5/market/kline",
                                   {"category": "linear", "symbol": symbol,
                                    "interval": imap.get(interval, "60"),
                                    "limit": limit})
        if not result:
            return []
        candles = [
            CandleData(int(c[0]), float(c[1]), float(c[2]),
                       float(c[3]), float(c[4]), float(c[5]),
                       float(c[6]) if len(c) > 6 else 0.0)
            for c in result.get("list", [])
        ]
        candles.reverse()  # Bybit: новые→старые, нам нужно старые→новые
        return candles

    async def get_24h_ticker(self, symbol: Optional[str] = None) -> Optional[Dict]:
        await self._init_source()
        if self._use_binance:
            params = {"symbol": symbol} if symbol else {}
            return await self._binance("/fapi/v1/ticker/24hr", params)
        if symbol:
            result = await self._bybit("/v5/market/tickers",
                                       {"category": "linear", "symbol": symbol})
            if result:
                items = result.get("list", [])
                if items:
                    t = items[0]
                    pct = float(t.get("price24hPcnt", 0))
                    return {
                        "quoteVolume": t.get("turnover24h", 0),
                        "priceChangePercent": pct * 100  # Bybit даёт дробь (0.05 = 5%)
                    }
        return None

    # =========================================================================
    # FUNDING
    # =========================================================================

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        await self._init_source()
        if self._use_binance:
            d = await self._binance("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
            if d and len(d) > 0:
                return float(d[0].get("fundingRate", 0))
        result = await self._bybit("/v5/market/tickers",
                                   {"category": "linear", "symbol": symbol})
        if result:
            items = result.get("list", [])
            if items:
                fr = items[0].get("fundingRate")
                return float(fr) if fr else None
        return None

    async def get_funding_history(self, symbol: str, limit: int = 12) -> List[Dict]:
        await self._init_source()
        if self._use_binance:
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
        await self._init_source()
        if self._use_binance:
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
        await self._init_source()
        if self._use_binance:
            d = await self._binance("/fapi/v1/openInterestHist",
                                    {"symbol": symbol, "period": period, "limit": limit})
            return d or []
        imap = {"5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1d"}
        result = await self._bybit("/v5/market/open-interest",
                                   {"category": "linear", "symbol": symbol,
                                    "intervalTime": imap.get(period, "1h"),
                                    "limit": limit})
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
        await self._init_source()
        if self._use_binance:
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
                buy = items[0].get("buyRatio")
                return float(buy) * 100 if buy else 50.0
        return 50.0

    # =========================================================================
    # VOLUME PROFILE
    # =========================================================================

    async def get_agg_trades(self, symbol: str, start_time=None,
                             end_time=None, limit: int = 500) -> List[Dict]:
        if not self._use_binance:
            return []
        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._binance("/fapi/v1/aggTrades", params) or []

    async def get_hourly_volume_profile(self, symbol: str, hours: int = 7) -> List[float]:
        """Объёмы из часовых свечей — быстро и надёжно"""
        try:
            candles = await self.get_klines(symbol, "1h", hours + 2)
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
            await self._init_source()

            results = await asyncio.gather(
                self.get_price(symbol),
                self.get_funding_rate(symbol),
                self.get_open_interest(symbol),
                self.get_long_short_ratio(symbol),
                self.get_24h_ticker(symbol),
                self.get_klines(symbol, "1h", 100),
                return_exceptions=True
            )

            price, funding, oi, ratio, ticker, klines = results

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
            print(f"Market data error {symbol}: {e}")
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

_client = None

def get_binance_client() -> BinanceFuturesClient:
    global _client
    if _client is None:
        _client = BinanceFuturesClient()
    return _client
