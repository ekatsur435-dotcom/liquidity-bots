"""
Auto Trader v2.4

ИСПРАВЛЕНИЯ v2.4:
  ✅ risk_per_trade = 0.0005 (0.05% от баланса — было 0.001)
  ✅ code=101209 FIX: авто-уменьшение size если позиция > max notional для плеча
  ✅ SYMBOL_BLACKLIST: XAG, XAU, PAXG и другие commodity/спот символы
     которые не торгуются как фьючерсы на BingX (оффлайн или ограничены)
  ✅ Размещение нескольких TP ордеров (TP1-TP6) после открытия позиции
  ✅ Детальный лог каждого шага, причина любого отказа в Telegram
"""

import os
import asyncio
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from api.bingx_client import BingXClient
from upstash.redis_client import get_redis_client


# ============================================================================
# СИМВОЛЫ КОТОРЫЕ НЕ ТОРГУЮТСЯ КАК ФЬЮЧЕРСЫ НА BINGX
# ============================================================================
# XAG = серебро (Silver), XAU = золото (Gold) — это commodity, не крипто
# PAXG = Pax Gold — gold-backed token, ограниченный лимит позиции
# CLO  = Callisto — малоликвидный
# Добавляй сюда любые символы, которые дают "offline" или code=101209

SYMBOL_BLACKLIST = {
    "XAGUSDT",    # Silver — offline на BingX futures
    "XAUUSDT",    # Gold — offline на BingX futures
    # PAXGUSDT и XAUTUSDT работают, но с маленьким max notional — обрабатываем авто
}


@dataclass
class TradeConfig:
    enabled:             bool  = True
    demo_mode:           bool  = True
    max_positions:       int   = 20
    risk_per_trade:      float = 0.0005   # ✅ 0.05% (было 0.001 = 0.1%)
    max_daily_risk:      float = 0.05
    default_leverage:    int   = 20
    min_leverage:        int   = 5
    max_leverage:        int   = 50
    min_score_for_trade: int   = 65
    # Максимальная позиция в USDT (защита от code=101209)
    max_position_usdt:   float = 5000.0   # BingX часто ограничивает до 1K-5K на малых альтах


