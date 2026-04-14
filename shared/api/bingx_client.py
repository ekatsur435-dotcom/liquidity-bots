"""
BingX Futures API Client  v2.1 — FULL LOGGING

ИЗМЕНЕНИЯ v2.1:
  - _parse_response: сохраняет последнюю ошибку в self.last_error
  - place_order: возвращает (order | None, error_msg) — ошибка не теряется
  - get_symbol_info: получает tickSize и stepSize для правильного округления
  - _round_price / _round_qty: округление с учётом точности биржи
  - Все ошибки BingX теперь видны в Render логах
"""

import os, json, hmac, hashlib, time, math
from typing import Optional, Dict, List, Any, Tuple
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
    stop_price: Optional[float] = None
    status: str = "PENDING"
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    created_at: Optional[datetime] = None


class BingXClient:
    DEMO_BASE_URL = "https://open-api-vst.bingx.com"
    REAL_BASE_URL = "https://open-api.bingx.com"

    # Коды ошибок BingX с описанием (для удобного дебага)
    ERROR_CODES = {
        80001: "Parameter error",
        80012: "Price precision error — цена не соответствует tick size",
        80014: "Quantity precision error — объём не соответствует step size",
        80016: "Order does not exist",
        80017: "Order already closed",
        80020: "Insufficient margin",
        80021: "Position does not exist",
        80022: "Max positions reached",
        80030: "Symbol does not exist",
        80041: "SL/TP price invalid",
        100400: "API endpoint does not exist",
        100410: "API key invalid",
        100500: "Internal server error",
    }

    def __init__(self,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 demo: bool = True):
        self.api_key    = api_key    or os.getenv("BINGX_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINGX_API_SECRET", "")

        force_real = os.getenv("BINGX_FORCE_REAL", "false").lower() == "true"
        self.demo = (not force_real) or demo

        if not self.demo:
            print("🚨 WARNING: RUNNING IN REAL MODE!")

        if not self.api_key or not self.api_secret:
            raise ValueError("BingX API key and secret are required")

        self.base_url = self.DEMO_BASE_URL if self.demo else self.REAL_BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None

        # Кэш точности символов (tickSize, stepSize)
        self._symbol_info_cache: Dict[str, Dict] = {}

        # Последняя ошибка (для внешнего доступа)
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[int] = None

        print(f"🚀 BingX Client initialized ({'DEMO' if self.demo else 'REAL'})")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key}
            )
        return self.session

    def _sign(self, params: Dict[str, Any]) -> str:
        ordered = sorted(params.items())
        query_string = "&".join(f"{k}={v}" for k, v in ordered)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _make_request(self,
                            method: str,
                            endpoint: str,
                            params: Optional[Dict] = None,
                            body: Optional[Dict] = None,
                            signed: bool = True) -> Optional[Dict]:
        try:
            session = await self._get_session()
            url = f"{self.base_url}{endpoint}"

            all_params: Dict[str, Any] = {}
            if params:
                all_params.update(params)
            if body:
                all_params.update(body)

            if signed:
                all_params["timestamp"] = int(time.time() * 1000)
                all_params["signature"] = self._sign(all_params)

            timeout = aiohttp.ClientTimeout(total=30)

            if method == "GET":
                async with session.get(url, params=all_params, timeout=timeout) as r:
                    return await self._parse_response(r, endpoint)
            elif method == "POST":
                async with session.post(url, params=all_params, timeout=timeout) as r:
                    return await self._parse_response(r, endpoint)
            elif method == "DELETE":
                async with session.delete(url, params=all_params, timeout=timeout) as r:
                    return await self._parse_response(r, endpoint)

        except Exception as e:
            self.last_error = str(e)
            print(f"❌ [BingX] Request error {method} {endpoint}: {e}")
            return None

    async def _parse_response(self, response, endpoint: str = "") -> Optional[Dict]:
        text = await response.text()
        self.last_error = None
        self.last_error_code = None

        if response.status == 200:
            try:
                data = json.loads(text)
                code = data.get("code")
                if code != 0:
                    msg = data.get("msg", "unknown error")
                    hint = self.ERROR_CODES.get(code, "")
                    self.last_error = msg
                    self.last_error_code = code
                    # ✅ ПОЛНЫЙ лог ошибки — виден в Render
                    print(f"❌ [BingX] API Error on {endpoint}")
                    print(f"   Code: {code} | Msg: {msg}")
                    if hint:
                        print(f"   Hint: {hint}")
                return data
            except Exception as e:
                self.last_error = f"JSON parse error: {e}"
                print(f"❌ [BingX] JSON parse error: {e} | raw: {text[:200]}")
                return None
        else:
            self.last_error = f"HTTP {response.status}"
            print(f"❌ [BingX] HTTP {response.status} on {endpoint}: {text[:300]}")
            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # =========================================================================
    # SYMBOL INFO & PRECISION
    # =========================================================================

    async def get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """
        Получить информацию о символе: tickSize, stepSize, minQty.
        Кэшируется в памяти.
        """
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        result = await self._make_request(
            "GET", "/openApi/swap/v2/quote/contracts",
            params={}, signed=False
        )
        if result and result.get("code") == 0:
            for contract in result.get("data", []):
                sym = contract.get("symbol", "")
                info = {
                    "tickSize":    float(contract.get("pricePrecision", 0.01)),
                    "stepSize":    float(contract.get("quantityPrecision", 0.001)),
                    "minQty":      float(contract.get("tradeMinQuantity", 0.001)),
                    "maxLeverage": int(contract.get("maxLeverage", 50)),
                }
                self._symbol_info_cache[sym] = info

            if symbol in self._symbol_info_cache:
                info = self._symbol_info_cache[symbol]
                print(f"📐 {symbol} precision: tickSize={info['tickSize']} stepSize={info['stepSize']} minQty={info['minQty']}")
                return info

        # Fallback: безопасные значения
        print(f"⚠️ [BingX] Symbol info not found for {symbol}, using defaults")
        return {"tickSize": 0.0001, "stepSize": 0.001, "minQty": 0.001, "maxLeverage": 50}

    def _round_to_step(self, value: float, step: float) -> float:
        """Округлить value до ближайшего кратного step."""
        if step <= 0:
            return value
        precision = max(0, -int(math.floor(math.log10(step))))
        return round(round(value / step) * step, precision)

    async def _round_price(self, symbol: str, price: float) -> float:
        """Округлить цену согласно tickSize символа."""
        info = await self.get_symbol_info(symbol)
        tick = info.get("tickSize", 0.0001) if info else 0.0001
        return self._round_to_step(price, tick)

    async def _round_qty(self, symbol: str, qty: float) -> float:
        """Округлить объём согласно stepSize символа."""
        info = await self.get_symbol_info(symbol)
        step = info.get("stepSize", 0.001) if info else 0.001
        min_qty = info.get("minQty", 0.001) if info else 0.001
        rounded = self._round_to_step(qty, step)
        return max(rounded, min_qty)

    # =========================================================================
    # ACCOUNT
    # =========================================================================

    async def get_account_balance(self) -> Optional[Dict]:
        result = await self._make_request("GET", "/openApi/swap/v2/user/balance")
        if result and result.get("code") == 0:
            data = result.get("data", {})
            bal  = data.get("balance", [])
            if isinstance(bal, list) and bal:
                b = bal[0]
            elif isinstance(bal, dict):
                b = bal
            else:
                b = {}
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

    async def get_positions(self, symbol: Optional[str] = None) -> List[BingXPosition]:
        params = {}
        if symbol:
            params["symbol"] = symbol
        result = await self._make_request("GET", "/openApi/swap/v2/user/positions", params=params)
        positions = []
        if result and result.get("code") == 0:
            for d in result.get("data", []):
                try:
                    size = float(d.get("positionAmt", 0))
                    if size == 0:
                        continue
                    positions.append(BingXPosition(
                        symbol=d.get("symbol", ""),
                        side="LONG" if d.get("positionSide") == "LONG" else "SHORT",
                        position_side=d.get("positionSide", ""),
                        size=size,
                        entry_price=float(d.get("avgPrice", 0)),
                        leverage=int(d.get("leverage", 1)),
                        unrealized_pnl=float(d.get("unrealizedProfit", 0)),
                        realized_pnl=float(d.get("realizedProfit", 0)),
                        stop_loss=float(d.get("stopLoss", 0)) or None,
                        take_profit=float(d.get("takeProfit", 0)) or None,
                    ))
                except Exception as e:
                    print(f"⚠️ [BingX] Position parse error: {e}")
        return positions

    async def close_position(self, symbol: str, position_side: str) -> bool:
        result = await self._make_request(
            "POST", "/openApi/swap/v2/trade/closePosition",
            body={"symbol": symbol, "positionSide": position_side},
        )
        if result and result.get("code") == 0:
            print(f"✅ Closed: {symbol} {position_side}")
            return True
        print(f"❌ Close failed: {symbol} {position_side} | error: {self.last_error}")
        return False

    async def close_all_positions(self) -> int:
        positions = await self.get_positions()
        closed = 0
        for p in positions:
            if abs(p.size) > 0:
                ok = await self.close_position(p.symbol, p.position_side)
                if ok:
                    closed += 1
        return closed

    # =========================================================================
    # ORDERS — с правильным округлением и полным логом
    # =========================================================================

    async def place_order(self,
                          symbol: str,
                          side: str,
                          position_side: str,
                          order_type: str,
                          size: float,
                          price: Optional[float] = None,
                          stop_loss: Optional[float] = None,
                          take_profit: Optional[float] = None) -> Optional[BingXOrder]:
        """
        Разместить ордер с правильным округлением цен/объёмов.
        При ошибке — полный лог в Render + self.last_error для Telegram.
        """
        # ✅ Округляем согласно точности символа на BingX
        rounded_size = await self._round_qty(symbol, size)
        rounded_sl   = await self._round_price(symbol, stop_loss) if stop_loss else None
        rounded_tp   = await self._round_price(symbol, take_profit) if take_profit else None
        rounded_px   = await self._round_price(symbol, price) if price else None

        print(f"📤 Placing order: {symbol} {side} {position_side} | "
              f"type={order_type} | qty={rounded_size} (raw={size:.6f}) | "
              f"SL={rounded_sl} | TP={rounded_tp}")

        body: Dict[str, Any] = {
            "symbol":       symbol,
            "side":         side,
            "positionSide": position_side,
            "type":         order_type,
            "quantity":     str(rounded_size),
        }
        if rounded_px and order_type == "LIMIT":
            body["price"] = str(rounded_px)
        if rounded_sl:
            body["stopLoss"] = json.dumps({
                "type": "MARK_PRICE",
                "price": str(rounded_sl),
                "workingType": "MARK_PRICE"
            })
        if rounded_tp:
            body["takeProfit"] = json.dumps({
                "type": "MARK_PRICE",
                "price": str(rounded_tp),
                "workingType": "MARK_PRICE"
            })

        result = await self._make_request(
            "POST", "/openApi/swap/v2/trade/order", body=body
        )

        if result and result.get("code") == 0:
            d = result.get("data", {}).get("order", result.get("data", {}))
            order_id = str(d.get("orderId", ""))
            print(f"✅ Order placed OK: {symbol} {side} {position_side} "
                  f"qty={rounded_size} id={order_id}")
            return BingXOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                position_side=position_side,
                type=order_type,
                size=rounded_size,
                price=rounded_px,
                status="PENDING",
            )

        # ❌ Ордер отклонён — детальный лог
        code = result.get("code") if result else None
        msg  = result.get("msg")  if result else self.last_error
        hint = self.ERROR_CODES.get(code, "") if code else ""
        print(f"❌ Order REJECTED: {symbol} {side} {position_side}")
        print(f"   Params: qty={rounded_size} SL={rounded_sl} TP={rounded_tp}")
        print(f"   BingX: code={code} | msg={msg}")
        if hint:
            print(f"   Hint: {hint}")
        return None

    async def place_market_order(self,
                                  symbol: str,
                                  side: str,
                                  position_side: str,
                                  size: float,
                                  stop_loss: Optional[float] = None,
                                  take_profit: Optional[float] = None) -> Optional[BingXOrder]:
        return await self.place_order(
            symbol=symbol, side=side, position_side=position_side,
            order_type="MARKET", size=size,
            stop_loss=stop_loss, take_profit=take_profit,
        )

    # =========================================================================
    # LEVERAGE
    # =========================================================================

    async def set_leverage(self, symbol: str, leverage: int,
                           position_side: str = "BOTH") -> bool:
        sides = ["LONG", "SHORT"] if position_side == "BOTH" else [position_side]
        all_ok = True
        for side in sides:
            result = await self._make_request(
                "POST", "/openApi/swap/v2/trade/leverage",
                body={"symbol": symbol, "leverage": str(leverage), "side": side},
            )
            if result and result.get("code") == 0:
                print(f"✅ Leverage set: {symbol} {side} {leverage}x")
            else:
                print(f"❌ Leverage failed: {symbol} {side} | {self.last_error}")
                all_ok = False
        return all_ok

    # =========================================================================
    # CONNECTION TEST
    # =========================================================================

    async def test_connection(self) -> bool:
        try:
            balance = await self.get_account_balance()
            if balance:
                equity = balance.get("equity", "?")
                print(f"✅ BingX OK ({'DEMO' if self.demo else 'REAL'}) equity={equity}")
                return True
            print(f"❌ BingX connection failed | last error: {self.last_error}")
            return False
        except Exception as e:
            print(f"❌ BingX connection error: {e}")
            return False


# ============================================================================
# SINGLETON
# ============================================================================

_bingx_client: Optional[BingXClient] = None

def get_bingx_client(demo: bool = True) -> BingXClient:
    global _bingx_client
    if _bingx_client is None:
        _bingx_client = BingXClient(demo=demo)
    return _bingx_client
