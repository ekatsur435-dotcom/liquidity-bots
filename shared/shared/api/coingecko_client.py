"""
CoinGecko API Client
Бесплатный tier: 10-30 запросов/мин (без API ключа)
С API ключом: до 500 запросов/мин
https://www.coingecko.com/en/api/documentation
"""

import os
import asyncio
import time
import aiohttp
from typing import Optional, Dict, List, Any, Set
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class CoinGeckoMarketData:
    """Данные с CoinGecko"""
    symbol: str
    coin_id: str
    price: float
    market_cap: float
    total_volume: float
    price_change_1h: float
    price_change_24h: float
    price_change_7d: float
    price_change_30d: float
    ath: float  # All time high
    ath_change_percentage: float
    circulating_supply: float
    total_supply: float
    max_supply: Optional[float]
    market_cap_rank: int
    last_updated: datetime


@dataclass
class CoinGeckoTrending:
    """Трендовые монеты CoinGecko"""
    coin_id: str
    symbol: str
    name: str
    market_cap_rank: int
    price_btc: float
    score: int  # Trending score


class CoinGeckoClient:
    """
    CoinGecko API Client
    Не требует API key для базовых функций!
    """
    
    BASE_URL = "https://api.coingecko.com/api/v3"
    PRO_URL = "https://pro-api.coingecko.com/api/v3"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Инициализация CoinGecko клиента
        
        Args:
            api_key: API ключ (опционально, увеличивает лимиты)
                   Получить: https://www.coingecko.com/en/api/pricing
        """
        self.api_key = api_key or os.getenv("COINGECKO_API_KEY", "")
        self.use_pro = bool(self.api_key)
        
        self.base_url = self.PRO_URL if self.use_pro else self.BASE_URL
        
        # Rate limiting
        if self.use_pro:
            self.min_interval = 0.12  # 500 запросов/мин = 0.12с
            self.max_requests_per_min = 500
            print("🚀 CoinGecko PRO Client initialized (500 req/min)")
        else:
            self.min_interval = 2.0  # 30 запросов/мин = 2с
            self.max_requests_per_min = 30
            print("🚀 CoinGecko Free Client initialized (30 req/min)")
        
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_request_time = 0
        
        # ✅ Прокси поддержка (те же что и для других API)
        proxy_env = os.getenv("PROXY_LIST", "")
        self._proxies = [p.strip() for p in proxy_env.split(",") if p.strip()]
        self._proxy_idx = 0
        self._active_proxy: Optional[str] = None
        self._proxy_enabled = os.getenv("USE_PROXY_FOR_COINGECKO", "true").lower() == "true"
        
        if self._proxies and self._proxy_enabled:
            print(f"   🌐 Proxy rotation enabled: {len(self._proxies)} proxies")
        
        # Cache for symbol → coin_id mapping
        self._coin_map_cache: Optional[Dict[str, str]] = None
        self._coin_map_last_update: Optional[datetime] = None
        self._coin_map_ttl = 3600  # 1 hour
    
    def _next_proxy(self) -> Optional[str]:
        """Получить следующий прокси из списка (rotation)"""
        if not self._proxies or not self._proxy_enabled:
            return None
        p = self._proxies[self._proxy_idx % len(self._proxies)]
        self._proxy_idx += 1
        return p
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать сессию"""
        if self.session is None or self.session.closed:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            headers["Content-Type"] = "application/json"
            
            connector = aiohttp.TCPConnector(ssl=False)
            self.session = aiohttp.ClientSession(
                connector=connector, 
                headers=headers,
                trust_env=False
            )
        return self.session
    
    async def _make_request(self, endpoint: str, params: Optional[Dict] = None, retry_with_proxy: bool = True) -> Optional[Dict]:
        """Выполнить запрос с rate limiting и proxy rotation"""
        import time
        
        # Rate limiting
        current_time = time.time()
        elapsed = current_time - self.last_request_time
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        
        url = f"{self.base_url}{endpoint}"
        
        # Если есть прокси и включено — пробуем с прокси
        if self._proxies and self._proxy_enabled and retry_with_proxy:
            errors = []
            for idx, proxy in enumerate(self._proxies):
                try:
                    result = await self._try_request(url, params, proxy)
                    if result is not None:
                        if self._active_proxy != proxy:
                            self._active_proxy = proxy
                            host = proxy.split('@')[-1] if '@' in proxy else proxy
                            logger.info(f"🔄 [COINGECKO] Switched to working proxy ({host})")
                        return result
                except Exception as e:
                    errors.append(f"proxy{idx+1}:{type(e).__name__}")
                    continue
            
            # Все прокси упали — пробуем напрямую
            logger.warning(f"⚠️ [COINGECKO] All proxies failed ({', '.join(errors)}), trying direct...")
        
        # Пробуем напрямую (или если прокси отключены)
        return await self._try_request(url, params, None)
    
    async def _try_request(self, url: str, params: Optional[Dict], proxy: Optional[str]) -> Optional[Dict]:
        """Выполнить один запрос с опциональным прокси"""
        try:
            session = await self._get_session()
            proxy_kwargs = {"proxy": proxy} if proxy else {}
            
            async with session.get(url, params=params or {}, timeout=30, **proxy_kwargs) as response:
                self.last_request_time = time.time()
                
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"✅ CoinGecko: OK")
                    return data
                elif response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    logger.warning(f"⏱️ CoinGecko Rate limit, waiting {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    return await self._try_request(url, params, proxy)
                elif response.status == 403:
                    raise Exception("Geo-block (403)")
                else:
                    error_text = await response.text()
                    logger.error(f"❌ CoinGecko Error {response.status}: {error_text[:100]}")
                    return None
        
        except Exception as e:
            raise e  # Прокидываем для обработки в _make_request
    
    async def close(self):
        """Закрыть сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    # =========================================================================
    # COIN MAPPING (Symbol → CoinGecko ID)
    # =========================================================================
    
    async def _get_coin_map(self) -> Dict[str, str]:
        """
        Получить маппинг symbol → coin_id
        Кэшируется на 1 час
        """
        # Проверяем кэш
        if self._coin_map_cache and self._coin_map_last_update:
            if (datetime.utcnow() - self._coin_map_last_update).total_seconds() < self._coin_map_ttl:
                return self._coin_map_cache
        
        # Получаем список всех монет
        result = await self._make_request("/coins/list")
        
        if not result:
            logger.warning("⚠️ CoinGecko: Failed to get coin list")
            return {}
        
        # Создаём маппинг symbol → id
        coin_map = {}
        for coin in result:
            symbol = coin.get("symbol", "").upper()
            coin_id = coin.get("id", "")
            if symbol and coin_id:
                # Если несколько монет с одним символом, берём первую
                if symbol not in coin_map:
                    coin_map[symbol] = coin_id
        
        self._coin_map_cache = coin_map
        self._coin_map_last_update = datetime.utcnow()
        
        logger.info(f"✅ CoinGecko: Loaded {len(coin_map)} coin mappings")
        return coin_map
    
    def _normalize_symbol(self, symbol: str) -> str:
        """Нормализовать символ для CoinGecko"""
        return symbol.replace("USDT", "").replace("USD", "").upper()
    
    # =========================================================================
    # MARKET DATA
    # =========================================================================
    
    async def get_coin_data(self, symbol: str) -> Optional[CoinGeckoMarketData]:
        """
        Получить полные данные по монете
        
        Args:
            symbol: Тикер (BTC, ETH, SOL)
        
        Returns:
            CoinGeckoMarketData или None
        """
        coin_map = await self._get_coin_map()
        coin_id = coin_map.get(self._normalize_symbol(symbol))
        
        if not coin_id:
            logger.warning(f"⚠️ CoinGecko: Symbol '{symbol}' not found in coin list")
            return None
        
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false"
        }
        
        result = await self._make_request(f"/coins/{coin_id}", params)
        
        if not result:
            return None
        
        try:
            market_data = result.get("market_data", {})
            
            return CoinGeckoMarketData(
                symbol=symbol.upper(),
                coin_id=coin_id,
                price=market_data.get("current_price", {}).get("usd", 0.0),
                market_cap=market_data.get("market_cap", {}).get("usd", 0.0),
                total_volume=market_data.get("total_volume", {}).get("usd", 0.0),
                price_change_1h=market_data.get("price_change_percentage_1h_in_currency", {}).get("usd", 0.0) or 0.0,
                price_change_24h=market_data.get("price_change_percentage_24h_in_currency", {}).get("usd", 0.0) or 0.0,
                price_change_7d=market_data.get("price_change_percentage_7d_in_currency", {}).get("usd", 0.0) or 0.0,
                price_change_30d=market_data.get("price_change_percentage_30d_in_currency", {}).get("usd", 0.0) or 0.0,
                ath=market_data.get("ath", {}).get("usd", 0.0),
                ath_change_percentage=market_data.get("ath_change_percentage", {}).get("usd", 0.0) or 0.0,
                circulating_supply=market_data.get("circulating_supply", 0.0),
                total_supply=market_data.get("total_supply", 0.0),
                max_supply=market_data.get("max_supply"),
                market_cap_rank=market_data.get("market_cap_rank", 0) or 0,
                last_updated=datetime.utcnow()
            )
        except Exception as e:
            logger.error(f"❌ CoinGecko: Error parsing data for {symbol}: {e}")
            return None
    
    async def get_multiple_coins_data(self, symbols: List[str]) -> Dict[str, CoinGeckoMarketData]:
        """
        Получить данные для нескольких монет (один batch запрос)
        
        Args:
            symbols: Список тикеров ['BTC', 'ETH', 'SOL']
        
        Returns:
            Словарь symbol → CoinGeckoMarketData
        """
        coin_map = await self._get_coin_map()
        
        # Конвертируем символы в coin_ids
        coin_ids = []
        symbol_to_id = {}
        for sym in symbols:
            norm_sym = self._normalize_symbol(sym)
            coin_id = coin_map.get(norm_sym)
            if coin_id:
                coin_ids.append(coin_id)
                symbol_to_id[coin_id] = sym.upper()
        
        if not coin_ids:
            logger.warning("⚠️ CoinGecko: No valid coin IDs found for symbols")
            return {}
        
        # CoinGecko ограничивает количество IDs в URL
        # Делаем батчами по 50
        batch_size = 50
        all_results = {}
        
        for i in range(0, len(coin_ids), batch_size):
            batch = coin_ids[i:i+batch_size]
            ids_param = ",".join(batch)
            
            params = {
                "ids": ids_param,
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_last_updated_at": "true"
            }
            
            result = await self._make_request("/simple/price", params)
            
            if result:
                for coin_id, data in result.items():
                    symbol = symbol_to_id.get(coin_id, coin_id.upper())
                    all_results[symbol] = CoinGeckoMarketData(
                        symbol=symbol,
                        coin_id=coin_id,
                        price=data.get("usd", 0.0),
                        market_cap=data.get("usd_market_cap", 0.0) or 0.0,
                        total_volume=data.get("usd_24h_vol", 0.0) or 0.0,
                        price_change_1h=0.0,  # Not available in simple/price
                        price_change_24h=data.get("usd_24h_change", 0.0) or 0.0,
                        price_change_7d=0.0,
                        price_change_30d=0.0,
                        ath=0.0,
                        ath_change_percentage=0.0,
                        circulating_supply=0.0,
                        total_supply=0.0,
                        max_supply=None,
                        market_cap_rank=0,
                        last_updated=datetime.utcnow()
                    )
            
            # Rate limit between batches
            if i + batch_size < len(coin_ids):
                await asyncio.sleep(self.min_interval)
        
        logger.info(f"✅ CoinGecko: Got data for {len(all_results)}/{len(symbols)} symbols")
        return all_results
    
    # =========================================================================
    # TRENDING
    # =========================================================================
    
    async def get_trending(self) -> List[CoinGeckoTrending]:
        """
        Получить топ-7 трендовых монет
        
        Returns:
            Список CoinGeckoTrending
        """
        result = await self._make_request("/search/trending")
        
        if not result or "coins" not in result:
            logger.warning("⚠️ CoinGecko: Failed to get trending data")
            return []
        
        trending = []
        for item in result.get("coins", []):
            coin = item.get("item", {})
            trending.append(CoinGeckoTrending(
                coin_id=coin.get("id", ""),
                symbol=coin.get("symbol", "").upper(),
                name=coin.get("name", ""),
                market_cap_rank=coin.get("market_cap_rank", 0) or 0,
                price_btc=coin.get("price_btc", 0.0) or 0.0,
                score=item.get("score", 0) or 0
            ))
        
        logger.info(f"✅ CoinGecko: Got {len(trending)} trending coins")
        return trending
    
    async def get_trending_symbols(self) -> Set[str]:
        """Получить только символы трендовых монет"""
        trending = await self.get_trending()
        return {t.symbol for t in trending}
    
    # =========================================================================
    # GLOBAL DATA
    # =========================================================================
    
    async def get_global_data(self) -> Optional[Dict]:
        """Получить глобальные данные рынка"""
        return await self._make_request("/global")
    
    async def get_fear_greed_index(self) -> Optional[Dict]:
        """Fear & Greed Index (if available via alternative endpoint)"""
        # CoinGecko doesn't have direct F&G, we'll skip for now
        return None


# =========================================================================
# SINGLETON
# =========================================================================

_cg_client = None

def get_coingecko_client() -> CoinGeckoClient:
    """Получить singleton CoinGecko клиент"""
    global _cg_client
    if _cg_client is None:
        _cg_client = CoinGeckoClient()
    return _cg_client


# =========================================================================
# EXAMPLE
# =========================================================================

async def test_coingecko():
    """Тест CoinGecko API"""
    client = CoinGeckoClient()
    
    # Тест trending
    print("\n📈 Trending:")
    trending = await client.get_trending()
    for t in trending[:3]:
        print(f"  {t.symbol}: rank={t.market_cap_rank}, score={t.score}")
    
    # Тест данных по монете
    print("\n💰 BTC data:")
    btc = await client.get_coin_data("BTC")
    if btc:
        print(f"  Price: ${btc.price:,.2f}")
        print(f"  24h change: {btc.price_change_24h:.2f}%")
        print(f"  Market cap: ${btc.market_cap:,.0f}")
        print(f"  Rank: #{btc.market_cap_rank}")
    
    await client.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_coingecko())
