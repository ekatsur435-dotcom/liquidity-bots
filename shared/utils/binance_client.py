"""
Binance Futures API Client
Бесплатно: 1200 запросов/мин
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


class BinanceFuturesClient:
    """Клиент для Binance Futures API (бесплатный tier)"""
    
    BASE_URL = "https://fapi.binance.com"
    
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Proxy support - rotation list
        self.proxies = self._load_proxies()
        self.current_proxy_index = 0
        
    def _load_proxies(self) -> List[str]:
        """Load proxy list from env or use defaults"""
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
        
        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = 0.05  # 20 запросов/сек макс (1200/мин)
        
        # Кэш
        self._cache = {}
        self._cache_ttl = 60  # 60 секунд
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать сессию с прокси"""
        if self.session is None or self.session.closed:
            connector = None
            if self.proxies:
                # Use rotating proxy
                proxy = self._get_next_proxy()
                if proxy:
                    connector = aiohttp.TCPConnector()
            
            self.session = aiohttp.ClientSession(
                headers={
                    "X-MBX-APIKEY": self.api_key
                } if self.api_key else {},
                connector=connector
            )
        return self.session
    
    async def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make request with proxy retry logic"""
        await self._rate_limit()
        
        url = f"{self.BASE_URL}{endpoint}"
        
        # Try without proxy first, then with proxies
        attempts = [(None, "no proxy")] + [(p, f"proxy {i+1}") for i, p in enumerate(self.proxies[:3])]
        
        for proxy, proxy_name in attempts:
            try:
                session = await self._get_session()
                async with session.get(
                    url, 
                    params=params or {}, 
                    timeout=15,
                    proxy=proxy
                ) as response:
                    if response.status == 451:  # Restricted location
                        print(f"⚠️ Binance blocked with {proxy_name}, trying next...")
                        continue
                    if response.status == 200:
                        return await response.json()
                    else:
                        print(f"⚠️ Binance error {response.status} with {proxy_name}")
                        
            except Exception as e:
                print(f"⚠️ Request failed with {proxy_name}: {e}")
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
        """Получить информацию о бирже (список пар)"""
        return await self._make_request("/fapi/v1/exchangeInfo")
    
    async def get_all_symbols(self, min_volume_usdt: float = 10_000_000) -> List[str]:
        """
        Получить список всех USDT perpetual пар
        
        Args:
            min_volume_usdt: Минимальный объём за 24ч ($10M по умолчанию)
        """
        try:
            info = await self.get_exchange_info()
            if not info:
                return []
            
            symbols = []
            for symbol_info in info.get("symbols", []):
                if (symbol_info.get("symbol", "").endswith("USDT") and
                    symbol_info.get("status") == "TRADING" and
                    symbol_info.get("contractType") == "PERPETUAL"):
                    symbols.append(symbol_info["symbol"])
            
            # Фильтруем по объёму
            if min_volume_usdt > 0:
                filtered_symbols = []
                for symbol in symbols[:50]:  # Проверяем топ-50
                    ticker = await self.get_24h_ticker(symbol)
                    if ticker and ticker.get("quoteVolume", 0) >= min_volume_usdt:
                        filtered_symbols.append(symbol)
                return filtered_symbols
            
            return symbols
        except Exception as e:
            print(f"Error getting symbols: {e}")
            return []
    
    # =========================================================================
    # PRICE DATA
    # =========================================================================
    
    async def get_klines(self, symbol: str, interval: str = "1h", 
                        limit: int = 100) -> List[CandleData]:
        """
        Получить свечи (OHLCV)
        
        Args:
            symbol: Торговая пара
            interval: Таймфрейм (1m, 5m, 15m, 1h, 4h, 1d)
            limit: Количество свечей (max 1500)
        """
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        data = await self._make_request("/fapi/v1/klines", params)
        
        if not data:
            return []
        
        candles = []
        for candle in data:
            # Binance format: [time, open, high, low, close, volume, ...]
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
        """Получить статистику за 24 часа"""
        params = {"symbol": symbol} if symbol else {}
        return await self._make_request("/fapi/v1/ticker/24hr", params)
    
    async def get_price(self, symbol: str) -> Optional[float]:
        """Получить текущую цену"""
        data = await self._make_request("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"]) if data else None
    
    # =========================================================================
    # FUNDING RATE
    # =========================================================================
    
    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Получить текущий фандинг рейт"""
        data = await self._make_request(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": 1}
        )
        
        if data and len(data) > 0:
            return float(data[0].get("fundingRate", 0))
        return None
    
    async def get_funding_history(self, symbol: str, limit: int = 12) -> List[Dict]:
        """
        Получить историю фандинга
        
        limit=12 → последние 4 дня (3 раза в день)
        """
        data = await self._make_request(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": limit}
        )
        return data or []
    
    async def get_accumulated_funding(self, symbol: str, days: int = 4) -> float:
        """Получить накопленный фандинг за N дней"""
        periods = days * 3  # 3 раза в день
        history = await self.get_funding_history(symbol, periods)
        
        if not history:
            return 0.0
        
        total = sum(float(h.get("fundingRate", 0)) for h in history)
        return round(total * 100, 4)  # В процентах
    
    # =========================================================================
    # OPEN INTEREST
    # =========================================================================
    
    async def get_open_interest(self, symbol: str, 
                                period: str = "1h") -> Optional[float]:
        """Получить открытый интерес"""
        data = await self._make_request(
            "/fapi/v1/openInterest",
            {"symbol": symbol}
        )
        
        if data:
            return float(data.get("openInterest", 0))
        return None
    
    async def get_open_interest_history(self, symbol: str, 
                                       period: str = "1h", 
                                       limit: int = 100) -> List[Dict]:
        """Получить историю OI"""
        return await self._make_request(
            "/fapi/v1/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit}
        ) or []
    
    async def get_oi_change(self, symbol: str, days: int = 4) -> float:
        """Получить изменение OI за N дней в процентах"""
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
        """
        Получить соотношение лонгов к шортам (long account ratio)
        
        Returns:
            Процент лонгов (e.g., 45.5 means 45.5% longs, 54.5% shorts)
        """
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
        """Получить агрегированные сделки (для расчёта дельты)"""
        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        
        return await self._make_request("/fapi/v1/aggTrades", params) or []
    
    async def get_hourly_volume_profile(self, symbol: str, 
                                       hours: int = 7) -> List[float]:
        """
        Получить профиль объёма по часам (приближение к дельте)
        
        Returns:
            Список объёмов за последние N часов
        """
        now = int(time.time() * 1000)
        one_hour = 60 * 60 * 1000
        
        volumes = []
        for i in range(hours, 0, -1):
            start = now - (i * one_hour)
            end = now - ((i - 1) * one_hour)
            
            trades = await self.get_agg_trades(symbol, start, end, 1000)
            volume = sum(float(t.get("q", 0)) for t in trades)
            volumes.append(volume)
            
            # Small delay to avoid rate limits
            await asyncio.sleep(0.1)
        
        return volumes
    
    # =========================================================================
    # COMPLETE MARKET DATA
    # =========================================================================
    
    async def get_complete_market_data(self, symbol: str) -> Optional[MarketData]:
        """
        Получить полные данные для анализа одним запросом
        
        Использует параллельные запросы для скорости
        """
        try:
            # Параллельные запросы
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
            
            if not all([price, klines]):
                return None
            
            # Расчёт RSI (упрощённый)
            rsi = self._calculate_rsi([c.close for c in klines])
            
            # Накопленный фандинг
            funding_acc = await self.get_accumulated_funding(symbol, 4)
            
            # Изменение OI
            oi_change = await self.get_oi_change(symbol, 4)
            
            # Приближение дельты по часам (через объём)
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
        """
        Сканировать все символы параллельно
        
        Args:
            symbols: Список символов для сканирования
        
        Returns:
            Список MarketData для каждого символа
        """
        tasks = [self.get_complete_market_data(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Фильтруем ошибки и None
        valid_results = []
        for result in results:
            if isinstance(result, MarketData):
                valid_results.append(result)
            elif isinstance(result, Exception):
                print(f"Scan error: {result}")
        
        return valid_results
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    @staticmethod
    def _calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """Расчёт RSI (упрощённый)"""
        if len(prices) < period + 1:
            return None
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        if len(gains) < period:
            return None
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(rsi, 2)


# ============================================================================
# SINGLETON
# ============================================================================

_client = None

def get_binance_client() -> BinanceFuturesClient:
    """Получить singleton instance"""
    global _client
    if _client is None:
        _client = BinanceFuturesClient()
    return _client


# ============================================================================
# EXAMPLE
# ============================================================================

async def test():
    """Тест клиента"""
    client = BinanceFuturesClient()
    
    # Получить символы
    symbols = await client.get_all_symbols(min_volume_usdt=50_000_000)
    print(f"Found {len(symbols)} symbols with >$50M volume")
    print(f"Top 5: {symbols[:5]}")
    
    # Получить данные для одного символа
    if symbols:
        data = await client.get_complete_market_data(symbols[0])
        if data:
            print(f"\nData for {data.symbol}:")
            print(f"  Price: ${data.price:,.2f}")
            print(f"  RSI: {data.rsi_1h}")
            print(f"  Funding: {data.funding_rate}%")
            print(f"  OI Change 4d: {data.oi_change_4d}%")
            print(f"  L/S Ratio: {data.long_short_ratio}% longs")
    
    await client.close()


if __name__ == "__main__":
    asyncio.run(test())
