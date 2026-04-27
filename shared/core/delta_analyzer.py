"""
Delta Analyzer v1.0 (from Aegis HARD BOT)
CVD (Cumulative Volume Delta) + Order Flow Imbalance Detection

Анализ потока ордеров:
  1. CVD calculation (Delta накопление)
  2. Order flow imbalance (перевес покупок/продаж)
  3. Bearish/Bullish divergence detection
  4. Volume profile analysis

Сигналы:
  BEARISH DIVERGENCE: Цена растёт, CVD падает = слабость = SHORT setup
  BULLISH DIVERGENCE: Цена падает, CVD растёт = накопление = LONG setup
  
Вес в скоринге: 10%
Эксклюзив Aegis — нет в Lite v7
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delta_analyzer")


@dataclass
class DeltaConfig:
    """Конфигурация Delta Analyzer"""
    lookback_candles: int = 20      # Свечей для анализа
    divergence_threshold: float = 3.0  # Минимум % расхождения для divergence
    imbalance_threshold: float = 1.5  # Порог имбаланса (1.5 = 60/40)


@dataclass
class DeltaAnalysisResult:
    """Результат анализа дельты"""
    score: float           # 0-100 для интеграции в скорер
    cvd: float            # Текущее значение CVD
    cvd_change: float     # Изменение CVD за lookback
    price_change: float  # Изменение цены за lookback
    divergence: str       # "bullish", "bearish", "none"
    divergence_strength: float  # 0-1
    imbalance_ratio: float  # buy_vol / sell_vol
    imbalance_direction: str  # "buy", "sell", "neutral"
    hourly_deltas: List[float]  # Дельта по часам
    reasons: List[str]
    metadata: Dict[str, Any]


class DeltaAnalyzer:
    """
    Анализатор дельты/потока ордеров.
    Использует CVD для определения силы тренда и дивергенций.
    """

    def __init__(self, config: Optional[DeltaConfig] = None):
        self.cfg = config or DeltaConfig()

    def _estimate_delta(self, ohlcv: list) -> float:
        """
        Оценка дельты (buy - sell volume) по свече.
        Используем формулу: delta = volume * (2*close - high - low) / (high - low)
        """
        if len(ohlcv) == 0:
            return 0.0
        
        candle = ohlcv[-1]  # [open, high, low, close, volume]
        o, h, l, c, v = candle[0], candle[1], candle[2], candle[3], candle[4]
        
        if h == l or v == 0:
            return 0.0
        
        # Оценка: где в диапазоне закрылся
        # Close ближе к high = больше покупок
        # Close ближе к low = больше продаж
        range_size = h - l
        position = (c - l) / range_size  # 0 = low, 1 = high
        
        # Дельта: -1.0 (всё продажи) до +1.0 (всё покупки)
        delta = (2 * position - 1) * v
        
        return delta

    def _calculate_cvd(self, ohlcv: list, lookback: int) -> Tuple[float, List[float]]:
        """
        Расчёт CVD (Cumulative Volume Delta) за lookback свечей.
        
        Returns:
            (текущий CVD, список hourly deltas)
        """
        if len(ohlcv) < lookback:
            lookback = len(ohlcv)
        
        recent = ohlcv[-lookback:]
        cvd = 0.0
        hourly_deltas = []
        
        for candle in recent:
            delta = self._estimate_delta([candle])
            cvd += delta
            hourly_deltas.append(delta)
        
        return cvd, hourly_deltas

    def _detect_divergence(
        self, 
        price_start: float, 
        price_end: float,
        cvd_start: float,
        cvd_end: float,
        direction: str = "short"
    ) -> Tuple[str, float]:
        """
        Детекция дивергенции цены и CVD.
        
        Bearish divergence (для SHORT):
            Цена растёт (high >), но CVD падает = покупатели слабеют
        
        Bullish divergence (для LONG):
            Цена падает (low <), но CVD растёт = продавцы слабеют
        
        Returns:
            (тип дивергенции, сила 0-1)
        """
        price_change = (price_end - price_start) / price_start * 100
        cvd_change = cvd_end - cvd_start  # Нормализованное изменение
        
        divergence_type = "none"
        strength = 0.0
        
        if direction == "short":
            # Bearish divergence: цена растёт, CVD падает
            if price_change > 2.0 and cvd_change < -self.cfg.divergence_threshold:
                divergence_type = "bearish"
                # Сила зависит от расхождения
                strength = min(1.0, abs(price_change) / 10 + abs(cvd_change) / 10)
        else:  # long
            # Bullish divergence: цена падает, CVD растёт
            if price_change < -2.0 and cvd_change > self.cfg.divergence_threshold:
                divergence_type = "bullish"
                strength = min(1.0, abs(price_change) / 10 + cvd_change / 10)
        
        return divergence_type, strength

    def _analyze_imbalance(self, hourly_deltas: List[float]) -> Tuple[float, str]:
        """
        Анализ имбаланса покупок/продаж.
        
        Returns:
            (ratio buy/sell, direction)
        """
        if not hourly_deltas:
            return 1.0, "neutral"
        
        buy_vol = sum(d for d in hourly_deltas if d > 0)
        sell_vol = abs(sum(d for d in hourly_deltas if d < 0))
        
        if sell_vol == 0:
            return 10.0, "buy"  # Всё покупки
        if buy_vol == 0:
            return 0.1, "sell"  # Всё продажи
        
        ratio = buy_vol / sell_vol
        
        if ratio > self.cfg.imbalance_threshold:
            direction = "buy"
        elif ratio < 1 / self.cfg.imbalance_threshold:
            direction = "sell"
        else:
            direction = "neutral"
        
        return ratio, direction

    def analyze(
        self, 
        ohlcv: list, 
        direction: str = "short",
        market_data: Optional[Any] = None
    ) -> DeltaAnalysisResult:
        """
        Полный анализ дельты и потока ордеров.
        
        Args:
            ohlcv: Список свечей [open, high, low, close, volume]
            direction: "short" или "long"
            market_data: Опциональные рыночные данные
        
        Returns:
            DeltaAnalysisResult со score и метаданными
        """
        if len(ohlcv) < self.cfg.lookback_candles:
            return DeltaAnalysisResult(
                score=0.0, cvd=0.0, cvd_change=0.0, price_change=0.0,
                divergence="none", divergence_strength=0.0,
                imbalance_ratio=1.0, imbalance_direction="neutral",
                hourly_deltas=[], reasons=["Insufficient data"], metadata={}
            )

        # CVD calculation
        cvd, hourly_deltas = self._calculate_cvd(ohlcv, self.cfg.lookback_candles)
        
        # Price change over same period
        price_start = ohlcv[-self.cfg.lookback_candles][3]  # close
        price_end = ohlcv[-1][3]  # current close
        price_change = (price_end - price_start) / price_start * 100
        
        # CVD change (нормализовано)
        avg_volume = sum(c[4] for c in ohlcv[-self.cfg.lookback_candles:]) / self.cfg.lookback_candles
        cvd_change = (cvd / avg_volume * 100) if avg_volume > 0 else 0
        
        # Divergence detection
        div_type, div_strength = self._detect_divergence(
            price_start, price_end, 0, cvd, direction
        )
        
        # Imbalance analysis
        imb_ratio, imb_dir = self._analyze_imbalance(hourly_deltas)
        
        # Score calculation (0-100)
        score = 0.0
        reasons = []
        
        if direction == "short":
            # Для SHORT: ищем bearish signals
            if div_type == "bearish":
                score += 40 * div_strength
                reasons.append(f"🔴 Bearish divergence: price +{price_change:.1f}% vs CVD {cvd_change:.1f} (strength {div_strength:.0%})")
            
            # Имбаланс в пользу продаж (ratio < 1)
            if imb_dir == "sell":
                imb_score = min(30, (1 / imb_ratio - 1) * 20)
                score += imb_score
                reasons.append(f"📊 Sell imbalance: {1/imb_ratio:.1f}:1 (score +{imb_score:.0f})")
            
            # Негативные дельты в большинстве часов
            neg_hours = sum(1 for d in hourly_deltas if d < 0)
            if neg_hours >= len(hourly_deltas) * 0.7:  # 70%+ negative
                score += 20
                reasons.append(f"⬇️ {neg_hours}/{len(hourly_deltas)}h negative delta")
            
            # Цена растёт, но дельта отрицательная = слабость
            if price_change > 5.0 and cvd < 0:
                score += 25
                reasons.append(f"⚠️ Price rising on weak volume (+{price_change:.1f}%, CVD {cvd:.0f})")
        
        else:  # direction == "long"
            # Для LONG: ищем bullish signals
            if div_type == "bullish":
                score += 40 * div_strength
                reasons.append(f"🟢 Bullish divergence: price {price_change:.1f}% vs CVD +{cvd_change:.1f}")
            
            # Имбаланс в пользу покупок
            if imb_dir == "buy":
                imb_score = min(30, (imb_ratio - 1) * 20)
                score += imb_score
                reasons.append(f"📊 Buy imbalance: {imb_ratio:.1f}:1 (score +{imb_score:.0f})")
            
            # Позитивные дельты в большинстве часов
            pos_hours = sum(1 for d in hourly_deltas if d > 0)
            if pos_hours >= len(hourly_deltas) * 0.7:
                score += 20
                reasons.append(f"⬆️ {pos_hours}/{len(hourly_deltas)}h positive delta")
            
            # Цена падает, но дельта позитивная = накопление
            if price_change < -5.0 and cvd > 0:
                score += 25
                reasons.append(f"💪 Price falling on accumulation ({price_change:.1f}%, CVD +{cvd:.0f})")
        
        score = min(100.0, score)
        
        if not reasons:
            reasons.append(f"Neutral delta: CVD={cvd:.0f}, imbalance={imb_ratio:.2f}")
        
        return DeltaAnalysisResult(
            score=round(score, 1),
            cvd=round(cvd, 2),
            cvd_change=round(cvd_change, 2),
            price_change=round(price_change, 2),
            divergence=div_type,
            divergence_strength=round(div_strength, 2),
            imbalance_ratio=round(imb_ratio, 2),
            imbalance_direction=imb_dir,
            hourly_deltas=[round(d, 2) for d in hourly_deltas[-6:]],  # Последние 6 часов
            reasons=reasons,
            metadata={
                "lookback": self.cfg.lookback_candles,
                "avg_hourly_volume": round(avg_volume, 2) if 'avg_volume' in dir() else 0
            }
        )


# Singleton instance
_delta_analyzer: Optional[DeltaAnalyzer] = None


def get_delta_analyzer() -> DeltaAnalyzer:
    """Получить singleton instance"""
    global _delta_analyzer
    if _delta_analyzer is None:
        _delta_analyzer = DeltaAnalyzer()
    return _delta_analyzer


def analyze_delta(ohlcv: list, direction: str = "short") -> DeltaAnalysisResult:
    """Удобная функция для быстрого анализа"""
    analyzer = get_delta_analyzer()
    return analyzer.analyze(ohlcv, direction)
