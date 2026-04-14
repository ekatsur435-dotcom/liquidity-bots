"""
Auto Trader v2.1 — FULL LOGGING

ИЗМЕНЕНИЯ v2.1:
  - open_position: детальный лог на каждом шаге (видно в Render)
  - Telegram уведомление при ОТКАЗЕ открыть позицию (с причиной)
  - execute_signal: ловит и логирует все исключения + Telegram
  - _count_positions_log: показывает текущие позиции при отказе
  - Убран silent except в scan_market вызове
"""

import os, asyncio
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from api.bingx_client import BingXClient, BingXPosition, BingXOrder
from upstash.redis_client import get_redis_client


@dataclass
class TradeConfig:
    enabled:             bool  = True
    demo_mode:           bool  = True
    max_positions:       int   = 10        # ← ENV: MAX_LONG_POSITIONS / MAX_SHORT_POSITIONS
    risk_per_trade:      float = 0.001     # 0.1% от баланса
    max_daily_risk:      float = 0.05      # 5% максимум в день
    default_leverage:    int   = 20
    min_leverage:        int   = 5
    max_leverage:        int   = 50
    min_score_for_trade: int   = 65
    use_trailing_stop:   bool  = True
    trailing_activation: float = 0.015
    trailing_distance:   float = 0.008


