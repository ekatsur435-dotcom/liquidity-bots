"""
TBS (Test Before Strike / Tap Before Sweep) Detector v2.0
Переработан на основе CRT уровней (логика Pine Script Statham v135).

Версия 1.x: искала Order Blocks — давала ложные сигналы, зоны "плавали".
Версия 2.0: TBS привязан к чётким CRT High/Low уровням.

Алгоритм (Pine Script):
  TBS BEAR: свеча пробила CRT High + закрылась НИЖЕ + медвежья → шорт сигнал
  TBS BULL: свеча пробила CRT Low  + закрылась ВЫШЕ + бычья   → лонг сигнал

Multi-timeframe (чего нет в TV):
  1D (macro): бонус +15 (редкий, очень сильный)
  4H:         бонус +12 (основной)
  1H:         бонус +8  (точный вход)
  Два ТФ одновременно → максимальный бонус

Совместимость: detect_tbs_entry() работает как раньше (drop-in replacement).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .crt_levels import CRTMultiFrame, CRTLevel, build_crt


def _h(c: Any) -> float:
    if isinstance(c, dict):        return float(c.get("high",  0))
    if isinstance(c, (list, tuple)): return float(c[1]) if len(c) > 1 else 0.0
    return float(getattr(c, "high",  0))

def _l(c: Any) -> float:
    if isinstance(c, dict):        return float(c.get("low",   0))
    if isinstance(c, (list, tuple)): return float(c[2]) if len(c) > 2 else 0.0
    return float(getattr(c, "low",   0))

def _c(c: Any) -> float:
    if isinstance(c, dict):        return float(c.get("close", 0))
    if isinstance(c, (list, tuple)): return float(c[3]) if len(c) > 3 else 0.0
    return float(getattr(c, "close", 0))

def _o(c: Any) -> float:
    if isinstance(c, dict):        return float(c.get("open",  0))
    if isinstance(c, (list, tuple)): return float(c[0]) if len(c) > 0 else 0.0
    return float(getattr(c, "open",  0))


@dataclass
class TBSSignal:
    found:      bool
    direction:  str          # "short" | "long"
    tf:         str          # "1D" | "4H" | "1H"
    zone:       float        # цена зоны TBS (midpoint тела свечи)
    crt_level:  float        # уровень CRT который был пробит
    bonus:      int          # бонус к скору
    confidence: int          # 0-100
    reasons:    List[str] = field(default_factory=list)

    # Совместимость со старым кодом
    @property
    def zone_price(self) -> float:
        return self.zone


def _check_tbs_on_level(
    candles: List[Any],
    level: CRTLevel,
    direction: str,
    lookback_bars: int = 5,
) -> Tuple[bool, float, float]:
    """
    Проверяет TBS на конкретном CRT уровне.
    Смотрит последние lookback_bars свечей.
    Возвращает: (found, zone_price, crt_price)
    """
    if not level or not level.is_valid:
        return False, 0.0, 0.0

    recent = candles[-lookback_bars:] if len(candles) >= lookback_bars else candles

    for candle in reversed(recent):
        ch, cl, cc, co = _h(candle), _l(candle), _c(candle), _o(candle)
        if cc <= 0:
            continue

        if direction == "short" and level.is_tbs_bear(ch, cc, co):
            zone = (cc + co) / 2.0   # midpoint тела свечи (Pine Script логика)
            return True, zone, level.high

        if direction == "long" and level.is_tbs_bull(cl, cc, co):
            zone = (cc + co) / 2.0
            return True, zone, level.low

    return False, 0.0, 0.0


def detect_tbs_multi(
    candles: List[Any],
    direction: str,
    ohlcv_1h: Optional[List[Any]] = None,
    ohlcv_4h: Optional[List[Any]] = None,
) -> TBSSignal:
    """
    Multi-timeframe TBS детектор.
    Ищет TBS на 1D, 4H, 1H уровнях CRT.
    Возвращает сигнал с наибольшим бонусом (самый сильный).
    """
    _1h = ohlcv_1h or candles
    crt  = build_crt(ohlcv_1h=_1h, ohlcv_4h=ohlcv_4h)

    # Порядок: сильнейший ТФ первый
    checks = [
        ("1D", crt.macro,  15, candles,  3),
        ("4H", crt.h4,     12, ohlcv_4h or candles, 4),
        ("1H", crt.h1,      8, _1h,      5),
    ]

    best: Optional[TBSSignal] = None

    for tf_name, level, bonus, tf_candles, lookback in checks:
        if not tf_candles:
            continue
        found, zone, crt_price = _check_tbs_on_level(tf_candles, level, direction, lookback)
        if found:
            dist_pct = abs(zone - crt_price) / crt_price * 100 if crt_price > 0 else 0
            conf = min(90, 60 + bonus * 2)
            signal = TBSSignal(
                found=True, direction=direction,
                tf=tf_name, zone=zone, crt_level=crt_price,
                bonus=bonus, confidence=conf,
                reasons=[
                    f"TBS {direction.upper()} на {tf_name} CRT={crt_price:.6g} "
                    f"зона={zone:.6g} (+{bonus} к скору)"
                ]
            )
            # Берём максимальный бонус
            if best is None or bonus > best.bonus:
                best = signal

    return best or TBSSignal(
        found=False, direction=direction, tf="", zone=0.0,
        crt_level=0.0, bonus=0, confidence=0
    )


def detect_tbs_entry(
    candles: List[Any],
    direction: str = "short",
    ohlcv_1h: Optional[List[Any]] = None,
    ohlcv_4h: Optional[List[Any]] = None,
) -> Optional[dict]:
    """
    Drop-in замена старого detect_tbs_entry().
    Совместим с вызовами из main.py:
      tbs = detect_tbs_entry(ohlcv_primary, direction="short")
      if tbs and tbs["found"]:
          zone = tbs["zone"]
    """
    signal = detect_tbs_multi(candles, direction, ohlcv_1h=ohlcv_1h, ohlcv_4h=ohlcv_4h)
    if not signal.found:
        return None
    return {
        "found":      True,
        "zone":       signal.zone,
        "crt_level":  signal.crt_level,
        "tf":         signal.tf,
        "bonus":      signal.bonus,
        "confidence": signal.confidence,
        "reasons":    signal.reasons,
    }


# ── AMD + TBS конфлюенс ───────────────────────────────────────────────────────

def amd_tbs_confluence_bonus(
    amd_phase: str,       # значение AMDPhase.value
    tbs_signal: dict,     # результат detect_tbs_entry()
    direction: str,       # "short" | "long"
) -> Tuple[int, List[str]]:
    """
    Матрица бонусов TBS + AMD конфлюенса.
    Вызывается из main.py после отдельного расчёта AMD и TBS.

    SHORT:
      DISTRIBUTION + TBS BEAR → +18 (максимум)
      OUTSIDE_HIGH + TBS BEAR → +20 (экстремальная перекупленность)
      DISTRIBUTION            → +5
      TBS BEAR только         → бонус от ТФ (8/12/15)

    LONG:
      ACCUMULATION + TBS BULL → +18
      OUTSIDE_LOW  + TBS BULL → +20
      ACCUMULATION            → +5
      TBS BULL только         → бонус от ТФ
    """
    bonus = 0
    reasons = []

    tbs_found = tbs_signal and tbs_signal.get("found", False)
    tbs_bonus = tbs_signal.get("bonus", 0) if tbs_found else 0
    tbs_tf    = tbs_signal.get("tf", "")   if tbs_found else ""

    if direction == "short":
        if amd_phase == "outside_high" and tbs_found:
            bonus = 20
            reasons.append(f"🔥 AMD OUTSIDE_HIGH + TBS {tbs_tf} = экстрем. распределение +20")
        elif amd_phase == "distribution" and tbs_found:
            bonus = 18
            reasons.append(f"🎯 AMD DISTRIBUTION + TBS {tbs_tf} = максимальный шорт +18")
        elif amd_phase == "distribution":
            bonus = 5
            reasons.append("🏗️ AMD DISTRIBUTION +5")
        elif amd_phase == "manipulation" and tbs_found:
            bonus = tbs_bonus + 2
            reasons.append(f"⚡ AMD MANIPULATION + TBS {tbs_tf} +{bonus}")
        elif tbs_found:
            bonus = tbs_bonus
            reasons.append(f"🎯 TBS {tbs_tf} +{tbs_bonus}")
        elif amd_phase in ("accumulation", "outside_low"):
            bonus = -5
            reasons.append("⚠️ AMD ACCUMULATION — против SHORT -5")

    else:  # long
        if amd_phase == "outside_low" and tbs_found:
            bonus = 20
            reasons.append(f"🔥 AMD OUTSIDE_LOW + TBS {tbs_tf} = экстрем. накопление +20")
        elif amd_phase == "accumulation" and tbs_found:
            bonus = 18
            reasons.append(f"🎯 AMD ACCUMULATION + TBS {tbs_tf} = максимальный лонг +18")
        elif amd_phase == "accumulation":
            bonus = 5
            reasons.append("🏗️ AMD ACCUMULATION +5")
        elif amd_phase == "manipulation" and tbs_found:
            bonus = tbs_bonus + 2
            reasons.append(f"⚡ AMD MANIPULATION + TBS {tbs_tf} +{bonus}")
        elif tbs_found:
            bonus = tbs_bonus
            reasons.append(f"🎯 TBS {tbs_tf} +{tbs_bonus}")
        elif amd_phase in ("distribution", "outside_high"):
            bonus = -5
            reasons.append("⚠️ AMD DISTRIBUTION — против LONG -5")

    return bonus, reasons
