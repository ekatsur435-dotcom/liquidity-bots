"""
BingX Futures API Client
Поддержка DEMO (testnet) и REAL торговли
"""

import os
import json
import hmac
import hashlib
import time
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime
import aiohttp


@dataclass
class BingXPosition:
    """Позиция на BingX"""
    symbol: str
    side: str  # 'LONG' или 'SHORT'
    position_side: str  # 'LONG', 'SHORT', или 'BOTH'
    size: float
    entry_price: float
    leverage: int
    unrealized_pnl: float
    realized_pnl: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class BingXOrder:
    """Ордер на BingX"""
    order_id: str
    symbol: str
    side: str  # 'BUY' или 'SELL'
    position_side: str
    type: str  # 'MARKET', 'LIMIT', 'STOP_MARKET'
    size: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "PENDING"  # PENDING, FILLED, CANCELED
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    created_at: Optional[datetime] = None


class BingXClient:
    """
    Клиент для BingX Futures API
    Поддерживает DEMO (testnet) режим
    """
    
    # DEMO (testnet) endpoints
    DEMO_BASE_URL = "https://open-api-vst.bingx.com"
    
    # REAL endpoints  
    REAL_BASE_URL = "https://open-api.bingx.com"
    
    def __init__(self, 
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 demo: bool = True):
        """
        Инициализация клиента BingX
        
        Args:
            api_key: API ключ из BingX
            api_secret: API секрет из BingX
            demo: True для DEMO (testnet), False для REAL торговли
        """
        self.api_key = api_key or os.getenv("BINGX_API_KEY")
        self.api_secret = api_secret or os.getenv("BINGX_API_SECRET")
        
        # 🔒 FORCE DEMO MODE - всегда DEMO, никогда REAL!
        # Чтобы включить REAL, нужно явно установить BINGX_FORCE_REAL=true
        force_real = os.getenv("BINGX_FORCE_REAL", "false").lower() == "true"
        self.demo = True if not force_real else demo
        
        if not self.demo:
            print("🚨🚨🚨 WARNING: RUNNING IN REAL MODE! 🚨🚨🚨")
        
        if not self.api_key or not self.api_secret:
            raise ValueError("BingX API key and secret required")
        
        self.base_url = self.DEMO_BASE_URL if self.demo else self.REAL_BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
        
        print(f"🚀 BingX Client initialized ({'DEMO' if demo else 'REAL'} mode)")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать сессию"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "X-BX-APIKEY": self.api_key,
                    "Content-Type": "application/json"
                }
            )
        return self.session
    
    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """Генерация подписи для API запроса"""
        # Добавляем timestamp
        params['timestamp'] = int(time.time() * 1000)
        
        # Сортируем параметры
        sorted_params = sorted(params.items())
        
        # Создаём строку для подписи
        signature_payload = '&'.join([f"{k}={v}" for k, v in sorted_params])
        
        # Генерируем HMAC SHA256
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return signature, params['timestamp']
    
    async def _make_request(self, 
                           method: str, 
                           endpoint: str, 
                           params: Optional[Dict] = None,
                           signed: bool = True) -> Optional[Dict]:
        """
        Выполнить API запрос
        
        Args:
            method: HTTP метод (GET, POST, DELETE)
            endpoint: API endpoint
            params: Параметры запроса
            signed: Требуется ли подпись
        """
        try:
            url = f"{self.base_url}{endpoint}"
            params = params or {}
            
            # Добавляем подпись если нужно
            if signed:
                signature, timestamp = self._generate_signature(params)
                params['signature'] = signature
            
            session = await self._get_session()
            
            if method == "GET":
                async with session.get(url, params=params, timeout=30) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error_text = await response.text()
                        print(f"BingX API Error {response.status}: {error_text}")
                        return None
            
            elif method == "POST":
                async with session.post(url, json=params, timeout=30) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error_text = await response.text()
                        print(f"BingX API Error {response.status}: {error_text}")
                        return None
            
            elif method == "DELETE":
                async with session.delete(url, params=params, timeout=30) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error_text = await response.text()
                        print(f"BingX API Error {response.status}: {error_text}")
                        return None
        
        except Exception as e:
            print(f"Request error: {e}")
            return None
    
    async def close(self):
        """Закрыть сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    # =========================================================================
    # ACCOUNT INFO
    # =========================================================================
    
    async def get_account_balance(self) -> Optional[Dict]:
        """Получить баланс аккаунта"""
        result = await self._make_request("GET", "/openApi/swap/v2/user/balance", signed=True)
        
        # Debug: показываем что вернул API
        print(f"🔍 DEBUG Balance API response: {result}")
        
        if result and result.get("code") == 0:
            data = result.get("data", {})
            # Debug: показываем структуру данных
            print(f"🔍 DEBUG Balance data: {data}")
            return data
        
        print(f"⚠️ DEBUG Balance API error: code={result.get('code') if result else 'None'}, msg={result.get('msg') if result else 'None'}")
        return None
    
    async def get_account_info(self) -> Optional[Dict]:
        """Получить информацию об аккаунте"""
        result = await self._make_request("GET", "/openApi/swap/v1/account", signed=True)
        
        if result and result.get("code") == 0:
            return result.get("data", {})
        return None
    
    # =========================================================================
    # MARKET DATA
    # =========================================================================
    
    async def get_symbols(self) -> List[str]:
        """Получить список доступных пар"""
        result = await self._make_request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
        
        symbols = []
        if result and result.get("code") == 0:
            for contract in result.get("data", []):
                symbol = contract.get("symbol")
                if symbol:
                    symbols.append(symbol)
        
        return symbols
    
    async def get_price(self, symbol: str) -> Optional[float]:
        """Получить текущую цену символа"""
        result = await self._make_request(
            "GET", 
            "/openApi/swap/v1/ticker/price",
            params={"symbol": symbol},
            signed=False
        )
        
        if result and result.get("code") == 0:
            return float(result["data"].get("price", 0))
        return None
    
    async def get_klines(self, 
                        symbol: str, 
                        interval: str = "15m",
                        limit: int = 100) -> Optional[List]:
        """
        Получить свечи (OHLCV)
        
        Args:
            symbol: Торговая пара (BTC-USDT)
            interval: Таймфрейм (1m, 5m, 15m, 1h, 4h, 1d)
            limit: Количество свечей
        """
        result = await self._make_request(
            "GET",
            "/openApi/swap/v3/quote/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            },
            signed=False
        )
        
        if result and result.get("code") == 0:
            return result.get("data", [])
        return None
    
    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Получить фандинг рейт"""
        result = await self._make_request(
            "GET",
            "/openApi/swap/v1/fundingRate",
            params={"symbol": symbol},
            signed=False
        )
        
        if result and result.get("code") == 0:
            return float(result["data"].get("fundingRate", 0))
        return None
    
    async def get_open_interest(self, symbol: str) -> Optional[float]:
        """Получить Open Interest"""
        result = await self._make_request(
            "GET",
            "/openApi/swap/v1/openInterest",
            params={"symbol": symbol},
            signed=False
        )
        
        if result and result.get("code") == 0:
            return float(result["data"].get("openInterest", 0))
        return None
    
    # =========================================================================
    # POSITIONS
    # =========================================================================
    
    async def get_positions(self, symbol: Optional[str] = None) -> List[BingXPosition]:
        """Получить открытые позиции"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        
        result = await self._make_request(
            "GET",
            "/openApi/swap/v2/user/positions",
            params=params,
            signed=True
        )
        
        positions = []
        if result and result.get("code") == 0:
            for pos_data in result.get("data", []):
                position = BingXPosition(
                    symbol=pos_data.get("symbol", ""),
                    side="LONG" if pos_data.get("positionSide") == "LONG" else "SHORT",
                    position_side=pos_data.get("positionSide", ""),
                    size=float(pos_data.get("positionAmt", 0)),
                    entry_price=float(pos_data.get("avgPrice", 0)),
                    leverage=int(pos_data.get("leverage", 1)),
                    unrealized_pnl=float(pos_data.get("unrealizedProfit", 0)),
                    realized_pnl=float(pos_data.get("realizedProfit", 0)),
                    stop_loss=float(pos_data.get("stopLoss", 0)) if pos_data.get("stopLoss") else None,
                    take_profit=float(pos_data.get("takeProfit", 0)) if pos_data.get("takeProfit") else None
                )
                positions.append(position)
        
        return positions
    
    async def close_position(self, symbol: str, position_side: str) -> bool:
        """Закрыть позицию"""
        result = await self._make_request(
            "POST",
            "/openApi/swap/v2/trade/closePosition",
            params={
                "symbol": symbol,
                "positionSide": position_side
            },
            signed=True
        )
        
        if result and result.get("code") == 0:
            print(f"✅ Position closed: {symbol} {position_side}")
            return True
        else:
            print(f"❌ Failed to close position: {result}")
            return False
    
    # =========================================================================
    # ORDERS
    # =========================================================================
    
    async def place_order(self,
                         symbol: str,
                         side: str,  # BUY или SELL
                         position_side: str,  # LONG или SHORT
                         order_type: str,  # MARKET, LIMIT, STOP_MARKET
                         size: float,
                         price: Optional[float] = None,
                         stop_price: Optional[float] = None,
                         stop_loss: Optional[float] = None,
                         take_profit: Optional[float] = None) -> Optional[BingXOrder]:
        """
        Разместить ордер
        
        Args:
            symbol: Торговая пара
            side: BUY или SELL
            position_side: LONG или SHORT (для хеджирования)
            order_type: MARKET, LIMIT, STOP_MARKET
            size: Размер позиции (в монетах)
            price: Цена (для LIMIT)
            stop_price: Стоп цена (для STOP_MARKET)
            stop_loss: Цена стоп-лосса
            take_profit: Цена тейк-профита
        """
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": size
        }
        
        if price and order_type == "LIMIT":
            params["price"] = price
        
        if stop_price and order_type == "STOP_MARKET":
            params["stopPrice"] = stop_price
        
        if stop_loss:
            params["stopLoss"] = stop_loss
        
        if take_profit:
            params["takeProfit"] = take_profit
        
        result = await self._make_request(
            "POST",
            "/openApi/swap/v2/trade/order",
            params=params,
            signed=True
        )
        
        if result and result.get("code") == 0:
            order_data = result.get("data", {})
            print(f"✅ Order placed: {symbol} {side} {size} @ {order_type}")
            
            return BingXOrder(
                order_id=order_data.get("orderId", ""),
                symbol=symbol,
                side=side,
                position_side=position_side,
                type=order_type,
                size=size,
                price=price,
                stop_price=stop_price,
                status="PENDING"
            )
        else:
            print(f"❌ Order failed: {result}")
            return None
    
    async def place_market_order(self,
                                symbol: str,
                                side: str,
                                position_side: str,
                                size: float,
                                stop_loss: Optional[float] = None,
                                take_profit: Optional[float] = None) -> Optional[BingXOrder]:
        """Упрощённый рыночный ордер"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            position_side=position_side,
            order_type="MARKET",
            size=size,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
    
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Отменить ордер"""
        result = await self._make_request(
            "DELETE",
            "/openApi/swap/v2/trade/order",
            params={
                "symbol": symbol,
                "orderId": order_id
            },
            signed=True
        )
        
        if result and result.get("code") == 0:
            print(f"✅ Order canceled: {order_id}")
            return True
        return False
    
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[BingXOrder]:
        """Получить открытые ордера"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        
        result = await self._make_request(
            "GET",
            "/openApi/swap/v2/trade/openOrders",
            params=params,
            signed=True
        )
        
        orders = []
        if result and result.get("code") == 0:
            for order_data in result.get("data", []):
                order = BingXOrder(
                    order_id=order_data.get("orderId", ""),
                    symbol=order_data.get("symbol", ""),
                    side=order_data.get("side", ""),
                    position_side=order_data.get("positionSide", ""),
                    type=order_data.get("type", ""),
                    size=float(order_data.get("quantity", 0)),
                    price=float(order_data.get("price", 0)) if order_data.get("price") else None,
                    status=order_data.get("status", "PENDING")
                )
                orders.append(order)
        
        return orders
    
    # =========================================================================
    # LEVERAGE
    # =========================================================================
    
    async def set_leverage(self, symbol: str, leverage: int, position_side: str = "BOTH") -> bool:
        """Установить плечо для символа"""
        result = await self._make_request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            params={
                "symbol": symbol,
                "leverage": leverage,
                "positionSide": position_side
            },
            signed=True
        )
        
        if result and result.get("code") == 0:
            print(f"✅ Leverage set: {symbol} {leverage}x")
            return True
        else:
            print(f"❌ Failed to set leverage: {result}")
            return False
    
    # =========================================================================
    # TEST CONNECTION
    # =========================================================================
    
    async def test_connection(self) -> bool:
        """Тест соединения с API"""
        try:
            balance = await self.get_account_balance()
            if balance:
                print(f"✅ BingX connection OK ({'DEMO' if self.demo else 'REAL'})")
                return True
            else:
                print("❌ BingX connection failed")
                return False
        except Exception as e:
            print(f"❌ BingX connection error: {e}")
            return False


