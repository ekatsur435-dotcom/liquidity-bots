"""
Binance Futures API Client
Бесплатный tier: 1200 запросов/мин

FIXES:
- self.last_request_time и self.min_request_interval теперь инициализируются в __init__
  (раньше код был недостижим — был после `return proxy`)
- Добавлен fallback watchlist если Binance заблокирован (HTTP 451)
- _rate_limit() теперь работает корректно
"""

import os
import asyncio
import aiohttp
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
import time


@dataclass
class CandleData:
    """OHLCV данные свечи"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass
class MarketData:
    """Полные рыночные данные для анализа"""
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


# ✅ Fallback watchlist если Binance заблокирован (HTTP 451 с Render US серверов)
FALLBACK_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT",
    "XLMUSDT", "BCHUSDT", "FILUSDT", "AAVEUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "TIAUSDT", "WLDUSDT", "ORDIUSDT", "BLURUSDT",
    "LDOUSDT", "STXUSDT", "RUNEUSDT", "MKRUSDT", "SNXUSDT",
    "GALAUSDT", "SANDUSDT", "MANAUSDT", "AXSUSDT", "APEUSDT",
    "GMXUSDT", "DYDXUSDT", "OCEANUSDT", "FTMUSDT", "ALGOUSDT",
    "FLOWUSDT", "HBARUSDT", "QNTUSDT", "EGLDUSDT", "THETAUSDT",
]


class BinanceFuturesClient:
    """Клиент для Binance Futures API (бесплатный tier)"""
    
    BASE_URL = "https://fapi.binance.com"
    
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")
        self.session: Optional[aiohttp.ClientSession] = None
        
        # ✅ FIX: эти переменные ДОЛЖНЫ быть здесь, в __init__
        # Раньше они были после `return proxy` в _get_next_proxy() — недостижимый код!
        self.last_request_time = 0
        self.min_request_interval = 0.05  # 20 запросов/сек макс (1200/мин)
        self._cache = {}
        self._cache_ttl = 60
        
        # Proxy support
        self.proxies = self._load_proxies()
        self.current_proxy_index = 0
        
    def _load_proxies(self) -> List[str]:
        """Load proxy list from env"""
        proxy_env = os.getenv("PROXY_LIST", "")
        if proxy_env:
            return [p.strip() for p in proxy_env.split(",") if p.strip()]
        return []
    
    def _get_next_proxy(self) -> Optional[str]:
        """Get next proxy in rotation"""
        if not self.proxies:
            return None
        proxy = self.proxies[self.current_proxy_index]
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.proxies)
        return proxy
    
    async def _rate_limit(self):
        """Соблюдаем rate limit"""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать сессию"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self.api_key} if self.api_key else {}
            )
        return self.session
    
    async def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Make request with proxy retry logic"""
        await self._rate_limit()
        
        url = f"{self.BASE_URL}{endpoint}"
        
        # Try without proxy first, then with proxies
        proxy_list = [None] + self.proxies[:3]
        
        for proxy in proxy_list:
            try:
                session = await self._get_session()
                async with session.get(
                    url,
                    params=params or {},
                    timeout=aiohttp.ClientTimeout(total=15),
                    proxy=proxy
                ) as response:
                    if response.status == 451:  # Geo-restricted
                        print(f"⚠️ Binance HTTP 451 (geo-restricted) for {endpoint}")
                        return None
                    if response.status == 200:
                        return await response.json()
                    else:
                        print(f"⚠️ Binance error {response.status} for {endpoint}")
                        
            except Exception as e:
                print(f"⚠️ Request failed for {endpoint}: {type(e).__name__}: {e}")
                continue
        
        return None
    
    async def close(self):
        """Закрыть сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    # =========================================================================
    # BASE DATA
    # =========================================================================
    
    async def get_exchange_info(self) -> Optional[Dict]:
        return await self._make_request("/fapi/v1/exchangeInfo")
    
    async def get_all_symbols(self, min_volume_usdt: float = 10_000_000) -> List[str]:
        """
        Получить список всех USDT perpetual пар.
        Если Binance недоступен — возвращает fallback список.
        """
        try:
            info = await self.get_exchange_info()
            
            # ✅ Если Binance заблокирован — используем fallback
            if not info:
                print(f"⚠️ Binance unreachable. Using fallback watchlist ({len(FALLBACK_WATCHLIST)} symbols)")
                return FALLBACK_WATCHLIST
            
            symbols = []
            for symbol_info in info.get("symbols", []):
                if (symbol_info.get("symbol", "").endswith("USDT") and
                    symbol_info.get("status") == "TRADING" and
                    symbol_info.get("contractType") == "PERPETUAL"):
                    symbols.append(symbol_info["symbol"])
            
            if not symbols:
                print("⚠️ No symbols from Binance. Using fallback.")
                return FALLBACK_WATCHLIST
            
            # Фильтруем по объёму (только если можем получить данные)
            if min_volume_usdt > 0:
                filtered = []
                for symbol in symbols[:50]:
                    ticker = await self.get_24h_ticker(symbol)
                    if ticker and float(ticker.get("quoteVolume", 0)) >= min_volume_usdt:
                        filtered.append(symbol)
                
                if filtered:
                    print(f"✅ Binance watchlist: {len(filtered)} symbols (volume filter)")
                    return filtered
                else:
                    print("⚠️ Volume filter returned 0 symbols. Using raw list.")
                    return symbols[:50]
            
            return symbols[:50]
            
        except Exception as e:
            print(f"Error getting symbols: {e}. Using fallback watchlist.")
            return FALLBACK_WATCHLIST
    
    # =========================================================================
    # PRICE DATA
    # =========================================================================
    
    async def get_klines(self, symbol: str, interval: str = "1h",
                         limit: int = 100) -> List[CandleData]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = await self._make_request("/fapi/v1/klines", params)
        
        if not data:
            return []
        
        candles = []
        for candle in data:
            candles.append(CandleData(
                timestamp=candle[0],
                open=float(candle[1]),
                high=float(candle[2]),
                low=float(candle[3]),
                close=float(candle[4]),
                volume=float(candle[5]),
                quote_volume=float(candle[7])
            ))
        return candles
    
    async def get_24h_ticker(self, symbol: Optional[str] = None) -> Optional[Dict]:
        params = {"symbol": symbol} if symbol else {}
        return await self._make_request("/fapi/v1/ticker/24hr", params)
    
    async def get_price(self, symbol: str) -> Optional[float]:
        data = await self._make_request("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"]) if data else None
    
    # =========================================================================
    # FUNDING RATE
    # =========================================================================
    
    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        data = await self._make_request(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": 1}
        )
        if data and len(data) > 0:
            return float(data[0].get("fundingRate", 0))
        return None
    
    async def get_funding_history(self, symbol: str, limit: int = 12) -> List[Dict]:
        data = await self._make_request(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": limit}
        )
        return data or []
    
    async def get_accumulated_funding(self, symbol: str, days: int = 4) -> float:
        periods = days * 3
        history = await self.get_funding_history(symbol, periods)
        if not history:
            return 0.0
        total = sum(float(h.get("fundingRate", 0)) for h in history)
        return round(total * 100, 4)
    
    # =========================================================================
    # OPEN INTEREST
    # =========================================================================
    
    async def get_open_interest(self, symbol: str) -> Optional[float]:
        data = await self._make_request("/fapi/v1/openInterest", {"symbol": symbol})
        if data:
            return float(data.get("openInterest", 0))
        return None
    
    async def get_open_interest_history(self, symbol: str,
                                        period: str = "1h",
                                        limit: int = 100) -> List[Dict]:
        return await self._make_request(
            "/fapi/v1/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit}
        ) or []
    
    async def get_oi_change(self, symbol: str, days: int = 4) -> float:
        history = await self.get_open_interest_history(symbol, "1d", days + 1)
        if not history or len(history) < 2:
            return 0.0
        old_oi = float(history[0].get("sumOpenInterest", 0))
        new_oi = float(history[-1].get("sumOpenInterest", 0))
        if old_oi == 0:
            return 0.0
        return round((new_oi - old_oi) / old_oi * 100, 2)
    
    # =========================================================================
    # LONG/SHORT RATIO
    # =========================================================================
    
    async def get_long_short_ratio(self, symbol: str,
                                   period: str = "1h") -> Optional[float]:
        data = await self._make_request(
            "/futures/data/topLongShortAccountRatio",
            {"symbol": symbol, "period": period, "limit": 1}
        )
        if data and len(data) > 0:
            return float(data[0].get("longAccount", 0))
        return None
    
    # =========================================================================
    # VOLUME (Delta approximation)
    # =========================================================================
    
    async def get_agg_trades(self, symbol: str,
                             start_time: Optional[int] = None,
                             end_time: Optional[int] = None,
                             limit: int = 1000) -> List[Dict]:
        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._make_request("/fapi/v1/aggTrades", params) or []
    
    async def get_hourly_volume_profile(self, symbol: str, hours: int = 7) -> List[float]:
        """Профиль объёма по часам (приближение к дельте)"""
        now = int(time.time() * 1000)
        one_hour = 60 * 60 * 1000
        
        volumes = []
        for i in range(hours, 0, -1):
            start = now - (i * one_hour)
            end = now - ((i - 1) * one_hour)
            trades = await self.get_agg_trades(symbol, start, end, 1000)
            volume = sum(float(t.get("q", 0)) for t in trades)
            volumes.append(volume)
            await asyncio.sleep(0.1)
        
        return volumes
    
    # =========================================================================
    # COMPLETE MARKET DATA
    # =========================================================================
    
    async def get_complete_market_data(self, symbol: str) -> Optional[MarketData]:
        try:
            price_task = self.get_price(symbol)
            funding_task = self.get_funding_rate(symbol)
            oi_task = self.get_open_interest(symbol)
            ratio_task = self.get_long_short_ratio(symbol)
            ticker_task = self.get_24h_ticker(symbol)
            klines_task = self.get_klines(symbol, "1h", 100)
            
            price, funding, oi, ratio, ticker, klines = await asyncio.gather(
                price_task, funding_task, oi_task,
                ratio_task, ticker_task, klines_task
            )
            
            if not price or not klines:
                return None
            
            rsi = self._calculate_rsi([c.close for c in klines])
            funding_acc = await self.get_accumulated_funding(symbol, 4)
            oi_change = await self.get_oi_change(symbol, 4)
            hourly_volumes = await self.get_hourly_volume_profile(symbol, 7)
            
            return MarketData(
                symbol=symbol,
                price=price,
                rsi_1h=rsi,
                funding_rate=round(funding * 100, 4) if funding else 0.0,
                funding_accumulated=funding_acc,
                open_interest=oi or 0.0,
                oi_change_4d=oi_change,
                long_short_ratio=ratio or 50.0,
                volume_24h=float(ticker.get("quoteVolume", 0)) if ticker else 0.0,
                volume_change_24h=float(ticker.get("priceChangePercent", 0)) if ticker else 0.0,
                price_change_24h=float(ticker.get("priceChangePercent", 0)) if ticker else 0.0,
                hourly_deltas=hourly_volumes,
                last_updated=datetime.utcnow()
            )
        except Exception as e:
            print(f"Error getting complete data for {symbol}: {e}")
            return None
    
    async def scan_all_symbols(self, symbols: List[str]) -> List[MarketData]:
        tasks = [self.get_complete_market_data(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid_results = []
        for result in results:
            if isinstance(result, MarketData):
                valid_results.append(result)
            elif isinstance(result, Exception):
                print(f"Scan error: {result}")
        return valid_results
    
    # =========================================================================
    # RSI CALCULATION
    # =========================================================================
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Упрощённый расчёт RSI"""
        if len(prices) < period + 1:
            return None
        
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        
        if not gains and not losses:
            return 50.0
        
        recent = deltas[-period:]
        avg_gain = sum(d for d in recent if d > 0) / period
        avg_loss = sum(-d for d in recent if d < 0) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

_binance_client = None

def get_binance_client() -> BinanceFuturesClient:
    global _binance_client
    if _binance_client is None:
        _binance_client = BinanceFuturesClient()
    return _binance_client
