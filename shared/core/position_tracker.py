"""
Position Tracker v2.2 — shared/core/position_tracker.py

ИСПРАВЛЕНИЯ v2.2:
  ✅ send_reply: правильный порядок аргументов
     Было:  self.tg.send_reply(tg_msg_id, text)     ← НЕВЕРНО
     Стало: self.tg.send_reply(text, tg_msg_id)     ← ВЕРНО
  ✅ _record_pnl: пишет в ОБА места:
     - bot_state["daily_trades"]           (для backward compat)
     - redis.update_daily_stats(date, day) (для /stats команды)
  ✅ История сделок: сохраняется в {bot_type}:history:{symbol}
     с полями opened_at, closed_at, pnl, tp_level, direction, symbol
     (нужно для /leaderswr, /daily_rep, /monthly_rep)
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
      4. Отправляет уведомления в Telegram (ответом на исходный сигнал)
      5. Записывает P&L в дневную статистику (в stats:daily:{date})
      6. Экспирирует сигналы старше MAX_AGE_HOURS
    """

    CHECK_INTERVAL = 30      # секунд между проходами
    MAX_AGE_HOURS  = 24      # сигнал экспирируется через N часов

    TRAIL_ACTIVATION = 0.015   # активируется при прибыли +1.5%
    TRAIL_DISTANCE   = 0.008   # держим SL на 0.8% от текущей цены
    BREAKEVEN_BUFFER = 0.001   # SL в безубыток = entry + 0.1%

    def __init__(self, *,
                 bot_type:       str,
                 telegram,
                 redis_client,
                 binance_client,
                 config,
                 auto_trader=None):
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

        # --- Trailing stop ---
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
        entry            = _f(signal.get("entry_price", 0))
        direction        = signal.get("direction", "short")
        current_sl       = _f(signal.get("stop_loss", 0))
        trailing_active  = signal.get("trailing_active", False)

        if not entry or not current_sl:
            return

        if direction == "long":
            profit_pct = (price - entry) / entry
            if profit_pct >= self.TRAIL_ACTIVATION:
                if not trailing_active:
                    new_sl = entry * (1 + self.BREAKEVEN_BUFFER)
                    if new_sl > current_sl:
                        await self._move_sl(signal, current_sl, new_sl, "безубыток")
                else:
                    new_sl = price * (1 - self.TRAIL_DISTANCE)
                    if new_sl > current_sl * 1.005:
                        await self._move_sl(signal, current_sl, new_sl, "трейлинг")
        else:
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
        symbol    = signal["symbol"]
        direction = signal["direction"]
        entry     = _f(signal["entry_price"])

        signal["stop_loss"]       = round(new_sl, 8)
        signal["trailing_active"] = True
        self._save(symbol, signal)

        d_emoji = "🟢" if direction == "long" else "🔴"
        icon    = "🔒" if move_type == "безубыток" else "🔄"
        sl_pnl  = _pnl(direction, entry, new_sl)
        old_pnl = _pnl(direction, entry, old_sl)

        lines = [
            f"{icon} <b>Стоп передвинут — {move_type.upper()}</b>",
            "",
            f"{d_emoji} <code>#{symbol}</code>  {direction.upper()}",
            f"📍 Вход:     <b>${entry:,.6f}</b>",
            f"🛑 Было SL:  <b>${old_sl:,.6f}</b>  ({old_pnl:+.2f}%)",
            f"✅ Теперь SL: <b>${new_sl:,.6f}</b>  ({sl_pnl:+.2f}%)",
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
        ]
        if move_type == "безубыток":
            lines.append("\n<i>Позиция теперь в безубытке. Риск = 0.</i>")

        # ✅ ИСПРАВЛЕНО: send_reply(text, reply_to_message_id) — правильный порядок
        await self._notify(signal, "\n".join(lines))
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
        tp_label  = f"TP{tp_num}"

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
            signal["tp_level"]    = tp_label   # ← для отчётов /leaderswr

        self._save(symbol, signal)

        d_emoji = "🔴" if direction == "short" else "🟢"
        icon    = "🏆" if is_last else "🎯"

        lines = [
            f"{icon} <b>{tp_label}/{total} взят!</b>",
            "",
            f"{d_emoji} <code>#{symbol}</code>  {direction.upper()}",
            f"📍 Вход:      <b>${entry:,.6f}</b>",
            f"🎯 {tp_label}:     <b>${tp_price:,.6f}</b>  ({tp_weight:.0f}% позиции)",
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
            await self._record_pnl(signal, total_pnl_w, "tp", tp_label)
        else:
            remaining = total - len(taken)
            lines.append(f"⏳ До следующего TP: {remaining} шт.")

        await self._notify(signal, "\n".join(lines))

    async def _close_sl(self, signal: Dict, current_price: float):
        direction    = signal["direction"]
        entry        = _f(signal["entry_price"])
        sl_price     = _f(signal["stop_loss"])
        symbol       = signal["symbol"]
        was_trailing = signal.get("trailing_active", False)

        pnl_pct  = _pnl(direction, entry, current_price)
        time_str = _time_in_trade(signal)

        signal["status"]      = "closed_sl"
        signal["close_price"] = current_price
        signal["close_time"]  = datetime.utcnow().isoformat()
        signal["pnl_pct"]     = round(pnl_pct, 4)
        signal["tp_level"]    = "SL"

        self._save(symbol, signal)

        d_emoji  = "🔴" if direction == "short" else "🟢"
        sl_type  = "трейлинг-стоп" if was_trailing else "стоп-лосс"
        pnl_sign = "+" if pnl_pct >= 0 else ""

        lines = [
            f"🛑 <b>Стоп выбит</b>  ({sl_type})",
            "",
            f"{d_emoji} <code>#{symbol}</code>  {direction.upper()}",
            f"📍 Вход:     <b>${entry:,.6f}</b>",
            f"🛑 Стоп:     <b>${sl_price:,.6f}</b>",
            f"💰 Закрыто:  <b>${current_price:,.6f}</b>",
            f"📊 P&L:      <b>{pnl_sign}{pnl_pct:.2f}%</b>",
            f"⏱ В сделке: {time_str}",
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
        ]
        if was_trailing and pnl_pct >= 0:
            lines.append("\n<i>Позиция закрыта без убытка (трейлинг-стоп).</i>")

        await self._notify(signal, "\n".join(lines))
        await self._record_pnl(signal, pnl_pct, "sl", "SL")

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
            f"{d_emoji} <code>#{symbol}</code>\n"
            f"📍 Вход: <b>${entry:,.6f}</b>  |  ⏱ {time_str}\n"
            "Ни TP, ни SL не были достигнуты."
        )

    # =========================================================================
    # STATS — ИСПРАВЛЕНО: пишем в stats:daily:{date}
    # =========================================================================

    async def _record_pnl(self, signal: Dict, pnl_pct: float,
                          close_type: str, tp_level: str = ""):
        """
        ✅ v2.2: пишет статистику в ДВА места:
          1. bot_state["daily_trades"]     — для backward compat
          2. redis.update_daily_stats()    — для /stats, /daily_rep, /weekly_rep
        ✅ Сохраняет запись в историю сделок для /leaderswr и отчётов.
        """
        try:
            today  = datetime.utcnow().strftime("%Y-%m-%d")
            symbol = signal.get("symbol", "?")

            # ── Место 1: bot_state["daily_trades"] ───────────────────────────
            try:
                state_data = self.redis.get_bot_state(self.bot_type) or {}
                daily      = state_data.get("daily_trades", {})
                day        = daily.get(today, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
                day["trades"] += 1
                day["pnl"]     = round(day["pnl"] + pnl_pct, 4)
                if pnl_pct > 0:
                    day["wins"]   += 1
                else:
                    day["losses"] += 1
                daily[today] = day
                if len(daily) > 30:
                    del daily[sorted(daily.keys())[0]]
                state_data["daily_trades"] = daily
                self.redis.update_bot_state(self.bot_type, state_data)
            except Exception as e:
                print(f"[PositionTracker] bot_state stats error: {e}")

            # ── Место 2: stats:daily:{date} (читается /stats командой) ───────
            try:
                day2 = self.redis.get_daily_stats(self.bot_type, today) or \
                       {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
                day2["trades"] += 1
                day2["pnl"]     = round(day2.get("pnl", 0.0) + pnl_pct, 4)
                if pnl_pct > 0:
                    day2["wins"]   = day2.get("wins", 0) + 1
                else:
                    day2["losses"] = day2.get("losses", 0) + 1
                self.redis.update_daily_stats(self.bot_type, today, day2)
            except Exception as e:
                print(f"[PositionTracker] daily_stats error: {e}")

            # ── Место 3: история сделок для отчётов ──────────────────────────
            try:
                history_record = {
                    "symbol":    symbol,
                    "direction": signal.get("direction", "?"),
                    "entry_price": signal.get("entry_price", 0),
                    "close_price": signal.get("close_price", 0),
                    "pnl":        round(pnl_pct, 4),
                    "tp_level":   tp_level,       # "TP1", "TP2", ..., "SL"
                    "close_type": close_type,     # "tp" | "sl"
                    "opened_at":  signal.get("timestamp", ""),
                    "closed_at":  signal.get("close_time", datetime.utcnow().isoformat()),
                    "score":      signal.get("score", 0),
                }
                hkey = f"{self.bot_type}:history:{symbol}"
                self.redis.client.lpush(hkey, json.dumps(history_record))
                self.redis.client.ltrim(hkey, 0, 199)   # храним до 200 записей на символ
                self.redis.client.expire(hkey, 2592000)  # TTL 30 дней
            except Exception as e:
                print(f"[PositionTracker] history save error: {e}")

            # ── AutoTrader sync ───────────────────────────────────────────────
            if self.auto_trader:
                self.auto_trader.record_trade_result(pnl_pct)

        except Exception as e:
            print(f"[PositionTracker] _record_pnl error: {e}")

    # =========================================================================
    # HELPERS
    # =========================================================================

    async def _notify(self, signal: Dict, text: str):
        """
        ✅ ИСПРАВЛЕНО: правильный порядок аргументов send_reply.
        Telegram: send_reply(text, reply_to_message_id)
        """
        tg_msg_id = signal.get("tg_msg_id")
        if tg_msg_id:
            try:
                # ✅ text первый, tg_msg_id второй (как объявлено в telegram.py)
                await self.tg.send_reply(text, reply_to_message_id=tg_msg_id)
                return
            except Exception as e:
                print(f"[PositionTracker] send_reply failed: {e}")
        # Fallback: обычное сообщение
        await self._send(text)

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
    # STATIC HELPER
    # =========================================================================

    @staticmethod
    async def notify_trade_error(telegram, symbol: str, direction: str,
                                  reason: str, score: float):
        d_emoji = "🟢" if direction == "long" else "🔴"
        try:
            await telegram.send_message(
                f"⚠️ <b>Сделка не открыта</b>\n\n"
                f"{d_emoji} <code>#{symbol}</code>  {direction.upper()}\n"
                f"🎯 Score: {score:.0f}%\n"
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
