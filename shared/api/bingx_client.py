"""
BingX Futures API Client  v2 — FIXED

ИСПРАВЛЕНИЯ:
  - POST запросы: signature/timestamp идут в URL query string (не в body)
  - _make_request: правильная сигнатура для BingX API
  - Добавлен test_connection при инициализации
"""

import os, json, hmac, hashlib, time
from typing import Optional, Dict, List, Any
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

    def __init__(self,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 demo: bool = True):
        self.api_key    = api_key    or os.getenv("BINGX_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINGX_API_SECRET", "")

        # FORCE DEMO — безопасность. Для REAL нужен BINGX_FORCE_REAL=true
        force_real = os.getenv("BINGX_FORCE_REAL", "false").lower() == "true"
        self.demo = (not force_real) or demo

        if not self.demo:
            print("🚨 WARNING: RUNNING IN REAL MODE!")

        if not self.api_key or not self.api_secret:
            raise ValueError("BingX API key and secret are required")

        self.base_url = self.DEMO_BASE_URL if self.demo else self.REAL_BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
        print(f"🚀 BingX Client initialized ({'DEMO' if self.demo else 'REAL'})")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key}
            )
        return self.session

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Генерация подписи BingX.
        Возвращает params с добавленными timestamp и signature.
        """
        params = dict(params)  # копия
        params["timestamp"] = int(time.time() * 1000)

        # Строка для подписи: отсортированные пары key=value
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        params["signature"] = signature
        return params

    async def _make_request(self,
                            method: str,
                            endpoint: str,
                            params: Optional[Dict] = None,
                            body: Optional[Dict] = None,
                            signed: bool = True) -> Optional[Dict]:
        """
        ✅ FIX: для BingX signature и timestamp ВСЕГДА идут в URL query string,
        даже для POST запросов. Тело запроса (body) — отдельно.

        BingX API pattern:
          GET  → params в query string (+ signature)
          POST → business params в body (JSON), signature в query string
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}{endpoint}"

            query_params = dict(params or {})
            json_body    = dict(body or {})

            if signed:
                # Подписываем query_params (включая business params для GET,
                # или только timestamp для POST)
                if method == "GET":
                    query_params = self._sign(query_params)
                else:
                    # Для POST: business params в body, подпись — в query
                    # Но BingX также принимает всё в query для POST (надёжнее)
                    # Поэтому дублируем params и в query, и в body
                    combined = {**query_params, **json_body}
                    signed_p = self._sign(combined)
                    # В query идут timestamp и signature
                    query_params = {
                        "timestamp": signed_p["timestamp"],
                        "signature": signed_p["signature"],
                    }
                    # В body идут бизнес-параметры
                    json_body = {k: v for k, v in signed_p.items()
                                 if k not in ("timestamp", "signature")}

            timeout = aiohttp.ClientTimeout(total=30)

            if method == "GET":
                async with session.get(url, params=query_params, timeout=timeout) as r:
                    return await self._parse_response(r)

            elif method == "POST":
                async with session.post(url, params=query_params,
                                        json=json_body, timeout=timeout) as r:
                    return await self._parse_response(r)

            elif method == "DELETE":
                async with session.delete(url, params=query_params, timeout=timeout) as r:
                    return await self._parse_response(r)

        except Exception as e:
            print(f"[BingX] Request error {method} {endpoint}: {e}")
            return None

    async def _parse_response(self, response) -> Optional[Dict]:
        text = await response.text()
        if response.status == 200:
            try:
                data = json.loads(text)
                if data.get("code") != 0:
                    print(f"[BingX] API error: code={data.get('code')} msg={data.get('msg')}")
                return data
            except Exception:
                return None
        else:
            print(f"[BingX] HTTP {response.status}: {text[:200]}")
            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # =========================================================================
    # ACCOUNT
    # =========================================================================

    async def get_account_balance(self) -> Optional[Dict]:
        result = await self._make_request("GET", "/openApi/swap/v2/user/balance")
        if result and result.get("code") == 0:
            data = result.get("data", {})
            bal  = data.get("balance", [])
            # Demo: balance может быть списком
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
                    if size == 0:  # пропускаем нулевые
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
                    print(f"[BingX] Position parse error: {e}")
        return positions

    async def close_position(self, symbol: str, position_side: str) -> bool:
        """Закрыть позицию рыночным ордером."""
        result = await self._make_request(
            "POST", "/openApi/swap/v2/trade/closePosition",
            body={"symbol": symbol, "positionSide": position_side},
        )
        if result and result.get("code") == 0:
            print(f"✅ Closed: {symbol} {position_side}")
            return True
        print(f"❌ Close failed: {result}")
        return False

    async def close_all_positions(self) -> int:
        """Закрыть все открытые позиции. Возвращает количество закрытых."""
        positions = await self.get_positions()
        closed = 0
        for p in positions:
            if abs(p.size) > 0:
                ok = await self.close_position(p.symbol, p.position_side)
                if ok:
                    closed += 1
        return closed

    # =========================================================================
    # ORDERS
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
        Разместить ордер.
        ✅ FIX: params идут в body, signature — в URL query string.
        """
        body: Dict[str, Any] = {
            "symbol":       symbol,
            "side":         side,
            "positionSide": position_side,
            "type":         order_type,
            "quantity":     str(size),
        }
        if price and order_type == "LIMIT":
            body["price"] = str(price)
        if stop_loss:
            body["stopLoss"]   = json.dumps({"type": "MARK_PRICE",
                                              "price": str(stop_loss),
                                              "workingType": "MARK_PRICE"})
        if take_profit:
            body["takeProfit"] = json.dumps({"type": "MARK_PRICE",
                                              "price": str(take_profit),
                                              "workingType": "MARK_PRICE"})

        result = await self._make_request(
            "POST", "/openApi/swap/v2/trade/order", body=body
        )

        if result and result.get("code") == 0:
            d = result.get("data", {}).get("order", result.get("data", {}))
            order_id = str(d.get("orderId", ""))
            print(f"✅ Order placed: {symbol} {side} {position_side} qty={size} type={order_type}")
            return BingXOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                position_side=position_side,
                type=order_type,
                size=size,
                price=price,
                status="PENDING",
            )
        print(f"❌ Order failed: {result}")
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
        """
        BingX требует устанавливать плечо отдельно для LONG и SHORT.
        leverage передаётся как строка (BingX API требование).
        """
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
                print(f"❌ Leverage failed: {symbol} {side} | {result}")
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
            print("❌ BingX connection failed (no balance)")
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
