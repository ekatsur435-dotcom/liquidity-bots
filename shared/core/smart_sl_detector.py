"""
Smart Multi-Timeframe SL Detector v1.0
Умный детектор стоп-лосса на основе анализа свечей манипуляции.

Логика:
  1. Сканируем 4 таймфрейма: 15m, 30m, 1h, 4h
  2. Ищем свечи-манипуляции (с длинными хвостами) за 20 свечей назад
  3. Скорим каждую свечу по критериям:
     - Длина хвоста (≥2× тела = идеал)
     - Объём (всплеск при манипуляции)
     - Свежесть (ближе к входу = лучше)
     - Размер тела (пин-бар = сильный сигнал)
  4. Группируем по близким ценам (кластеры)
  5. Выбираем лучший кластер (макс суммарный скор)
  6. SL = за хвост + буфер ATR×0.3
  7. Лимиты: мин 0.8%, макс 4%

Fallback: если нет свечей = entry ± ATR×1.5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger("smart_sl_detector")


@dataclass
class SLConfig:
    """Конфигурация Smart SL Detector"""
    lookback: int = 20                  # Свечей назад для поиска
    min_wick_ratio: float = 1.5         # Мин хвост/тело для манипуляции
    atr_multiplier: float = 0.3         # Буфер ATR за хвост
    max_sl_pct: float = 4.0             # Макс SL в процентах
    min_sl_pct: float = 0.8             # Мин SL в процентах
    cluster_threshold: float = 0.5      # Группировка по цене (±%)
    
    # Веса для скоринга
    weight_wick: float = 40.0           # Длина хвоста
    weight_volume: float = 25.0         # Объём
    weight_freshness: float = 20.0      # Свежесть (0-20)
    weight_body: float = 15.0           # Размер тела
    
    # Бонусы таймфрейма
    tf_bonus_4h: float = 15.0
    tf_bonus_1h: float = 10.0
    tf_bonus_30m: float = 5.0


@dataclass
class ManipulationCandle:
    """Свеча манипуляции"""
    tf: str                           # Таймфрейм (15m, 30m, 1h, 4h)
    timestamp: int                    # Время свечи
    candles_ago: int                  # Сколько свечей назад
    
    open_price: float
    high: float
    low: float
    close: float
    volume: float
    volume_avg: float                 # Средний объём за период
    
    # Параметры манипуляции
    wick_size: float                  # Размер хвоста
    body_size: float                  # Размер тела
    wick_ratio: float                 # Хвост/тело
    volume_ratio: float               # Объём/средний
    
    # Расчёт SL
    sl_price: float                   # Цена SL (за хвост + буфер)
    sl_distance_pct: float            # Дистанция от текущей цены (%)
    
    # Скор
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class SLCluster:
    """Кластер свечей манипуляции"""
    center_price: float               # Центр кластера
    sl_candidates: List[ManipulationCandle] = field(default_factory=list)
    total_score: float = 0.0
    
    def min_sl_price(self, side: str) -> float:
        """Минимальная цена SL для кластера"""
        if not self.sl_candidates:
            return 0.0
        if side == "long":
            return min(c.sl_price for c in self.sl_candidates)
        else:
            return max(c.sl_price for c in self.sl_candidates)


@dataclass
class SLResult:
    """Результат расчёта SL"""
    symbol: str
    side: str
    entry_price: float
    
    sl_price: float                   # Итоговая цена SL
    sl_distance_pct: float            # Дистанция в процентах
    
    based_on: str                     # На чём основан SL (manipulation/fallback)
    manipulation_candle: Optional[ManipulationCandle] = None
    cluster_info: Optional[SLCluster] = None
    
    # Детали
    all_candidates: List[ManipulationCandle] = field(default_factory=list)
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    
    is_valid: bool = False


class SmartSLDetector:
    """
    Умный детектор стоп-лосса.
    Анализирует свечи манипуляции на 4 таймфреймах.
    """
    
    def __init__(self, config: Optional[SLConfig] = None):
        self.cfg = config or SLConfig()
        
    def find_manipulation_candles(self,
                                     candles: List[Dict],
                                     tf: str,
                                     side: str,
                                     current_price: float
                                    ) -> List[ManipulationCandle]:
        """
        Найти свечи манипуляции в списке свечей.
        
        Args:
            candles: Список свечей (open, high, low, close, volume, timestamp)
            tf: Таймфрейм (15m, 30m, 1h, 4h)
            side: "long" или "short"
            current_price: Текущая цена для расчёта дистанции
        """
        if len(candles) < 5:
            return []
        
        candidates = []
        
        # Средний объём за период
        avg_volume = sum(c.get("volume", 0) for c in candles) / len(candles)
        
        for i, candle in enumerate(candles):
            o = candle["open"]
            h = candle["high"]
            l = candle["low"]
            c = candle["close"]
            v = candle.get("volume", 0)
            ts = candle.get("timestamp", 0)
            
            body_size = abs(c - o)
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            
            # Для LONG ищем свечи с длинным НИЖНИМ хвостом (sweep стопов снизу)
            if side == "long":
                if lower_wick < body_size * self.cfg.min_wick_ratio:
                    continue
                wick_size = lower_wick
                sl_price = l - (body_size * self.cfg.atr_multiplier)
                
            # Для SHORT ищем свечи с длинным ВЕРХНИМ хвостом (sweep стопов сверху)
            else:
                if upper_wick < body_size * self.cfg.min_wick_ratio:
                    continue
                wick_size = upper_wick
                sl_price = h + (body_size * self.cfg.atr_multiplier)
            
            # Расчёт показателей
            wick_ratio = wick_size / body_size if body_size > 0 else 0
            volume_ratio = v / avg_volume if avg_volume > 0 else 1
            
            # Расчёт скора
            score = 0
            reasons = []
            
            # 1. Длина хвоста (max 40)
            wick_score = min(self.cfg.weight_wick, wick_ratio * 20)
            score += wick_score
            reasons.append(f"wick={wick_ratio:.1f}x")
            
            # 2. Объём (max 25)
            vol_score = min(self.cfg.weight_volume, volume_ratio * 12.5)
            score += vol_score
            reasons.append(f"vol={volume_ratio:.1f}x")
            
            # 3. Свежесть (max 20) - чем ближе к входу, тем лучше
            freshness = max(0, self.cfg.weight_freshness - (i * 1))
            score += freshness
            
            # 4. Размер тела (max 15) - крошечное тело = сильный сигнал
            if body_size < (h - l) * 0.1:  # Тело < 10% диапазона
                score += self.cfg.weight_body
                reasons.append("pinbar")
            
            # Бонус таймфрейма
            if tf == "4h":
                score += self.cfg.tf_bonus_4h
            elif tf == "1h":
                score += self.cfg.tf_bonus_1h
            elif tf == "30m":
                score += self.cfg.tf_bonus_30m
            
            # Расчёт дистанции SL
            sl_distance = abs(sl_price - current_price) / current_price * 100
            
            candidates.append(ManipulationCandle(
                tf=tf,
                timestamp=ts,
                candles_ago=i,
                open_price=o,
                high=h,
                low=l,
                close=c,
                volume=v,
                volume_avg=avg_volume,
                wick_size=wick_size,
                body_size=body_size,
                wick_ratio=wick_ratio,
                volume_ratio=volume_ratio,
                sl_price=sl_price,
                sl_distance_pct=sl_distance,
                score=score,
                reasons=reasons
            ))
        
        return candidates
    
    def cluster_candidates(self, candidates: List[ManipulationCandle], side: str) -> List[SLCluster]:
        """Группировать кандидатов по близким ценам"""
        if not candidates:
            return []
        
        # Сортируем по цене SL
        sorted_candidates = sorted(candidates, key=lambda c: c.sl_price)
        
        clusters = []
        current_cluster = [sorted_candidates[0]]
        
        for cand in sorted_candidates[1:]:
            # Проверяем расстояние до центра текущего кластера
            cluster_center = sum(c.sl_price for c in current_cluster) / len(current_cluster)
            distance = abs(cand.sl_price - cluster_center) / cluster_center * 100
            
            if distance <= self.cfg.cluster_threshold:
                current_cluster.append(cand)
            else:
                # Сохраняем текущий кластер
                center = sum(c.sl_price for c in current_cluster) / len(current_cluster)
                total_score = sum(c.score for c in current_cluster)
                clusters.append(SLCluster(
                    center_price=center,
                    sl_candidates=current_cluster,
                    total_score=total_score
                ))
                # Начинаем новый
                current_cluster = [cand]
        
        # Добавляем последний кластер
        if current_cluster:
            center = sum(c.sl_price for c in current_cluster) / len(current_cluster)
            total_score = sum(c.score for c in current_cluster)
            clusters.append(SLCluster(
                center_price=center,
                sl_candidates=current_cluster,
                total_score=total_score
            ))
        
        return clusters
    
    def calculate_sl(self,
                      symbol: str,
                      side: str,
                      entry_price: float,
                      candles_15m: Optional[List[Dict]] = None,
                      candles_30m: Optional[List[Dict]] = None,
                      candles_1h: Optional[List[Dict]] = None,
                      candles_4h: Optional[List[Dict]] = None,
                      atr: Optional[float] = None
                     ) -> SLResult:
        """
        Рассчитать оптимальный стоп-лосс.
        
        Args:
            symbol: Торговая пара
            side: "long" или "short"
            entry_price: Цена входа
            candles_15m: Свечи 15m (опционально)
            candles_30m: Свечи 30m (опционально)
            candles_1h: Свечи 1h (опционально)
            candles_4h: Свечи 4h (опционально)
            atr: ATR для fallback
        """
        
        reasons = []
        all_candidates = []
        
        # Собираем свечи манипуляции со всех таймфреймов
        tf_data = [
            ("15m", candles_15m),
            ("30m", candles_30m),
            ("1h", candles_1h),
            ("4h", candles_4h)
        ]
        
        for tf, candles in tf_data:
            if candles and len(candles) >= 5:
                candidates = self.find_manipulation_candles(
                    candles[:self.cfg.lookback], tf, side, entry_price
                )
                all_candidates.extend(candidates)
                reasons.append(f"{tf}: {len(candidates)} candidates")
        
        if not all_candidates:
            # Fallback на ATR
            if atr:
                if side == "long":
                    sl_price = entry_price * (1 - atr * 1.5 / 100)
                else:
                    sl_price = entry_price * (1 + atr * 1.5 / 100)
                
                sl_distance = abs(sl_price - entry_price) / entry_price * 100
                
                # Проверяем лимиты
                if sl_distance < self.cfg.min_sl_pct:
                    sl_distance = self.cfg.min_sl_pct
                    sl_price = entry_price * (1 - sl_distance/100) if side == "long" else entry_price * (1 + sl_distance/100)
                if sl_distance > self.cfg.max_sl_pct:
                    sl_distance = self.cfg.max_sl_pct
                    sl_price = entry_price * (1 - sl_distance/100) if side == "long" else entry_price * (1 + sl_distance/100)
                
                logger.info(f"[SMART-SL] {symbol}: Fallback ATR×1.5 | SL={sl_price:.6f} ({-sl_distance:.2f}%)")
                
                return SLResult(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    sl_distance_pct=sl_distance,
                    based_on="fallback_atr",
                    all_candidates=[],
                    score=0,
                    reasons=["No manipulation candles found, using ATR fallback"],
                    is_valid=True
                )
            else:
                return SLResult(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    sl_price=0,
                    sl_distance_pct=0,
                    based_on="none",
                    reasons=["No candles and no ATR provided"],
                    is_valid=False
                )
        
        # Кластеризуем
        clusters = self.cluster_candidates(all_candidates, side)
        
        if not clusters:
            return SLResult(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                sl_price=0,
                sl_distance_pct=0,
                based_on="none",
                all_candidates=all_candidates,
                reasons=["Clustering failed"],
                is_valid=False
            )
        
        # Выбираем лучший кластер (макс суммарный скор)
        best_cluster = max(clusters, key=lambda c: c.total_score)
        
        # Для LONG берём минимальный SL из кластера (самый низкий)
        # Для SHORT берём максимальный SL из кластера (самый высокий)
        sl_price = best_cluster.min_sl_price(side)
        
        # Добавляем буфер ATR
        if atr:
            buffer = atr * self.cfg.atr_multiplier
            if side == "long":
                sl_price = sl_price - (entry_price * buffer / 100)
            else:
                sl_price = sl_price + (entry_price * buffer / 100)
        
        sl_distance = abs(sl_price - entry_price) / entry_price * 100
        
        # Применяем лимиты
        if sl_distance < self.cfg.min_sl_pct:
            sl_distance = self.cfg.min_sl_pct
            sl_price = entry_price * (1 - sl_distance/100) if side == "long" else entry_price * (1 + sl_distance/100)
            reasons.append(f"Adjusted to min {self.cfg.min_sl_pct}%")
        
        if sl_distance > self.cfg.max_sl_pct:
            sl_distance = self.cfg.max_sl_pct
            sl_price = entry_price * (1 - sl_distance/100) if side == "long" else entry_price * (1 + sl_distance/100)
            reasons.append(f"Adjusted to max {self.cfg.max_sl_pct}%")
        
        # Находим лучшую свечу в кластере для логирования
        if side == "long":
            best_candle = min(best_cluster.sl_candidates, key=lambda c: c.sl_price)
        else:
            best_candle = max(best_cluster.sl_candidates, key=lambda c: c.sl_price)
        
        log_msg = (f"🎯 [SMART-SL] {symbol}: SL={sl_price:.6f} ({-sl_distance:.2f}%) | "
                  f"Cluster score: {best_cluster.total_score:.0f} | "
                  f"Based on {best_candle.tf} {best_candle.candles_ago} candles ago | "
                  f"wick={best_candle.wick_ratio:.1f}x vol={best_candle.volume_ratio:.1f}x")
        logger.info(log_msg)
        
        return SLResult(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            sl_price=sl_price,
            sl_distance_pct=sl_distance,
            based_on="manipulation_candle",
            manipulation_candle=best_candle,
            cluster_info=best_cluster,
            all_candidates=all_candidates,
            score=best_cluster.total_score,
            reasons=reasons,
            is_valid=True
        )


# Singleton instance
_sl_detector: Optional[SmartSLDetector] = None

def get_smart_sl_detector(config: Optional[SLConfig] = None) -> SmartSLDetector:
    """Get or create SmartSLDetector singleton"""
    global _sl_detector
    if _sl_detector is None:
        _sl_detector = SmartSLDetector(config)
    return _sl_detector
