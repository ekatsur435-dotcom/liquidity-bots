"""
Auto Trader v2 — FIXED

ИСПРАВЛЕНИЯ:
  - __init__ принимает telegram (был TypeError при инициализации)
  - open_position: правильный вызов BingX с stop_loss как JSON
  - execute_signal: удобный метод для вызова из scan_symbol
  - Добавлен Telegram notify при открытии/закрытии позиции
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
    enabled:            bool  = True
    demo_mode:          bool  = True
    max_positions:      int   = 5
    risk_per_trade:     float = 0.01    # 1% от баланса
    max_daily_risk:     float = 0.05    # 5% максимум в день
    default_leverage:   int   = 5
    min_leverage:       int   = 3
    max_leverage:       int   = 10
    min_score_for_trade: int  = 65
    use_trailing_stop:  bool  = True
    trailing_activation: float = 0.015
    trailing_distance:  float = 0.008


class AutoTrader:
    """
    Авто-трейдер для исполнения сигналов через BingX.
    Поддерживает DEMO и REAL режимы.
    """

    def __init__(self,
                 bingx_client: Optional[BingXClient] = None,
                 config: Optional[TradeConfig] = None,
                 telegram=None):                        # ✅ FIX: принимаем telegram
        self.config   = config or TradeConfig()
        self.bingx    = bingx_client or BingXClient(demo=self.config.demo_mode)
        self.redis    = get_redis_client()
        self.telegram = telegram                        # ✅ сохраняем для уведомлений

        # Статистика
        self.daily_pnl    = 0.0
        self.daily_trades = 0
        self.total_pnl    = 0.0
        self.win_count    = 0
        self.loss_count   = 0
        self.last_reset   = datetime.utcnow().date()

        mode = "DEMO" if self.config.demo_mode else "REAL"
        print(f"🤖 AutoTrader initialized ({mode})")
        print(f"   Risk/trade: {self.config.risk_per_trade*100:.1f}% | "
              f"Max pos: {self.config.max_positions} | "
              f"Min score: {self.config.min_score_for_trade}")

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        """
        Удобная обёртка — вызывается из scan_market() после сигнала.
        Извлекает нужные поля и передаёт в open_position.
        """
        return await self.open_position(
            symbol=signal["symbol"],
            direction=signal["direction"],
            entry_price=signal["entry_price"],
            stop_loss=signal["stop_loss"],
            take_profits=signal["take_profits"],
            signal_score=signal["score"],
            smc_data=signal.get("smc"),
        )

    async def open_position(self,
                            symbol: str,
                            direction: str,
                            entry_price: float,
                            stop_loss: float,
                            take_profits: List,
                            signal_score: int,
                            smc_data: Optional[Dict] = None) -> Optional[Dict]:
        """
        Открыть позицию на BingX.

        direction: "long" | "short"
        take_profits: список (price, weight%) или [(price, weight), ...]
        """
        try:
            if not self.config.enabled:
                print("⏸ AutoTrader disabled")
                return None

            # Проверяем Score
            if signal_score < self.config.min_score_for_trade:
                print(f"⏸ Score {signal_score} < min {self.config.min_score_for_trade}")
                return None

            # Дневной лимит
            self._check_daily_reset()
            if self.daily_pnl <= -self.config.max_daily_risk:
                print(f"⏸ Daily risk limit reached ({self.daily_pnl:.2%})")
                return None

            # Проверяем количество позиций на бирже
            current_positions = await self.bingx.get_positions()
            if len(current_positions) >= self.config.max_positions:
                print(f"⏸ Max positions ({self.config.max_positions}) reached")
                return None

            # Проверяем нет ли уже позиции по этой паре
            bingx_symbol = self._to_bingx_symbol(symbol)
            existing = [p for p in current_positions
                        if p.symbol.replace("-", "") == symbol.replace("-", "")]
            if existing:
                print(f"⏸ Position already exists for {symbol}")
                return None

            # Получаем баланс
            balance_data = await self.bingx.get_account_balance()
            if not balance_data:
                print("❌ Failed to get balance")
                return None

            available = float(balance_data.get("availableMargin", 0))
            if available <= 0:
                print("❌ No available margin")
                return None

            # Smart position sizing по Score
            if signal_score >= 85:
                risk_mult = 1.5
            elif signal_score >= 75:
                risk_mult = 1.2
            else:
                risk_mult = 1.0

            actual_risk   = self.config.risk_per_trade * risk_mult
            risk_amount   = available * actual_risk

            # Расстояние до SL
            sl_distance = abs(entry_price - stop_loss) / entry_price
            if sl_distance < 0.001:
                print(f"❌ SL distance too small ({sl_distance:.4%})")
                return None

            position_value = risk_amount / sl_distance
            leverage       = self._calc_leverage(signal_score)
            size           = position_value / entry_price

            # Минимальный размер (BingX требует минимум)
            size = max(size, 0.001)

            # BingX side/positionSide
            side          = "BUY"  if direction == "long"  else "SELL"
            position_side = "LONG" if direction == "long"  else "SHORT"

            # Устанавливаем плечо
            await self.bingx.set_leverage(bingx_symbol, leverage, position_side)

            # Ближайший TP1 для BingX take_profit параметра
            tp1_price = None
            if take_profits:
                tp_item = take_profits[0]
                if isinstance(tp_item, (list, tuple)):
                    tp1_price = float(tp_item[0])
                elif isinstance(tp_item, dict):
                    tp1_price = float(tp_item.get("price", 0)) or None

            # Размещаем рыночный ордер
            order = await self.bingx.place_market_order(
                symbol=bingx_symbol,
                side=side,
                position_side=position_side,
                size=round(size, 4),
                stop_loss=round(stop_loss, 6),
                take_profit=round(tp1_price, 6) if tp1_price else None,
            )

            if not order:
                print(f"❌ Order placement failed for {symbol}")
                return None

            # Сохраняем позицию в Redis
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
                "risk_pct":     round(actual_risk * 100, 2),
            }
            bot_type = "long" if direction == "long" else "short"
            self.redis.save_position(bot_type, symbol, position_data)

            self.daily_trades += 1

            mode = "DEMO" if self.config.demo_mode else "REAL"
            print(f"✅ Position opened [{mode}]: {symbol} {direction.upper()}")
            print(f"   Entry: {entry_price:.6f} | SL: {stop_loss:.6f} | "
                  f"Size: {size:.4f} | Leverage: {leverage}x | Risk: {actual_risk*100:.1f}%")

            # Telegram уведомление
            if self.telegram:
                try:
                    d_emoji = "🟢" if direction == "long" else "🔴"
                    await self.telegram.send_message(
                        f"🤖 <b>AUTO-TRADE [{mode}]</b>\n\n"
                        f"{d_emoji} <code>{symbol}</code> {direction.upper()}\n"
                        f"📍 Entry: <b>{entry_price:.6f}</b>\n"
                        f"🛑 SL: <b>{stop_loss:.6f}</b>\n"
                        f"📊 Size: {size:.4f}  |  {leverage}x  |  {actual_risk*100:.1f}% risk\n"
                        f"🎯 Score: {signal_score}%"
                    )
                except Exception:
                    pass

            return position_data

        except Exception as e:
            print(f"❌ open_position error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def close_position(self, symbol: str, position_side: str) -> bool:
        bingx_symbol = self._to_bingx_symbol(symbol)
        ok = await self.bingx.close_position(bingx_symbol, position_side)
        if ok:
            bot_type = "long" if position_side == "LONG" else "short"
            self.redis.close_position(bot_type, symbol, 0.0, 0.0)
        return ok

    async def close_all_positions(self) -> int:
        """Закрыть все позиции на BingX и очистить Redis."""
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
                "balance":       balance,
                "open_positions": len(positions),
                "unrealized_pnl": unrealized,
                "daily_trades":  self.daily_trades,
                "daily_pnl":     self.daily_pnl,
                "total_pnl":     self.total_pnl,
                "win_count":     self.win_count,
                "loss_count":    self.loss_count,
                "mode":          "DEMO" if self.config.demo_mode else "REAL",
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
        """Записать результат сделки (вызывается из PositionTracker)."""
        self.total_pnl    += pnl_pct
        self.daily_pnl    += pnl_pct
        self.daily_trades += 1
        if pnl_pct > 0:
            self.win_count  += 1
        else:
            self.loss_count += 1
