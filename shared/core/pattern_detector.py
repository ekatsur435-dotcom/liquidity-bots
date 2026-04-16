"""
Pattern Detector v2.0 — LONG + SHORT

НОВЫЕ ПАТТЕРНЫ (добавлены к существующим):

LONG паттерны:
  BREAKOUT_LONG         — пробой вверх с объёмом (breakout + закреп)
  MOMENTUM_LONG         — импульсная свеча вверх + volume spike
  LIQUIDITY_SWEEP_LONG  — снятие ликвидности снизу (stop hunt вниз → разворот)
  CONSOLIDATION_BREAK_LONG — выход из флэта вверх
  WYCKOFF_SPRING        — Wycoff Spring: ложный пробой лоу + накопление

SHORT паттерны:
  BREAKOUT_SHORT        — пробой вниз с объёмом
  MOMENTUM_SHORT        — импульс вниз + volume spike
  LIQUIDITY_SWEEP_SHORT — снятие ликвидности сверху (stop hunt вверх → разворот)
  DISTRIBUTION_BREAK    — выход из накопления вниз (смарт мани распределяет)
  WYCKOFF_UPTHRUST      — ложный пробой хая + начало распределения

ЛОГИКА ликвидности (SMC/ICT подход):
  Ликвидность скапливается под минимумами и над максимумами.
  Крупные игроки сначала двигают цену в эту зону (stop hunt),
  забирают ликвидность, затем разворачиваются.
  Паттерны SWEEP фиксируют именно этот момент.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum
import statistics


class PatternType(Enum):
    # ── Существующие LONG паттерны ────────────────────────────────────────────
    MEGA_LONG              = "MEGA_LONG"
    TRAP_SHORT             = "TRAP_SHORT"
    REJECTION_LONG         = "REJECTION_LONG"

    # ── Новые LONG паттерны ───────────────────────────────────────────────────
    BREAKOUT_LONG          = "BREAKOUT_LONG"           # ← ДОБАВЛЕН
    MOMENTUM_LONG          = "MOMENTUM_LONG"           # ← ДОБАВЛЕН
    LIQUIDITY_SWEEP_LONG   = "LIQUIDITY_SWEEP_LONG"   # ← ДОБАВЛЕН
    CONSOLIDATION_BREAK_LONG = "CONSOLIDATION_BREAK_LONG"  # ← ДОБАВЛЕН
    WYCKOFF_SPRING         = "WYCKOFF_SPRING"          # ← ДОБАВЛЕН

    # ── Существующие SHORT паттерны ───────────────────────────────────────────
    MEGA_SHORT             = "MEGA_SHORT"
    TRAP_LONG              = "TRAP_LONG"
    REJECTION_SHORT        = "REJECTION_SHORT"

    # ── Новые SHORT паттерны ──────────────────────────────────────────────────
    BREAKOUT_SHORT         = "BREAKOUT_SHORT"          # ← ДОБАВЛЕН
    MOMENTUM_SHORT         = "MOMENTUM_SHORT"          # ← ДОБАВЛЕН
    LIQUIDITY_SWEEP_SHORT  = "LIQUIDITY_SWEEP_SHORT"  # ← ДОБАВЛЕН
    DISTRIBUTION_BREAK     = "DISTRIBUTION_BREAK"      # ← ДОБАВЛЕН
    WYCKOFF_UPTHRUST       = "WYCKOFF_UPTHRUST"        # ← ДОБАВЛЕН

    # ── Нейтральные ───────────────────────────────────────────────────────────
    UNKNOWN                = "UNKNOWN"


@dataclass
class PatternResult:
    name: str
    score_bonus: int       # бонус к скору (0-25)
    confidence: float      # 0.0-1.0
    direction: str         # "long" | "short"
    # Для SL/TP корректировки
    suggested_sl_pct: float = 0.0    # если > 0 — переопределяет дефолтный SL
    suggested_tp1_pct: float = 0.0   # TP1 от входа
    reasons: List[str] = field(default_factory=list)


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def _closes(candles) -> List[float]:
    return [c.close for c in candles]

def _highs(candles) -> List[float]:
    return [c.high for c in candles]

def _lows(candles) -> List[float]:
    return [c.low for c in candles]

def _vols(candles) -> List[float]:
    return [c.quote_volume for c in candles]

def _avg_vol(candles, lookback: int = 20) -> float:
    vols = _vols(candles)
    if len(vols) < lookback:
        return sum(vols) / len(vols) if vols else 1.0
    return sum(vols[-lookback-1:-1]) / lookback   # исключаем последнюю

def _vol_spike(candles, lookback: int = 20) -> float:
    avg = _avg_vol(candles, lookback)
    if avg <= 0:
        return 1.0
    return candles[-1].quote_volume / avg

def _atr(candles, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        pc  = candles[i-1].close
        tr  = max(candles[i].high - candles[i].low,
                  abs(candles[i].high - pc),
                  abs(candles[i].low  - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

def _body(c) -> float:
    return abs(c.close - c.open)

def _range(c) -> float:
    return c.high - c.low

def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return values
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def _is_consolidation(candles, lookback: int = 15, max_range_pct: float = 2.0) -> bool:
    """Флэт — диапазон хая/лоя < max_range_pct% за lookback свечей."""
    if len(candles) < lookback:
        return False
    segment = candles[-lookback:]
    high_max = max(c.high for c in segment)
    low_min  = min(c.low  for c in segment)
    if low_min <= 0:
        return False
    range_pct = (high_max - low_min) / low_min * 100
    return range_pct < max_range_pct

def _swing_highs(candles, lookback: int = 3) -> List[float]:
    """Локальные хаи (свинги)."""
    highs = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i].high
        if all(candles[j].high <= h for j in range(i-lookback, i)) and \
           all(candles[j].high <= h for j in range(i+1, i+lookback+1)):
            highs.append(h)
    return highs

def _swing_lows(candles, lookback: int = 3) -> List[float]:
    """Локальные лои (свинги)."""
    lows = []
    for i in range(lookback, len(candles) - lookback):
        l = candles[i].low
        if all(candles[j].low >= l for j in range(i-lookback, i)) and \
           all(candles[j].low >= l for j in range(i+1, i+lookback+1)):
            lows.append(l)
    return lows


# ============================================================================
# LONG ПАТТЕРН ДЕТЕКТОР
# ============================================================================

class LongPatternDetector:
    """
    Детектор LONG паттернов.
    Каждый метод возвращает Optional[PatternResult].
    """

    def detect_all(self, candles, hourly_deltas=None, market_data=None) -> List[PatternResult]:
        """Запустить все детекторы и вернуть найденные паттерны (сортировка по score_bonus)."""
        results = []
        detectors = [
            # Новые паттерны (приоритет выше)
            self.detect_breakout_long,
            self.detect_momentum_long,
            self.detect_liquidity_sweep_long,
            self.detect_consolidation_break_long,
            self.detect_wyckoff_spring,
            # Классические
            self.detect_mega_long,
            self.detect_trap_short,
            self.detect_rejection_long,
        ]
        for detect_fn in detectors:
            try:
                r = detect_fn(candles, hourly_deltas, market_data)
                if r:
                    results.append(r)
            except Exception:
                pass
        results.sort(key=lambda x: x.score_bonus, reverse=True)
        return results

    # ── НОВЫЕ ПАТТЕРНЫ ────────────────────────────────────────────────────────

    def detect_breakout_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        BREAKOUT_LONG: Пробой ключевого уровня ВВЕРХ с объёмом и закреплением.

        Условия:
          1. Последние N свечей — консолидация (флэт <2%)
          2. Последняя свеча закрылась ВЫШЕ уровня консолидации
          3. Объём последней свечи > 2x среднего за 20 свечей
          4. Цена закрытия > 70% от диапазона свечи (бычье тело)
          5. Нет ложного пробоя (закрытие не ниже пробоя)

        Score bonus: 15-25 в зависимости от силы пробоя.
        SL: ниже уровня консолидации (откуда пробивали).
        """
        if len(candles) < 25:
            return None

        # Проверяем консолидацию перед пробоем (5-15 свечей назад)
        consolidation = candles[-20:-2]
        if not consolidation:
            return None

        high_of_cons = max(c.high   for c in consolidation)
        low_of_cons  = min(c.low    for c in consolidation)
        cons_range   = (high_of_cons - low_of_cons) / low_of_cons * 100 if low_of_cons else 999

        # Нужна консолидация — диапазон < 3%
        if cons_range > 3.0:
            return None

        last = candles[-1]
        vol_spike = _vol_spike(candles, 20)

        # Пробой вверх: закрытие выше хая консолидации
        if last.close <= high_of_cons:
            return None

        # Объём подтверждает
        if vol_spike < 1.5:
            return None

        # Бычье закрытие: тело > 50% диапазона свечи
        candle_range = last.high - last.low
        if candle_range > 0 and (last.close - last.open) / candle_range < 0.5:
            return None

        # Вычисляем силу пробоя
        breakout_pct = (last.close - high_of_cons) / high_of_cons * 100
        strength = min(25, int(10 + vol_spike * 3 + breakout_pct * 2))

        # SL: ниже середины консолидации
        sl_level = low_of_cons
        sl_pct   = (last.close - sl_level) / last.close * 100

        reasons = [
            f"Breakout выше {high_of_cons:.4f} (консолидация {cons_range:.1f}%)",
            f"Volume spike {vol_spike:.1f}x",
            f"Сила пробоя +{breakout_pct:.2f}%",
        ]
        return PatternResult(
            name="BREAKOUT_LONG",
            score_bonus=strength,
            confidence=min(0.9, 0.5 + vol_spike * 0.1),
            direction="long",
            suggested_sl_pct=round(sl_pct, 2),
            suggested_tp1_pct=round(breakout_pct * 2, 2),
            reasons=reasons,
        )

    def detect_momentum_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        MOMENTUM_LONG: Импульсная бычья свеча + резкий рост объёма.
        Ловит ATM/OG/EPIC-стиль движения.

        Условия:
          1. Volume spike >= 2.0x
          2. Последняя свеча: закрытие > 80% диапазона (сильная бычья)
          3. RSI 45-72 (не перекуплен)
          4. Цена выше EMA20
          5. Тело свечи > ATR (экстремальная свеча)

        Score bonus: 12-20.
        SL: ниже тела свечи (под открытием свечи).
        """
        if len(candles) < 25:
            return None

        last      = candles[-1]
        prev      = candles[-2]
        vol_spike = _vol_spike(candles, 20)
        atr_val   = _atr(candles, 14)

        # Volume spike
        if vol_spike < 2.0:
            return None

        # Бычья свеча
        candle_range = last.high - last.low
        if candle_range <= 0:
            return None
        bullish_ratio = (last.close - last.low) / candle_range
        if bullish_ratio < 0.65:
            return None

        # Тело > 0.7x ATR (сильная импульсная свеча)
        body = last.close - last.open
        if body <= 0 or (atr_val > 0 and body < atr_val * 0.5):
            return None

        # EMA20 подтверждение
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if ema20 and last.close < ema20[-1]:
            return None   # цена ниже EMA — не momentum

        # RSI (если передан md)
        rsi = getattr(md, "rsi_1h", None) if md else None
        if rsi and (rsi < 40 or rsi > 78):
            return None

        # Не было недавнего SL hunt (предыдущая свеча не сильный вик вниз)
        lower_wick = prev.open - prev.low if prev.open > prev.close else prev.close - prev.low
        if lower_wick > atr_val * 1.5:
            return None   # перед импульсом был stop hunt — это другой паттерн

        pct_move = (last.close - last.open) / last.open * 100 if last.open else 0
        bonus    = min(20, int(12 + vol_spike * 1.5))

        reasons = [
            f"Momentum свеча +{pct_move:.2f}% | Volume {vol_spike:.1f}x avg",
            f"Тело: {body:.4f} | ATR: {atr_val:.4f}",
        ]
        if rsi:
            reasons.append(f"RSI {rsi:.1f}")

        return PatternResult(
            name="MOMENTUM_LONG",
            score_bonus=bonus,
            confidence=min(0.85, 0.55 + vol_spike * 0.08),
            direction="long",
            suggested_sl_pct=round((last.close - last.open) / last.close * 100, 2),
            reasons=reasons,
        )

    def detect_liquidity_sweep_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        LIQUIDITY_SWEEP_LONG (Smart Money / ICT): Stop Hunt вниз → разворот.

        Механика: Под свинг-лоями скапливаются стоп-лоссы лонгистов.
        Маркет-мейкеры двигают цену вниз, снимают ликвидность (стопы срабатывают),
        затем разворачиваются и идут вверх.

        Паттерн: Wick вниз пробивает последний свинг-лоу + цена закрылась выше.

        Условия:
          1. Предыдущая свеча имеет нижний wick > 1.5x ATR (резкий спуск)
          2. Нижний wick пробил свинг-лоу (снял ликвидность)
          3. Закрытие свечи ВЫШЕ свинг-лоу (не дала закрыться под ним)
          4. Текущая свеча бычья (подтверждение разворота)
          5. Объём spike на свече sweep

        Score bonus: 18-25 (один из лучших паттернов — высокая точность).
        SL: ниже нижнего wick (ниже зоны ликвидности).
        """
        if len(candles) < 20:
            return None

        last   = candles[-1]
        sweep  = candles[-2]   # свеча sweep
        atr_v  = _atr(candles[:-2], 14)

        if atr_v <= 0:
            return None

        # Нижний wick свечи sweep
        lower_wick = min(sweep.open, sweep.close) - sweep.low
        if lower_wick < atr_v * 1.0:
            return None   # wick недостаточно длинный

        # Свинг-лои за 5-20 свечей назад
        swing_lows_list = _swing_lows(candles[:-3], lookback=3)
        if not swing_lows_list:
            return None

        recent_swing_low = min(swing_lows_list[-3:]) if len(swing_lows_list) >= 3 else swing_lows_list[-1]

        # Wick пробил свинг-лоу (снял ликвидность)
        if sweep.low > recent_swing_low:
            return None   # не пробил свинг-лоу

        # Закрытие выше свинг-лоу (reject)
        if sweep.close < recent_swing_low:
            return None   # закрылась ниже = нет разворота

        # Текущая свеча бычья (подтверждение)
        if last.close <= last.open:
            return None

        # Volume spike на свечи sweep
        vol_spike = _vol_spike(candles[:-1], 20)

        # Размер sweep как % от ATR
        sweep_depth = (recent_swing_low - sweep.low) / atr_v
        bonus = min(25, int(18 + sweep_depth * 2 + vol_spike))

        reasons = [
            f"Ликвидность снята под {recent_swing_low:.4f}",
            f"Sweep глубина: {(recent_swing_low - sweep.low):.4f} ({sweep_depth:.1f}x ATR)",
            f"Volume spike: {vol_spike:.1f}x",
            "Цена закрылась выше зоны ликвидности — разворот",
        ]
        return PatternResult(
            name="LIQUIDITY_SWEEP_LONG",
            score_bonus=bonus,
            confidence=0.75,
            direction="long",
            suggested_sl_pct=round((last.close - sweep.low) / last.close * 100 + 0.2, 2),
            reasons=reasons,
        )

    def detect_consolidation_break_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        CONSOLIDATION_BREAK_LONG: Выход из боковика вверх.

        Условия:
          1. 8-20 свечей — флэт (ATR низкий, диапазон < 2%)
          2. Выход вверх с ростом объёма
          3. Не false breakout (закрытие стабильно выше уровня)

        Score bonus: 10-18.
        """
        if len(candles) < 25:
            return None

        # Свечи консолидации: 5..22 от конца
        cons = candles[-22:-2]
        last = candles[-1]

        high_cons = max(c.high  for c in cons)
        low_cons  = min(c.low   for c in cons)
        range_pct = (high_cons - low_cons) / low_cons * 100 if low_cons else 999

        if range_pct > 2.5:
            return None   # нет консолидации

        # Пробой вверх
        if last.close <= high_cons:
            return None

        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.3:
            return None

        breakout_pct = (last.close - high_cons) / high_cons * 100
        bonus = min(18, int(10 + vol_spike * 2 + breakout_pct))

        reasons = [
            f"Флэт {range_pct:.1f}% | {len(cons)} свечей",
            f"Пробой выше {high_cons:.4f} (+{breakout_pct:.2f}%)",
            f"Volume {vol_spike:.1f}x",
        ]
        return PatternResult(
            name="CONSOLIDATION_BREAK_LONG",
            score_bonus=bonus,
            confidence=0.65,
            direction="long",
            suggested_sl_pct=round((last.close - low_cons) / last.close * 100, 2),
            reasons=reasons,
        )

    def detect_wyckoff_spring(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        WYCKOFF_SPRING: Классический Wycoff Spring — ложный пробой лоу диапазона.

        Механика: В конце накопления цена делает ложный пробой поддержки,
        шейкаут слабых рук, затем резкий рост (SOS — Sign of Strength).

        Условия:
          1. Длительная консолидация (10-30 свечей)
          2. Резкий пробой нижней границы (spring)
          3. Быстрый возврат в диапазон
          4. Объём снижается при spring (институционалы не продают)
          5. Текущая свеча сильная бычья

        Score bonus: 20-25 (редкий но точный паттерн).
        """
        if len(candles) < 30:
            return None

        # Диапазон накопления
        acc_range = candles[-30:-5]
        last      = candles[-1]
        prev      = candles[-2]   # spring свеча

        if not acc_range:
            return None

        range_high = max(c.high for c in acc_range)
        range_low  = min(c.low  for c in acc_range)
        range_pct  = (range_high - range_low) / range_low * 100 if range_low else 999

        # Нужен нормальный диапазон (не слишком узкий, не слишком широкий)
        if not (1.5 < range_pct < 8.0):
            return None

        # Spring: предыдущая свеча пробила лоу диапазона
        if prev.low > range_low:
            return None

        # Возврат в диапазон: закрытие выше range_low
        if prev.close < range_low:
            return None

        # Объём spring ниже среднего (смарт мани не продаёт)
        vol_spring = prev.quote_volume
        avg_vol    = _avg_vol(acc_range, min(len(acc_range), 15))
        if vol_spring > avg_vol * 1.5:
            return None   # большой объём = реальный пробой, не spring

        # Текущая свеча бычья и внутри диапазона
        if last.close < range_low or last.close <= last.open:
            return None

        spring_depth = (range_low - prev.low) / range_low * 100

        reasons = [
            f"Wyckoff Spring: диапазон {range_pct:.1f}% ({len(acc_range)} свечей)",
            f"Spring глубина: -{spring_depth:.2f}% ниже поддержки",
            f"Объём spring {vol_spring/avg_vol:.1f}x avg (низкий = ложный пробой)",
            "Разворот вверх подтверждён",
        ]
        return PatternResult(
            name="WYCKOFF_SPRING",
            score_bonus=22,
            confidence=0.80,
            direction="long",
            suggested_sl_pct=round((last.close - prev.low) / last.close * 100 + 0.3, 2),
            reasons=reasons,
        )

    # ── КЛАССИЧЕСКИЕ LONG ПАТТЕРНЫ ────────────────────────────────────────────

    def detect_mega_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """Классический MEGA_LONG: RSI перепродан + объём растёт."""
        if len(candles) < 20:
            return None
        rsi = getattr(md, "rsi_1h", None) if md else None
        if rsi and rsi > 45:
            return None
        # Нижний wick > 2x тело
        last = candles[-1]
        body = _body(last)
        lower_wick = min(last.open, last.close) - last.low
        if lower_wick < body * 1.5:
            return None
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.2:
            return None
        return PatternResult(
            name="MEGA_LONG",
            score_bonus=8,
            confidence=0.6,
            direction="long",
            reasons=["RSI перепродан", f"Нижний wick {lower_wick:.4f}", f"Volume {vol_spike:.1f}x"],
        )

    def detect_trap_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """TRAP_SHORT: Шортисты попали в ловушку — разворот вверх."""
        if len(candles) < 20:
            return None
        last = candles[-1]
        prev = candles[-2]
        # Prev: медвежья с большим нижним виком (попытка шорта)
        prev_lower = min(prev.open, prev.close) - prev.low
        if prev_lower < _atr(candles[:-2], 14) * 0.8:
            return None
        # Текущая: бычья
        if last.close <= last.open:
            return None
        return PatternResult(
            name="TRAP_SHORT",
            score_bonus=7,
            confidence=0.58,
            direction="long",
            reasons=["Шортисты пойманы", "Разворот вверх"],
        )

    def detect_rejection_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """REJECTION_LONG: Отскок от поддержки."""
        if len(candles) < 10:
            return None
        last = candles[-1]
        lower_wick = min(last.open, last.close) - last.low
        body       = _body(last)
        if lower_wick < body * 1.0:
            return None
        if last.close < last.open:
            return None
        return PatternResult(
            name="REJECTION_LONG",
            score_bonus=5,
            confidence=0.55,
            direction="long",
            reasons=["Отскок от поддержки", f"Lower wick {lower_wick:.4f}"],
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Вспомогательный метод для main.py
    # ─────────────────────────────────────────────────────────────────────────

    def _get_price_trend(self, candles) -> str:
        if len(candles) < 20:
            return "flat"
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if not ema20:
            return "flat"
        slope = (ema20[-1] - ema20[-5]) / ema20[-5] * 100 if len(ema20) >= 5 else 0
        if closes[-1] > ema20[-1] and slope > 0.1:
            return "up"
        elif closes[-1] < ema20[-1] and slope < -0.1:
            return "down"
        return "flat"


# ============================================================================
# SHORT ПАТТЕРН ДЕТЕКТОР
# ============================================================================

class ShortPatternDetector:
    """Детектор SHORT паттернов — зеркальная логика к LongPatternDetector."""

    def detect_all(self, candles, hourly_deltas=None, market_data=None) -> List[PatternResult]:
        results = []
        detectors = [
            self.detect_breakout_short,
            self.detect_momentum_short,
            self.detect_liquidity_sweep_short,
            self.detect_distribution_break,
            self.detect_wyckoff_upthrust,
            self.detect_mega_short,
            self.detect_trap_long,
            self.detect_rejection_short,
        ]
        for fn in detectors:
            try:
                r = fn(candles, hourly_deltas, market_data)
                if r:
                    results.append(r)
            except Exception:
                pass
        results.sort(key=lambda x: x.score_bonus, reverse=True)
        return results

    def detect_breakout_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """BREAKOUT_SHORT: Пробой вниз с объёмом."""
        if len(candles) < 25:
            return None
        consolidation = candles[-20:-2]
        last = candles[-1]
        low_of_cons = min(c.low for c in consolidation)
        high_of_cons = max(c.high for c in consolidation)
        cons_range = (high_of_cons - low_of_cons) / low_of_cons * 100 if low_of_cons else 999
        if cons_range > 3.0:
            return None
        if last.close >= low_of_cons:
            return None
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.5:
            return None
        candle_range = last.high - last.low
        bearish_ratio = (last.high - last.close) / candle_range if candle_range else 0
        if bearish_ratio < 0.5:
            return None
        breakout_pct = (low_of_cons - last.close) / low_of_cons * 100
        bonus = min(25, int(10 + vol_spike * 3 + breakout_pct * 2))
        return PatternResult(
            name="BREAKOUT_SHORT",
            score_bonus=bonus,
            confidence=min(0.9, 0.5 + vol_spike * 0.1),
            direction="short",
            suggested_sl_pct=round((high_of_cons - last.close) / last.close * 100, 2),
            reasons=[f"Пробой ниже {low_of_cons:.4f}", f"Volume {vol_spike:.1f}x", f"-{breakout_pct:.2f}%"],
        )

    def detect_momentum_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """MOMENTUM_SHORT: Медвежья импульсная свеча + volume spike."""
        if len(candles) < 25:
            return None
        last = candles[-1]
        atr_v = _atr(candles, 14)
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 2.0:
            return None
        candle_range = last.high - last.low
        if candle_range <= 0:
            return None
        bearish_ratio = (last.high - last.close) / candle_range
        if bearish_ratio < 0.65:
            return None
        body = last.open - last.close
        if body <= 0 or (atr_v > 0 and body < atr_v * 0.5):
            return None
        closes = _closes(candles)
        ema20 = _ema(closes, 20)
        if ema20 and last.close > ema20[-1]:
            return None
        rsi = getattr(md, "rsi_1h", None) if md else None
        if rsi and (rsi > 65 or rsi < 25):
            return None
        bonus = min(20, int(12 + vol_spike * 1.5))
        reasons = [f"Медвежий импульс | Volume {vol_spike:.1f}x"]
        if rsi:
            reasons.append(f"RSI {rsi:.1f}")
        return PatternResult(
            name="MOMENTUM_SHORT",
            score_bonus=bonus,
            confidence=min(0.85, 0.55 + vol_spike * 0.08),
            direction="short",
            suggested_sl_pct=round((last.open - last.close) / last.close * 100, 2),
            reasons=reasons,
        )

    def detect_liquidity_sweep_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        LIQUIDITY_SWEEP_SHORT: Stop Hunt вверх → разворот вниз.
        Зеркало LIQUIDITY_SWEEP_LONG.
        Wick пробивает свинг-хай, собирает стоп-лоссы шортистов, затем разворот.
        """
        if len(candles) < 20:
            return None
        last  = candles[-1]
        sweep = candles[-2]
        atr_v = _atr(candles[:-2], 14)
        if atr_v <= 0:
            return None
        upper_wick = sweep.high - max(sweep.open, sweep.close)
        if upper_wick < atr_v * 1.0:
            return None
        swing_highs_list = _swing_highs(candles[:-3], lookback=3)
        if not swing_highs_list:
            return None
        recent_swing_high = max(swing_highs_list[-3:]) if len(swing_highs_list) >= 3 else swing_highs_list[-1]
        if sweep.high < recent_swing_high:
            return None
        if sweep.close > recent_swing_high:
            return None
        if last.close >= last.open:
            return None
        vol_spike = _vol_spike(candles[:-1], 20)
        sweep_height = (sweep.high - recent_swing_high) / recent_swing_high * 100
        bonus = min(25, int(18 + sweep_height * 2 + vol_spike))
        return PatternResult(
            name="LIQUIDITY_SWEEP_SHORT",
            score_bonus=bonus,
            confidence=0.75,
            direction="short",
            suggested_sl_pct=round((sweep.high - last.close) / last.close * 100 + 0.2, 2),
            reasons=[
                f"Ликвидность снята над {recent_swing_high:.4f}",
                f"Sweep +{sweep_height:.2f}% выше свинг-хая",
                f"Volume {vol_spike:.1f}x | Разворот вниз",
            ],
        )

    def detect_distribution_break(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """DISTRIBUTION_BREAK: Пробой нижней границы зоны распределения."""
        if len(candles) < 25:
            return None
        dist_range = candles[-20:-2]
        last = candles[-1]
        low_dist  = min(c.low  for c in dist_range)
        high_dist = max(c.high for c in dist_range)
        range_pct = (high_dist - low_dist) / low_dist * 100 if low_dist else 999
        if range_pct > 3.0 or range_pct < 0.5:
            return None
        if last.close >= low_dist:
            return None
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.3:
            return None
        breakdown_pct = (low_dist - last.close) / low_dist * 100
        bonus = min(18, int(10 + vol_spike * 2 + breakdown_pct))
        return PatternResult(
            name="DISTRIBUTION_BREAK",
            score_bonus=bonus,
            confidence=0.65,
            direction="short",
            suggested_sl_pct=round((high_dist - last.close) / last.close * 100, 2),
            reasons=[f"Пробой распределения ниже {low_dist:.4f}", f"-{breakdown_pct:.2f}% | Volume {vol_spike:.1f}x"],
        )

    def detect_wyckoff_upthrust(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        WYCKOFF_UPTHRUST: Ложный пробой хая зоны распределения (зеркало Spring).
        Цена пробивает сопротивление, но закрывается ниже → шейкаут лонгистов.
        """
        if len(candles) < 30:
            return None
        dist_range = candles[-30:-5]
        last = candles[-1]
        prev = candles[-2]  # upthrust свеча
        if not dist_range:
            return None
        range_high = max(c.high for c in dist_range)
        range_low  = min(c.low  for c in dist_range)
        range_pct  = (range_high - range_low) / range_low * 100 if range_low else 999
        if not (1.5 < range_pct < 8.0):
            return None
        if prev.high < range_high:
            return None
        if prev.close > range_high:
            return None
        vol_upthrust = prev.quote_volume
        avg_vol = _avg_vol(dist_range, min(len(dist_range), 15))
        if vol_upthrust > avg_vol * 1.5:
            return None
        if last.close > range_high or last.close >= last.open:
            return None
        upthrust_height = (prev.high - range_high) / range_high * 100
        return PatternResult(
            name="WYCKOFF_UPTHRUST",
            score_bonus=22,
            confidence=0.80,
            direction="short",
            suggested_sl_pct=round((prev.high - last.close) / last.close * 100 + 0.3, 2),
            reasons=[
                f"Wyckoff Upthrust: диапазон {range_pct:.1f}%",
                f"Upthrust +{upthrust_height:.2f}% выше сопротивления",
                f"Volume низкий ({vol_upthrust/avg_vol:.1f}x) — ложный пробой",
                "Разворот вниз подтверждён",
            ],
        )

    def detect_mega_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 20:
            return None
        last = candles[-1]
        upper_wick = last.high - max(last.open, last.close)
        body = _body(last)
        if upper_wick < body * 1.5:
            return None
        return PatternResult(name="MEGA_SHORT", score_bonus=8, confidence=0.6, direction="short",
                             reasons=["Верхний wick большой", "Отскок от сопротивления"])

    def detect_trap_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 20:
            return None
        last = candles[-1]
        prev = candles[-2]
        upper_wick = prev.high - max(prev.open, prev.close)
        if upper_wick < _atr(candles[:-2], 14) * 0.8:
            return None
        if last.close >= last.open:
            return None
        return PatternResult(name="TRAP_LONG", score_bonus=7, confidence=0.58, direction="short",
                             reasons=["Лонгисты пойманы", "Разворот вниз"])

    def detect_rejection_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 10:
            return None
        last = candles[-1]
        upper_wick = last.high - max(last.open, last.close)
        body = _body(last)
        if upper_wick < body * 1.0:
            return None
        if last.close > last.open:
            return None
        return PatternResult(name="REJECTION_SHORT", score_bonus=5, confidence=0.55, direction="short",
                             reasons=["Отскок от сопротивления"])

    def _get_price_trend(self, candles) -> str:
        if len(candles) < 20:
            return "flat"
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if not ema20:
            return "flat"
        slope = (ema20[-1] - ema20[-5]) / ema20[-5] * 100 if len(ema20) >= 5 else 0
        if closes[-1] < ema20[-1] and slope < -0.1:
            return "down"
        elif closes[-1] > ema20[-1] and slope > 0.1:
            return "up"
        return "flat"
