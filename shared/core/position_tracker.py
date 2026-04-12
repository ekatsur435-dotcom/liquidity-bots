"""
Position Tracker — shared/core/position_tracker.py
Мониторинг активных сигналов: TP / SL / трейлинг / экспирация / P&L

НОВОЕ в этой версии:
  - Trailing stop: после +1.5% двигаем SL в безубыток, потом трейлим
  - Уведомление "🔄 Стоп передвинут"
  - Уведомление при ошибке открытия сделки
  - Подключение к AutoTrader.record_trade_result()
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


class PositionTracker:
    """
    Каждые CHECK_INTERVAL секунд:
      1. Берёт все active-сигналы из Redis
      2. Получает текущую цену через Binance
      3. Проверяет TP / SL / трейлинг-стоп
      4. Отправляет уведомления в Telegram
      5. Записывает P&L в дневную статистику
      6. Экспирирует сигналы старше MAX_AGE_HOURS

    Использование в main.py:
        state.tracker = PositionTracker(
            bot_type       = Config.BOT_TYPE,
            telegram       = state.telegram,
            redis_client   = state.redis,
            binance_client = state.binance,
            config         = Config,
            auto_trader    = state.auto_trader,   # ← передай для record_trade_result
        )
        tracker_task = asyncio.create_task(state.tracker.run())
    """

    CHECK_INTERVAL = 30     # секунд между проходами
    MAX_AGE_HOURS  = 24     # сигнал экспирируется через N часов

    # Trailing stop settings
    TRAIL_ACTIVATION = 0.015    # активируется при прибыли +1.5%
    TRAIL_DISTANCE   = 0.008    # держим SL на 0.8% от текущей цены
    BREAKEVEN_BUFFER = 0.001    # SL в безубыток = entry + 0.1%

    def __init__(self, *,
                 bot_type:       str,
                 telegram,
                 redis_client,
                 binance_client,
                 config,
                 auto_trader=None):         # ← опционально для record_trade_result
        self.bot_type    = bot_type
        self.tg          = telegram
        self.redis       = redis_client
        self.binance     = binance_client
        self.config      = config
        self.auto_trader = auto_trader
        self._running    = False

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    async def run(self):
        self._running = True
        print(f"📍 PositionTracker started (interval={self.CHECK_INTERVAL}s)")
        while self._running:
            try:
                await self._scan_all()
            except Exception as e:
                print(f"[PositionTracker] loop error: {e}")
            await asyncio.sleep(self.CHECK_INTERVAL)

    def stop(self):
        self._running = False

    # =========================================================================
    # MAIN SCAN
    # =========================================================================

    async def _scan_all(self):
        try:
            signals = self.redis.get_active_signals(self.bot_type)
        except Exception as e:
            print(f"[PositionTracker] redis error: {e}")
            return

        if not signals:
            return

        for sig in signals:
            if sig.get("status") != "active":
                continue
            try:
                await self._check_one(sig)
            except Exception as e:
                print(f"[PositionTracker] {sig.get('symbol')} error: {e}")
            await asyncio.sleep(0.3)

    async def _check_one(self, signal: Dict):
        symbol    = signal.get("symbol", "")
        entry     = _f(signal.get("entry_price", 0))
        sl        = _f(signal.get("stop_loss", 0))
        direction = signal.get("direction", "short")
        opened_at = signal.get("timestamp", "")
        tps_raw   = signal.get("take_profits", [])
        taken     = list(signal.get("taken_tps", []))

        if not symbol or not entry:
            return

        # --- Экспирация ---
        if opened_at:
            try:
                age = datetime.utcnow() - datetime.fromisoformat(opened_at)
                if age > timedelta(hours=self.MAX_AGE_HOURS):
                    await self._expire(signal)
                    return
            except Exception:
                pass

        # --- Текущая цена ---
        md = await self.binance.get_complete_market_data(symbol)
        if not md:
            return
        price = _f(md.price)

        # --- Trailing stop (проверяем ДО SL hit) ---
        await self._check_trailing(signal, price)

        # --- Обновляем SL из сигнала (мог измениться при трейлинге) ---
        sl = _f(signal.get("stop_loss", 0))

        # --- Стоп-лосс ---
        if sl and _sl_hit(direction, price, sl):
            await self._close_sl(signal, price)
            return

        # --- Тейк-профиты ---
        for i, tp_raw in enumerate(tps_raw):
            if i in taken:
                continue
            tp_price, tp_weight = _parse_tp(tp_raw)
            if tp_price <= 0:
                continue
            if _tp_hit(direction, price, tp_price):
                is_last = (len(taken) + 1 >= len(tps_raw))
                await self._close_tp(signal, i, tp_price, tp_weight, price, is_last)
                break

    # =========================================================================
    # TRAILING STOP
    # =========================================================================

    async def _check_trailing(self, signal: Dict, price: float):
        """
        Логика трейлинг-стопа:
          1. Когда прибыль >= TRAIL_ACTIVATION (1.5%):
             - Первый раз: двигаем SL в безубыток (entry + buffer)
             - Сообщаем: "🔄 Стоп передвинут в безубыток"
          2. Когда trailing уже активен:
             - Трейлим SL на TRAIL_DISTANCE (0.8%) от текущей цены
             - Сообщаем если SL существенно сдвинулся (>= 0.5%)
        """
        entry     = _f(signal.get("entry_price", 0))
        direction = signal.get("direction", "short")
        current_sl = _f(signal.get("stop_loss", 0))
        trailing_active = signal.get("trailing_active", False)

        if not entry or not current_sl:
            return

        if direction == "long":
            profit_pct = (price - entry) / entry

            if profit_pct >= self.TRAIL_ACTIVATION:
                if not trailing_active:
                    # Шаг 1: безубыток
                    new_sl = entry * (1 + self.BREAKEVEN_BUFFER)
                    if new_sl > current_sl:
                        await self._move_sl(signal, current_sl, new_sl, "безубыток")
                else:
                    # Шаг 2: трейлим
                    new_sl = price * (1 - self.TRAIL_DISTANCE)
                    if new_sl > current_sl * 1.005:  # двигаем только если > 0.5%
                        await self._move_sl(signal, current_sl, new_sl, "трейлинг")

        else:  # short
            profit_pct = (entry - price) / entry

            if profit_pct >= self.TRAIL_ACTIVATION:
                if not trailing_active:
                    new_sl = entry * (1 - self.BREAKEVEN_BUFFER)
                    if new_sl < current_sl:
                        await self._move_sl(signal, current_sl, new_sl, "безубыток")
                else:
                    new_sl = price * (1 + self.TRAIL_DISTANCE)
                    if new_sl < current_sl * 0.995:
                        await self._move_sl(signal, current_sl, new_sl, "трейлинг")

    async def _move_sl(self, signal: Dict, old_sl: float, new_sl: float, move_type: str):
        """Сдвинуть SL и уведомить в Telegram."""
        symbol    = signal["symbol"]
        direction = signal["direction"]
        entry     = _f(signal["entry_price"])

        signal["stop_loss"]      = round(new_sl, 8)
        signal["trailing_active"] = True
        self._save(symbol, signal)

        d_emoji = "🟢" if direction == "long" else "🔴"
        icon    = "🔒" if move_type == "безубыток" else "🔄"
        sl_pnl  = _pnl(direction, entry, new_sl)
        old_pnl = _pnl(direction, entry, old_sl)

        lines = [
            f"{icon} <b>Стоп передвинут — {move_type.upper()}</b>",
            "",
            f"{d_emoji} <code>{symbol}</code>  {direction.upper()}",
            f"📍 Вход:     <b>${entry:,.6f}</b>",
            f"🛑 Было SL:  <b>${old_sl:,.6f}</b>  ({old_pnl:+.2f}%)",
            f"✅ Теперь SL: <b>${new_sl:,.6f}</b>  ({sl_pnl:+.2f}%)",
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
        ]

        if move_type == "безубыток":
            lines.append("\n<i>Позиция теперь в безубытке. Риск = 0.</i>")

        await self._send("\n".join(lines))
        print(f"[PositionTracker] SL moved {move_type}: {symbol} {old_sl:.6f} → {new_sl:.6f}")

    # =========================================================================
    # CLOSE EVENTS
    # =========================================================================

    async def _close_tp(self, signal: Dict, tp_idx: int,
                        tp_price: float, tp_weight: float,
                        current_price: float, is_last: bool):
        direction = signal["direction"]
        entry     = _f(signal["entry_price"])
        symbol    = signal["symbol"]
        total     = len(signal.get("take_profits", []))
        tp_num    = tp_idx + 1

        pnl_pct  = _pnl(direction, entry, tp_price)
        time_str = _time_in_trade(signal)

        taken = list(signal.get("taken_tps", []))
        taken.append(tp_idx)
        signal["taken_tps"] = taken

        if is_last:
            total_pnl = _calc_weighted_pnl(
                direction, entry, signal.get("take_profits", []), taken
            )
            signal["status"]      = "closed_tp"
            signal["close_price"] = current_price
            signal["close_time"]  = datetime.utcnow().isoformat()
            signal["pnl_pct"]     = round(total_pnl, 4)

        self._save(symbol, signal)

        d_emoji = "🔴" if direction == "short" else "🟢"
        icon    = "🏆" if is_last else "🎯"

        lines = [
            f"{icon} <b>TP{tp_num}/{total} взят!</b>",
            "",
            f"{d_emoji} <code>{symbol}</code>  {direction.upper()}",
            f"📍 Вход:      <b>${entry:,.6f}</b>",
            f"🎯 TP{tp_num}:     <b>${tp_price:,.6f}</b>  ({tp_weight:.0f}% позиции)",
            f"📊 P&L:       <b>+{pnl_pct:.2f}%</b>",
            f"⏱ В сделке:  {time_str}",
        ]

        if is_last:
            total_pnl_w = signal.get("pnl_pct", pnl_pct)
            lines += [
                "",
                "🏆 <b>Все тейки взяты!</b>",
                f"💰 Итоговый P&L: <b>+{total_pnl_w:.2f}%</b>",
            ]
            await self._record_pnl(total_pnl_w, "tp")
        else:
            remaining = total - len(taken)
            lines.append(f"⏳ До следующего TP: {remaining} шт.")

        await self._send("\n".join(lines))

    async def _close_sl(self, signal: Dict, current_price: float):
        direction = signal["direction"]
        entry     = _f(signal["entry_price"])
        sl_price  = _f(signal["stop_loss"])
        symbol    = signal["symbol"]
        was_trailing = signal.get("trailing_active", False)

        pnl_pct  = _pnl(direction, entry, current_price)
        time_str = _time_in_trade(signal)

        signal["status"]      = "closed_sl"
        signal["close_price"] = current_price
        signal["close_time"]  = datetime.utcnow().isoformat()
        signal["pnl_pct"]     = round(pnl_pct, 4)

        self._save(symbol, signal)

        d_emoji = "🔴" if direction == "short" else "🟢"
        sl_type = "трейлинг-стоп" if was_trailing else "стоп-лосс"

        lines = [
            f"🛑 <b>Стоп выбит</b>  ({sl_type})",
            "",
            f"{d_emoji} <code>{symbol}</code>  {direction.upper()}",
            f"📍 Вход:     <b>${entry:,.6f}</b>",
            f"🛑 Стоп:     <b>${sl_price:,.6f}</b>",
            f"💰 Закрыто:  <b>${current_price:,.6f}</b>",
            f"📊 P&L:      <b>{pnl_pct:.2f}%</b>",
            f"⏱ В сделке: {time_str}",
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
        ]

        if was_trailing and pnl_pct >= 0:
            lines.append("\n<i>Позиция закрыта без убытка (трейлинг-стоп).</i>")

        await self._send("\n".join(lines))
        await self._record_pnl(pnl_pct, "sl")

    async def _expire(self, signal: Dict):
        symbol   = signal.get("symbol", "?")
        entry    = signal.get("entry_price", 0)
        time_str = _time_in_trade(signal)

        signal["status"]     = "expired"
        signal["close_time"] = datetime.utcnow().isoformat()

        self._save(symbol, signal)

        d_emoji = "🔴" if signal.get("direction") == "short" else "🟢"
        await self._send(
            f"⏰ <b>Сигнал истёк (24ч)</b>\n"
            f"{d_emoji} <code>{symbol}</code>\n"
            f"📍 Вход: <b>${entry:,.6f}</b>  |  ⏱ {time_str}\n"
            "Ни TP, ни SL не были достигнуты."
        )

    # =========================================================================
    # STATS
    # =========================================================================

    async def _record_pnl(self, pnl_pct: float, close_type: str):
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            state = self.redis.get_bot_state(self.bot_type) or {}

            daily = state.get("daily_trades", {})
            day   = daily.get(today, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})

            day["trades"] += 1
            day["pnl"]     = round(day["pnl"] + pnl_pct, 4)
            if pnl_pct > 0:
                day["wins"]   += 1
            else:
                day["losses"] += 1

            daily[today] = day

            if len(daily) > 30:
                oldest = sorted(daily.keys())[0]
                del daily[oldest]

            state["daily_trades"] = daily
            self.redis.update_bot_state(self.bot_type, state)

            # Синхронизируем с AutoTrader если передан
            if self.auto_trader:
                self.auto_trader.record_trade_result(pnl_pct)

        except Exception as e:
            print(f"[PositionTracker] stats error: {e}")

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _save(self, symbol: str, signal: Dict):
        try:
            self.redis.save_signal(self.bot_type, symbol, signal)
        except Exception as e:
            print(f"[PositionTracker] redis save error: {e}")

    async def _send(self, text: str):
        try:
            await self.tg.send_message(text)
        except Exception as e:
            print(f"[PositionTracker] telegram error: {e}")

    # =========================================================================
    # STATIC: уведомление об ошибке открытия (вызывается из main.py)
    # =========================================================================

    @staticmethod
    async def notify_trade_error(telegram, symbol: str, direction: str,
                                  reason: str, score: int):
        """
        Отправить уведомление в Telegram если сделка не открылась.
        Вызывается из scan_market() в main.py при ошибке AutoTrader.

        Пример:
            await PositionTracker.notify_trade_error(
                state.telegram, symbol, "long",
                "No available margin", signal["score"]
            )
        """
        d_emoji = "🟢" if direction == "long" else "🔴"
        try:
            await telegram.send_message(
                f"⚠️ <b>Сделка не открыта</b>\n\n"
                f"{d_emoji} <code>{symbol}</code>  {direction.upper()}\n"
                f"🎯 Score: {score}%\n"
                f"❌ Причина: {reason}\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
            )
        except Exception:
            pass


# =============================================================================
# PURE HELPERS
# =============================================================================

def _f(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _sl_hit(direction: str, price: float, sl: float) -> bool:
    if direction == "short":
        return price >= sl
    return price <= sl


def _tp_hit(direction: str, price: float, tp: float) -> bool:
    if direction == "short":
        return price <= tp
    return price >= tp


def _pnl(direction: str, entry: float, close: float) -> float:
    if entry == 0:
        return 0.0
    if direction == "short":
        return (entry - close) / entry * 100
    return (close - entry) / entry * 100


def _parse_tp(raw) -> Tuple[float, float]:
    try:
        if isinstance(raw, (list, tuple)):
            return _f(raw[0]), _f(raw[1]) if len(raw) > 1 else 20.0
        if isinstance(raw, dict):
            return _f(raw.get("price", 0)), _f(raw.get("weight", 20))
    except Exception:
        pass
    return 0.0, 0.0


def _calc_weighted_pnl(direction: str, entry: float,
                       tps_raw: list, taken_indices: list) -> float:
    total = 0.0
    for i in taken_indices:
        if i < len(tps_raw):
            tp_price, tp_weight = _parse_tp(tps_raw[i])
            if tp_price > 0:
                total += _pnl(direction, entry, tp_price) * tp_weight / 100
    return round(total, 4)


def _time_in_trade(signal: Dict) -> str:
    try:
        opened = datetime.fromisoformat(signal["timestamp"])
        delta  = datetime.utcnow() - opened
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}ч {m}м" if h else f"{m}м"
    except Exception:
        return "N/A"