class AutoTrader:

    def __init__(self, bingx_client=None, config=None, telegram=None):
        self.config   = config or TradeConfig()
        self.bingx    = bingx_client or BingXClient(demo=self.config.demo_mode)
        self.redis    = get_redis_client()
        self.telegram = telegram

        self.daily_pnl    = 0.0
        self.daily_trades = 0
        self.total_pnl    = 0.0
        self.win_count    = 0
        self.loss_count   = 0
        self.last_reset   = datetime.utcnow().date()

        mode = "DEMO" if self.config.demo_mode else "REAL"
        print(f"🤖 AutoTrader initialized ({mode})")
        print(f"   Risk/trade: {self.config.risk_per_trade*100:.3f}% | "
              f"Max pos: {self.config.max_positions} | "
              f"Min score: {self.config.min_score_for_trade} | "
              f"Max notional: ${self.config.max_position_usdt:,.0f}")

    # =========================================================================
    # TELEGRAM HELPER
    # =========================================================================

    async def _tg(self, msg: str):
        if self.telegram:
            try:
                await self.telegram.send_message(msg)
            except Exception as e:
                print(f"⚠️ Telegram: {e}")

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        symbol = signal.get("symbol", "?")
        score  = signal.get("score", 0)
        print(f"\n🚀 [AutoTrader] {symbol} | score={score:.1f}")
        try:
            return await self.open_position(
                symbol      = symbol,
                direction   = signal["direction"],
                entry_price = signal["entry_price"],
                stop_loss   = signal["stop_loss"],
                take_profits = signal["take_profits"],
                signal_score = signal["score"],
                smc_data    = signal.get("smc"),
                tg_msg_id   = signal.get("tg_msg_id"),
            )
        except KeyError as e:
            print(f"❌ [AutoTrader] {symbol}: missing field {e}")
            return None
        except Exception as e:
            import traceback
            print(f"❌ [AutoTrader] {symbol}: {e}\n{traceback.format_exc()}")
            return None

    async def open_position(self,
                            symbol: str,
                            direction: str,
                            entry_price: float,
                            stop_loss: float,
                            take_profits: List,
                            signal_score: float,
                            smc_data: Optional[Dict] = None,
                            tg_msg_id: Optional[int] = None) -> Optional[Dict]:
        mode = "DEMO" if self.config.demo_mode else "REAL"
        pfx  = f"[AT][{symbol}][{direction.upper()}]"

        try:
            # ── 0. Blacklist ──────────────────────────────────────────────────
            if symbol.upper() in SYMBOL_BLACKLIST:
                print(f"{pfx} ⏭ SKIP — symbol in blacklist")
                return None

            # ── 1. Enabled? ───────────────────────────────────────────────────
            if not self.config.enabled:
                print(f"{pfx} ⏸ SKIP — AutoTrader disabled")
                return None

            # ── 2. Score ──────────────────────────────────────────────────────
            if signal_score < self.config.min_score_for_trade:
                print(f"{pfx} ⏸ SKIP — score {signal_score:.1f} < {self.config.min_score_for_trade}")
                return None

            # ── 3. Daily risk ─────────────────────────────────────────────────
            self._check_daily_reset()
            if self.daily_pnl <= -self.config.max_daily_risk:
                print(f"{pfx} ⏸ SKIP — daily risk limit {self.daily_pnl:.2%}")
                return None

            # ── 4. Max positions ──────────────────────────────────────────────
            print(f"{pfx} 🔍 Checking open positions...")
            current_positions = await self.bingx.get_positions()
            n_pos = len(current_positions)
            pos_list = " | ".join(f"{p.symbol}({p.side})" for p in current_positions)
            print(f"{pfx} 📊 Open: {n_pos}/{self.config.max_positions}")
            if pos_list:
                print(f"{pfx} 📋 {pos_list}")

            if n_pos >= self.config.max_positions:
                print(f"{pfx} ⏸ SKIP — max positions reached")
                await self._tg(
                    f"⏸ <b>AutoTrader [{mode}]</b>\n"
                    f"<code>#{symbol}</code>: max позиций ({n_pos}/{self.config.max_positions})\n"
                    f"Открытые: {pos_list or 'нет'}"
                )
                return None

            # ── 5. Duplicate ──────────────────────────────────────────────────
            bingx_symbol = self._to_bingx_symbol(symbol)
            existing = [p for p in current_positions
                        if p.symbol.replace("-", "") == symbol.replace("-", "")]
            if existing:
                print(f"{pfx} ⏸ SKIP — position exists ({existing[0].side})")
                return None

            # ── 6. BingX symbol active? ───────────────────────────────────────
            is_active = await self.bingx.is_symbol_active(bingx_symbol)
            if not is_active:
                reason = f"{bingx_symbol} offline/delisted on BingX"
                print(f"{pfx} ⏭ SKIP — {reason}")
                await self._tg(
                    f"⏭ <b>AutoTrader [{mode}]</b>\n"
                    f"<code>#{symbol}</code>: {reason}"
                )
                return None

            # ── 7. Balance ────────────────────────────────────────────────────
            print(f"{pfx} 💰 Getting balance...")
            bal = await self.bingx.get_account_balance()
            if not bal:
                reason = f"balance error: {self.bingx.last_error}"
                print(f"{pfx} ❌ SKIP — {reason}")
                return None

            available = float(bal.get("availableMargin", 0))
            equity    = float(bal.get("equity", 0))
            print(f"{pfx} 💰 Equity={equity:.2f} | Available={available:.2f}")

            if available <= 0:
                print(f"{pfx} ❌ SKIP — no available margin")
                return None

            # ── 8. Position sizing ────────────────────────────────────────────
            risk_mult = 1.5 if signal_score >= 85 else (1.2 if signal_score >= 75 else 1.0)
            actual_risk  = self.config.risk_per_trade * risk_mult
            risk_amount  = available * actual_risk

            sl_distance = abs(entry_price - stop_loss) / entry_price
            print(f"{pfx} 📐 entry={entry_price} | SL={stop_loss} | sl_dist={sl_distance:.4%}")

            if sl_distance < 0.001:
                print(f"{pfx} ❌ SKIP — SL distance too small ({sl_distance:.4%})")
                return None

            position_value = risk_amount / sl_distance
            leverage       = self._calc_leverage(signal_score)
            size           = position_value / entry_price

            # ── 9. MAX NOTIONAL CAP (fix code=101209) ─────────────────────────
            # BingX ограничивает notional value для малых альтов на высоких плечах.
            # При превышении → уменьшаем size пропорционально.
            # Notional = size * entry_price
            notional = size * entry_price

            # Из symbol info получаем max notional (если есть)
            sym_info = await self.bingx.get_symbol_info(bingx_symbol)
            max_notional_sym = sym_info.get("max_notional", self.config.max_position_usdt)
            # Берём меньшее из глобального лимита и символьного
            effective_max = min(self.config.max_position_usdt, max_notional_sym)

            if notional > effective_max:
                old_size    = size
                size        = effective_max / entry_price
                new_notional = size * entry_price
                print(f"{pfx} ⚠️ Notional capped: ${notional:.0f} → ${new_notional:.0f} "
                      f"(max ${effective_max:.0f}) | size {old_size:.4f} → {size:.4f}")
                # Пересчитываем реальный риск
                actual_risk = (size * entry_price * sl_distance) / available if available else actual_risk

            print(f"{pfx} 📐 risk={actual_risk*100:.3f}% | notional=${size*entry_price:,.0f} | "
                  f"size={size:.6f} | leverage={leverage}x")

            # ── 10. BingX order params ────────────────────────────────────────
            side          = "BUY"  if direction == "long"  else "SELL"
            position_side = "LONG" if direction == "long"  else "SHORT"

            # TP1 для BingX takeProfit (встроенный в ордер)
            tp1_price = None
            if take_profits:
                tp_item = take_profits[0]
                if isinstance(tp_item, (list, tuple)):
                    tp1_price = float(tp_item[0])
                elif isinstance(tp_item, dict):
                    tp1_price = float(tp_item.get("price", 0)) or None

            # ── 11. Размещаем основной ордер ─────────────────────────────────
            print(f"{pfx} 📤 Sending order to BingX [{mode}]...")
            order = await self.bingx.place_market_order(
                symbol=bingx_symbol, side=side, position_side=position_side,
                size=size,
                stop_loss=stop_loss,
                take_profit=tp1_price,
            )

            if not order:
                bingx_err  = self.bingx.last_error or "unknown"
                bingx_code = self.bingx.last_error_code
                hint = BingXClient.ERROR_CODES.get(bingx_code, "") if bingx_code else ""
                reason = f"code={bingx_code} | {bingx_err}" + (f" | {hint}" if hint else "")
                print(f"{pfx} ❌ ORDER FAILED — {reason}")
                await self._tg(
                    f"❌ <b>AutoTrader [{mode}] — ОРДЕР ОТКЛОНЁН</b>\n\n"
                    f"<code>#{symbol}</code> {direction.upper()}\n"
                    f"Score: {signal_score:.0f}% | SL: {stop_loss}\n\n"
                    f"🔴 Причина: <code>{bingx_err}</code>"
                    + (f"\n💡 {hint}" if hint else "")
                )
                return None

            # ── 12. Размещаем TP2-TP6 как отдельные лимитные ордера ──────────
            if take_profits and len(take_profits) > 1:
                asyncio.create_task(
                    self._place_tp_orders(
                        bingx_symbol, side, position_side,
                        order.size, take_profits[1:], entry_price
                    )
                )

            # ── 13. Сохраняем в Redis ─────────────────────────────────────────
            position_data = {
                "symbol":       symbol,
                "direction":    direction,
                "entry_price":  entry_price,
                "size":         order.size,
                "leverage":     leverage,
                "stop_loss":    stop_loss,
                "take_profits": take_profits,
                "signal_score": signal_score,
                "smc_data":     smc_data,
                "order_id":     order.order_id,
                "opened_at":    datetime.utcnow().isoformat(),
                "status":       "open",
                "risk_pct":     round(actual_risk * 100, 4),
                "tg_msg_id":    tg_msg_id,
                "timestamp":    datetime.utcnow().isoformat(),
            }
            bot_type = "long" if direction == "long" else "short"
            self.redis.save_position(bot_type, symbol, position_data)
            self.redis.save_signal(bot_type, symbol, {**position_data, "status": "active"})
            self.daily_trades += 1

            print(f"✅ {pfx} Position opened [{mode}]! order_id={order.order_id}")

            d_emoji = "🟢" if direction == "long" else "🔴"
            msg = (
                f"🤖 <b>AUTO-TRADE [{mode}]</b>\n\n"
                f"{d_emoji} <code>#{symbol}</code> {direction.upper()}\n"
                f"📍 Entry: <b>{entry_price}</b>\n"
                f"🛑 SL: <b>{stop_loss}</b>\n"
                f"📊 Size: {order.size} | {leverage}x | {actual_risk*100:.3f}% risk\n"
                f"🎯 Score: {signal_score:.0f}%\n"
                f"🆔 OrderID: {order.order_id}"
            )
            if tg_msg_id:
                try:
                    await self.telegram.send_reply(msg, reply_to_message_id=tg_msg_id)
                except Exception:
                    await self._tg(msg)
            else:
                await self._tg(msg)

            return position_data

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"❌ {pfx} EXCEPTION: {e}\n{tb}")
            await self._tg(f"❌ <b>AutoTrader EXCEPTION</b>\n{symbol}: {e}")
            return None

    # =========================================================================
    # MULTIPLE TP ORDERS
    # =========================================================================

    async def _place_tp_orders(self,
                                bingx_symbol: str,
                                side: str,
                                position_side: str,
                                total_size: float,
                                remaining_tps: List,
                                entry_price: float):
        """
        Разместить TP2-TP6 как отдельные ордера после открытия позиции.
        Запускается как asyncio.Task — не блокирует основной поток.

        BingX поддерживает TAKE_PROFIT_MARKET ордера для закрытия позиции.
        Каждый TP ордер закрывает свою долю позиции.
        """
        await asyncio.sleep(2)  # Небольшая пауза после основного ордера

        # Закрывающие ордера: сторона противоположная входу
        close_side          = "SELL" if side == "BUY" else "BUY"
        close_position_side = position_side  # для hedge mode остаётся

        for i, tp_raw in enumerate(remaining_tps):
            try:
                if isinstance(tp_raw, (list, tuple)):
                    tp_price  = float(tp_raw[0])
                    tp_weight = float(tp_raw[1]) / 100 if len(tp_raw) > 1 else 0.2
                elif isinstance(tp_raw, dict):
                    tp_price  = float(tp_raw.get("price", 0))
                    tp_weight = float(tp_raw.get("weight", 20)) / 100
                else:
                    continue

                if tp_price <= 0:
                    continue

                tp_size = round(total_size * tp_weight, 4)
                if tp_size <= 0:
                    continue

                tp_num = i + 2  # TP2, TP3, ...

                # Размещаем TAKE_PROFIT_MARKET закрывающий ордер
                result = await self.bingx._make_request(
                    "POST", "/openApi/swap/v2/trade/order",
                    body={
                        "symbol":       bingx_symbol,
                        "side":         close_side,
                        "positionSide": close_position_side,
                        "type":         "TAKE_PROFIT_MARKET",
                        "quantity":     str(tp_size),
                        "stopPrice":    str(await self.bingx._round_price(bingx_symbol, tp_price)),
                        "workingType":  "MARK_PRICE",
                        "reduceOnly":   "true",
                    }
                )

                if result and result.get("code") == 0:
                    order_id = result.get("data", {}).get("order", {}).get("orderId", "?")
                    print(f"✅ TP{tp_num} order placed: {bingx_symbol} "
                          f"size={tp_size} price={tp_price} id={order_id}")
                else:
                    err = result.get("msg") if result else self.bingx.last_error
                    print(f"⚠️ TP{tp_num} order failed: {bingx_symbol} | {err}")

                await asyncio.sleep(0.5)  # Rate limit

            except Exception as e:
                print(f"⚠️ TP order exception: {e}")

    # =========================================================================
    # CLOSE
    # =========================================================================

    async def close_position(self, symbol: str, position_side: str) -> bool:
        bingx_symbol = self._to_bingx_symbol(symbol)
        ok = await self.bingx.close_position(bingx_symbol, position_side)
        if ok:
            bot_type = "long" if position_side == "LONG" else "short"
            self.redis.close_position(bot_type, symbol, 0.0, 0.0)
        return ok

    async def close_all_positions(self) -> int:
        positions = await self.bingx.get_positions()
        closed = 0
        for p in positions:
            if abs(p.size) > 0 and await self.close_position(p.symbol, p.position_side):
                closed += 1
        return closed

    async def get_account_summary(self) -> Dict:
        try:
            balance   = await self.bingx.get_account_balance() or {}
            positions = await self.bingx.get_positions()
            unrealized = sum(p.unrealized_pnl for p in positions)
            return {
                "balance":        balance,
                "open_positions": len(positions),
                "unrealized_pnl": unrealized,
                "daily_trades":   self.daily_trades,
                "daily_pnl":      self.daily_pnl,
                "total_pnl":      self.total_pnl,
                "win_count":      self.win_count,
                "loss_count":     self.loss_count,
                "mode":           "DEMO" if self.config.demo_mode else "REAL",
            }
        except Exception as e:
            print(f"❌ get_account_summary: {e}")
            return {}

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _to_bingx_symbol(self, symbol: str) -> str:
        if "-" not in symbol and symbol.endswith("USDT"):
            return symbol[:-4] + "-USDT"
        return symbol

    def _calc_leverage(self, score: float) -> int:
        base = self.config.default_leverage
        if score >= 85:
            return min(self.config.max_leverage, base + 2)
        elif score >= 75:
            return min(self.config.max_leverage, base + 1)
        return base

    def _check_daily_reset(self):
        today = datetime.utcnow().date()
        if today != self.last_reset:
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.last_reset   = today
            print("📅 Daily stats reset")

    def record_trade_result(self, pnl_pct: float):
        self.total_pnl    += pnl_pct
        self.daily_pnl    += pnl_pct
        self.daily_trades += 1
        if pnl_pct > 0:
            self.win_count  += 1
        else:
            self.loss_count += 1


# Импорт ERROR_CODES для использования в except блоках
try:
    from api.bingx_client import BingXClient
except ImportError:
    class BingXClient:
        ERROR_CODES = {}
