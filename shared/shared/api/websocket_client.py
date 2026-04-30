"""
WebSocket Client для бирж (Bybit, Binance, OKX)
Реальное время: стакан, лента сделок, цены
"""

import asyncio
import json
import time
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field
from collections import defaultdict
import websockets
from websockets.exceptions import ConnectionClosed


@dataclass
class OrderBookLevel:
    """Уровень стакана"""
    price: float
    size: float
    side: str  # 'bid' или 'ask'


@dataclass
class Trade:
    """Сделка из ленты"""
    symbol: str
    price: float
    size: float
    side: str  # 'buy' или 'sell'
    timestamp: int
    is_buyer_maker: bool  # True если покупатель был мейкером


@dataclass
class OrderBookSnapshot:
    """Снапшот стакана"""
    symbol: str
    bids: List[OrderBookLevel]  # Сортировано по убыванию цены
    asks: List[OrderBookLevel]  # Сортировано по возрастанию цены
    timestamp: int
    
    def get_spread(self) -> float:
        """Получить спред"""
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return 0.0
    
    def get_mid_price(self) -> float:
        """Средняя цена"""
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return 0.0
    
    def get_bid_ask_imbalance(self, depth: int = 10) -> float:
        """
        Получить дисбаланс bid/ask
        > 0 = больше ликвидности на покупку (бычье)
        < 0 = больше ликвидности на продажу (медвежье)
        """
        bid_vol = sum(l.size for l in self.bids[:depth])
        ask_vol = sum(l.size for l in self.asks[:depth])
        total = bid_vol + ask_vol
        if total > 0:
            return (bid_vol - ask_vol) / total
        return 0.0
    
    def find_big_walls(self, min_size_usd: float = 50000) -> List[Dict]:
        """Найти крупные стены в стакане"""
        walls = []
        mid = self.get_mid_price()
        
        for bid in self.bids[:20]:  # Топ 20 bid
            size_usd = bid.size * bid.price
            if size_usd >= min_size_usd:
                walls.append({
                    "side": "bid",
                    "price": bid.price,
                    "size": bid.size,
                    "size_usd": size_usd,
                    "distance_pct": (mid - bid.price) / mid * 100
                })
        
        for ask in self.asks[:20]:  # Топ 20 ask
            size_usd = ask.size * ask.price
            if size_usd >= min_size_usd:
                walls.append({
                    "side": "ask",
                    "price": ask.price,
                    "size": ask.size,
                    "size_usd": size_usd,
                    "distance_pct": (ask.price - mid) / mid * 100
                })
        
        return sorted(walls, key=lambda x: x["size_usd"], reverse=True)


