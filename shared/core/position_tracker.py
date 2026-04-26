"""
Position Tracker v2.9 — Phase 2: Micro-Step Trailing Stop

ИЗМЕНЕНИЯ v2.9:
  🎢 Micro-Step Trailing Stop — плавное движение SL микро-шагами
     TP1 → +0.3%, TP2 → +0.8%, TP3 → +1.5% (вместо агрессивного трейлинга)
     Решает проблему выбивания при ретестах (как PUMPBTCUSDT)

ИЗМЕНЕНИЯ v2.7:
  ✅ Стоп в безубыток ПОСЛЕ TP2 (было: при +1.5% прибыли)
     Логика: после закрытия 2-го тейка (40% позиции зафиксировано)
     SL переносится на entry+0.1%. Это лучше чем немедленный BE,
     потому что даёт позиции "дышать" и не срабатывает преждевременно.
     Математика: TP2 = +3%, 40% зафиксировано → даже если SL hit → 0% потерь.
  ✅ _notify: правильный порядок аргументов send_reply
  ✅ _record_pnl: пишет в stats:daily:{date} для /stats команды
  ✅ tp_level в историю сделок для /leaderswr и отчётов
"""

import asyncio
import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 🎢 Phase 2: Micro-Step Trailing Stop
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from execution.micro_trailing_stop import get_micro_trailing


