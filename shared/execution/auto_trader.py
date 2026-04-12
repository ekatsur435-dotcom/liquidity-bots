"""
Auto Trader v3

НОВОЕ:
  - Telegram уведомление при КАЖДОЙ ошибке открытия сделки (с причиной)
  - Telegram уведомление об успехе содержит TP уровни
  - MAX_POSITIONS и RISK_PER_TRADE читаются из Config (не захардкожены)
  - Метод notify_error() для вызова извне
"""

import os, asyncio, traceback
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
    max_positions:       int   = 10     # ← теперь 10
    risk_per_trade:      float = 0.001  # ← 0.1% от баланса
    max_daily_risk:      float = 0.05
    default_leverage:    int   = 5
    min_leverage:        int   = 3
    max_leverage:        int   = 10
    min_score_for_trade: int   = 65
    use_trailing_stop:   bool  = True
    trailing_activation: float = 0.015
    trailing_distance:   float = 0.008


class AutoTrader:
    """Авто-трейдер BingX. DEMO и REAL режимы."""

    def __init__(self,
                 bingx_client: Optional[BingXClient] = None,
                 config:       Optional[TradeConfig]  = None,
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
    # PUBLIC API
    # =========================================================================

    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        """Вызывается из scan_market() после генерации сигнала."""
        return await self.open_position(
            symbol       = signal["symbol"],
            direction    = signal["direction"],
            entry_price  = signal["entry_price"],
            stop_loss    = signal["stop_loss"],
            take_profits = signal["take_profits"],
            signal_score = signal["score"],
            smc_data     = signal.get("smc"),
        )

    async def open_position(self,
                            symbol:       str,
                            direction:    str,
                            entry_price:  float,
                            stop_loss:    float,
                            take_profits: List,
                            signal_score: int,
                            smc_data:     Optional[Dict] = None) -> Optional[Dict]:
        """
        Открыть позицию. При любой ошибке — уведомление в Telegram.
        """
        mode = "DEMO" if self.config.demo_mode else "REAL"

        try:
            # ── Проверки перед открытием ──────────────────────────────────

            if not self.config.enabled:
                await self._notify_skip(symbol, direction, signal_score,
                                        "AutoTrader отключён (AUTO_TRADING_ENABLED=false)")
                return None

            if signal_score < self.config.min_score_for_trade:
                # Тихий пропуск — не спамим при каждом слабом сигнале
                print(f"⏸ [{symbol}] Score {signal_score} < {self.config.min_score_for_trade}")
                return None

            self._check_daily_reset()
            if self.daily_pnl <= -self.config.max_daily_risk:
                await self._notify_skip(symbol, direction, signal_score,
                                        f"Дневной лимит риска достигнут ({self.daily_pnl:.2%})")
                return None

            current_positions = await self.bingx.get_positions()
            if len(current_positions) >= self.config.max_positions:
                await self._notify_skip(symbol, direction, signal_score,
                                        f"Макс. позиций на бирже: {len(current_positions)}/{self.config.max_positions}")
                return None

            bingx_symbol = self._to_bingx_symbol(symbol)
            existing = [p for p in current_positions
                        if p.symbol.replace("-", "") == symbol.replace("-", "")]
            if existing:
                print(f"⏸ [{symbol}] Позиция уже открыта")
                return None

            # ── Баланс и расчёт размера ───────────────────────────────────

            balance_data = await self.bingx.get_account_balance()
            if not balance_data:
                await self._notify_error(symbol, direction, signal_score,
                                         "Не удалось получить баланс BingX")
                return None

            available = float(balance_data.get("availableMargin", 0))
            if available <= 0:
                await self._notify_error(symbol, direction, signal_score,
                                         f"Нет свободной маржи (available={available})")
                return None

            # Smart sizing по Score
            if signal_score >= 85:
                risk_mult = 1.5
            elif signal_score >= 75:
                risk_mult = 1.2
            else:
                risk_mult = 1.0

            actual_risk    = self.config.risk_per_trade * risk_mult
            risk_amount    = available * actual_risk

            sl_distance = abs(entry_price - stop_loss) / entry_price
            if sl_distance < 0.001:
                await self._notify_error(symbol, direction, signal_score,
                                         f"SL слишком близко ({sl_distance:.4%})")
                return None

            position_value = risk_amount / sl_distance
            leverage       = self._calc_leverage(signal_score)
            size           = position_value / entry_price
            size           = max(size, 0.001)

            side          = "BUY"  if direction == "long"  else "SELL"
            position_side = "LONG" if direction == "long"  else "SHORT"

            # ── Выставляем плечо ──────────────────────────────────────────

            lev_ok = await self.bingx.set_leverage(bingx_symbol, leverage, position_side)
            if not lev_ok:
                await self._notify_error(symbol, direction, signal_score,
                                         f"Не удалось выставить плечо {leverage}x")
                # Продолжаем — плечо могло быть уже установлено

            # ── TP1 для BingX параметра ───────────────────────────────────

            tp1_price = None
            if take_profits:
                tp_item = take_profits[0]
                if isinstance(tp_item, (list, tuple)):
                    tp1_price = float(tp_item[0])
                elif isinstance(tp_item, dict):
                    tp1_price = float(tp_item.get("price", 0)) or None

            # ── Ордер ─────────────────────────────────────────────────────

            order = await self.bingx.place_market_order(
                symbol        = bingx_symbol,
                side          = side,
                position_side = position_side,
                size          = round(size, 4),
                stop_loss     = round(stop_loss, 6),
                take_profit   = round(tp1_price, 6) if tp1_price else None,
            )

            if not order:
                await self._notify_error(symbol, direction, signal_score,
                                         "BingX отклонил ордер (проверь логи BingX клиента)")
                return None

            # ── Сохраняем в Redis ─────────────────────────────────────────

            position_data = {
                "symbol":       symbol,
                "direction":    direction,
                "entry_price":  entry_price,
                "size":         size,
                "leverage":     leverage,
                "stop_loss":    stop_loss,
                "take_profits": take_profits,
                "signal_score": signal_score,
                "smc_data":     smc_data,
                "order_id":     order.order_id,
                "opened_at":    datetime.utcnow().isoformat(),
                "status":       "open",
                "risk_pct":     round(actual_risk * 100, 3),
                "mode":         mode,
            }
            bot_type = "long" if direction == "long" else "short"
            self.redis.save_position(bot_type, symbol, position_data)

            self.daily_trades += 1

            print(f"✅ [{mode}] {symbol} {direction.upper()} opened")
            print(f"   Entry={entry_price:.6f} SL={stop_loss:.6f} "
                  f"Size={size:.4f} Lev={leverage}x Risk={actual_risk*100:.2f}%")

            # ── Telegram: подтверждение входа ────────────────────────────
            await self._notify_opened(
                symbol, direction, entry_price, stop_loss,
                take_profits, size, leverage, actual_risk, signal_score, mode
            )

            return position_data

        except Exception as e:
            tb = traceback.format_exc()
            print(f"❌ open_position error [{symbol}]: {e}\n{tb}")
            await self._notify_error(symbol, direction, signal_score,
                                     f"Неожиданная ошибка: {e}")
            return None

    # =========================================================================
    # TELEGRAM NOTIFICATIONS
    # =========================================================================

    async def _notify_opened(self,
                              symbol, direction, entry, sl,
                              take_profits, size, leverage, risk, score, mode):
        """Подтверждение успешного открытия сделки."""
        if not self.telegram:
            return
        try:
            d_emoji = "🟢" if direction == "long" else "🔴"
            sl_pct  = abs(entry - sl) / entry * 100

            # Первые 3 TP
            tp_lines = ""
            for i, tp_raw in enumerate(take_profits[:3], 1):
                try:
                    if isinstance(tp_raw, (list, tuple)):
                        tp_p, tp_w = float(tp_raw[0]), float(tp_raw[1])
                    elif isinstance(tp_raw, dict):
                        tp_p, tp_w = float(tp_raw["price"]), float(tp_raw.get("weight", 20))
                    else:
                        continue
                    tp_pct = abs(tp_p - entry) / entry * 100
                    tp_lines += f"  TP{i}: <b>${tp_p:,.6f}</b>  (+{tp_pct:.1f}%)  [{tp_w:.0f}%]\n"
                except Exception:
                    pass

            mode_icon = "🟡" if mode == "DEMO" else "💚"
            await self.telegram.send_message(
                f"✅ <b>СДЕЛКА ОТКРЫТА [{mode}]</b> {mode_icon}\n\n"
                f"{d_emoji} <code>{symbol}</code>  {direction.upper()}\n"
                f"📍 Вход:  <b>${entry:,.6f}</b>\n"
                f"🛑 SL:    <b>${sl:,.6f}</b>  (-{sl_pct:.2f}%)\n"
                f"📊 Размер: {size:.4f}  |  {leverage}x  |  {risk*100:.2f}% риска\n"
                f"🎯 Score: {score}%\n\n"
                f"<b>Take Profits:</b>\n{tp_lines}"
                f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
            )
        except Exception as e:
            print(f"[AutoTrader] notify_opened error: {e}")

    async def _notify_error(self, symbol: str, direction: str,
                             score: int, reason: str):
        """Уведомление об ошибке открытия — отправляется всегда."""
        print(f"❌ Trade error [{symbol}]: {reason}")
        if not self.telegram:
            return
        try:
            d_emoji = "🟢" if direction == "long" else "🔴"
            await self.telegram.send_message(
                f"❌ <b>Сделка не открыта</b>\n\n"
                f"{d_emoji} <code>{symbol}</code>  {direction.upper()}\n"
                f"🎯 Score: {score}%\n"
                f"❌ Причина: {reason}\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
            )
        except Exception:
            pass

    async def _notify_skip(self, symbol: str, direction: str,
                            score: int, reason: str):
        """Тихий пропуск с уведомлением (не ошибка, а ограничение)."""
        print(f"⏸ Trade skipped [{symbol}]: {reason}")
        if not self.telegram:
            return
        try:
            d_emoji = "🟢" if direction == "long" else "🔴"
            await self.telegram.send_message(
                f"⏸ <b>Сделка пропущена</b>\n\n"
                f"{d_emoji} <code>{symbol}</code>  {direction.upper()}\n"
                f"🎯 Score: {score}%\n"
                f"⚠️ Причина: {reason}\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
            )
        except Exception:
            pass

    # =========================================================================
    # OTHER METHODS
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
            if abs(p.size) > 0:
                ok = await self.close_position(p.symbol, p.position_side)
                if ok:
                    closed += 1
        return closed

    async def get_account_summary(self) -> Dict:
        try:
            balance    = await self.bingx.get_account_balance() or {}
            positions  = await self.bingx.get_positions()
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
                "winrate":        round(self.win_count / max(self.win_count + self.loss_count, 1) * 100, 1),
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

    def _calc_leverage(self, score: int) -> int:
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
        """Вызывается из PositionTracker при закрытии сделки."""
        self.total_pnl    += pnl_pct
        self.daily_pnl    += pnl_pct
        self.daily_trades += 1
        if pnl_pct > 0:
            self.win_count  += 1
        else:
            self.loss_count += 1
        print(f"📊 Trade result: {pnl_pct:+.2f}% | "
              f"Total P&L: {self.total_pnl:+.2f}% | "
              f"WinRate: {self.win_count}/{self.win_count+self.loss_count}")