# ============================================================================
# SINGLETON
# ============================================================================

_bingx_client = None

def get_bingx_client(demo: bool = True) -> BingXClient:
    """Получить singleton BingX клиент"""
    global _bingx_client
    if _bingx_client is None:
        _bingx_client = BingXClient(demo=demo)
    return _bingx_client


# ============================================================================
# EXAMPLE
# ============================================================================

async def test_bingx():
    """Тест BingX API"""
    import os
    
    # Проверяем переменные окружения
    api_key = os.getenv("BINGX_API_KEY")
    api_secret = os.getenv("BINGX_API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ BINGX_API_KEY или BINGX_API_SECRET не установлены")
        print("Установите их для тестирования")
        return
    
    # Создаём клиент (DEMO mode)
    client = BingXClient(demo=True)
    
    # Тест соединения
    connected = await client.test_connection()
    if not connected:
        return
    
    # Получаем баланс
    balance = await client.get_account_balance()
    print(f"Balance: {balance}")
    
    # Получаем позиции
    positions = await client.get_positions()
    print(f"Open positions: {len(positions)}")
    for pos in positions:
        print(f"  {pos.symbol} {pos.side}: {pos.size} @ {pos.entry_price}")
    
    # Получаем цену BTC
    price = await client.get_price("BTC-USDT")
    print(f"BTC price: ${price}")
    
    # Закрываем
    await client.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_bingx())
