"""
Pump/Dump Detector v1.0 (from Aegis HARD BOT)
Z-Score + VWAP deviation + Volume Spike = Climax Exhaustion Detection.

Логика:
  1. Расчёт VWAP + rolling σ (std deviation)
  2. Z-Score = (Price - VWAP) / σ
  3. Volume Spike: текущий объём / SMA(20) объёма
  4. RSI перекупленность (>75) / перепроданность (<25)
  5. Price Velocity (скорость движения за 5 свечей)

Сигналы:
  ULTRA:    Z > 3.0 + Vol > 3x + RSI > 78 (для short) / RSI < 22 (для long)
  STRONG:   Z > 2.5 + Vol > 2.5x + RSI > 72 / RSI < 28
  MODERATE: Z > 2.0 + Vol > 2x

Интеграция в скорер:
  - Для SHORT: Z-Score > +2.5 = перекупленность = сигнал к шорту
  - Для LONG:  Z-Score < -2.5 = перепроданность = сигнал к лонгу
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pump_detector")


@dataclass
class ZScoreConfig:
    """Конфигурация Z-Score детектора"""
    z_threshold:       float = 2.5     # Z-Score порог для сигнала
    volume_spike:      float = 3.0     # Volume/SMA порог
    rsi_overbought:    float = 75.0    # RSI порог для short
    rsi_oversold:      float = 25.0    # RSI порог для long
    lookback:          int   = 20      # Период для VWAP/SMA
    confirmation_candles: int = 2      # Свечей подтверждения


@dataclass
class PumpDetectionResult:
    """Результат детекции пампа/дампа"""
    detected:      bool        # Сигнал detected?
    z_score:       float       # Z-Score value
    vwap:          float       # VWAP price
    std_dev:       float       # Standard deviation
    volume_spike:  float       # Volume ratio
    rsi:           float       # RSI value
    price_velocity: float      # % change over 5 candles
    signal_type:   str         # "pump" (for short) or "dump" (for long)
    strength:      str         # "ULTRA", "STRONG", "MODERATE", "WEAK"
    score:         float       # 0-100 score for integration
    reasons:       List[str]   # Human-readable reasons
    metadata:      Dict[str, Any]


class PumpDetector:
    """
    Институциональный детектор кульминации пампа/дампа.
    Ищет точки истощения для входа против тренда.
    """

    def __init__(self, config: Optional[ZScoreConfig] = None):
        self.cfg = config or ZScoreConfig()

    def _calc_rsi(self, closes: List[float], period: int = 14) -> float:
        """RSI через EMA smoothing"""
        if len(closes) < period + 1:
            return 50.0
        
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [d if d > 0 else 0.0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0.0 for d in deltas[-period:]]
        
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        
        if avg_l == 0:
            return 100.0
        
        rs = avg_g / avg_l
        return round(100.0 - 100.0 / (1.0 + rs), 2)

    def _calc_vwap_std(self, ohlcv: list, lookback: int) -> Tuple[float, float]:
        """VWAP и стандартное отклонение типичных цен"""
        recent = ohlcv[-lookback:] if len(ohlcv) >= lookback else ohlcv
        typical_prices, volumes = [], []

        for c in recent:
            tp = (c[1] + c[2] + c[3]) / 3  # (high + low + close) / 3
            typical_prices.append(tp)
            volumes.append(c[4])  # volume

        total_vol = sum(volumes)
        if total_vol == 0:
            return typical_prices[-1] if typical_prices else 0.0, 1.0

        vwap = sum(tp * v for tp, v in zip(typical_prices, volumes)) / total_vol
        
        # Standard deviation
        variance = sum((tp - vwap) ** 2 for tp in typical_prices) / len(typical_prices)
        std_dev = math.sqrt(variance) if variance > 0 else 0.001
        
        return vwap, std_dev

    def _calc_volume_spike(self, ohlcv: list, lookback: int = 20) -> float:
        """Расчёт spike объёма"""
        if len(ohlcv) < lookback + 5:
            return 1.0
        
        # Средний объём (без последних 5)
        avg_vol = sum(c[4] for c in ohlcv[-lookback-5:-5]) / lookback
        # Текущий объём (3 последние свечи)
        current_vol = sum(c[4] for c in ohlcv[-3:]) / 3
        
        return current_vol / avg_vol if avg_vol > 0 else 1.0

    def _calc_price_velocity(self, ohlcv: list) -> float:
        """Скорость движения цены за 5 свечей (%)"""
        if len(ohlcv) < 6:
            return 0.0
        
        price_5_ago = ohlcv[-6][3]  # close 5 candles ago
        current_price = ohlcv[-1][3]  # current close
        
        return (current_price - price_5_ago) / price_5_ago * 100

    def detect(self, ohlcv: list, direction: str = "short") -> PumpDetectionResult:
        """
        Основной метод детекции.
        
        Args:
            ohlcv: Список свечей [open, high, low, close, volume]
            direction: "short" (ищет памп) или "long" (ищет дамп)
        
        Returns:
            PumpDetectionResult с score и метаданными
        """
        if len(ohlcv) < self.cfg.lookback + 5:
            return PumpDetectionResult(
                detected=False, z_score=0.0, vwap=0.0, std_dev=0.0,
                volume_spike=1.0, rsi=50.0, price_velocity=0.0,
                signal_type="none", strength="WEAK", score=0.0,
                reasons=["Insufficient data"], metadata={}
            )

        # Расчёты
        vwap, std_dev = self._calc_vwap_std(ohlcv, self.cfg.lookback)
        current_price = ohlcv[-1][3]  # close
        current_high = ohlcv[-1][1]   # high
        current_low = ohlcv[-1][2]    # low
        
        # Z-Score (используем high для short, low для long — более консервативно)
        price_for_z = current_high if direction == "short" else current_low
        z_score = (price_for_z - vwap) / std_dev if std_dev > 0 else 0.0
        
        # RSI
        closes = [c[3] for c in ohlcv]
        rsi = self._calc_rsi(closes)
        
        # Volume spike
        vol_spike = self._calc_volume_spike(ohlcv, self.cfg.lookback)
        
        # Price velocity
        velocity = self._calc_price_velocity(ohlcv)

        # Определение сигнала
        reasons = []
        score = 0.0
        strength = "WEAK"
        detected = False
        signal_type = "none"

        if direction == "short":
            # Для SHORT: ищем перекупленность (Z > 0, RSI > 75)
            if z_score >= 3.0 and vol_spike >= 3.0 and rsi >= 78:
                detected = True
                strength = "ULTRA"
                score = 95.0
                signal_type = "pump"
                reasons.append(f"🚨 ULTRA PUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x, RSI={rsi:.0f}")
            elif z_score >= 2.5 and vol_spike >= 2.5 and rsi >= 72:
                detected = True
                strength = "STRONG"
                score = 85.0
                signal_type = "pump"
                reasons.append(f"🔴 STRONG PUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x, RSI={rsi:.0f}")
            elif z_score >= 2.0 and vol_spike >= 2.0:
                detected = True
                strength = "MODERATE"
                score = 70.0
                signal_type = "pump"
                reasons.append(f"🟡 MODERATE PUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x, RSI={rsi:.0f}")
            elif z_score >= 1.5 and vol_spike >= 1.5:
                detected = True
                strength = "WEAK"
                score = 55.0
                signal_type = "pump"
                reasons.append(f"⚠️ WEAK PUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x")
            
            # Дополнительные факторы для short
            if velocity > 10 and z_score > 2.0:  # Быстрый памп
                score += 10
                reasons.append(f"⚡ Fast velocity: +{velocity:.1f}%")
            if rsi >= 80:
                score += 5
                reasons.append("🔥 Extreme RSI")
                
        else:  # direction == "long"
            # Для LONG: ищем перепроданность (Z < 0, RSI < 25)
            if z_score <= -3.0 and vol_spike >= 3.0 and rsi <= 22:
                detected = True
                strength = "ULTRA"
                score = 95.0
                signal_type = "dump"
                reasons.append(f"🚨 ULTRA DUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x, RSI={rsi:.0f}")
            elif z_score <= -2.5 and vol_spike >= 2.5 and rsi <= 28:
                detected = True
                strength = "STRONG"
                score = 85.0
                signal_type = "dump"
                reasons.append(f"🔴 STRONG DUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x, RSI={rsi:.0f}")
            elif z_score <= -2.0 and vol_spike >= 2.0:
                detected = True
                strength = "MODERATE"
                score = 70.0
                signal_type = "dump"
                reasons.append(f"🟡 MODERATE DUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x, RSI={rsi:.0f}")
            elif z_score <= -1.5 and vol_spike >= 1.5:
                detected = True
                strength = "WEAK"
                score = 55.0
                signal_type = "dump"
                reasons.append(f"⚠️ WEAK DUMP: Z={z_score:.2f}, Vol={vol_spike:.1f}x")
            
            # Дополнительные факторы для long
            if velocity < -10 and z_score < -2.0:  # Быстрый дамп
                score += 10
                reasons.append(f"⚡ Fast drop: {velocity:.1f}%")
            if rsi <= 20:
                score += 5
                reasons.append("❄️ Extreme RSI")

        # Cap score at 100
        score = min(score, 100.0)

        if not reasons:
            reasons.append(f"Neutral: Z={z_score:.2f}, RSI={rsi:.0f}, Vol={vol_spike:.1f}x")

        return PumpDetectionResult(
            detected=detected,
            z_score=round(z_score, 2),
            vwap=round(vwap, 6),
            std_dev=round(std_dev, 6),
            volume_spike=round(vol_spike, 2),
            rsi=round(rsi, 1),
            price_velocity=round(velocity, 2),
            signal_type=signal_type,
            strength=strength,
            score=round(score, 1),
            reasons=reasons,
            metadata={
                "lookback": self.cfg.lookback,
                "price_used": price_for_z,
                "candles_analyzed": len(ohlcv)
            }
        )


# Singleton instance
_pump_detector: Optional[PumpDetector] = None


def get_pump_detector() -> PumpDetector:
    """Получить singleton instance PumpDetector"""
    global _pump_detector
    if _pump_detector is None:
        _pump_detector = PumpDetector()
    return _pump_detector


def detect_pump(ohlcv: list, direction: str = "short") -> PumpDetectionResult:
    """Удобная функция для быстрой детекции"""
    detector = get_pump_detector()
    return detector.detect(ohlcv, direction)
