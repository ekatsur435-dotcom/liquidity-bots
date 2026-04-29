"""
OKX API Client
Публичные данные: OI, фандинг, ликвидации, volume - без API ключа!
https://www.okx.com/docs-v5/en/#rest-api-public-data
"""

import os
import asyncio
import time
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
import aiohttp


@dataclass
class OKXOpenInterest:
    """Open Interest данные"""
    symbol: str
    oi: float
    oi_change_24h: float
    timestamp: int


@dataclass
class OKXFundingRate:
    """Фандинг данные"""
    symbol: str
    funding_rate: float  # Текущий фандинг
    next_funding_time: int
    avg_24h_funding: Optional[float] = None
    extreme: bool = False  # > 0.1% или < -0.1%


@dataclass
class OKXLiquidation:
    """Данные о ликвидации"""
    symbol: str
    side: str  # 'long' или 'short'
    size: float
    price: float
    usd_value: float
    timestamp: int


@dataclass
class OKXTakerVolume:
    """Taker volume данные (альтернатива taker buy/sell ratio)"""
    symbol: str
    buy_volume: float
    sell_volume: float
    ratio: float  # buy / (buy + sell), 0.0-1.0
    period: str


class OKXClient:
    """
    OKX API Client - публичные данные без авторизации
    Базовый URL: https://www.okx.com
    """
    
    BASE_URL = "https://www.okx.com"
    
    def __init__(self):
        """Инициализация OKX клиента - API ключ НЕ требуется для публичных данных"""
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Rate limiting: 20 requests per 2 seconds = 10/sec
        self.last_request_time = 0
        self.min_interval = 0.1  # 100ms между запросами
        
        # Прокси поддержка
        proxy_env = os.getenv("PROXY_LIST", "")
        self._proxies = [p.strip() for p in proxy_env.split(",") if p.strip()]
        self._proxy_idx = 0
        self._proxy_enabled = os.getenv("USE_PROXY_FOR_OKX", "true").lower() == "true"
        
        # Статистика
        self._request_count = 0
        self._error_count = 0
        
        print("🚀 OKX Client initialized")
        print("   ✅ No API key required for public data")
        if self._proxies and self._proxy_enabled:
            print(f"   🌐 Proxy rotation enabled: {len(self._proxies)} proxies")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать сессию"""
        if self.session is None or self.session.closed:
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            connector = aiohttp.TCPConnector(ssl=False, limit=100)
            self.session = aiohttp.ClientSession(
                headers=headers, 
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session
    
    def _get_proxy(self) -> Optional[str]:
        """Получить следующий прокси из списка"""
        if not self._proxies or not self._proxy_enabled:
            return None
        proxy = self._proxies[self._proxy_idx]
        self._proxy_idx = (self._proxy_idx + 1) % len(self._proxies)
        return proxy
    
    async def _rate_limit(self):
        """Rate limiting между запросами"""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()
    
    async def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """
        Выполнить HTTP запрос к OKX API с rate limiting и retry
        """
        await self._rate_limit()
        
        session = await self._get_session()
        url = f"{self.BASE_URL}{endpoint}"
        
        # Пробуем с прокси и без
        attempts = []
        if self._proxy_enabled and self._proxies:
            attempts.append(self._get_proxy())
        attempts.append(None)  # Без прокси
        
        for attempt, proxy in enumerate(attempts):
            try:
                connector_params = {}
                if proxy:
                    connector_params["proxy"] = proxy
                    print(f"   🌐 Using proxy: {proxy[:20]}...")
                
                async with session.get(url, params=params, **connector_params) as response:
                    self._request_count += 1
                    
                    if response.status == 200:
                        data = await response.json()
                        if data.get("code") == "0":
                            return data.get("data", [])
                        else:
                            print(f"   ⚠️ OKX API error: {data.get('msg')}")
                            return None
                    elif response.status == 429:
                        print(f"   ⏳ Rate limited, waiting...")
                        await asyncio.sleep(1)
                        continue
                    else:
                        print(f"   ⚠️ HTTP {response.status}")
                        if attempt < len(attempts) - 1:
                            continue
                        return None
                            
            except Exception as e:
                print(f"   ⚠️ Request error: {e}")
                self._error_count += 1
                if attempt < len(attempts) - 1:
                    continue
                return None
        
        return None
    
    # =========================================================================
    # OPEN INTEREST
    # =========================================================================
    
    async def get_open_interest(self, symbol: str, period: str = "1h") -> Optional[OKXOpenInterest]:
        """
        Получить текущий Open Interest
        
        Args:
            symbol: Тикер, например "BTC-USDT-SWAP" или "BTCUSDT"
            period: Таймфрейм: 5m, 15m, 30m, 1H, 4H, 1D
        """
        # Конвертируем формат символа
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/public/open-interest"
        params = {
            "instType": "SWAP",
            "instId": okx_symbol,
            "period": period.upper()
        }
        
        data = await self._make_request(endpoint, params)
        if data and len(data) > 0:
            item = data[0]
            return OKXOpenInterest(
                symbol=symbol,
                oi=float(item.get("oi", 0)),
                oi_change_24h=float(item.get("oiRatio", 0)),
                timestamp=int(item.get("ts", 0))
            )
        return None
    
    async def get_open_interest_history(self, symbol: str, period: str = "1h", limit: int = 100) -> List[Dict]:
        """
        Получить историю Open Interest
        
        Returns:
            Список словарей с ключами: timestamp, sumOpenInterest
        """
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/public/open-interest"
        params = {
            "instType": "SWAP",
            "instId": okx_symbol,
            "period": period.upper(),
            "limit": str(limit)
        }
        
        data = await self._make_request(endpoint, params)
        if data:
            return [
                {
                    "timestamp": int(item.get("ts", 0)),
                    "sumOpenInterest": float(item.get("oi", 0)),
                    "oiChange": float(item.get("oiRatio", 0))
                }
                for item in data
            ]
        return []
    
    async def get_oi_change(self, symbol: str, days: int = 4) -> float:
        """Получить изменение OI за несколько дней (в %)"""
        history = await self.get_open_interest_history(symbol, "1D", days + 1)
        if not history or len(history) < 2:
            return 0.0
        
        old = history[0].get("sumOpenInterest", 0)
        new = history[-1].get("sumOpenInterest", 0)
        return round((new - old) / old * 100, 2) if old else 0.0
    
    # =========================================================================
    # FUNDING RATE
    # =========================================================================
    
    async def get_funding_rate(self, symbol: str) -> Optional[OKXFundingRate]:
        """
        Получить текущий funding rate
        
        Args:
            symbol: Тикер, например "BTCUSDT"
        """
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/public/funding-rate"
        params = {
            "instId": okx_symbol
        }
        
        data = await self._make_request(endpoint, params)
        if data and len(data) > 0:
            item = data[0]
            funding = float(item.get("fundingRate", 0))
            
            return OKXFundingRate(
                symbol=symbol,
                funding_rate=funding * 100,  # В процентах
                next_funding_time=int(item.get("fundingTime", 0)),
                extreme=abs(funding) > 0.001  # > 0.1%
            )
        return None
    
    async def get_funding_rate_history(self, symbol: str, limit: int = 100) -> List[Dict]:
        """Получить историю фандинга"""
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/public/funding-rate-history"
        params = {
            "instId": okx_symbol,
            "limit": str(limit)
        }
        
        data = await self._make_request(endpoint, params)
        if data:
            return [
                {
                    "fundingRate": float(item.get("fundingRate", 0)) * 100,
                    "fundingTime": int(item.get("fundingTime", 0))
                }
                for item in data
            ]
        return []
    
    # =========================================================================
    # LIQUIDATIONS
    # =========================================================================
    
    async def get_liquidations(self, symbol: str, limit: int = 100, mgnMode: str = "") -> Optional[Dict]:
        """
        Получить данные о ликвидациях
        
        Args:
            symbol: Тикер
            limit: Количество записей
            mgnMode: 'cross' (кросс-маржа) или 'isolated' (изолированная)
        """
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/public/liquidation-orders"
        params = {
            "instType": "SWAP",
            "instId": okx_symbol,
            "mgnMode": mgnMode or "cross",  # По умолчанию cross
            "limit": str(limit)
        }
        
        data = await self._make_request(endpoint, params)
        if data:
            long_liq = 0.0
            short_liq = 0.0
            
            for item in data:
                side = item.get("posSide", "")  # 'long' или 'short'
                sz = float(item.get("sz", 0))
                price = float(item.get("fillPx", 0))
                usd = sz * price
                
                if side == "long":
                    long_liq += usd
                else:
                    short_liq += usd
            
            total = long_liq + short_liq
            return {
                "total_usd": total,
                "long_liq_usd": long_liq,
                "short_liq_usd": short_liq,
                "dominant_side": "LONG" if long_liq > short_liq else "SHORT" if short_liq > long_liq else None,
                "count": len(data)
            }
        return None
    
    # =========================================================================
    # TAKER VOLUME (альтернатива taker buy/sell ratio)
    # =========================================================================
    
    async def get_taker_volume(self, symbol: str, period: str = "15m") -> Optional[OKXTakerVolume]:
        """
        Получить taker volume - альтернатива taker buy/sell ratio
        
        OKX не имеет прямого taker ratio, но можно оценить из:
        1. Candles (объём свечей)
        2. Recent trades volume
        
        Args:
            symbol: Тикер
            period: Таймфрейм для анализа
        """
        okx_symbol = self._convert_symbol(symbol)
        
        # Получаем свечи и анализируем объём
        endpoint = "/api/v5/market/candles"
        params = {
            "instId": okx_symbol,
            "bar": period,
            "limit": "20"
        }
        
        data = await self._make_request(endpoint, params)
        if data and len(data) >= 2:
            # Анализируем последние свечи
            total_vol = 0
            buy_vol = 0  # Свечи с close > open считаем "buy"
            
            for candle in data[:5]:  # Последние 5 свечей
                # Формат: [ts, o, h, l, c, vol, volCcy]
                if len(candle) >= 6:
                    open_p = float(candle[1])
                    close_p = float(candle[4])
                    vol = float(candle[5])
                    
                    total_vol += vol
                    if close_p > open_p:
                        buy_vol += vol
            
            if total_vol > 0:
                ratio = buy_vol / total_vol
                return OKXTakerVolume(
                    symbol=symbol,
                    buy_volume=buy_vol,
                    sell_volume=total_vol - buy_vol,
                    ratio=ratio,
                    period=period
                )
        
        return None
    
    # =========================================================================
    # LONG/SHORT RATIO
    # =========================================================================
    
    async def get_long_short_ratio(self, symbol: str, period: str = "5m") -> Optional[float]:
        """
        Получить Long/Short ratio (позиции трейдеров)
        
        Returns:
            Процент лонгов (0-100), или None если данных нет
        """
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/public/position-tiers"
        # Альтернатива: используем margin mode stats
        
        # OKX не имеет прямого endpoint для L/S ratio публично
        # Можно оценить из open interest + price action
        # Возвращаем None, будет использоваться estimation
        return None
    
    # =========================================================================
    # MARKET DATA
    # =========================================================================
    
    async def get_ticker(self, symbol: str) -> Optional[Dict]:
        """Получить текущий тикер (цена, объём, изменение)"""
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/market/ticker"
        params = {"instId": okx_symbol}
        
        data = await self._make_request(endpoint, params)
        if data and len(data) > 0:
            item = data[0]
            return {
                "symbol": symbol,
                "price": float(item.get("last", 0)),
                "volume_24h": float(item.get("vol24h", 0)),
                "volume_ccy_24h": float(item.get("volCcy24h", 0)),
                "price_change_24h": float(item.get("priceChange24h", 0)),
                "high_24h": float(item.get("high24h", 0)),
                "low_24h": float(item.get("low24h", 0)),
                "ts": int(item.get("ts", 0))
            }
        return None
    
    async def get_all_tickers(self) -> List[Dict]:
        """Получить все тикеры с объёмом"""
        endpoint = "/api/v5/market/tickers"
        params = {"instType": "SWAP"}
        
        data = await self._make_request(endpoint, params)
        if data:
            return [
                {
                    "symbol": item.get("instId", "").replace("-SWAP", "").replace("-", ""),
                    "price": float(item.get("last", 0)),
                    "volume_24h": float(item.get("vol24h", 0)),
                    "volume_usd": float(item.get("volCcy24h", 0)),
                    "oi": float(item.get("oi", 0)),
                    "oi_change": float(item.get("oiRatio", 0))
                }
                for item in data
            ]
        return []
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _convert_symbol(self, symbol: str) -> str:
        """
        Конвертировать символ в формат OKX
        
        Examples:
            BTCUSDT → BTC-USDT-SWAP
            ETHUSDT → ETH-USDT-SWAP
            BTC-USDT-SWAP → BTC-USDT-SWAP (уже в формате)
        """
        if "-SWAP" in symbol:
            return symbol
        if "USDT" in symbol:
            base = symbol.replace("USDT", "")
            return f"{base}-USDT-SWAP"
        return symbol
    
    def health_check(self) -> Dict:
        """Проверка здоровья клиента"""
        return {
            "requests_total": self._request_count,
            "errors_total": self._error_count,
            "error_rate": round(self._error_count / max(self._request_count, 1) * 100, 2),
            "proxies_available": len(self._proxies) if self._proxy_enabled else 0
        }
    
    async def close(self):
        """Закрыть сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
            print("👋 OKX Client closed")


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_okx_client: Optional[OKXClient] = None


def get_okx_client() -> OKXClient:
    """Получить singleton instance OKX клиента"""
    global _okx_client
    if _okx_client is None:
        _okx_client = OKXClient()
    return _okx_client


async def reset_okx_client():
    """Сбросить клиент (для тестирования)"""
    global _okx_client
    if _okx_client:
        await _okx_client.close()
    _okx_client = None
