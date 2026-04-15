"""
BingX Futures API Client  v2.4

КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ v2.4:
  ✅ code=109400 ИСПРАВЛЕНО: неверное поле "type" в stopLoss/takeProfit
     Было:  {"type": "MARK_PRICE", "price": 9.317, "workingType": "MARK_PRICE"}
     Стало: {"type": "STOP_MARKET", "stopPrice": 9.317, "workingType": "MARK_PRICE"}
  v2.3 — price как float, фильтр offline символов
  v2.2 — compact JSON
  v2.1 — RAW URL signature fix
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
        80012:  "Price precision error",
        80014:  "Quantity min/precision error",
        80020:  "Insufficient margin",
        80022:  "Max positions reached",
        80030:  "Symbol does not exist",
        80041:  "SL/TP price invalid",
        109400: "SL/TP type invalid — должен быть STOP_MARKET/TAKE_PROFIT_MARKET",
    }

    def __init__(self, api_key=None, api_secret=None, demo=True):
        self.api_key    = api_key    or os.getenv("BINGX_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINGX_API_SECRET", "")
        force_real = os.getenv("BINGX_FORCE_REAL", "false").lower() == "true"
        self.demo  = (not force_real) or demo
        if not self.demo:
            print("WARNING: REAL MODE!")
        if not self.api_key or not self.api_secret:
            raise ValueError("BingX API key and secret are required")
        self.base_url = self.DEMO_BASE_URL if self.demo else self.REAL_BASE_URL
        self.session:  Optional[aiohttp.ClientSession] = None
        self._symbol_info_cache: Dict[str, Dict] = {}
        self._active_symbols: Set[str] = set()
        self._symbols_loaded = False
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[int] = None
        print(f"BingX Client initialized ({'DEMO' if self.demo else 'REAL'})")

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"X-BX-APIKEY": self.api_key})
        return self.session

    def _sign(self, raw_qs):
        return hmac.new(self.api_secret.encode(), raw_qs.encode(), hashlib.sha256).hexdigest()

    def _build_raw_qs(self, params):
        return "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    async def _make_request(self, method, endpoint, params=None, body=None, signed=True):
        try:
            session  = await self._get_session()
            base_url = f"{self.base_url}{endpoint}"
            all_params = {}
            if params: all_params.update(params)
            if body:   all_params.update(body)
            if signed:
                all_params["timestamp"] = int(time.time() * 1000)
                raw_qs = self._build_raw_qs(all_params)
                full_url = f"{base_url}?{raw_qs}&signature={self._sign(raw_qs)}"
            else:
                raw_qs = self._build_raw_qs(all_params)
                full_url = f"{base_url}?{raw_qs}" if raw_qs else base_url
            timeout = aiohttp.ClientTimeout(total=30)
            if method == "GET":
                async with session.get(full_url, timeout=timeout) as r:
                    return await self._parse_response(r, endpoint)
            elif method == "POST":
                async with session.post(full_url, timeout=timeout) as r:
                    return await self._parse_response(r, endpoint)
            elif method == "DELETE":
                async with session.delete(full_url, timeout=timeout) as r:
                    return await self._parse_response(r, endpoint)
        except Exception as e:
            self.last_error = str(e)
            print(f"[BingX] Request exception {endpoint}: {e}")
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
                    print(f"[BingX] [{endpoint}] code={code} | {msg}" + (f" | {hint}" if hint else ""))
                return data
            except Exception as e:
                self.last_error = f"JSON: {e}"
                print(f"[BingX] JSON: {e} | raw: {text[:200]}")
                return None
        else:
            self.last_error = f"HTTP {response.status}"
            print(f"[BingX] HTTP {response.status} [{endpoint}]: {text[:300]}")
            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _load_contracts(self):
        if self._symbols_loaded:
            return
        result = await self._make_request("GET", "/openApi/swap/v2/quote/contracts", params={}, signed=False)
        if result and result.get("code") == 0:
            for c in result.get("data", []):
                sym = c.get("symbol", "")
                status = c.get("status", 1)
                if sym:
                    self._symbol_info_cache[sym] = {
                        "price_precision": int(c.get("pricePrecision", 4)),
                        "qty_precision":   int(c.get("quantityPrecision", 3)),
                        "min_qty":         float(c.get("tradeMinQuantity", 0.001)),
                        "max_leverage":    int(c.get("maxLeverage", 50)),
                        "online":          (status != 0),
                    }
                    if status != 0:
                        self._active_symbols.add(sym)
            self._symbols_loaded = True
            print(f"[BingX] Loaded {len(self._symbol_info_cache)} contracts, {len(self._active_symbols)} active")

    async def get_symbol_info(self, symbol):
        await self._load_contracts()
        return self._symbol_info_cache.get(symbol, {"price_precision": 4, "qty_precision": 3, "min_qty": 0.001, "max_leverage": 50, "online": False})

    async def is_symbol_active(self, symbol):
        await self._load_contracts()
        info = self._symbol_info_cache.get(symbol)
        return info.get("online", True) if info else False

    async def _round_price(self, symbol, price):
        info = await self.get_symbol_info(symbol)
        return round(price, info.get("price_precision", 4))

    async def _round_qty(self, symbol, qty):
        info = await self.get_symbol_info(symbol)
        return max(round(qty, info.get("qty_precision", 3)), info.get("min_qty", 0.001))

    async def get_account_balance(self):
        result = await self._make_request("GET", "/openApi/swap/v2/user/balance")
        if result and result.get("code") == 0:
            data = result.get("data", {})
            bal  = data.get("balance", [])
            b    = (bal[0] if isinstance(bal, list) and bal else bal if isinstance(bal, dict) else {})
            return {"equity": b.get("equity","0"), "availableMargin": b.get("availableMargin","0"),
                    "walletBalance": b.get("walletBalance","0"), "unrealizedPNL": b.get("unrealizedPNL","0")}
        return None

    async def get_positions(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        result = await self._make_request("GET", "/openApi/swap/v2/user/positions", params=params)
        positions = []
        if result and result.get("code") == 0:
            for d in result.get("data", []):
                try:
                    size = float(d.get("positionAmt", 0))
                    if size == 0: continue
                    positions.append(BingXPosition(
                        symbol=d.get("symbol",""), side="LONG" if d.get("positionSide")=="LONG" else "SHORT",
                        position_side=d.get("positionSide",""), size=size,
                        entry_price=float(d.get("avgPrice",0)), leverage=int(d.get("leverage",1)),
                        unrealized_pnl=float(d.get("unrealizedProfit",0)), realized_pnl=float(d.get("realizedProfit",0)),
                        stop_loss=float(d.get("stopLoss",0)) or None, take_profit=float(d.get("takeProfit",0)) or None,
                    ))
                except Exception as e:
                    print(f"[BingX] Position parse: {e}")
        return positions

    async def close_position(self, symbol, position_side):
        result = await self._make_request("POST", "/openApi/swap/v2/trade/closePosition",
                                          body={"symbol": symbol, "positionSide": position_side})
        if result and result.get("code") == 0:
            print(f"Closed: {symbol} {position_side}")
            return True
        print(f"Close failed: {symbol} | {self.last_error}")
        return False

    async def close_all_positions(self):
        positions = await self.get_positions()
        closed = 0
        for p in positions:
            if abs(p.size) > 0 and await self.close_position(p.symbol, p.position_side):
                closed += 1
        return closed

    async def place_order(self, symbol, side, position_side, order_type, size,
                          price=None, stop_loss=None, take_profit=None):
        """
        v2.4 FIX: Правильная структура SL/TP для BingX Futures API.

        stopLoss:   {"type": "STOP_MARKET",       "stopPrice": 9.317, "workingType": "MARK_PRICE"}
        takeProfit: {"type": "TAKE_PROFIT_MARKET", "stopPrice": 9.5,  "workingType": "MARK_PRICE"}

        Поля:
          type      = STOP_MARKET (для SL) / TAKE_PROFIT_MARKET (для TP)
          stopPrice = цена триггера (НЕ "price"!)
          workingType = MARK_PRICE (активация по mark price)
        """
        # 1. Онлайн проверка
        if not await self.is_symbol_active(symbol):
            self.last_error = f"{symbol} is offline on BingX"
            print(f"SKIP — {self.last_error}")
            return None

        # 2. Округление
        rounded_size = await self._round_qty(symbol, size)
        rounded_sl   = await self._round_price(symbol, stop_loss)   if stop_loss   else None
        rounded_tp   = await self._round_price(symbol, take_profit)  if take_profit else None
        rounded_px   = await self._round_price(symbol, price)        if price       else None

        print(f"Order: {symbol} {side} {position_side} {order_type} | qty={rounded_size} | SL={rounded_sl} | TP={rounded_tp}")

        body = {
            "symbol": symbol, "side": side, "positionSide": position_side,
            "type": order_type, "quantity": str(rounded_size),
        }
        if rounded_px and order_type == "LIMIT":
            body["price"] = str(rounded_px)

        # v2.4: STOP_MARKET / TAKE_PROFIT_MARKET + stopPrice (не price!)
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
            d = result.get("data", {})
            order = d.get("order", d)
            order_id = str(order.get("orderId", ""))
            print(f"Order placed: {symbol} {side} qty={rounded_size} id={order_id}")
            return BingXOrder(order_id=order_id, symbol=symbol, side=side,
                              position_side=position_side, type=order_type,
                              size=rounded_size, price=rounded_px, status="PENDING")

        code = (result or {}).get("code")
        hint = self.ERROR_CODES.get(code, "") if code else ""
        print(f"Order REJECTED: {symbol} | code={code} | {self.last_error}" + (f" | {hint}" if hint else ""))
        return None

    async def place_market_order(self, symbol, side, position_side, size,
                                  stop_loss=None, take_profit=None):
        return await self.place_order(symbol=symbol, side=side, position_side=position_side,
                                       order_type="MARKET", size=size,
                                       stop_loss=stop_loss, take_profit=take_profit)

    async def set_leverage(self, symbol, leverage, position_side="BOTH"):
        sides = ["LONG", "SHORT"] if position_side == "BOTH" else [position_side]
        all_ok = True
        for side in sides:
            result = await self._make_request("POST", "/openApi/swap/v2/trade/leverage",
                                              body={"symbol": symbol, "leverage": str(leverage), "side": side})
            ok = result and result.get("code") == 0
            print(f"{'OK' if ok else 'FAIL'} Leverage {symbol} {side} {leverage}x")
            if not ok: all_ok = False
        return all_ok

    async def test_connection(self):
        try:
            balance = await self.get_account_balance()
            if balance:
                print(f"BingX OK ({'DEMO' if self.demo else 'REAL'}) equity={balance.get('equity','?')}")
                return True
            return False
        except Exception as e:
            print(f"BingX connection error: {e}")
            return False


_bingx_client = None

def get_bingx_client(demo=True):
    global _bingx_client
    if _bingx_client is None:
        _bingx_client = BingXClient(demo=demo)
    return _bingx_client
