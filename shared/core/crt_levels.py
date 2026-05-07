"""
CRT (Consequent Range Theory) Multi-Timeframe Calculator v1.0

Адаптация логики Pine Script Statham v135 + усиление реальными данными биржи.

CRT диапазон = Swing High → Swing Low текущего ТФ.
  ACCUMULATION zone : нижние 35% диапазона (лонг зона)
  EQUILIBRIUM zone  : средние 30% (нейтраль)
  DISTRIBUTION zone : верхние 35% диапазона (шорт зона)

Multi-TF иерархия: 1D (macro) > 4H (medium) > 1H (micro)
Совпадение 2+ ТФ = значительно усиливает сигнал.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


def _h(c: Any) -> float:
    if isinstance(c, dict):   return float(c.get("high", 0))
    if isinstance(c, (list, tuple)): return float(c[1]) if len(c) > 1 else 0.0
    return float(getattr(c, "high", 0))

def _l(c: Any) -> float:
    if isinstance(c, dict):   return float(c.get("low", 0))
    if isinstance(c, (list, tuple)): return float(c[2]) if len(c) > 2 else 0.0
    return float(getattr(c, "low", 0))

def _c(c: Any) -> float:
    if isinstance(c, dict):   return float(c.get("close", 0))
    if isinstance(c, (list, tuple)): return float(c[3]) if len(c) > 3 else 0.0
    return float(getattr(c, "close", 0))

def _o(c: Any) -> float:
    if isinstance(c, dict):   return float(c.get("open", 0))
    if isinstance(c, (list, tuple)): return float(c[0]) if len(c) > 0 else 0.0
    return float(getattr(c, "open", 0))


@dataclass
class CRTLevel:
    """CRT уровни для одного таймфрейма"""
    timeframe:  str
    high:       float   # Swing High (CRT High — верхняя граница)
    low:        float   # Swing Low  (CRT Low  — нижняя граница)
    eq:         float   # 50% midpoint (магнит для цены)
    acc_top:    float   # нижние 35% → граница ACCUMULATION
    dist_bot:   float   # верхние 65% → граница DISTRIBUTION
    range_size: float
    sweep_buf:  float   # 0.05% буфер для TBS
    is_valid:   bool

    def classify(self, price: float) -> str:
        """Определяет AMD фазу по позиции цены в диапазоне."""
        if not self.is_valid or self.range_size == 0:
            return "UNKNOWN"
        if price > self.high + self.sweep_buf:
            return "OUTSIDE_HIGH"
        if price < self.low - self.sweep_buf:
            return "OUTSIDE_LOW"
        if price <= self.acc_top:
            return "ACCUMULATION"
        if price >= self.dist_bot:
            return "DISTRIBUTION"
        return "EQUILIBRIUM"

    def is_tbs_bear(self, high: float, close: float, open_: float) -> bool:
        """Свеча пробила CRT High сверху + закрылась ниже (медвежий ретест)."""
        return (self.is_valid and
                high > self.high + self.sweep_buf and
                close < self.high and
                close < open_)

    def is_tbs_bull(self, low: float, close: float, open_: float) -> bool:
        """Свеча пробила CRT Low снизу + закрылась выше (бычий ретест)."""
        return (self.is_valid and
                low < self.low - self.sweep_buf and
                close > self.low and
                close > open_)

    def dist_to_high_pct(self, price: float) -> float:
        """Расстояние от цены до CRT High в %"""
        if not self.is_valid or price <= 0: return 0.0
        return (self.high - price) / price * 100

    def dist_to_low_pct(self, price: float) -> float:
        """Расстояние от цены до CRT Low в %"""
        if not self.is_valid or price <= 0: return 0.0
        return (price - self.low) / price * 100


@dataclass
class CRTMultiFrame:
    """Multi-timeframe CRT анализ"""
    macro: Optional[CRTLevel] = None  # 1D (или синтетика из 4H × 30 баров)
    h4:    Optional[CRTLevel] = None  # 4H (основной)
    h1:    Optional[CRTLevel] = None  # 1H (точный вход)

    def primary(self) -> Optional[CRTLevel]:
        """Основной уровень: 4H → macro → 1H"""
        return self.h4 or self.macro or self.h1

    def classify_all(self, price: float) -> dict:
        """Фаза AMD на всех ТФ"""
        return {
            "1D": self.macro.classify(price) if self.macro else "UNKNOWN",
            "4H": self.h4.classify(price)    if self.h4    else "UNKNOWN",
            "1H": self.h1.classify(price)    if self.h1    else "UNKNOWN",
        }

    def alignment_score(self, price: float, direction: str) -> Tuple[int, List[str]]:
        """
        Считает совпадение ТФ для direction ("short" / "long").
        Возвращает (score 0-9, reasons).

        short → ищем DISTRIBUTION
        long  → ищем ACCUMULATION
        """
        target = "DISTRIBUTION" if direction == "short" else "ACCUMULATION"
        weights = [("1D", self.macro, 3), ("4H", self.h4, 4), ("1H", self.h1, 2)]

        score, reasons = 0, []
        for tf_name, lvl, w in weights:
            if not lvl or not lvl.is_valid:
                continue
            zone = lvl.classify(price)
            if zone == target:
                score += w
                reasons.append(f"CRT {tf_name}: {zone} +{w}")
            elif zone in ("OUTSIDE_HIGH", "OUTSIDE_LOW"):
                if (direction == "short" and zone == "OUTSIDE_HIGH") or \
                   (direction == "long"  and zone == "OUTSIDE_LOW"):
                    score += w // 2
                    reasons.append(f"CRT {tf_name}: вне диапазона ({zone}) +{w//2}")
        return score, reasons

    def tbs_detected(self, candles: List[Any], direction: str) -> Tuple[bool, str, float, int]:
        """
        Проверяет TBS на всех ТФ для последних N свечей.
        direction: "short" / "long"

        Возвращает: (found, tf_name, zone_price, score_bonus)
        Бонусы: 1D=15, 4H=12, 1H=8
        """
        checks = [("1D", self.macro, 15), ("4H", self.h4, 12), ("1H", self.h1, 8)]
        # Для 1H и 4H используем последние 3 свечи (ищем свежий TBS)
        recent = candles[-3:] if len(candles) >= 3 else candles

        for tf_name, lvl, bonus in checks:
            if not lvl or not lvl.is_valid:
                continue
            for candle in reversed(recent):
                ch, cl, cc, co = _h(candle), _l(candle), _c(candle), _o(candle)
                if direction == "short" and lvl.is_tbs_bear(ch, cc, co):
                    zone_price = (cc + co) / 2  # midpoint тела свечи
                    return True, tf_name, zone_price, bonus
                if direction == "long" and lvl.is_tbs_bull(cl, cc, co):
                    zone_price = (cc + co) / 2
                    return True, tf_name, zone_price, bonus

        return False, "", 0.0, 0


def _find_swing_hl(candles: List[Any], lookback: int, swing_n: int) -> Tuple[float, float]:
    """
    Находит последние значимые swing high и swing low.
    swing_n: кол-во баров с каждой стороны которые должны быть ниже/выше.
    """
    data = candles[-lookback:] if len(candles) > lookback else candles
    n = len(data)
    if n < swing_n * 2 + 3:
        highs = [_h(c) for c in data]
        lows  = [_l(c) for c in data]
        return (max(highs) if highs else 0.0), (min(lows) if lows else 0.0)

    sw_highs, sw_lows = [], []
    for i in range(swing_n, n - swing_n):
        hi = _h(data[i])
        lo = _l(data[i])
        left_h  = [_h(data[j]) for j in range(i - swing_n, i)]
        right_h = [_h(data[j]) for j in range(i + 1, i + swing_n + 1)]
        left_l  = [_l(data[j]) for j in range(i - swing_n, i)]
        right_l = [_l(data[j]) for j in range(i + 1, i + swing_n + 1)]
        if hi > max(left_h) and hi > max(right_h):
            sw_highs.append(hi)
        if lo < min(left_l) and lo < min(right_l):
            sw_lows.append(lo)

    swing_high = sw_highs[-1] if sw_highs else max(_h(c) for c in data)
    swing_low  = sw_lows[-1]  if sw_lows  else min(_l(c) for c in data)
    return swing_high, swing_low


def _build_level(candles: List[Any], tf: str, lookback: int, swing_n: int) -> CRTLevel:
    if not candles or len(candles) < swing_n * 2 + 3:
        return CRTLevel(tf, 0, 0, 0, 0, 0, 0, 0, False)

    sh, sl = _find_swing_hl(candles, lookback, swing_n)
    if sh <= sl or sl <= 0:
        return CRTLevel(tf, 0, 0, 0, 0, 0, 0, 0, False)

    rng       = sh - sl
    eq        = (sh + sl) / 2.0
    acc_top   = sl + rng * 0.35   # нижние 35%
    dist_bot  = sl + rng * 0.65   # верхние 65% — начало DISTRIBUTION
    sweep_buf = sh * 0.0005        # 0.05% буфер

    return CRTLevel(tf, sh, sl, eq, acc_top, dist_bot, rng, sweep_buf, True)


def build_crt(
    ohlcv_1h: Optional[List[Any]] = None,
    ohlcv_4h: Optional[List[Any]] = None,
) -> CRTMultiFrame:
    """
    Строит multi-TF CRT уровни из доступных данных.

    4H (основной):  20 баров lookback, swing_n=2   → ~80 часов (~3-4 дня)
    1H (микро):     48 баров lookback, swing_n=3   → 48 часов (2 дня)
    Macro (1D):     30 баров 4H lookback, swing_n=2 → ~5 дней (синтетика)
    """
    result = CRTMultiFrame()

    if ohlcv_4h and len(ohlcv_4h) >= 8:
        result.h4    = _build_level(ohlcv_4h, "4H",     lookback=20, swing_n=2)
        result.macro = _build_level(ohlcv_4h, "1D(4H)", lookback=len(ohlcv_4h), swing_n=2)

    if ohlcv_1h and len(ohlcv_1h) >= 10:
        result.h1 = _build_level(ohlcv_1h, "1H", lookback=48, swing_n=3)

    return result