class PositionTracker:
    """
    Каждые CHECK_INTERVAL секунд:
      1. Берёт active сигналы из Redis
      2. Получает текущую цену через Binance
      3. Проверяет TP / SL / трейлинг
      4. Перемещает SL в безубыток ПОСЛЕ TP2
      5. Thread reply через tg_msg_id
      6. Записывает P&L в stats:daily:{date}
    """

    CHECK_INTERVAL = 30

    # ── Трейлинг (активируется ПОСЛЕ BE, не сразу) ───────────────────────────
    TRAIL_DISTANCE  = 0.008   # 0.8% ниже текущей цены (для LONG)
    BREAKEVEN_BUFFER = 0.001  # SL в безубыток = entry + 0.1%

    # ── Безубыток: переносим SL после этого тейка ────────────────────────────
    # ✅ FIX v5.0: BE после TP2 = 40% позиции зафиксировано, меньше шумовых SL
    BREAKEVEN_AFTER_TP = 2   # ✅ FIX: BE после TP2 (было 1) — даём позиции дышать

    def __init__(self, *, bot_type, telegram, redis_client,
                 binance_client, config, auto_trader=None):
        self.bot_type    = bot_type
        self.tg          = telegram
        self.redis       = redis_client
        self.binance     = binance_client
        self.config      = config
        self.auto_trader = auto_trader
        
        # Trail activation thresholds из config (env vars)
        # LONG: 2.5% по умолчанию, SHORT: 3% по умолчанию
        self.long_trail_threshold  = getattr(config, 'LONG_TRAIL_ACTIVATION', 0.025)
        self.short_trail_threshold = getattr(config, 'SHORT_TRAIL_ACTIVATION', 0.030)
        
        # Конвертируем строку в float если нужно
        if isinstance(self.long_trail_threshold, str):
            self.long_trail_threshold = float(self.long_trail_threshold)
        if isinstance(self.short_trail_threshold, str):
            self.short_trail_threshold = float(self.short_trail_threshold)
        
        # 🎢 Phase 2: Micro-Step Trailing Stop
        self.micro_trailing = get_micro_trailing()
        
        self._running    = False

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

    def _log(self, symbol: str, direction: str, message: str):
        """Логирование для Render."""
        d_str = "LONG" if direction == "long" else "SHORT"
        print(f"[PT][{d_str}][{symbol}] {message}")

    async def _scan_all(self):
        try:
            signals = self.redis.get_active_signals(self.bot_type)
        except Exception as e:
            print(f"[PositionTracker] redis error: {e}")
            return
        if not signals:
            return

        # ✅ v4.0: Zombie cleanup — раз в 10 итераций чистим «мёртвые» Redis позиции
        if not hasattr(self, '_scan_count'):
            self._scan_count = 0
        self._scan_count += 1
        if self._scan_count % 10 == 0:
            await self._cleanup_zombie_positions(signals)

        for sig in signals:
            if sig.get("status") != "active":
                continue
            try:
                await self._check_one(sig)
            except Exception as e:
                print(f"[PositionTracker] {sig.get('symbol')} error: {e}")
            await asyncio.sleep(0.3)

    async def _cleanup_zombie_positions(self, signals: list):
        """
        ✅ v4.0: Удаляет из Redis позиции которых нет на бирже.
        Zombie = сигнал в Redis со status=active, но BingX не знает о позиции.
        Без этого — SL никогда не срабатывает → бесконечный убыток.
        """
        if not self.auto_trader or not hasattr(self.auto_trader, 'bingx'):
            return
        
        bingx = self.auto_trader.bingx
        if not bingx:
            return

        for sig in signals:
            symbol = sig.get('symbol', '')
            direction = sig.get('direction', 'long')
            if not symbol:
                continue
            try:
                pos_side = 'LONG' if direction == 'long' else 'SHORT'
                positions = await bingx.get_positions(symbol)
                has_real_position = any(
                    abs(p.size) > 0 and p.position_side == pos_side
                    for p in positions
                )
                if not has_real_position:
                    # Позиция есть в Redis, нет на бирже — это zombie
                    entry = sig.get('entry_price', 0)
                    current_price = sig.get('last_price', entry)
                    
                    # Вычисляем P&L на момент закрытия
                    if entry and entry > 0:
                        pnl = _pnl(direction, float(entry), float(current_price))
                    else:
                        pnl = 0.0
                    
                    sig['status'] = 'closed_zombie'
                    sig['close_price'] = current_price
                    sig['close_time'] = datetime.utcnow().isoformat()
                    sig['pnl_pct'] = round(pnl, 4)
                    sig['tp_level'] = 'ZOMBIE'
                    self._save(symbol, sig)
                    self.micro_trailing.remove(symbol)
                    
                    print(f"🧟 [ZOMBIE-CLEANUP] {symbol} {direction}: позиция в Redis но не на бирже → закрываем. P&L={pnl:.2f}%")
                    
                    # Уведомление в Telegram
                    d_emoji = '🟢' if direction == 'long' else '🔴'
                    await self._notify(sig, (
                        f"🧟 <b>Zombie позиция закрыта</b>\n\n"
                        f"{d_emoji} <b>#{symbol}</b> {direction.upper()}\n"
                        f"📍 Вход: <b>${float(entry):,.6f}</b>\n"
                        f"📊 P&L: <b>{pnl:+.2f}%</b>\n"
                        f"<i>Позиция не найдена на бирже — очищаем Redis</i>"
                    ))
            except Exception as e:
                print(f"⚠️ [ZOMBIE-CLEANUP] {symbol}: {e}")

    async def _check_one(self, signal: Dict):
        symbol    = signal.get("symbol", "")
        entry     = _f(signal.get("entry_price", 0))
        sl        = _f(signal.get("stop_loss", 0))
        direction = signal.get("direction", "long")
        opened_at = signal.get("timestamp", "")
        tps_raw   = signal.get("take_profits", [])
        taken     = list(signal.get("taken_tps", []))

        if not symbol or not entry:
            return

        # 🎢 Phase 2: Инициализация Micro-Step Trailing при первом обнаружении позиции
        trailing_state = self.micro_trailing.get_state(symbol)
        if trailing_state is None and len(taken) == 0:
            # Новая позиция — инициализируем трейлинг
            self.micro_trailing.initialize(
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                initial_sl=sl
            )
            print(f"🎢 [MicroTrail][{symbol}] Initialized: entry={entry:.6f}, SL={sl:.6f}")

        # Экспирация 24ч
        if opened_at:
            try:
                age = datetime.utcnow() - datetime.fromisoformat(opened_at)
                if age > timedelta(hours=24):
                    await self._expire(signal)
                    return
            except Exception:
                pass

        md = await self.binance.get_complete_market_data(symbol)
        if not md:
            return
        price = _f(md.price)

        # 🆕 Обновляем unrealized P&L в Redis position для дашборда
        try:
            unrealized_pnl = _pnl(direction, entry, price)
            self._update_position_pnl(symbol, price, unrealized_pnl)
        except Exception as e:
            print(f"[PT][{symbol}] unrealized_pnl update error: {e}")

        # ── ДЕТАЛЬНЫЙ ЛОГ RENDER ─────────────────────────────────────────────
        be_done      = signal.get("be_done", False)
        trail_active = signal.get("trailing_active", False)
        max_tp_log   = signal.get("max_tp_reached", "")
        mode_str     = "TRAIL" if trail_active else ("BE" if be_done else "ACTIVE")
        d_str        = "LONG" if direction == "long" else "SHORT"
        sl_pct_now   = abs((price - sl) / entry * 100) if entry else 0
        print(f"[PT][{d_str}][{symbol}] 🔍 цена={price:.6f} вход={entry:.6f} "
              f"SL={sl:.6f}({sl_pct_now:.2f}%) режим={mode_str} "
              f"TP={len(taken)}/{len(tps_raw)}"
              + (f" макс={max_tp_log}" if max_tp_log else ""))
        # ─────────────────────────────────────────────────────────────────────

        # Трейлинг (только если уже в безубытке)
        await self._check_trailing(signal, price)

        # Обновляем SL после трейлинга
        sl = _f(signal.get("stop_loss", 0))

        # SL hit
        if sl and _sl_hit(direction, price, sl):
            print(f"[PT][{d_str}][{symbol}] 🛑 SL HIT! "
                  f"цена={price:.6f} sl={sl:.6f} "
                  f"TP взято={len(taken)} макс={max_tp_log or '—'}")
            await self._close_sl(signal, price)
            return

        # TP hit
        for i, tp_raw in enumerate(tps_raw):
            if i in taken:
                continue
            tp_price, tp_weight = _parse_tp(tp_raw)
            if tp_price <= 0:
                continue
            if _tp_hit(direction, price, tp_price):
                is_last = (len(taken) + 1 >= len(tps_raw))
                pnl_now = _pnl(direction, entry, tp_price)
                print(f"[PT][{d_str}][{symbol}] 🎯 TP{i+1} HIT! "
                      f"цена={price:.6f} tp={tp_price:.6f} "
                      f"P&L={pnl_now:+.2f}% вес={tp_weight:.0f}% is_last={is_last}")
                await self._close_tp(signal, i, tp_price, tp_weight, price, is_last)
                break

    # =========================================================================
    # TRAILING — активируется ТОЛЬКО после BE (после TP2)
    # =========================================================================

    async def _check_trailing(self, signal: Dict, price: float):
        symbol          = signal.get("symbol", "")   # ✅ FIX: NameError fix
        entry           = _f(signal.get("entry_price", 0))
        direction       = signal.get("direction", "long")
        current_sl      = _f(signal.get("stop_loss", 0))
        trailing_active = signal.get("trailing_active", False)
        be_done         = signal.get("be_done", False)   # безубыток уже выставлен
        taken_tps       = signal.get("taken_tps", [])

        if not entry or not current_sl:
            return

        # ✅ FIX: Проверяем be_done — если уже в безубытке, не обновляем повторно
        if be_done:
            trailing_active = True  # активируем трейлинг если BE уже был

        if direction == "long":
            profit_pct = (price - entry) / entry

            # Безубыток выставляем только после TP2 (BREAKEVEN_AFTER_TP)
            taken_count = len(taken_tps)

            if not be_done and taken_count >= self.BREAKEVEN_AFTER_TP:
                new_sl = entry * (1 + self.BREAKEVEN_BUFFER)
                # ✅ FIX: Минимальный порог 0.05% для изменения SL (избегаем микро-движений)
                min_move_threshold = current_sl * 0.0005  # 0.05%
                
                # Если SL уже в безубытке (в пределах порога) — просто помечаем флагом
                if abs(current_sl - new_sl) <= min_move_threshold:
                    signal["be_done"] = True
                    signal["trailing_active"] = True
                    self._save(symbol, signal)
                    return
                
                if new_sl > current_sl + min_move_threshold:
                    print(f"[PT][LONG][{symbol}] 🔒 BE АКТИВИРОВАН | "
                          f"SL: {current_sl:.6f} → {new_sl:.6f} | "
                          f"взято TP={len(taken_tps)}")
                    await self._move_sl(signal, current_sl, new_sl, "безубыток")
                    signal["be_done"]         = True
                    signal["trailing_active"] = True
                    return

            # Трейлинг только после BE
            # Trail activation threshold из config (по умолчанию 2.5%)
            if trailing_active and profit_pct > self.long_trail_threshold:
                    # ✅ FIX: Определяем new_sl для LONG (трейлинг вверх)
                    new_sl = price * (1 - self.TRAIL_DISTANCE)
                    if new_sl > current_sl * 1.003:  # двигаем только если значительно выше
                        self._log(symbol, direction,
                                  f"📈 TRAIL SL MOVE | "
                                  f"{current_sl:.6f} → {new_sl:.6f} | "
                                  f"цена={price:.6f} profit={profit_pct*100:+.2f}%")
                        await self._move_sl(signal, current_sl, new_sl, "трейлинг")

        else:  # SHORT
            profit_pct = (entry - price) / entry
            taken_count = len(taken_tps)

            if not be_done and taken_count >= self.BREAKEVEN_AFTER_TP:
                new_sl = entry * (1 - self.BREAKEVEN_BUFFER)
                # ✅ FIX: Минимальный порог 0.05% для изменения SL
                min_move_threshold = current_sl * 0.0005  # 0.05%
                
                # Если SL уже в безубытке (в пределах порога) — просто помечаем флагом
                if abs(current_sl - new_sl) <= min_move_threshold:
                    signal["be_done"] = True
                    signal["trailing_active"] = True
                    self._save(symbol, signal)
                    return
                
                if new_sl < current_sl - min_move_threshold:
                    print(f"[PT][SHORT][{symbol}] 🔒 BE АКТИВИРОВАН | "
                          f"SL: {current_sl:.6f} → {new_sl:.6f} | "
                          f"взято TP={len(taken_tps)}")
                    await self._move_sl(signal, current_sl, new_sl, "безубыток")
                    signal["be_done"]         = True
                    signal["trailing_active"] = True
                    return

            # Трейлинг только после BE — пороги из config
            trail_threshold = self.short_trail_threshold if direction == "short" else self.long_trail_threshold
            if trailing_active and profit_pct > trail_threshold:
                    # ✅ FIX: Определяем new_sl для SHORT (трейлинг вниз)
                    new_sl = price * (1 + self.TRAIL_DISTANCE)
                    if new_sl < current_sl * 0.997:  # двигаем только если значительно ниже
                        self._log(symbol, direction,
                                  f"📈 TRAIL SL MOVE | "
                                  f"{current_sl:.6f} → {new_sl:.6f} | "
                                  f"цена={price:.6f} profit={profit_pct*100:+.2f}%")
                        await self._move_sl(signal, current_sl, new_sl, "трейлинг")

    async def _move_sl(self, signal: Dict, old_sl: float, new_sl: float, move_type: str):
        """
        ✅ v2.5 FIX КРИТИЧЕСКИЙ:
        Было: только Redis + Telegram — биржа не знала о новом SL!
        Стало: 1) Обновляем SL на BingX (cancel old → place new STOP_MARKET)
               2) Обновляем Redis
               3) Уведомляем в Telegram
        """
        symbol    = signal["symbol"]
        direction = signal["direction"]
        entry     = _f(signal["entry_price"])
        position_side = "LONG" if direction == "long" else "SHORT"

        # ✅ ШАГ 1: Обновляем SL на бирже (если auto_trader доступен)
        exchange_updated = False
        if self.auto_trader and self.auto_trader.bingx:
            try:
                # ✅ FIX: Правильное форматирование символа как в aegis-bots
                # MAGMAUSDT → MAGMA-USDT
                if "-" not in symbol and symbol.endswith("USDT"):
                    bingx_symbol = symbol[:-4] + "-USDT"
                else:
                    bingx_symbol = symbol
                print(f"🔍 [PT] _move_sl: symbol={symbol}, bingx_symbol={bingx_symbol}, position_side={position_side}")
                # ✅ RETRY: 3 попытки с паузой 1 секунда (v2.7)
                for attempt in range(3):
                    print(f"🔍 [PT] Attempt {attempt + 1}/3")
                    # ✅ FIX: Правильный порядок форматов — сначала с дефисом
                    for sym_fmt in [bingx_symbol, symbol]:
                        print(f"🔍 [PT] Trying sym_fmt={sym_fmt}")
                        ok = await self.auto_trader.bingx.update_stop_loss(
                            sym_fmt, position_side, new_sl, direction
                        )
                        print(f"🔍 [PT] update_stop_loss returned: {ok}")
                        if ok:
                            exchange_updated = True
                            print(f"✅ [PT] SL updated successfully with sym_fmt={sym_fmt}")
                            break
                    if exchange_updated:
                        break
                    if attempt < 2:  # Пауза между попытками (не после последней)
                        print(f"🔍 [PT] Waiting 1s before next attempt...")
                        await asyncio.sleep(1)
                if not exchange_updated:
                    print(f"⚠️  [PT] SL на бирже не обновлён для {symbol} после 3 попыток — только Redis")
            except Exception as e:
                print(f"⚠️  [PT] update_stop_loss error {symbol}: {e}")
                import traceback
                traceback.print_exc()

        # ✅ ШАГ 2: Обновляем Redis (всегда)
        signal["stop_loss"] = round(new_sl, 8)
        self._save(symbol, signal)

        d_emoji = "🟢" if direction == "long" else "🔴"
        icon    = "🔒" if move_type == "безубыток" else "🔄"
        sl_pnl  = _pnl(direction, entry, new_sl)
        old_pnl = _pnl(direction, entry, old_sl)
        taken   = len(signal.get("taken_tps", []))
        ex_icon = "✅ Биржа" if exchange_updated else "⚠️ Только Redis"

        lines = [
            f"{icon} <b>Стоп передвинут — {move_type.upper()}</b>",
            "",
            f"{d_emoji} <b>#{symbol}</b>  {direction.upper()}",
            f"📍 Вход:      <b>${entry:,.6f}</b>",
            f"🛑 Было SL:   <b>${old_sl:,.6f}</b>  ({old_pnl:+.2f}%)",
            f"✅ Теперь SL: <b>${new_sl:,.6f}</b>  ({sl_pnl:+.2f}%)",
            f"📊 TP взято: {taken}",
            f"🔄 Обновление: {ex_icon}",
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
        ]
        if move_type == "безубыток":
            lines.append(f"\n<i>Сработало после TP{self.BREAKEVEN_AFTER_TP} — позиция в безубытке.</i>")

        await self._notify(signal, "\n".join(lines))
        print(f"[PositionTracker] SL {move_type}: {symbol} {old_sl:.6f} → {new_sl:.6f} | Биржа: {exchange_updated}")

    # =========================================================================
    # CLOSE TP
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
        # ✅ FIX: define d_emoji early (was used before definition → crash)
        d_emoji   = "🟢" if direction == "long" else "🔴"

        pnl_pct  = _pnl(direction, entry, tp_price)
        time_str = _time_in_trade(signal)

        taken = list(signal.get("taken_tps", []))
        taken.append(tp_idx)
        signal["taken_tps"] = taken

        if is_last:
            total_pnl = _calc_weighted_pnl(direction, entry, signal.get("take_profits", []), taken)
            signal["status"]      = "closed_tp"
            signal["close_price"] = current_price
            signal["close_time"]  = datetime.utcnow().isoformat()
            signal["pnl_pct"]     = round(total_pnl, 4)
            signal["tp_level"]    = tp_label
            
            # 🎢 Phase 2: Очистка Micro-Step Trailing при закрытии всех TP
            self.micro_trailing.remove(symbol)

        # ✅ v2.5: Трекаем максимальный взятый TP уровень
        signal["max_tp_reached"] = tp_label
        signal["tp_taken_count"] = len(taken)
        
        # 🎢 Phase 2: Micro-Step Trailing — обновляем SL после TP
        if not is_last:  # Не обновляем если последний TP (позиция закрывается)
            new_sl_micro = self.micro_trailing.on_tp_taken(
                symbol=symbol,
                tp_level=tp_num,
                current_price=current_price
            )
            if new_sl_micro:
                # Обновляем SL в сигнале и на бирже
                old_sl = signal.get("stop_loss", 0)
                signal["stop_loss"] = new_sl_micro
                signal["trailing_active"] = True
                self._save(symbol, signal)
                
                # Перемещаем SL на бирже
                await self._move_sl(signal, old_sl, new_sl_micro, "трейлинг")
                
                # 🎢 Phase 3: Красивое уведомление о Micro-Step Trailing
                summary = self.micro_trailing.get_summary(symbol)
                if summary:
                    trail_lines = [
                        f"🎢 <b>Стоп передвинут — Micro-Step #{summary['steps_taken']}</b>",
                        "",
                        f"{d_emoji} <b>#{symbol}</b>  {direction.upper()}",
                        f"📍 Вход:       <b>${entry:,.6f}</b>",
                        f"🛑 Было SL:    <b>${old_sl:,.6f}</b>",
                        f"✅ Теперь SL:  <b>${new_sl_micro:,.6f}</b>",
                        f"📊 TP взято:   {len(taken)}/{total}",
                        f"🎯 Защита:    <b>+{summary['total_moved_pct']:.2f}%</b> от входа",
                    ]
                    await self._notify(signal, "\n".join(trail_lines))
        
        self._save(symbol, signal)

        d_emoji = "🔴" if direction == "short" else "🟢"
        icon    = "🏆" if is_last else "🎯"

        lines = [
            f"{icon} <b>{tp_label}/{total} взят!</b>",
            "",
            f"{d_emoji} <b>#{symbol}</b>  {direction.upper()}",
            f"📍 Вход:       <b>${entry:,.6f}</b>",
            f"🎯 {tp_label}:      <b>${tp_price:,.6f}</b>  ({tp_weight:.0f}% позиции)",
            f"📊 P&L:        <b>+{pnl_pct:.2f}%</b>",
            f"⏱ В сделке:   {time_str}",
        ]

        if is_last:
            total_pnl_w = signal.get("pnl_pct", pnl_pct)
            lines += ["", "🏆 <b>Все тейки взяты!</b>",
                      f"💰 Итоговый P&L: <b>+{total_pnl_w:.2f}%</b>"]
            await self._record_pnl(signal, total_pnl_w, "tp", tp_label)
        else:
            remaining = total - len(taken)
            lines.append(f"⏳ До следующего TP: {remaining} шт.")
            # Уведомление о безубытке который будет после TP2
            if tp_num == self.BREAKEVEN_AFTER_TP:
                lines.append(f"\n🔒 <i>SL переносится в безубыток после TP{self.BREAKEVEN_AFTER_TP}</i>")

        await self._notify(signal, "\n".join(lines))

    # =========================================================================
    # CLOSE SL
    # =========================================================================

    async def _close_sl(self, signal: Dict, current_price: float):
        """
        ✅ v2.4 FIX: Итоговый P&L учитывает уже взятые TP.
        Было: pnl = _pnl(entry, price) = всегда -1.5% (игнорировал TP1..5).
        Стало: tp_profit + sl_loss × remaining_weight.
        Пример: TP1=+5% (25%) взят → SL=-1.5%(75%) → net=+0.125% (WIN!)
        """
        direction    = signal["direction"]
        entry        = _f(signal["entry_price"])
        sl_price     = _f(signal["stop_loss"])
        symbol       = signal["symbol"]
        was_trailing = signal.get("trailing_active", False)
        be_done      = signal.get("be_done", False)
        taken        = list(signal.get("taken_tps", []))
        tps_raw      = signal.get("take_profits", [])

        # P&L от уже взятых TP
        tp_pnl = _calc_weighted_pnl(direction, entry, tps_raw, taken) if taken else 0.0

        # Вес оставшейся позиции
        taken_weight = sum(_parse_tp(tps_raw[i])[1] for i in taken if i < len(tps_raw))
        remaining_w  = max(0.0, 100.0 - taken_weight) / 100.0

        # Итоговый P&L = прибыль TP + убыток по стопу на остаток
        raw_sl_pnl = _pnl(direction, entry, current_price)
        total_pnl  = round(tp_pnl + raw_sl_pnl * remaining_w, 4)

        time_str = _time_in_trade(signal)

        # ✅ Определяем тип закрытия для статистики
        if was_trailing:
            tp_level_label = "SL-TRAIL"
        elif be_done and total_pnl >= -0.1:
            tp_level_label = "BE"
        else:
            tp_level_label = "SL"

        # 🎢 Phase 3: Информация о Micro-Step при закрытии
        trail_summary = self.micro_trailing.get_summary(symbol)
        micro_info = ""
        if trail_summary and trail_summary['steps_taken'] > 0:
            micro_info = (f"\n🎢 Micro-Step: {trail_summary['steps_taken']} шагов, "
                         f"защита +{trail_summary['total_moved_pct']:.2f}%")
        
        # Очистка Micro-Step Trailing
        self.micro_trailing.remove(symbol)
        
        signal["status"]      = "closed_sl"
        signal["close_price"] = current_price
        signal["close_time"]  = datetime.utcnow().isoformat()
        signal["pnl_pct"]     = total_pnl
        # ✅ v2.5: Показываем "SL(после TP1)" если был взят TP
        max_tp_hit = signal.get("max_tp_reached", "")
        if max_tp_hit:
            sl_type_label = f"SL(после {max_tp_hit})"
        elif be_done:
            sl_type_label = "BE"
        elif was_trailing:
            sl_type_label = "SL-TRAIL"
        else:
            sl_type_label = "SL"
        signal["tp_level"]    = sl_type_label
        self._save(symbol, signal)
        
        # ✅ v5.0: Устанавливаем cooldown после SL (2-4 часа = 7200-14400 сек)
        try:
            import random
            cooldown_ttl = random.randint(7200, 14400)  # 2-4 часа
            cooldown_key = f"sl_cooldown:{symbol}"
            self.redis.set(cooldown_key, "1", ex=cooldown_ttl)
            print(f"⏸️ [COOLDOWN] {symbol}: SL cooldown set for {cooldown_ttl//3600}h")
        except Exception as e:
            print(f"⚠️ [COOLDOWN] {symbol}: Failed to set cooldown: {e}")

        d_emoji  = "🔴" if direction == "short" else "🟢"
        sl_type  = ("трейлинг-стоп" if was_trailing else
                    "безубыток"     if be_done      else "стоп-лосс")
        pnl_sign = "+" if total_pnl >= 0 else ""

        lines = [
            f"🛑 <b>Стоп выбит</b>  ({sl_type})",
            "",
            f"{d_emoji} <b>#{symbol}</b>  {direction.upper()}",
            f"📍 Вход:      <b>${entry:,.6f}</b>",
            f"🛑 Стоп:      <b>${sl_price:,.6f}</b>",
            f"💰 Закрыто:   <b>${current_price:,.6f}</b>",
        ]
        if taken:
            lines.append(f"🎯 TP взято:  {len(taken)} шт.  (вклад {tp_pnl:+.2f}%)")
        lines += [
            f"📊 Итог P&L:  <b>{pnl_sign}{total_pnl:.2f}%</b>",
            f"⏱ В сделке:  {time_str}",
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
        ]
        if be_done and total_pnl >= -0.1:
            lines.append("\n<i>Закрыто в безубытке. Риск = 0.</i>")

        await self._notify(signal, "\n".join(lines))
        # ✅ FIX: используем sl_type_label вместо tp_level_label (учитывает max_tp_hit)
        await self._record_pnl(signal, total_pnl, "sl", sl_type_label)

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
            f"{d_emoji} <b>#{symbol}</b>\n"
            f"📍 Вход: <b>${entry:,.6f}</b>  |  ⏱ {time_str}"
        )

    # =========================================================================
    # STATS
    # =========================================================================

    async def _record_pnl(self, signal: Dict, pnl_pct: float,
                          close_type: str, tp_level: str = ""):
        try:
            today  = datetime.utcnow().strftime("%Y-%m-%d")
            symbol = signal.get("symbol", "?")

            # Bot state (backward compat)
            try:
                state_data = self.redis.get_bot_state(self.bot_type) or {}
                daily = state_data.get("daily_trades", {})
                day   = daily.get(today, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
                day["trades"] += 1
                day["pnl"]     = round(day["pnl"] + pnl_pct, 4)
                if pnl_pct > 0: day["wins"]   += 1
                else:           day["losses"] += 1
                daily[today] = day
                if len(daily) > 30:
                    del daily[sorted(daily.keys())[0]]
                state_data["daily_trades"] = daily
                self.redis.update_bot_state(self.bot_type, state_data)
            except Exception as e:
                print(f"[PT] bot_state stats: {e}")

            # stats:daily:{date} (для /stats команды)
            try:
                day2 = self.redis.get_daily_stats(self.bot_type, today) or \
                       {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
                day2["trades"] += 1
                day2["pnl"]     = round(day2.get("pnl", 0.0) + pnl_pct, 4)
                if pnl_pct > 0: day2["wins"]   = day2.get("wins", 0) + 1
                else:           day2["losses"] = day2.get("losses", 0) + 1
                self.redis.update_daily_stats(self.bot_type, today, day2)
            except Exception as e:
                print(f"[PT] daily_stats: {e}")

            # 🆕 ПОЛНАЯ история для /alltradestat
            try:
                opened_at = signal.get("timestamp", "")
                closed_at = signal.get("close_time", datetime.utcnow().isoformat())
                hold_secs = 0
                try:
                    t0 = datetime.fromisoformat(opened_at)
                    t1 = datetime.fromisoformat(closed_at)
                    hold_secs = int((t1 - t0).total_seconds())
                except Exception:
                    pass

                entry = signal.get("entry_price", 0)
                close_p = signal.get("close_price", 0)
                sl_price = signal.get("stop_loss", 0)
                tps = signal.get("take_profits", [])
                taken = signal.get("taken_tps", [])

                record = {
                    # Базовые
                    "symbol":       symbol,
                    "direction":    signal.get("direction", "?"),
                    "entry_price":  entry,
                    "close_price":  close_p,
                    "stop_loss":    sl_price,
                    "pnl":          round(pnl_pct, 4),
                    "tp_level":     tp_level,
                    "close_type":   close_type,
                    "opened_at":    opened_at,
                    "closed_at":    closed_at,
                    "hold_minutes": hold_secs // 60,
                    # Debug
                    "_debug_be_done": signal.get("be_done", False),
                    "_debug_trailing": signal.get("trailing_active", False),
                    # Скоринг и паттерны
                    "score":        signal.get("score", 0),
                    "pattern":      signal.get("pattern", ""),
                    "leverage":     signal.get("leverage", "?"),
                    "risk":         signal.get("risk", "?"),
                    # Рыночные данные на момент входа
                    "rsi_1h":       signal.get("rsi_1h", 0),
                    "funding_rate": signal.get("funding_rate", 0),
                    "oi_change":    signal.get("oi_change", 0),
                    "long_short_ratio": signal.get("long_short_ratio", 0),
                    "volume_spike": signal.get("volume_spike_ratio", 0),
                    "atr_pct":      signal.get("atr_14_pct", 0),
                    # SMC данные
                    "smc_ob":       signal.get("smc_data", {}).get("has_ob", False),
                    "smc_fvg":      signal.get("smc_data", {}).get("has_fvg", False),
                    "smc_bonus":    signal.get("smc_data", {}).get("score_bonus", 0),
                    # TP детали
                    "tp_count":     len(tps),
                    "tp_taken":     len(taken),
                    "tp_prices":    [t[0] if isinstance(t, (list,tuple)) else t.get("price",0) for t in tps[:6]],
                    # Причины сигнала
                    "reasons":      signal.get("reasons", [])[:8],
                    "realtime_factors": signal.get("realtime_factors", [])[:5],
                }
                # Пишем в общую историю бота
                hkey = f"{self.bot_type}:history:{symbol}"
                self.redis.client.lpush(hkey, json.dumps(record))
                self.redis.client.ltrim(hkey, 0, 199)
                self.redis.client.expire(hkey, 2592000)
                # 🆕 Также пишем в глобальный лог для /alltradestat (все сделки)
                all_key = f"{self.bot_type}:all_trades"
                self.redis.client.lpush(all_key, json.dumps(record))
                self.redis.client.ltrim(all_key, 0, 9999)   # 10k сделок
                self.redis.client.expire(all_key, 7776000)  # 90 дней
                print(f"[PT][RECORD][{symbol}] tp_level={tp_level} pnl={pnl_pct:.2f}% close_type={close_type}")
            except Exception as e:
                print(f"[PT] history: {e}")

            if self.auto_trader:
                self.auto_trader.record_trade_result(pnl_pct)

        except Exception as e:
            print(f"[PT] _record_pnl: {e}")

    # =========================================================================
    # HELPERS
    # =========================================================================

    async def _notify(self, signal: Dict, text: str):
        """Thread reply на исходный сигнал, fallback — обычное сообщение."""
        tg_msg_id = signal.get("tg_msg_id")
        if tg_msg_id:
            try:
                await self.tg.send_reply(text, reply_to_message_id=tg_msg_id)
                return
            except Exception as e:
                print(f"[PT] send_reply failed: {e}")
        await self._send(text)

    def _save(self, symbol: str, signal: Dict):
        try:
            self.redis.save_signal(self.bot_type, symbol, signal)
        except Exception as e:
            print(f"[PT] redis save: {e}")

    def _update_position_pnl(self, symbol: str, current_price: float, unrealized_pnl: float):
        """Обновляем текущий P&L и цену в позиции для дашборда"""
        try:
            # Получаем текущую позицию
            pos = self.redis.get_position(self.bot_type, symbol)
            if pos:
                pos["current_price"] = current_price
                pos["unrealized_pnl"] = round(unrealized_pnl, 2)
                pos["last_updated"] = datetime.utcnow().isoformat()
                self.redis.save_position(self.bot_type, symbol, pos)
        except Exception as e:
            print(f"[PT] _update_position_pnl error: {e}")

    async def _send(self, text: str):
        try:
            await self.tg.send_message(text)
        except Exception as e:
            print(f"[PT] telegram: {e}")


# ============================================================================
# PURE HELPERS
# ============================================================================

def _f(v) -> float:
    try:   return float(v)
    except: return 0.0

def _sl_hit(direction: str, price: float, sl: float) -> bool:
    return price >= sl if direction == "short" else price <= sl

def _tp_hit(direction: str, price: float, tp: float) -> bool:
    return price <= tp if direction == "short" else price >= tp

def _pnl(direction: str, entry: float, close: float) -> float:
    if entry == 0: return 0.0
    return (entry - close) / entry * 100 if direction == "short" else (close - entry) / entry * 100

def _parse_tp(raw) -> Tuple[float, float]:
    try:
        if isinstance(raw, (list, tuple)):
            return _f(raw[0]), _f(raw[1]) if len(raw) > 1 else 20.0
        if isinstance(raw, dict):
            return _f(raw.get("price", 0)), _f(raw.get("weight", 20))
    except Exception:
        pass
    return 0.0, 0.0

def _calc_weighted_pnl(direction: str, entry: float, tps_raw: list, taken: list) -> float:
    total = 0.0
    for i in taken:
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
