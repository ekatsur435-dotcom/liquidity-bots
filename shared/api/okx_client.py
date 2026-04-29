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
                        elif data.get("code") == "51001":
                            # Instrument not found - normal for some tickers
                            return None
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
    # RECENT TRADES (ЛЕНТА СДЕЛОК)
    # =========================================================================
    
    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        """
        Получить ленту сделок (Recent Trades)
        
        Args:
            symbol: Тикер, например "BTCUSDT"
            limit: Количество сделок (max 100)
        
        Returns:
            Список сделок:
            [
                {
                    "price": float,
                    "size": float,
                    "side": str,  # 'buy' или 'sell'
                    "timestamp": int,
                    "trade_id": str
                }
            ]
        """
        okx_symbol = self._convert_symbol(symbol)
        
        endpoint = "/api/v5/market/trades"
        params = {
            "instId": okx_symbol,
            "limit": str(min(limit, 100))  # Max 100
        }
        
        data = await self._make_request(endpoint, params)
        if data:
            trades = []
            for item in data:
                # side: 'buy' = мейкер продал (агрессор покупал)
                # side: 'sell' = мейкер купил (агрессор продавал)
                side = item.get("side", "")
                price = float(item.get("px", 0))
                size = float(item.get("sz", 0))
                
                trades.append({
                    "price": price,
                    "size": size,
                    "side": side,  # 'buy' или 'sell'
                    "timestamp": int(item.get("ts", 0)),
                    "trade_id": item.get("tradeId", ""),
                    "usd_value": price * size
                })
            
            return trades
        return []
    
    async def analyze_recent_trades(self, symbol: str, limit: int = 100) -> Optional[Dict]:
        """
        Проанализировать ленту сделок
        
        Returns:
            {
                "total_trades": int,
                "buy_volume": float,  # USD
                "sell_volume": float,  # USD
                "buy_count": int,
                "sell_count": int,
                "dominant_side": str,  # 'buy', 'sell', 'neutral'
                "avg_trade_size": float,
                "large_trades": List[Dict],  # Сделки > $50k
                "pressure": float,  # -1.0 to 1.0
                "signal": str  # 'strong_buy', 'buy', 'neutral', 'sell', 'strong_sell'
            }
        """
        trades = await self.get_recent_trades(symbol, limit)
        if not trades:
            return None
        
        buy_vol = 0.0
        sell_vol = 0.0
        buy_count = 0
        sell_count = 0
        large_trades = []
        
        min_large_usd = 50000  # $50k
        
        for trade in trades:
            usd = trade["usd_value"]
            
            if trade["side"] == "buy":
                buy_vol += usd
                buy_count += 1
            else:
                sell_vol += usd
                sell_count += 1
            
            # Крупные сделки
            if usd >= min_large_usd:
                large_trades.append({
                    "price": trade["price"],
                    "size": trade["size"],
                    "usd": usd,
                    "side": trade["side"]
                })
        
        total_vol = buy_vol + sell_vol
        total_count = len(trades)
        
        # Доминирующая сторона
        dominant = "neutral"
        if buy_vol > sell_vol * 1.5:
            dominant = "buy"
        elif sell_vol > buy_vol * 1.5:
            dominant = "sell"
        
        # Давление (-1.0 to 1.0)
        pressure = 0.0
        if total_vol > 0:
            pressure = (buy_vol - sell_vol) / total_vol
        
        # Сигнал
        signal = "neutral"
        if pressure > 0.6:
            signal = "strong_buy"
        elif pressure > 0.3:
            signal = "buy"
        elif pressure < -0.6:
            signal = "strong_sell"
        elif pressure < -0.3:
            signal = "sell"
        
        return {
            "symbol": symbol,
            "total_trades": total_count,
            "buy_volume": round(buy_vol, 2),
            "sell_volume": round(sell_vol, 2),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "dominant_side": dominant,
            "avg_trade_size": round(total_vol / total_count, 2) if total_count > 0 else 0,
            "large_trades": large_trades[:10],  # Топ 10
            "large_trade_count": len(large_trades),
            "pressure": round(pressure, 3),
            "signal": signal,
            "timestamp": int(time.time() * 1000)
        }
    
    # =========================================================================
    # ORDER BOOK (СТАКАН)
    # =========================================================================
    
    async def get_order_book(self, symbol: str, depth: int = 50) -> Optional[Dict]:
        """
        Получить стакан (Order Book)
        
        Args:
            symbol: Тикер, например "BTCUSDT"
            depth: Глубина стакана (5, 50, 400)
        
        Returns:
            Dict с bids и asks:
            {
                "symbol": str,
                "bids": [[price, size], ...],  # От high к low
                "asks": [[price, size], ...],  # От low к high
                "timestamp": int,
                "spread": float,
                "mid_price": float
            }
        """
        okx_symbol = self._convert_symbol(symbol)
        
        # OKX позволяет: 5, 50, 400 уровней
        valid_depths = [5, 50, 400]
        sz = min(valid_depths, key=lambda x: abs(x - depth))
        
        endpoint = "/api/v5/market/books"
        params = {
            "instId": okx_symbol,
            "sz": str(sz)
        }
        
        data = await self._make_request(endpoint, params)
        if data and len(data) > 0:
            item = data[0]
            
            bids = [[float(p), float(s)] for p, s in item.get("bids", [])]
            asks = [[float(p), float(s)] for p, s in item.get("asks", [])]
            
            # Рассчитываем метрики
            spread = 0.0
            mid_price = 0.0
            if bids and asks:
                best_bid = bids[0][0]
                best_ask = asks[0][0]
                spread = best_ask - best_bid
                mid_price = (best_bid + best_ask) / 2
            
            # Сортируем: bids по убыванию (high → low), asks по возрастанию (low → high)
            bids.sort(key=lambda x: x[0], reverse=True)
            asks.sort(key=lambda x: x[0])
            
            return {
                "symbol": symbol,
                "bids": bids,
                "asks": asks,
                "timestamp": int(item.get("ts", time.time() * 1000)),
                "spread": spread,
                "mid_price": mid_price,
                "spread_pct": (spread / mid_price * 100) if mid_price else 0
            }
        return None
    
    async def analyze_order_book(self, symbol: str, depth: int = 50) -> Optional[Dict]:
        """
        Провести анализ стакана
        
        Returns:
            {
                "symbol": str,
                "spread": float,
                "spread_pct": float,
                "bid_ask_imbalance": float,  # -1.0 to 1.0
                "total_bid_volume": float,
                "total_ask_volume": float,
                "big_walls": List[Dict],  # Крупные стены > $50k
                "support_levels": List[float],  # Уровни поддержки
                "resistance_levels": List[float],  # Уровни сопротивления
            }
        """
        ob = await self.get_order_book(symbol, depth)
        if not ob:
            return None
        
        bids = ob["bids"]
        asks = ob["asks"]
        mid_price = ob["mid_price"]
        
        # Объёмы
        total_bid_vol = sum(s for _, s in bids)
        total_ask_vol = sum(s for _, s in asks)
        total_vol = total_bid_vol + total_ask_vol
        
        # Имбаланс
        imbalance = 0.0
        if total_vol > 0:
            imbalance = (total_bid_vol - total_ask_vol) / total_vol
        
        # Крупные стены (> $50k)
        min_wall_usd = 50000
        big_walls = []
        
        for price, size in bids[:20]:  # Топ 20 bids
            usd = price * size
            if usd >= min_wall_usd:
                big_walls.append({
                    "side": "bid",
                    "price": price,
                    "size": size,
                    "usd": usd,
                    "distance_pct": (mid_price - price) / mid_price * 100
                })
        
        for price, size in asks[:20]:  # Топ 20 asks
            usd = price * size
            if usd >= min_wall_usd:
                big_walls.append({
                    "side": "ask", 
                    "price": price,
                    "size": size,
                    "usd": usd,
                    "distance_pct": (price - mid_price) / mid_price * 100
                })
        
        # Сортируем стены по размеру
        big_walls.sort(key=lambda x: x["usd"], reverse=True)
        
        # Уровни поддержки (кластеры bids)
        support_levels = []
        if len(bids) >= 5:
            # Находим кластеры объёма
            for i in range(0, min(10, len(bids)), 2):
                price = bids[i][0]
                support_levels.append(price)
        
        # Уровни сопротивления (кластеры asks)
        resistance_levels = []
        if len(asks) >= 5:
            for i in range(0, min(10, len(asks)), 2):
                price = asks[i][0]
                resistance_levels.append(price)
        
        return {
            "symbol": symbol,
            "spread": ob["spread"],
            "spread_pct": ob["spread_pct"],
            "bid_ask_imbalance": round(imbalance, 3),
            "total_bid_volume": round(total_bid_vol, 4),
            "total_ask_volume": round(total_ask_vol, 4),
            "big_walls": big_walls[:5],  # Топ 5 стен
            "support_levels": support_levels[:3],
            "resistance_levels": resistance_levels[:3],
            "timestamp": ob["timestamp"]
        }
    
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
