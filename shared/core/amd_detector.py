"""
AMD (Accumulation / Manipulation / Distribution) Detector v1.0
Детектор фаз рынка по методологии ICT/Smart Money Concepts.

Фазы:
  1. ACCUMULATION (Накопление): Smart Money собирает позицию
     - Диапазон цен, низкая волатильность
     - Большие объёмы, но цена не растёт (поглощение ликвидности)
     - CVD показывает накопление (buy/sell pressure)
     
  2. MANIPULATION (Манипуляция): Выбивание стопов, сбор ликвидности
     - Ложный пробой уровней
     - Быстрое движение + возврат
     - Большой объём на свече манипуляции
     
  3. DISTRIBUTION (Распределение): Smart Money разгружает позицию
     - Диапазон цен, цена "залипает" на highs
     - Распределение объёма на продажу
     - Подготовка к падению

  4. ADVANCE (Рост): Фаза после накопления
  5. DECLINE (Падение): Фаза после распределения

Признаки:
  - EQH/EQL зоны (пули ликвидности)
  - OB качество (Order Blocks)
  - CVD divergence
  - Volume Profile (пики объёма)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("amd_detector")


class AMDPhase(Enum):
    """Фазы AMD"""
    UNKNOWN = "unknown"
    ACCUMULATION = "accumulation"      # Накопление (готовимся к росту)
    MANIPULATION = "manipulation"      # Манипуляция (выбиваем стопы)
    DISTRIBUTION = "distribution"      # Распределение (готовимся к падению)
    ADVANCE = "advance"               # Рост (после накопления)
    DECLINE = "decline"               # Падение (после распределения)


@dataclass
class AMDConfig:
    """Конфигурация AMD Detector"""
    # Для ACCUMULATION
    accumulation_range_pct: float = 3.0     # Диапазон накопления (%)
    accumulation_min_candles: int = 10      # Мин свечей в диапазоне
    accumulation_volume_spike: float = 1.5  # Всплеск объёма при накоплении
    
    # Для MANIPULATION
    manipulation_reversal_pct: float = 1.5   # Откат после пробоя (%)
    manipulation_speed: float = 2.0          # Скорость движения (ATR множитель)
    
    # Для DISTRIBUTION
    distribution_range_pct: float = 4.0     # Диапазон распределения (%)
    distribution_top_rejections: int = 3    # Мин отторжений от верха
    
    # CVD thresholds
    cvd_accumulation_threshold: float = 65.0   # Давление покупок
    cvd_distribution_threshold: float = 35.0   # Давление продаж


@dataclass
class AMDResult:
    """Результат анализа AMD"""
    phase: AMDPhase
    confidence: float                  # 0-100 уверенность
    reasons: List[str] = field(default_factory=list)
    
    # Детали фазы
    phase_duration: int = 0           # Сколько свечей в фазе
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    
    # Сигналы
    is_ready_for_move: bool = False   # Готов ли импульс
    expected_direction: Optional[str] = None  # "up" или "down"
    
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RangeInfo:
    """Информация о ценовом диапазоне"""
    high: float
    low: float
    duration: int                      # Свечей в диапазоне
    volume_profile: Dict[float, float] = field(default_factory=dict)  # Цена -> объём
    touches_high: int = 0             # Касаний верха
    touches_low: int = 0              # Касаний низа


class AMDDetector:
    """
    Детектор фаз Accumulation/Manipulation/Distribution.
    Определяет где находится цена и готов ли Smart Money к движению.
    """
    
    def __init__(self, config: Optional[AMDConfig] = None):
        self.cfg = config or AMDConfig()
        
    def analyze_range(self, candles: List[Dict]) -> RangeInfo:
        """Анализировать ценовой диапазон"""
        if len(candles) < 5:
            return RangeInfo(high=0, low=0, duration=0)
        
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        
        range_high = max(highs)
        range_low = min(lows)
        
        # Считаем касания
        tolerance = (range_high - range_low) * 0.02  # 2% от диапазона
        touches_high = sum(1 for h in highs if h >= range_high - tolerance)
        touches_low = sum(1 for l in lows if l <= range_low + tolerance)
        
        # Volume profile
        vol_profile = {}
        for c in candles:
            price_level = round(c["close"] / (range_high - range_low) * 10) * ((range_high - range_low) / 10)
            vol_profile[price_level] = vol_profile.get(price_level, 0) + c.get("volume", 0)
        
        return RangeInfo(
            high=range_high,
            low=range_low,
            duration=len(candles),
            volume_profile=vol_profile,
            touches_high=touches_high,
            touches_low=touches_low
        )
    
    def detect_manipulation(self, 
                            candles: List[Dict], 
                            eqh: Optional[float] = None,
                            eql: Optional[float] = None) -> Tuple[bool, List[str]]:
        """Детекция манипуляции (liquidity sweep)"""
        if len(candles) < 3:
            return False, []
        
        reasons = []
        
        # Проверяем пробой EQH с возвратом
        if eqh:
            recent_high = candles[-2]["high"]  # Предпоследняя свеча
            if recent_high > eqh:
                # Вернулись обратно?
                current_close = candles[-1]["close"]
                if current_close < eqh:
                    drop_pct = (recent_high - current_close) / recent_high * 100
                    if drop_pct >= self.cfg.manipulation_reversal_pct:
                        reasons.append(f"EQH sweep + {drop_pct:.1f}% reversal")
                        return True, reasons
        
        # Проверяем пробой EQL с возвратом
        if eql:
            recent_low = candles[-2]["low"]
            if recent_low < eql:
                current_close = candles[-1]["close"]
                if current_close > eql:
                    bounce_pct = (current_close - recent_low) / recent_low * 100
                    if bounce_pct >= self.cfg.manipulation_reversal_pct:
                        reasons.append(f"EQL sweep + {bounce_pct:.1f}% reversal")
                        return True, reasons
        
        # Проверяем быстрое движение с возвратом
        if len(candles) >= 3:
            c1, c2, c3 = candles[-3], candles[-2], candles[-1]
            
            # Лонг: резкий дамп + быстрый отскок
            if c2["low"] < c1["low"] * 0.98 and c3["close"] > c2["open"]:
                reasons.append("Quick dump + recovery")
                return True, reasons
            
            # Шорт: резкий памп + быстрое падение
            if c2["high"] > c1["high"] * 1.02 and c3["close"] < c2["open"]:
                reasons.append("Quick pump + drop")
                return True, reasons
        
        return False, reasons
    
    def analyze(self,
                candles: List[Dict],
                cvd_pressure: Optional[float] = None,  # 0-100
                eqh: Optional[float] = None,
                eql: Optional[float] = None,
                current_price: Optional[float] = None
                ) -> AMDResult:
        """
        Анализировать фазу AMD.
        
        Args:
            candles: Свечи для анализа (минимум 20)
            cvd_pressure: Давление CVD (0=sell, 100=buy)
            eqh: Equal Highs (пул ликвидности сверху)
            eql: Equal Lows (пул ликвидности снизу)
            current_price: Текущая цена
        """
        
        if len(candles) < 20:
            return AMDResult(
                phase=AMDPhase.UNKNOWN,
                confidence=0,
                reasons=["Insufficient candles (need 20+)"]
            )
        
        reasons = []
        price = current_price or candles[-1]["close"]
        
        # Анализируем диапазон
        range_info = self.analyze_range(candles[-20:])
        range_pct = (range_info.high - range_info.low) / range_info.low * 100
        
        # 1. Проверяем манипуляцию (самый сильный сигнал)
        is_manipulation, mani_reasons = self.detect_manipulation(candles[-5:], eqh, eql)
        if is_manipulation:
            reasons.extend(mani_reasons)
            # Определяем направление после манипуляции
            direction = "up" if "EQL" in str(mani_reasons) else "down"
            return AMDResult(
                phase=AMDPhase.MANIPULATION,
                confidence=80,
                reasons=reasons,
                is_ready_for_move=True,
                expected_direction=direction,
                metadata={"manipulation_type": "liquidity_sweep"}
            )
        
        # 2. Проверяем накопление
        if range_pct <= self.cfg.accumulation_range_pct:
            if range_info.duration >= self.cfg.accumulation_min_candles:
                volume_avg = sum(c.get("volume", 0) for c in candles[-20:]) / 20
                recent_volume = sum(c.get("volume", 0) for c in candles[-5:]) / 5
                
                if recent_volume > volume_avg * self.cfg.accumulation_volume_spike:
                    if cvd_pressure and cvd_pressure >= self.cfg.cvd_accumulation_threshold:
                        reasons.append(f"Tight range {range_pct:.1f}% + volume spike + buy pressure")
                        return AMDResult(
                            phase=AMDPhase.ACCUMULATION,
                            confidence=75,
                            reasons=reasons,
                            phase_duration=range_info.duration,
                            range_high=range_info.high,
                            range_low=range_info.low,
                            is_ready_for_move=range_info.touches_low >= 3,  # 3+ касания низа
                            expected_direction="up",
                            metadata={
                                "touches_high": range_info.touches_high,
                                "touches_low": range_info.touches_low
                            }
                        )
        
        # 3. Проверяем распределение
        if range_pct <= self.cfg.distribution_range_pct:
            if range_info.touches_high >= self.cfg.distribution_top_rejections:
                if cvd_pressure and cvd_pressure <= self.cfg.cvd_distribution_threshold:
                    reasons.append(f"Range {range_pct:.1f}% + {range_info.touches_high} top rejections + sell pressure")
                    return AMDResult(
                        phase=AMDPhase.DISTRIBUTION,
                        confidence=70,
                        reasons=reasons,
                        phase_duration=range_info.duration,
                        range_high=range_info.high,
                        range_low=range_info.low,
                        is_ready_for_move=range_info.touches_high >= 4,
                        expected_direction="down",
                        metadata={
                            "touches_high": range_info.touches_high,
                            "touches_low": range_info.touches_low
                        }
                    )
        
        # 4. Проверяем тренд (advance/decline)
        if len(candles) >= 10:
            price_10_ago = candles[-10]["close"]
            price_change = (price - price_10_ago) / price_10_ago * 100
            
            if price_change > 5:  # Рост > 5% за 10 свечей
                return AMDResult(
                    phase=AMDPhase.ADVANCE,
                    confidence=60,
                    reasons=[f"Uptrend: +{price_change:.1f}% in 10 candles"],
                    expected_direction="up"
                )
            elif price_change < -5:  # Падение > 5%
                return AMDResult(
                    phase=AMDPhase.DECLINE,
                    confidence=60,
                    reasons=[f"Downtrend: {price_change:.1f}% in 10 candles"],
                    expected_direction="down"
                )
        
        # Не определили фазу
        return AMDResult(
            phase=AMDPhase.UNKNOWN,
            confidence=30,
            reasons=[f"Range {range_pct:.1f}%, no clear phase detected"],
            range_high=range_info.high,
            range_low=range_info.low
        )


# Singleton instance
_amd: Optional[AMDDetector] = None

def get_amd_detector(config: Optional[AMDConfig] = None) -> AMDDetector:
    """Get or create AMDDetector singleton"""
    global _amd
    if _amd is None:
        _amd = AMDDetector(config)
    return _amd
