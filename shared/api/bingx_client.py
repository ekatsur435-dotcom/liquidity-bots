"""
BingX Futures API Client  v2.3

ИСПРАВЛЕНИЯ:
  v2.1 — RAW URL query string (signature verification fix)
  v2.2 — compact JSON separators для SL/TP
  v2.3 ✅ КРИТИЧНО: "price" в stopLoss/takeProfit = FLOAT, не str!
         Было: {"price": "9.317"} → Ошибка "Mismatch type float64 with value string"
         Стало: {"price": 9.317}  → BingX принимает
       ✅ Фильтр оффлайн символов: is_symbol_active()
         Ошибка "XTZ-USDT is offline currently" → символ пропускается до запроса
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
        80001: "Parameter error",
        80012: "Price precision error — цена не кратна tickSize пары",
        80014: "Quantity precision/min error — объём ниже минимума",
        80016: "Order does not exist",
        80020: "Insufficient margin",
        80021: "Position does not exist",
        80022: "Max positions reached",
        80030: "Symbol does not exist",
        80041: "SL/TP price invalid",
        101204: "Insufficient balance",
        109201: "Leverage exceeds max allowed",
    }

    def __init__(self,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 demo: bool = True):
        self.api_key    = api_key    or os.getenv("BINGX_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINGX_API_SECRET", "")

        force_real = os.getenv("BINGX_FORCE_REAL", "false").lower() == "true"
        self.demo  = (not force_real) or demo

        if not self.demo:
            print("🚨 WARNING: RUNNING IN REAL MODE!")
        if not self.api_key or not self.api_secret:
            raise ValueError("BingX API key and secret are required")

        self.base_url = self.DEMO_BASE_URL if self.demo else self.REAL_BASE_URL
        self.session:  Optional[aiohttp.ClientSession] = None

        self._symbol_info_cache: Dict[str, Dict] = {}
        self._active_symbols:    Set[str]         = set()
        self._symbols_loaded:    bool             = False

        self.last_error:      Optional[str] = None
        self.last_error_code: Optional[int] = None

        print(f"🚀 BingX Client initialized ({'DEMO' if self.demo else 'REAL'})")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"X-BX-APIKEY": self.api_key})
        return self.session

    # ── Подпись ──────────────────────────────────────────────────────────────

    def _sign(self, raw_qs: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            raw_qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_raw_qs(self, params: Dict[str, Any]) -> str:
        """RAW query string без URL-кодирования — критично для подписи!"""
        return "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def _make_request(self,
                            method: str,
                            endpoint: str,
                            params: Optional[Dict] = None,
                            body: Optional[Dict] = None,
                            signed: bool = True) -> Optional[Dict]:
        """
        ✅ RAW URL — строим вручную, НЕ используем params= в aiohttp.
        aiohttp URL-кодирует специальные символы → подпись не совпадает.
        """
        try:
            session  = await self._get_session()
            base_url = f"{self.base_url}{endpoint}"

            all_params: Dict[str, Any] = {}
            if params:
                all_params.update(params)
            if body:
                all_params.update(body)

            if signed:
                all_params["timestamp"] = int(time.time() * 1000)
                raw_qs    = self._build_raw_qs(all_params)
                signature = self._sign(raw_qs)
                full_url  = f"{base_url}?{raw_qs}&signature={signature}"
            else:
                raw_qs   = self._build_raw_qs(all_params)
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
            print(f"❌ [BingX] Request exception {method} {endpoint}: {e}")
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
                    msg  = data.get("msg", "unknown")
                    hint = self.ERROR_CODES.get(code, "")
                    self.last_error      = msg
                    self.last_error_code = code
                    print(f"❌ [BingX] [{endpoint}] code={code} | {msg}"
                          + (f" | {hint}" if hint else ""))
                return data
            except Exception as e:
                self.last_error = f"JSON: {e}"
                print(f"❌ [BingX] JSON parse: {e} | raw: {text[:200]}")
                return None
        else:
            self.last_error = f"HTTP {response.status}"
            print(f"❌ [BingX] HTTP {response.status} [{endpoint}]: {text[:300]}")
            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # =========================================================================
    # SYMBOL INFO, PRECISION, ONLINE CHECK
    # =========================================================================

    async def _load_contracts(self):
        """Загрузить все контракты BingX. Кэшируется на весь сеанс."""
        if self._symbols_loaded:
            return

        result = await self._make_request(
            "GET", "/openApi/swap/v2/quote/contracts", params={}, signed=False
        )
        if result and result.get("code") == 0:
            for c in result.get("data", []):
                sym    = c.get("symbol", "")
                # status 0 = offline, 1 = online (по документации BingX)
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
            print(f"📋 [BingX] Loaded {len(self._symbol_info_cache)} contracts, "
                  f"{len(self._active_symbols)} active")
        else:
            print(f"⚠️ [BingX] Failed to load contracts | {self.last_error}")

    async def get_symbol_info(self, symbol: str) -> Dict:
        """Получить точность символа. Кэшируется."""
        await self._load_contracts()
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        print(f"⚠️ [BingX] Symbol not found: {symbol} (offline or delisted)")
        return {"price_precision": 4, "qty_precision": 3, "min_qty": 0.001,
                "max_leverage": 50, "online": False}

    async def is_symbol_active(self, symbol: str) -> bool:
        """Проверить торгуется ли символ на BingX сейчас."""
        await self._load_contracts()
        info = self._symbol_info_cache.get(symbol)
        if info is None:
            return False   # не нашли в списке контрактов → оффлайн/делистинг
        return info.get("online", True)

    async def _round_price(self, symbol: str, price: float) -> float:
        info = await self.get_symbol_info(symbol)
        return round(price, info.get("price_precision", 4))

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

    async def get_positions(self, symbol: Optional[str] = None) -> List[BingXPosition]:
        params = {"symbol": symbol} if symbol else {}
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
                    print(f"⚠️ [BingX] Position parse: {e}")
        return positions

    async def close_position(self, symbol: str, position_side: str) -> bool:
        result = await self._make_request(
            "POST", "/openApi/swap/v2/trade/closePosition",
            body={"symbol": symbol, "positionSide": position_side},
        )
        if result and result.get("code") == 0:
            print(f"✅ Closed: {symbol} {position_side}")
            return True
        print(f"❌ Close failed: {symbol} {position_side} | {self.last_error}")
        return False

    async def close_all_positions(self) -> int:
        positions = await self.get_positions()
        closed = 0
        for p in positions:
            if abs(p.size) > 0 and await self.close_position(p.symbol, p.position_side):
                closed += 1
        return closed

    # =========================================================================
    # ORDERS  ← v2.3 CRITICAL FIX: price = float, не str
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
        ✅ v2.3: "price" в stopLoss/takeProfit JSON = FLOAT (не str).

        Ошибка "Mismatch type float64 with value string":
          Было:  json.dumps({"price": str(9.317)}) → {"price":"9.317"}
          Стало: json.dumps({"price": 9.317})      → {"price":9.317}

        BingX Go-backend ожидает number, получал string → type mismatch.

        ✅ Проверка is_symbol_active перед запросом (фильтр оффлайн).
        """
        # ── 1. Проверяем онлайн статус ───────────────────────────────────────
        bingx_sym = symbol   # уже в формате BTC-USDT (конвертация в auto_trader)
        active = await self.is_symbol_active(bingx_sym)
        if not active:
            self.last_error = f"{bingx_sym} is offline or delisted on BingX"
            print(f"⏭ [BingX] SKIP — {self.last_error}")
            return None

        # ── 2. Округление ────────────────────────────────────────────────────
        rounded_size = await self._round_qty(bingx_sym, size)
        rounded_sl   = await self._round_price(bingx_sym, stop_loss)   if stop_loss   else None
        rounded_tp   = await self._round_price(bingx_sym, take_profit)  if take_profit else None
        rounded_px   = await self._round_price(bingx_sym, price)        if price       else None

        print(f"📤 Order: {bingx_sym} {side} {position_side} {order_type} | "
              f"qty={rounded_size} | SL={rounded_sl} | TP={rounded_tp}")

        body: Dict[str, Any] = {
            "symbol":       bingx_sym,
            "side":         side,
            "positionSide": position_side,
            "type":         order_type,
            "quantity":     str(rounded_size),
        }
        if rounded_px and order_type == "LIMIT":
            body["price"] = str(rounded_px)

        # ✅ "price": rounded_sl — float, НЕ str(rounded_sl)!
        if rounded_sl is not None:
            body["stopLoss"] = json.dumps(
                {"type": "MARK_PRICE", "price": rounded_sl, "workingType": "MARK_PRICE"},
                separators=(',', ':')
            )
        if rounded_tp is not None:
            body["takeProfit"] = json.dumps(
                {"type": "MARK_PRICE", "price": rounded_tp, "workingType": "MARK_PRICE"},
                separators=(',', ':')
            )

        result = await self._make_request("POST", "/openApi/swap/v2/trade/order", body=body)

        if result and result.get("code") == 0:
            d        = result.get("data", {})
            order    = d.get("order", d)
            order_id = str(order.get("orderId", ""))
            print(f"✅ Order placed: {bingx_sym} {side} qty={rounded_size} id={order_id}")
            return BingXOrder(
                order_id=order_id, symbol=bingx_sym, side=side,
                position_side=position_side, type=order_type,
                size=rounded_size, price=rounded_px, status="PENDING",
            )

        code = (result or {}).get("code")
        hint = self.ERROR_CODES.get(code, "") if code else ""
        print(f"❌ Order REJECTED: {bingx_sym} | code={code} | {self.last_error}"
              + (f" | {hint}" if hint else ""))
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
        sides  = ["LONG", "SHORT"] if position_side == "BOTH" else [position_side]
        all_ok = True
        for side in sides:
            result = await self._make_request(
                "POST", "/openApi/swap/v2/trade/leverage",
                body={"symbol": symbol, "leverage": str(leverage), "side": side},
            )
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
        try:
            balance = await self.get_account_balance()
            if balance:
                equity = balance.get("equity", "?")
                print(f"✅ BingX OK ({'DEMO' if self.demo else 'REAL'}) equity={equity}")
                return True
            print(f"❌ BingX connection failed | {self.last_error}")
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