class AutoTrader:
    """
    Авто-трейдер для исполнения сигналов через BingX.
    Все отказы логируются в Render + Telegram.
    """

    def __init__(self,
                 bingx_client: Optional[BingXClient] = None,
                 config: Optional[TradeConfig] = None,
                 telegram=None):
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
        print(f"   Risk/trade: {self.config.risk_per_trade*100:.2f}% | "
              f"Max pos: {self.config.max_positions} | "
              f"Min score: {self.config.min_score_for_trade}")

    # =========================================================================
    # TELEGRAM HELPER
    # =========================================================================

    async def _tg(self, msg: str):
        """Отправить сообщение в Telegram (не бросает исключений)."""
        if self.telegram:
            try:
                await self.telegram.send_message(msg)
            except Exception as e:
                print(f"⚠️ Telegram send failed: {e}")

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        """
        Вызывается из scan_market() при каждом сигнале.
        Логирует все отказы в Render и Telegram.
        """
        symbol = signal.get("symbol", "?")
        score  = signal.get("score", 0)
        print(f"\n🚀 [AutoTrader] execute_signal: {symbol} | score={score:.1f}")

        try:
            result = await self.open_position(
                symbol=symbol,
                direction=signal["direction"],
                entry_price=signal["entry_price"],
                stop_loss=signal["stop_loss"],
                take_profits=signal["take_profits"],
                signal_score=signal["score"],
                smc_data=signal.get("smc"),
            )
            if result is None:
                print(f"⚠️ [AutoTrader] {symbol}: position NOT opened (see reason above)")
            return result

        except KeyError as e:
            msg = f"❌ [AutoTrader] {symbol}: missing field in signal: {e}"
            print(msg)
            await self._tg(f"⚠️ AutoTrader ошибка\n{symbol}: отсутствует поле {e}")
            return None
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"❌ [AutoTrader] {symbol}: unexpected error: {e}\n{tb}")
            await self._tg(f"⚠️ AutoTrader ошибка\n{symbol}: {e}")
            return None

    async def open_position(self,
                            symbol: str,
                            direction: str,
                            entry_price: float,
                            stop_loss: float,
                            take_profits: List,
                            signal_score: float,
                            smc_data: Optional[Dict] = None) -> Optional[Dict]:
        """
        Открыть позицию на BingX.
        Каждый шаг логируется — причина отказа всегда видна.
        """
        mode = "DEMO" if self.config.demo_mode else "REAL"
        log_prefix = f"[AutoTrader][{symbol}][{direction.upper()}]"

        try:
            # ── Шаг 1: AutoTrader включён? ─────────────────────────────────
            if not self.config.enabled:
                print(f"{log_prefix} ⏸ SKIP — AutoTrader disabled")
                return None

            # ── Шаг 2: Score ───────────────────────────────────────────────
            if signal_score < self.config.min_score_for_trade:
                print(f"{log_prefix} ⏸ SKIP — score {signal_score:.1f} < min {self.config.min_score_for_trade}")
                return None

            # ── Шаг 3: Дневной лимит ───────────────────────────────────────
            self._check_daily_reset()
            if self.daily_pnl <= -self.config.max_daily_risk:
                reason = f"daily risk limit reached ({self.daily_pnl:.2%})"
                print(f"{log_prefix} ⏸ SKIP — {reason}")
                await self._tg(f"⏸ <b>AutoTrader</b>\n{symbol}: {reason}")
                return None

            # ── Шаг 4: Количество позиций на BingX ────────────────────────
            print(f"{log_prefix} 🔍 Checking open positions...")
            current_positions = await self.bingx.get_positions()
            n_pos = len(current_positions)
            print(f"{log_prefix} 📊 Open positions: {n_pos}/{self.config.max_positions}")

            if current_positions:
                pos_list = " | ".join(
                    f"{p.symbol}({p.side})" for p in current_positions
                )
                print(f"{log_prefix} 📋 Positions: {pos_list}")

            if n_pos >= self.config.max_positions:
                reason = f"max positions reached ({n_pos}/{self.config.max_positions})"
                print(f"{log_prefix} ⏸ SKIP — {reason}")
                await self._tg(
                    f"⏸ <b>AutoTrader [{mode}]</b>\n"
                    f"{symbol}: {reason}\n"
                    f"Открытые: {pos_list if current_positions else 'нет'}"
                )
                return None

            # ── Шаг 5: Дублирование позиции ───────────────────────────────
            bingx_symbol = self._to_bingx_symbol(symbol)
            existing = [p for p in current_positions
                        if p.symbol.replace("-", "") == symbol.replace("-", "")]
            if existing:
                reason = f"position already exists ({existing[0].side})"
                print(f"{log_prefix} ⏸ SKIP — {reason}")
                return None

            # ── Шаг 6: Баланс ─────────────────────────────────────────────
            print(f"{log_prefix} 💰 Getting balance...")
            balance_data = await self.bingx.get_account_balance()
            if not balance_data:
                reason = f"failed to get balance | BingX error: {self.bingx.last_error}"
                print(f"{log_prefix} ❌ SKIP — {reason}")
                await self._tg(f"❌ <b>AutoTrader [{mode}]</b>\n{symbol}: {reason}")
                return None

            available = float(balance_data.get("availableMargin", 0))
            equity    = float(balance_data.get("equity", 0))
            print(f"{log_prefix} 💰 Equity={equity:.2f} | Available={available:.2f}")

            if available <= 0:
                reason = "no available margin"
                print(f"{log_prefix} ❌ SKIP — {reason}")
                await self._tg(f"❌ <b>AutoTrader [{mode}]</b>\n{symbol}: {reason}")
                return None

            # ── Шаг 7: Position sizing ────────────────────────────────────
            if signal_score >= 85:
                risk_mult = 1.5
            elif signal_score >= 75:
                risk_mult = 1.2
            else:
                risk_mult = 1.0

            actual_risk   = self.config.risk_per_trade * risk_mult
            risk_amount   = available * actual_risk

            sl_distance = abs(entry_price - stop_loss) / entry_price
            print(f"{log_prefix} 📐 entry={entry_price} | SL={stop_loss} | sl_dist={sl_distance:.4%}")

            if sl_distance < 0.001:
                reason = f"SL distance too small ({sl_distance:.4%}), min 0.1%"
                print(f"{log_prefix} ❌ SKIP — {reason}")
                await self._tg(f"❌ <b>AutoTrader [{mode}]</b>\n{symbol}: {reason}")
                return None

            position_value = risk_amount / sl_distance
            leverage       = self._calc_leverage(signal_score)
            size           = position_value / entry_price

            print(f"{log_prefix} 📐 risk={actual_risk*100:.2f}% | risk_amt={risk_amount:.2f} | "
                  f"pos_val={position_value:.2f} | size={size:.6f} | leverage={leverage}x")

            # ── Шаг 8: BingX параметры ────────────────────────────────────
            side          = "BUY"  if direction == "long"  else "SELL"
            position_side = "LONG" if direction == "long"  else "SHORT"

            tp1_price = None
            if take_profits:
                tp_item = take_profits[0]
                if isinstance(tp_item, (list, tuple)):
                    tp1_price = float(tp_item[0])
                elif isinstance(tp_item, dict):
                    tp1_price = float(tp_item.get("price", 0)) or None

            # ── Шаг 9: Размещаем ордер ───────────────────────────────────
            print(f"{log_prefix} 📤 Sending order to BingX [{mode}]...")
            order = await self.bingx.place_market_order(
                symbol=bingx_symbol,
                side=side,
                position_side=position_side,
                size=size,          # bingx_client сам округлит
                stop_loss=stop_loss,
                take_profit=tp1_price,
            )

            if not order:
                # Детальная причина из bingx_client
                bingx_err  = self.bingx.last_error or "unknown"
                bingx_code = self.bingx.last_error_code
                hint = BingXClient.ERROR_CODES.get(bingx_code, "") if bingx_code else ""
                reason = f"BingX rejected order: code={bingx_code} msg={bingx_err}"
                if hint:
                    reason += f" ({hint})"

                print(f"{log_prefix} ❌ ORDER FAILED — {reason}")
                await self._tg(
                    f"❌ <b>AutoTrader [{mode}] — ОРДЕР ОТКЛОНЁН</b>\n\n"
                    f"<code>{symbol}</code> {direction.upper()}\n"
                    f"Score: {signal_score:.1f}% | SL: {stop_loss}\n\n"
                    f"🔴 Причина: <code>{bingx_err}</code>\n"
                    + (f"💡 Hint: {hint}" if hint else "")
                )
                return None

            # ── Шаг 10: Успех ────────────────────────────────────────────
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
            }
            bot_type = "long" if direction == "long" else "short"
            self.redis.save_position(bot_type, symbol, position_data)
            self.daily_trades += 1

            print(f"✅ {log_prefix} Position opened [{mode}]!")
            print(f"   Entry: {entry_price} | SL: {stop_loss} | "
                  f"Size: {order.size} | Lev: {leverage}x | Risk: {actual_risk*100:.2f}%")

            d_emoji = "🟢" if direction == "long" else "🔴"
            await self._tg(
                f"🤖 <b>AUTO-TRADE [{mode}]</b>\n\n"
                f"{d_emoji} <code>{symbol}</code> {direction.upper()}\n"
                f"📍 Entry: <b>{entry_price}</b>\n"
                f"🛑 SL: <b>{stop_loss}</b>\n"
                f"📊 Size: {order.size} | {leverage}x | {actual_risk*100:.2f}% risk\n"
                f"🎯 Score: {signal_score:.1f}%\n"
                f"🆔 OrderID: {order.order_id}"
            )

            return position_data

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"❌ {log_prefix} EXCEPTION in open_position: {e}\n{tb}")
            await self._tg(f"❌ <b>AutoTrader [{mode}] EXCEPTION</b>\n{symbol}: {e}")
            return None

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
            if abs(p.size) > 0:
                ok = await self.close_position(p.symbol, p.position_side)
                if ok:
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
        """BTCUSDT → BTC-USDT (BingX формат)"""
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


# ── Импорт ERROR_CODES для Telegram сообщений ─────────────────────────────
try:
    from api.bingx_client import BingXClient
except ImportError:
    class BingXClient:
        ERROR_CODES = {}