class BybitWebSocketClient:
    """
    Bybit WebSocket Client v5
    Публичные данные без авторизации
    """
    
    WS_URL = "wss://stream.bybit.com/v5/public/linear"
    
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.subscriptions: set = set()
        self.order_books: Dict[str, OrderBookSnapshot] = {}
        self.recent_trades: Dict[str, List[Trade]] = defaultdict(list)
        self.tickers: Dict[str, Dict] = {}
        
        # Callbacks
        self.on_order_book: Optional[Callable[[OrderBookSnapshot], None]] = None
        self.on_trade: Optional[Callable[[Trade], None]] = None
        self.on_ticker: Optional[Callable[[Dict], None]] = None
        
        # CVD tracking
        self.cvd_data: Dict[str, Dict] = defaultdict(lambda: {
            "buy_volume": 0.0,
            "sell_volume": 0.0,
            "delta": 0.0,
            "cumulative": 0.0
        })
        
        self._running = False
        self._reconnect_delay = 1
        
    async def connect(self):
        """Подключиться к WebSocket"""
        try:
            self.ws = await websockets.connect(self.WS_URL)
            self._running = True
            self._reconnect_delay = 1
            print("✅ Bybit WebSocket connected")
            
            # Переподписаться на предыдущие каналы
            if self.subscriptions:
                await self._subscribe(list(self.subscriptions))
            
            # Запустить обработку сообщений
            asyncio.create_task(self._receive_loop())
            
        except Exception as e:
            print(f"❌ Bybit WS connect error: {e}")
            await self._reconnect()
    
    async def _reconnect(self):
        """Переподключение с backoff"""
        print(f"🔄 Reconnecting in {self._reconnect_delay}s...")
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, 60)
        await self.connect()
    
    async def _subscribe(self, topics: List[str]):
        """Подписаться на каналы"""
        if not self.ws:
            return
        
        msg = {
            "op": "subscribe",
            "args": [{"channel": t} for t in topics]
        }
        await self.ws.send(json.dumps(msg))
        print(f"📡 Subscribed: {topics}")
    
    async def subscribe_orderbook(self, symbol: str, depth: int = 50):
        """Подписаться на стакан"""
        topic = f"orderbook.{depth}.{symbol}"
        self.subscriptions.add(topic)
        if self.ws:
            await self._subscribe([topic])
    
    async def subscribe_trades(self, symbol: str):
        """Подписаться на ленту сделок"""
        topic = f"publicTrade.{symbol}"
        self.subscriptions.add(topic)
        if self.ws:
            await self._subscribe([topic])
    
    async def subscribe_ticker(self, symbol: str):
        """Подписаться на тикер"""
        topic = f"tickers.{symbol}"
        self.subscriptions.add(topic)
        if self.ws:
            await self._subscribe([topic])
    
    async def _receive_loop(self):
        """Цикл получения сообщений"""
        while self._running and self.ws:
            try:
                msg = await self.ws.recv()
                data = json.loads(msg)
                await self._process_message(data)
            except ConnectionClosed:
                print("⚠️ Bybit WS connection closed")
                await self._reconnect()
                break
            except Exception as e:
                print(f"⚠️ WS receive error: {e}")
    
    async def _process_message(self, data: Dict):
        """Обработать входящее сообщение"""
        topic = data.get("topic", "")
        
        if "orderbook" in topic:
            await self._process_orderbook(data)
        elif "publicTrade" in topic:
            await self._process_trade(data)
        elif "tickers" in topic:
            await self._process_ticker(data)
    
    async def _process_orderbook(self, data: Dict):
        """Обработать обновление стакана"""
        topic = data.get("topic", "")
        symbol = topic.split(".")[-1]
        
        type_ = data.get("type", "")
        ts = data.get("ts", int(time.time() * 1000))
        
        if type_ == "snapshot":
            # Полный снапшот
            self.order_books[symbol] = self._parse_orderbook(data, symbol, ts)
        elif type_ == "delta":
            # Дельта обновление
            if symbol in self.order_books:
                self._apply_orderbook_delta(symbol, data)
        
        # Callback
        if self.on_order_book and symbol in self.order_books:
            await asyncio.create_task(
                self._safe_callback(self.on_order_book, self.order_books[symbol])
            )
    
    def _parse_orderbook(self, data: Dict, symbol: str, ts: int) -> OrderBookSnapshot:
        """Парсить стакан из сообщения"""
        bids = [
            OrderBookLevel(price=float(p), size=float(s), side="bid")
            for p, s in data.get("data", {}).get("b", [])
        ]
        asks = [
            OrderBookLevel(price=float(p), size=float(s), side="ask")
            for p, s in data.get("data", {}).get("a", [])
        ]
        
        # Сортировка
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        
        return OrderBookSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=ts
        )
    
    def _apply_orderbook_delta(self, symbol: str, data: Dict):
        """Применить дельту к стакану"""
        ob = self.order_books[symbol]
        
        # Удаление
        for price, _ in data.get("data", {}).get("b", []):
            ob.bids = [b for b in ob.bids if b.price != float(price)]
        for price, _ in data.get("data", {}).get("a", []):
            ob.asks = [a for a in ob.asks if a.price != float(price)]
        
        # Добавление/обновление
        for price, size in data.get("data", {}).get("b", []):
            p, s = float(price), float(size)
            ob.bids = [b for b in ob.bids if b.price != p]
            if s > 0:
                ob.bids.append(OrderBookLevel(price=p, size=s, side="bid"))
        
        for price, size in data.get("data", {}).get("a", []):
            p, s = float(price), float(size)
            ob.asks = [a for a in ob.asks if a.price != p]
            if s > 0:
                ob.asks.append(OrderBookLevel(price=p, size=s, side="ask"))
        
        # Пересортировка
        ob.bids.sort(key=lambda x: x.price, reverse=True)
        ob.asks.sort(key=lambda x: x.price)
    
    async def _process_trade(self, data: Dict):
        """Обработать сделку"""
        topic = data.get("topic", "")
        symbol = topic.split(".")[-1]
        
        for trade_data in data.get("data", []):
            trade = Trade(
                symbol=symbol,
                price=float(trade_data.get("p", 0)),
                size=float(trade_data.get("v", 0)),
                side="buy" if trade_data.get("S") == "Buy" else "sell",
                timestamp=int(trade_data.get("T", time.time() * 1000)),
                is_buyer_maker=trade_data.get("m", False)
            )
            
            # Сохранить
            self.recent_trades[symbol].append(trade)
            if len(self.recent_trades[symbol]) > 1000:
                self.recent_trades[symbol] = self.recent_trades[symbol][-500:]
            
            # Обновить CVD
            usd_value = trade.price * trade.size
            if trade.side == "buy":
                self.cvd_data[symbol]["buy_volume"] += usd_value
                self.cvd_data[symbol]["delta"] += usd_value
            else:
                self.cvd_data[symbol]["sell_volume"] += usd_value
                self.cvd_data[symbol]["delta"] -= usd_value
            
            self.cvd_data[symbol]["cumulative"] = (
                self.cvd_data[symbol]["buy_volume"] - 
                self.cvd_data[symbol]["sell_volume"]
            )
            
            # Callback
            if self.on_trade:
                await asyncio.create_task(
                    self._safe_callback(self.on_trade, trade)
                )
    
    async def _process_ticker(self, data: Dict):
        """Обработать тикер"""
        topic = data.get("topic", "")
        symbol = topic.split(".")[-1]
        
        ticker_data = data.get("data", {})
        if ticker_data:
            self.tickers[symbol] = {
                "symbol": symbol,
                "price": float(ticker_data.get("lastPrice", 0)),
                "bid": float(ticker_data.get("bid1Price", 0)),
                "ask": float(ticker_data.get("ask1Price", 0)),
                "volume_24h": float(ticker_data.get("volume24h", 0)),
                "turnover_24h": float(ticker_data.get("turnover24h", 0)),
                "oi": float(ticker_data.get("openInterest", 0)),
                "funding": float(ticker_data.get("fundingRate", 0)),
                "timestamp": data.get("ts", int(time.time() * 1000))
            }
            
            if self.on_ticker:
                await asyncio.create_task(
                    self._safe_callback(self.on_ticker, self.tickers[symbol])
                )
    
    async def _safe_callback(self, callback: Callable, *args):
        """Безопасный вызов callback"""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(*args)
            else:
                callback(*args)
        except Exception as e:
            print(f"⚠️ Callback error: {e}")
    
    def get_order_book(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """Получить текущий стакан"""
        return self.order_books.get(symbol)
    
    def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Trade]:
        """Получить недавние сделки"""
        return self.recent_trades.get(symbol, [])[-limit:]
    
    def get_cvd(self, symbol: str) -> Dict:
        """Получить CVD данные"""
        return dict(self.cvd_data[symbol])
    
    def get_imbalance_signal(self, symbol: str) -> str:
        """
        Получить сигнал на основе имбаланса стакана
        
        Returns:
            'strong_buy', 'buy', 'neutral', 'sell', 'strong_sell'
        """
        ob = self.get_order_book(symbol)
        if not ob:
            return "neutral"
        
        imbalance = ob.get_bid_ask_imbalance(depth=10)
        
        if imbalance > 0.3:
            return "strong_buy"
        elif imbalance > 0.1:
            return "buy"
        elif imbalance < -0.3:
            return "strong_sell"
        elif imbalance < -0.1:
            return "sell"
        return "neutral"
    
    def get_aggressive_flow_signal(self, symbol: str, window: int = 50) -> Dict:
        """
        Анализ агрессивного потока из последних сделок
        """
        trades = self.get_recent_trades(symbol, window)
        if not trades:
            return {"signal": "neutral", "buy_pressure": 0.5, "notes": "No data"}
        
        buy_vol = sum(t.price * t.size for t in trades if t.side == "buy")
        sell_vol = sum(t.price * t.size for t in trades if t.side == "sell")
        total = buy_vol + sell_vol
        
        if total == 0:
            return {"signal": "neutral", "buy_pressure": 0.5, "notes": "Zero volume"}
        
        buy_pressure = buy_vol / total
        
        # Агрессивные покупки (покупатель агрессор = не мейкер)
        aggressive_buys = sum(
            t.price * t.size 
            for t in trades 
            if t.side == "buy" and not t.is_buyer_maker
        )
        aggressive_sells = sum(
            t.price * t.size 
            for t in trades 
            if t.side == "sell" and not t.is_buyer_maker
        )
        
        signal = "neutral"
        if buy_pressure > 0.7 and aggressive_buys > aggressive_sells * 2:
            signal = "strong_buy"
        elif buy_pressure > 0.6:
            signal = "buy"
        elif buy_pressure < 0.3 and aggressive_sells > aggressive_buys * 2:
            signal = "strong_sell"
        elif buy_pressure < 0.4:
            signal = "sell"
        
        return {
            "signal": signal,
            "buy_pressure": round(buy_pressure, 2),
            "aggressive_buy_usd": round(aggressive_buys, 2),
            "aggressive_sell_usd": round(aggressive_sells, 2),
            "total_volume_usd": round(total, 2),
            "trade_count": len(trades)
        }
    
    async def disconnect(self):
        """Отключиться"""
        self._running = False
        if self.ws:
            await self.ws.close()
            print("👋 Bybit WebSocket disconnected")


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_ws_client: Optional[BybitWebSocketClient] = None


def get_websocket_client() -> BybitWebSocketClient:
    """Получить singleton WebSocket клиента"""
    global _ws_client
    if _ws_client is None:
        _ws_client = BybitWebSocketClient()
    return _ws_client


async def reset_websocket_client():
    """Сбросить клиент"""
    global _ws_client
    if _ws_client:
        await _ws_client.disconnect()
    _ws_client = None
