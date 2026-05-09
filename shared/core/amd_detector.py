"""
AMD (Accumulation / Manipulation / Distribution) Detector v2.0
ПОЛНАЯ ПЕРЕРАБОТКА на основе CRT уровней.

Версия 1.0 (старая) использовала CVD данные — которых у нас нет → всегда unknown 30%.
Версия 2.0 использует:
  1. CRT позицию цены (логика Pine Script Statham v135) — основа
  2. Реальные данные биржи (OI, Funding, L/S ratio) — усиление/фильтрация

Фазы AMD (как в Pine Script):
  ACCUMULATION : цена в нижних 35% CRT диапазона
  EQUILIBRIUM  : цена в средних 30%
  DISTRIBUTION : цена в верхних 35% (65%+ от Low)
  MANIPULATION : TBS detected (sweep CRT уровня + возврат)
  OUTSIDE      : цена вне CRT диапазона

Усиление реальными данными (чего нет в TradingView):
  OI + Funding + L/S → подтверждают или повышают уверенность
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

from .crt_levels import CRTMultiFrame, build_crt


class AMDPhase(Enum):
    UNKNOWN       = "unknown"
    ACCUMULATION  = "accumulation"
    MANIPULATION  = "manipulation"
    DISTRIBUTION  = "distribution"
    EQUILIBRIUM   = "equilibrium"
    OUTSIDE_HIGH  = "outside_high"   # выше CRT High — сильная перекупленность
    OUTSIDE_LOW   = "outside_low"    # ниже CRT Low  — сильная перепроданность
    DECLINE       = "decline"        # совместимость со старым кодом
    ADVANCE       = "advance"        # совместимость со старым кодом


@dataclass
class AMDResult:
    phase:              AMDPhase
    confidence:         float           # 0-100
    reasons:            List[str] = field(default_factory=list)

    crt_alignment:      int  = 0        # 0-9 совпадение ТФ
    tf_phases:          Dict[str, str] = field(default_factory=dict)

    is_ready_for_move:  bool = False
    expected_direction: Optional[str] = None   # "up" | "down"

    oi_confirms:        bool = False
    funding_confirms:   bool = False
    ls_confirms:        bool = False

    # Совместимость со старым кодом
    phase_duration:     int  = 0
    range_high:         Optional[float] = None
    range_low:          Optional[float] = None
    metadata:           Dict = field(default_factory=dict)


def _exchange_confirmation(
    phase: AMDPhase,
    direction: str,
    oi_change_4d: float = 0.0,
    funding_rate:  float = 0.0,
    long_ratio:    float = 50.0,
) -> Tuple[bool, bool, bool, int, List[str]]:
    """
    Подтверждение AMD фазы реальными данными биржи.
    Возвращает: (oi_ok, funding_ok, ls_ok, bonus, reasons)
    """
    oi_ok = funding_ok = ls_ok = False
    bonus = 0
    reasons = []

    if direction == "short":
        if phase in (AMDPhase.DISTRIBUTION, AMDPhase.OUTSIDE_HIGH):
            if oi_change_4d >= 10:
                oi_ok = True; bonus += 3
                reasons.append(f"OI +{oi_change_4d:.1f}% — лонги в ловушке +3")
            if funding_rate >= 0.05:
                funding_ok = True; bonus += 3
                reasons.append(f"Funding {funding_rate:.3f}% — лонги переплачивают +3")
            if long_ratio >= 60:
                ls_ok = True; bonus += 2
                reasons.append(f"L/S {long_ratio:.0f}% лонгов — толпа против нас +2")
    else:
        if phase in (AMDPhase.ACCUMULATION, AMDPhase.OUTSIDE_LOW):
            if funding_rate <= -0.05:
                funding_ok = True; bonus += 3
                reasons.append(f"Funding {funding_rate:.3f}% — шорты переплачивают +3")
            if long_ratio <= 40:
                ls_ok = True; bonus += 2
                reasons.append(f"L/S {long_ratio:.0f}% лонгов — толпа в шортах +2")
            if oi_change_4d <= -5:
                oi_ok = True; bonus += 2
                reasons.append(f"OI {oi_change_4d:.1f}% — шорты выходят +2")

    return oi_ok, funding_ok, ls_ok, bonus, reasons


class AMDDetector:
    """AMD Detector v2.0 — CRT позиция + биржевые данные."""

    def analyze(
        self,
        candles: List[Any],
        cvd_pressure: Any = None,      # не используется (совместимость)
        current_price: float = 0.0,
        oi_change_4d: float = 0.0,
        funding_rate:  float = 0.0,
        long_ratio:    float = 50.0,
        ohlcv_1h: Optional[List[Any]] = None,
        ohlcv_4h: Optional[List[Any]] = None,
    ) -> AMDResult:
        """Старый интерфейс — совместим с вызовами из main.py"""
        _1h = ohlcv_1h or candles
        crt = build_crt(ohlcv_1h=_1h, ohlcv_4h=ohlcv_4h)
        return self._run(crt, current_price, oi_change_4d, funding_rate, long_ratio)

    def analyze_with_crt(
        self,
        crt: CRTMultiFrame,
        current_price: float,
        oi_change_4d: float = 0.0,
        funding_rate:  float = 0.0,
        long_ratio:    float = 50.0,
        direction: str = "short",
    ) -> AMDResult:
        """Новый интерфейс — принимает готовые CRT уровни."""
        return self._run(crt, current_price, oi_change_4d, funding_rate, long_ratio, direction)

    def _run(
        self,
        crt: CRTMultiFrame,
        price: float,
        oi_change_4d: float,
        funding_rate:  float,
        long_ratio:    float,
        direction: str = "both",
    ) -> AMDResult:
        if price <= 0:
            return AMDResult(phase=AMDPhase.UNKNOWN, confidence=0,
                             reasons=["Нет данных цены"])

        primary = crt.primary()
        if not primary or not primary.is_valid:
            return AMDResult(phase=AMDPhase.UNKNOWN, confidence=20,
                             reasons=["CRT диапазон не определён (мало данных)"])

        zone = primary.classify(price)
        phase_map = {
            "ACCUMULATION": AMDPhase.ACCUMULATION,
            "DISTRIBUTION":  AMDPhase.DISTRIBUTION,
            "EQUILIBRIUM":   AMDPhase.EQUILIBRIUM,
            "OUTSIDE_HIGH":  AMDPhase.OUTSIDE_HIGH,
            "OUTSIDE_LOW":   AMDPhase.OUTSIDE_LOW,
        }
        phase = phase_map.get(zone, AMDPhase.UNKNOWN)

        reasons = [
            f"CRT [{primary.low:.6g} — {primary.high:.6g}] "
            f"EQ={primary.eq:.6g} | цена={price:.6g} → {zone}"
        ]

        # Multi-TF выравнивание
        tf_phases = crt.classify_all(price)
        align_dir = ("short" if phase in (AMDPhase.DISTRIBUTION, AMDPhase.OUTSIDE_HIGH) else
                     "long"  if phase in (AMDPhase.ACCUMULATION, AMDPhase.OUTSIDE_LOW) else direction)
        align_score, align_reasons = crt.alignment_score(price, align_dir)
        reasons.extend(align_reasons)

        # Биржевые данные
        ex_dir = ("short" if phase in (AMDPhase.DISTRIBUTION, AMDPhase.OUTSIDE_HIGH) else
                  "long"  if phase in (AMDPhase.ACCUMULATION, AMDPhase.OUTSIDE_LOW)  else align_dir)
        oi_ok, funding_ok, ls_ok, ex_bonus, ex_reasons = _exchange_confirmation(
            phase, ex_dir, oi_change_4d, funding_rate, long_ratio)
        reasons.extend(ex_reasons)

        base = {
            AMDPhase.DISTRIBUTION: 55, AMDPhase.OUTSIDE_HIGH: 65,
            AMDPhase.ACCUMULATION: 55, AMDPhase.OUTSIDE_LOW:  65,
            AMDPhase.EQUILIBRIUM:  35,
        }.get(phase, 25)

        confidence = min(95, base + align_score * 4 + ex_bonus * 2)

        is_ready = (
            phase in (AMDPhase.DISTRIBUTION, AMDPhase.OUTSIDE_HIGH, AMDPhase.ACCUMULATION, AMDPhase.OUTSIDE_LOW)
            and confidence >= 55
        )
        expected_dir = (
            "down" if phase in (AMDPhase.DISTRIBUTION, AMDPhase.OUTSIDE_HIGH) else
            "up"   if phase in (AMDPhase.ACCUMULATION, AMDPhase.OUTSIDE_LOW)  else None
        )

        return AMDResult(
            phase=phase, confidence=round(confidence, 1), reasons=reasons,
            crt_alignment=align_score, tf_phases=tf_phases,
            is_ready_for_move=is_ready, expected_direction=expected_dir,
            oi_confirms=oi_ok, funding_confirms=funding_ok, ls_confirms=ls_ok,
            range_high=primary.high, range_low=primary.low,
        )


_instance: Optional[AMDDetector] = None

def get_amd_detector() -> AMDDetector:
    global _instance
    if _instance is None:
        _instance = AMDDetector()
    return _instance
