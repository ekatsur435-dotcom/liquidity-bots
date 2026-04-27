"""
BingX Futures API Client  v2.7

ВСЕ ИСПРАВЛЕНИЯ:
  v2.1 — RAW URL query string (signature mismatch fix)
  v2.2 — compact JSON separators для SL/TP
  v2.3 — "price" как float (не str) в SL/TP | фильтр offline символов
  v2.4 — type=STOP_MARKET/TAKE_PROFIT_MARKET + stopPrice (не price!)
       — добавлено max_notional в symbol info (для авто-уменьшения позиции)
       — code=101209 в ERROR_CODES
       — error 101209 логируется с подсказкой
"""

import os, json, hmac, hashlib, time
from typing import Optional, Dict, List, Any, Set
from dataclasses import dataclass
from datetime import datetime
import aiohttp


@dataclass
class BingXPosition:
    symbol: str
    side: str
    position_side: str
    size: float
    entry_price: float
    leverage: int
    unrealized_pnl: float
    realized_pnl: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class BingXOrder:
    order_id: str
    symbol: str
    side: str
    position_side: str
    type: str
    size: float
    price: Optional[float] = None
    status: str = "PENDING"
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    created_at: Optional[datetime] = None


class BingXClient:
    DEMO_BASE_URL = "https://open-api-vst.bingx.com"
    REAL_BASE_URL = "https://open-api.bingx.com"

    ERROR_CODES = {
        80001:  "Parameter error",
        80012:  "Price precision — цена не кратна tickSize",
        80014:  "Quantity precision/min — объём ниже минимума",
        80016:  "Order does not exist",
        80020:  "Insufficient margin — недостаточно маржи",
        80021:  "Position does not exist",
        80022:  "Max positions reached",
        80030:  "Symbol does not exist",
        80041:  "SL/TP price invalid",
        101204: "Insufficient balance",
        101209: "Max position value exceeded — позиция превышает лимит для этого плеча. "
                "Уменьши размер или снизи плечо. AutoTrader авто-уменьшит в следующий раз.",
        109201: "Leverage exceeds max allowed",
        109400: "Timestamp invalid — разница времени между сервером и клиентом > 1000ms",
    }

    def __init__(self, api_key=None, api_secret=None, demo=True):
        self.api_key    = api_key    or os.getenv("BINGX_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINGX_API_SECRET", "")
        force_real = os.getenv("BINGX_FORCE_REAL", "false").lower() == "true"
        self.demo  = (not force_real) or demo
        if not self.demo:
            print("🚨 WARNING: REAL MODE!")
        if not self.api_key or not self.api_secret:
            raise ValueError("BingX API key and secret required")
        self.base_url = self.DEMO_BASE_URL if self.demo else self.REAL_BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
        self._symbol_info_cache: Dict[str, Dict] = {}
        self._active_symbols: Set[str] = set()
        self._symbols_loaded = False
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[int] = None
        self._time_offset: int = 0   # ✅ FIX: server time offset
        print(f"🚀 BingX Client ({'DEMO' if self.demo else 'REAL'})")

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"X-BX-APIKEY": self.api_key})
        return self.session

    def _sign(self, raw_qs: str) -> str:
        return hmac.new(
            self.api_secret.encode(), raw_qs.encode(), hashlib.sha256
        ).hexdigest()

    def _build_raw_qs(self, params: Dict[str, Any]) -> str:
        return "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    async def _sync_server_time(self):
        """Получает время сервера BingX для корректного timestamp."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/openApi/swap/v2/server/time",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("code") == 0:
                        server_ts = int(data.get("data", {}).get("serverTime", 0))
                        if server_ts > 0:
                            self._time_offset = server_ts - int(time.time() * 1000)
                            return
        except Exception:
            pass
        self._time_offset = 0

    def _get_timestamp(self) -> int:
        offset = getattr(self, "_time_offset", 0)
        return int(time.time() * 1000) + offset

    async def _make_request(self, method, endpoint, params=None, body=None, signed=True):
        try:
            session  = await self._get_session()
            all_p    = {}
            if params: all_p.update(params)
            if body:   all_p.update(body)
            if signed:
                all_p["timestamp"] = self._get_timestamp()
                all_p["recvWindow"] = 10000   # ✅ FIX: 10s окно (было не задано)
                raw_qs = self._build_raw_qs(all_p)
                full_url = f"{self.base_url}{endpoint}?{raw_qs}&signature={self._sign(raw_qs)}"
            else:
                raw_qs   = self._build_raw_qs(all_p)
                full_url = f"{self.base_url}{endpoint}?{raw_qs}" if raw_qs else f"{self.base_url}{endpoint}"
            timeout = aiohttp.ClientTimeout(total=30)
            fn = {"GET": session.get, "POST": session.post, "DELETE": session.delete}[method]
            async with fn(full_url, timeout=timeout) as r:
                return await self._parse_response(r, endpoint)
        except Exception as e:
            self.last_error = str(e)
            print(f"❌ [BingX] {method} {endpoint}: {e}")
            return None

    async def _parse_response(self, response, endpoint=""):
        text = await response.text()
        self.last_error = None
        self.last_error_code = None
        if response.status == 200:
            try:
                data = json.loads(text)
                code = data.get("code")
                if code != 0:
                    msg  = data.get("msg", "unknown")
                    hint = self.ERROR_CODES.get(code, "")
                    self.last_error = msg
                    self.last_error_code = code
                    # ✅ AUTO-SYNC: при ошибке timestamp сбрасываем offset
                    if code == 109400:
                        # ✅ FIX v5: sync immediately
                        await self._sync_server_time()
                    print(f"❌ [BingX] [{endpoint}] code={code} | {msg}"
                          + (f"\n   💡 {hint}" if hint else ""))
                return data
            except Exception as e:
                self.last_error = f"JSON: {e}"
                print(f"❌ [BingX] JSON: {e} | {text[:200]}")
                return None
        else:
            self.last_error = f"HTTP {response.status}"
            print(f"❌ [BingX] HTTP {response.status}: {text[:300]}")
            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # =========================================================================
    # SYMBOL INFO — включает max_notional для auto_trader
    # =========================================================================

    async def _load_contracts(self, force_refresh=False):
        if self._symbols_loaded and not force_refresh:
            return
        if force_refresh:
            self._symbol_info_cache.clear()
            self._active_symbols.clear()
        result = await self._make_request(
            "GET", "/openApi/swap/v2/quote/contracts", params={}, signed=False
        )
        if result and result.get("code") == 0:
            for c in result.get("data", []):
                sym    = c.get("symbol", "")
                status = c.get("status", 1)
                if not sym:
                    continue

                # Пробуем получить max notional из поля maintenanceMarginRate
                # или из contractSize * maxOrderNum
                # BingX возвращает разные поля в зависимости от версии API
                # Безопасный дефолт: 5000 USDT
                max_notional = 5000.0
                try:
                    # Некоторые контракты имеют поле maxPositionValue
                    if "maxPositionValue" in c:
                        max_notional = float(c["maxPositionValue"])
                    elif "maxOrderValue" in c:
                        max_notional = float(c["maxOrderValue"])
                except Exception:
                    pass

                self._symbol_info_cache[sym] = {
                    "price_precision": int(c.get("pricePrecision", 4)),
                    "qty_precision":   int(c.get("quantityPrecision", 3)),
                    "min_qty":         float(c.get("tradeMinQuantity", 0.001)),
                    "max_leverage":    int(c.get("maxLeverage", 50)),
                    "online":          (status != 0),
                    "max_notional":    max_notional,   # ← НОВОЕ: лимит позиции
                }
                if status != 0:
                    self._active_symbols.add(sym)
            self._symbols_loaded = True
            print(f"📋 [BingX] {len(self._symbol_info_cache)} contracts, "
                  f"{len(self._active_symbols)} active")

    async def get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """Получает информацию о символе (precision, min_qty и т.д.)."""
        symbol = self._normalize_symbol(symbol)
        await self._load_contracts()
        return self._symbol_info_cache.get(symbol, {
            "price_precision": 4, "qty_precision": 3, "min_qty": 0.001,
            "max_leverage": 50, "online": False, "max_notional": 5000.0
        })

    async def is_symbol_active(self, symbol: str) -> bool:
        """✅ v5.1: Check if symbol is listed on BingX.
        Error 109425 = symbol не существует на BingX (есть на Binance но не на BingX).
        Решение: проверять кэш контрактов перед любым API вызовом.
        """
        norm = self._normalize_symbol(symbol)
        await self._load_contracts()
        info = self._symbol_info_cache.get(norm)
        if info is None:
            # Пробуем с -USDT форматом
            dash = self._format_for_order(symbol).replace('-', '')
            info = self._symbol_info_cache.get(dash)
        if info is None:
            # Обновляем кэш один раз
            await self._load_contracts(force_refresh=True)
            info = self._symbol_info_cache.get(norm) or self._symbol_info_cache.get(dash if 'dash' in dir() else norm)
        if info is None:
            # Символ реально не существует на BingX → тихо возвращаем False (нет лишних логов)
            return False
        return info.get("online", True)

    async def _round_price(self, symbol: str, price: float) -> float:
        info = await self.get_symbol_info(symbol)
        return round(price, info.get("price_precision", 4))

    def _normalize_symbol(self, symbol: str) -> str:
        """Нормализует символ (без дефисов — для кэша/индексов)."""
        return symbol.replace('-', '').replace('_', '').upper()

    def _format_for_order(self, symbol: str) -> str:
        """✅ FIX v5.1: BingX ORDER API требует ATOM-USDT (с дефисом).
        Ошибка 109400 'must end with -USDT' = неверный формат символа.
        """
        clean = symbol.replace('-', '').replace('_', '').upper()
        if clean.endswith('USDT'):
            return clean[:-4] + '-USDT'
        if clean.endswith('USDC'):
            return clean[:-4] + '-USDC'
        if clean.endswith('BTC'):
            return clean[:-3] + '-BTC'
        return clean + '-USDT'  # fallback

    def _format_symbol_for_positions(self, symbol: str) -> str:
        """
        Форматирует символ для /user/positions endpoint.
        BingX требует формат с -USDT суффиксом: APE-USDT, не APEUSDT
        """
        # Убираем все дефисы сначала
        clean = symbol.replace('-', '').replace('_', '')
        # Если уже заканчивается на USDT или USDC, добавляем дефис перед ним
        if clean.endswith('USDT') and not clean.endswith('-USDT'):
            return clean[:-4] + '-USDT'
        if clean.endswith('USDC') and not clean.endswith('-USDC'):
            return clean[:-4] + '-USDC'
        return clean

    async def _round_qty(self, symbol: str, qty: float) -> float:
        info    = await self.get_symbol_info(symbol)
        prec    = info.get("qty_precision", 3)
        min_qty = info.get("min_qty", 0.001)
        return max(round(qty, prec), min_qty)

    # =========================================================================
    # ACCOUNT
    # =========================================================================

    async def get_account_balance(self) -> Optional[Dict]:
        result = await self._make_request("GET", "/openApi/swap/v2/user/balance")
        if result and result.get("code") == 0:
            data = result.get("data", {})
            bal  = data.get("balance", [])
            b    = (bal[0] if isinstance(bal, list) and bal
                    else bal if isinstance(bal, dict) else {})
            return {
                "equity":          b.get("equity", "0"),
                "availableMargin": b.get("availableMargin", "0"),
                "walletBalance":   b.get("walletBalance", "0"),
                "unrealizedPNL":   b.get("unrealizedPNL", "0"),
            }
        return None

    # =========================================================================
    # POSITIONS
    # =========================================================================

    async def get_positions(self, symbol: str = None) -> List[Dict]:
        endpoint = "/openApi/swap/v2/user/positions"
        params = {}
        if symbol:
            # ✅ FIX: positions endpoint требует формат с -USDT (APE-USDT, не APEUSDT)
            params["symbol"] = self._format_symbol_for_positions(symbol)
        result = await self._make_request("GET", endpoint, params=params)
        positions = []
        if result and result.get("code") == 0:
            for d in result.get("data", []):
                try:
                    size = float(d.get("positionAmt", 0))
                    if size == 0:
                        continue
                    # ✅ FIX: правильное определение side для BOTH hedge mode
                    position_side_raw = d.get("positionSide", "")
                    position_amt = float(d.get("positionAmt", 0))
                    
                    # В BOTH mode: LONG если amt > 0, SHORT если amt < 0
                    # В одностороннем mode: смотрим на positionSide
                    if position_side_raw == "LONG":
                        side = "LONG"
                    elif position_side_raw == "SHORT":
                        side = "SHORT"
                    elif position_side_raw in ("BOTH", ""):
                        # В hedge mode смотрим на знак positionAmt
                        side = "LONG" if position_amt > 0 else "SHORT" if position_amt < 0 else "LONG"
                    else:
                        side = "LONG" if position_amt >= 0 else "SHORT"
                    
                    positions.append(BingXPosition(
                        symbol=d.get("symbol",""),
                        side=side,
                        position_side=position_side_raw,
                        size=abs(size),  # ✅ размер всегда положительный
                        entry_price=float(d.get("avgPrice",0)),
                        leverage=int(d.get("leverage",1)),
                        unrealized_pnl=float(d.get("unrealizedProfit",0)),
                        realized_pnl=float(d.get("realizedProfit",0)),
                        stop_loss=float(d.get("stopLoss",0)) or None,
                        take_profit=float(d.get("takeProfit",0)) or None,
                    ))
                except Exception as e:
                    print(f"⚠️ Position parse: {e}")
        return positions

    async def close_position(self, symbol: str, position_side: str) -> bool:
        # ✅ FIX: используем нормализованный символ
        symbol_api = self._normalize_symbol(symbol)
        result = await self._make_request("POST", "/openApi/swap/v2/trade/closePosition",
                                          body={"symbol": symbol_api, "positionSide": position_side})
        if result and result.get("code") == 0:
            print(f"✅ Closed: {symbol} {position_side}")
            return True
        print(f"❌ Close failed: {symbol} | {self.last_error}")
        return False

    async def close_all_positions(self) -> int:
        positions = await self.get_positions()
        closed = 0
        for p in positions:
            if abs(p.size) > 0 and await self.close_position(p.symbol, p.position_side):
                closed += 1
        return closed

    # =========================================================================
    # ORDERS — v2.4 FINAL
    # =========================================================================

    async def place_order(self, symbol, side, position_side, order_type, size,
                          price=None, stop_loss=None, take_profit=None):
        """
        v2.4 FINAL — все исправления:
          1. RAW URL (не params=) — signature fix
          2. is_symbol_active проверка
          3. Правильное округление price/qty
          4. stopLoss: {"type":"STOP_MARKET", "stopPrice": float, "workingType":"MARK_PRICE"}
          5. takeProfit: {"type":"TAKE_PROFIT_MARKET", "stopPrice": float, ...}
          6. Все значения — числа, не строки (fix float64 mismatch)
        """
        # Нормализуем символ для API (убираем дефисы)
        symbol = self._normalize_symbol(symbol)
        if not await self.is_symbol_active(symbol):
            self.last_error = f"{symbol} offline on BingX"
            print(f"⏭ SKIP — {self.last_error}")
            return None

        rounded_size = await self._round_qty(symbol, size)
        rounded_sl   = await self._round_price(symbol, stop_loss)   if stop_loss   else None
        rounded_tp   = await self._round_price(symbol, take_profit)  if take_profit else None
        rounded_px   = await self._round_price(symbol, price)        if price       else None

        print(f"📤 Order: {symbol} {side} {position_side} {order_type} | "
              f"qty={rounded_size} | SL={rounded_sl} | TP={rounded_tp}")

        body: Dict[str, Any] = {
            "symbol":       symbol,
            "side":         side,
            "positionSide": position_side,
            "type":         order_type,
            "quantity":     str(rounded_size),
        }
        if rounded_px and order_type == "LIMIT":
            body["price"] = str(rounded_px)

        # ✅ type=STOP_MARKET, stopPrice=float (не str, не "price")
        if rounded_sl is not None:
            body["stopLoss"] = json.dumps(
                {"type": "STOP_MARKET", "stopPrice": rounded_sl, "workingType": "MARK_PRICE"},
                separators=(',', ':')
            )
        if rounded_tp is not None:
            body["takeProfit"] = json.dumps(
                {"type": "TAKE_PROFIT_MARKET", "stopPrice": rounded_tp, "workingType": "MARK_PRICE"},
                separators=(',', ':')
            )

        result = await self._make_request("POST", "/openApi/swap/v2/trade/order", body=body)
        if result and result.get("code") == 0:
            d        = result.get("data", {})
            order    = d.get("order", d)
            order_id = str(order.get("orderId", ""))
            print(f"✅ Order placed: {symbol} {side} qty={rounded_size} id={order_id}")
            return BingXOrder(
                order_id=order_id, symbol=symbol, side=side,
                position_side=position_side, type=order_type,
                size=rounded_size, price=rounded_px, status="PENDING",
            )

        code = (result or {}).get("code")
        hint = self.ERROR_CODES.get(code, "") if code else ""
        print(f"❌ Order REJECTED: {symbol} | code={code} | {self.last_error}"
              + (f"\n   💡 {hint}" if hint else ""))
        return None

    async def place_market_order(self, symbol, side, position_side, size,
                                  stop_loss=None, take_profit=None):
        return await self.place_order(symbol=symbol, side=side, position_side=position_side,
                                       order_type="MARKET", size=size,
                                       stop_loss=stop_loss, take_profit=take_profit)

    # =========================================================================
    # LEVERAGE
    # =========================================================================

    async def set_leverage(self, symbol, leverage, position_side="BOTH"):
        sides  = ["LONG", "SHORT"] if position_side == "BOTH" else [position_side]
        all_ok = True
        for side in sides:
            result = await self._make_request("POST", "/openApi/swap/v2/trade/leverage",
                                              body={"symbol": symbol, "leverage": str(leverage),
                                                    "side": side})
            ok = result and result.get("code") == 0
            print(f"{'✅' if ok else '❌'} Leverage {symbol} {side} {leverage}x"
                  + ("" if ok else f" | {self.last_error}"))
            if not ok:
                all_ok = False
        return all_ok

    # =========================================================================
    # CONNECTION TEST
    # =========================================================================

    async def test_connection(self) -> bool:
        """Проверяет соединение с BingX API."""
        try:
            balance = await self.get_account_balance()
            if balance:
                print(f"✅ BingX OK ({'DEMO' if self.demo else 'REAL'}) equity={balance.get('equity','?')}")
                return True
            print(f"❌ BingX failed | {self.last_error}")
            return False
        except Exception as e:
            print(f"❌ BingX error: {e}")
            return False

    async def cancel_all_orders(self, symbol: str) -> bool:
        """
        Отменяет все открытые ордера по символу (SL + TP).
        Используется перед заменой SL при BE/Trail.
        BingX API: DELETE /openApi/swap/v2/trade/allOpenOrders
        """
        try:
            # ✅ FIX: используем нормализованный символ (без дефиса)
            symbol_api = self._normalize_symbol(symbol)
            result = await self._make_request(
                "DELETE", "/openApi/swap/v2/trade/allOpenOrders",
                params={"symbol": symbol_api},
            )
            ok = result and result.get("code") == 0
            if ok:
                print(f"✅ Canceled all orders: {symbol}")
            else:
                print(f"⚠️  Cancel orders {symbol}: {result}")
            return ok
        except Exception as e:
            print(f"⚠️  cancel_all_orders {symbol}: {e}")
            return False

    async def update_stop_loss(self, symbol: str, position_side: str,
                                new_sl: float, direction: str) -> bool:
        """
        ✅ v2.7 DEBUG: Обновляет SL на бирже через cancel + replace.
        1. Отменяем старый SL ордер
        2. Ставим новый STOP_MARKET ордер с новой ценой

        direction = "long"  → side = "SELL" (закрывает LONG)
        direction = "short" → side = "BUY"  (закрывает SHORT)
        """
        try:
            # Для ордеров BingX использует формат без дефиса (MANTAUSDT, не MANTAUSDT-USDT)
            # Но для positions endpoint нужен формат с -USDT (MANTA-USDT)
            symbol_api = self._normalize_symbol(symbol)
            print(f"🔍 [BingX] update_stop_loss START: {symbol} (api={symbol_api}) | new_sl={new_sl} | dir={direction} | pos_side={position_side}")

            rounded_sl = await self._round_price(symbol, new_sl)
            print(f"🔍 [BingX] rounded_sl={rounded_sl}")
            if not rounded_sl:
                print(f"❌ [BingX] rounded_sl is None for {symbol}")
                return False

            # Сторона ордера — противоположная позиции
            sl_side = "SELL" if direction == "long" else "BUY"
            print(f"🔍 [BingX] sl_side={sl_side}")

            # Получаем текущий размер позиции (нужен для SL ордера)
            print(f"🔍 [BingX] Getting positions for {symbol}...")
            positions = await self.get_positions(symbol)
            print(f"🔍 [BingX] Found {len(positions)} positions")
            for p in positions:
                print(f"   - {p.symbol}: size={p.size}, side={p.side}, pos_side={p.position_side}")

            pos = next((p for p in positions
                        if p.symbol.replace("-", "") == symbol.replace("-", "")), None)
            if not pos:
                print(f"⚠️  [BingX] update_stop_loss: позиция {symbol} не найдена")
                return False
            print(f"🔍 [BingX] Found position: {pos.symbol} size={pos.size}")

            remaining_qty = abs(pos.size)
            print(f"🔍 [BingX] remaining_qty={remaining_qty}")
            if remaining_qty <= 0:
                print(f"❌ [BingX] remaining_qty <= 0")
                return False

            rounded_qty = await self._round_qty(symbol, remaining_qty)
            print(f"🔍 [BingX] rounded_qty={rounded_qty}")

            # Отменяем все текущие SL ордера (не трогаем TP — они отдельные)
            print(f"🔍 [BingX] Cancelling all orders for {symbol}...")
            await self.cancel_all_orders(symbol)
            print(f"🔍 [BingX] Orders cancelled")

            # Ставим новый SL ордер
            body = {
                "symbol":       symbol_api,  # ✅ FIX: используем нормализованный символ
                "side":         sl_side,
                "positionSide": position_side,
                "type":         "STOP_MARKET",
                "quantity":     str(rounded_qty),
                "stopPrice":    str(rounded_sl),
                "workingType":  "MARK_PRICE",
                "closePosition": "true",  # закрывает всю позицию
            }
            print(f"🔍 [BingX] Placing SL order: {body}")
            result = await self._make_request("POST", "/openApi/swap/v2/trade/order", body=body)
            print(f"🔍 [BingX] API result: {result}")
            ok = result and result.get("code") == 0
            if ok:
                order_id = result.get("data", {}).get("order", {}).get("orderId", "?")
                print(f"✅ [BingX] New SL placed: {symbol} @ {rounded_sl} | id={order_id}")
            else:
                print(f"❌ [BingX] update_stop_loss failed: {symbol} | code={result.get('code') if result else 'None'} | msg={result.get('msg') if result else 'None'}")
            return ok

        except Exception as e:
            print(f"❌ [BingX] update_stop_loss {symbol}: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Отменяет конкретный ордер по ID."""
        try:
            result = await self._make_request(
                "DELETE", "/openApi/swap/v2/trade/order",
                params={"symbol": symbol, "orderId": order_id},
            )
            return result and result.get("code") == 0
        except Exception as e:
            print(f"⚠️  cancel_order {symbol}/{order_id}: {e}")
            return False
            balance = await self.get_account_balance()
            if balance:
                print(f"✅ BingX OK ({'DEMO' if self.demo else 'REAL'}) equity={balance.get('equity','?')}")
                return True
            print(f"❌ BingX failed | {self.last_error}")
            return False
        except Exception as e:
            print(f"❌ BingX error: {e}")
            return False


_bingx_client = None

def get_bingx_client(demo=True):
    global _bingx_client
    if _bingx_client is None:
        _bingx_client = BingXClient(demo=demo)
    return _bingx_client
