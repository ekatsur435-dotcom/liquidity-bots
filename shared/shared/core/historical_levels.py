"""
Historical Level Analyzer v1.0 — Phase 1 Implementation
Анализ исторических уровней поддержки/сопротивления с подсчётом касаний

Основные функции:
- Сканирование 90-180 дней истории (1h/4h свечи)
- Определение ключевых зон с 3+ касаниями
- "Zone Strength Score" — чем больше касаний, тем сильнее уровень
- "Breakout Probability" — вероятность пробоя растёт с каждым касанием
- Интеграция с LONG/SHORT ботами для улучшения точности входов

Для LONG: ищем поддержки с 3+ касаниями для входа
Для SHORT: ищем сопротивления с 3+ касаниями
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import defaultdict
import numpy as np


@dataclass
class HistoricalLevel:
    """Исторический уровень (поддержка или сопротивление)"""
    level_type: str           # "support" | "resistance"
    price: float              # Цена уровня
    zone_lower: float         # Нижняя граница зоны
    zone_upper: float         # Верхняя граница зоны
    touch_count: int          # Количество касаний
    touches: List[dict] = field(default_factory=list)  # Детали касаний
    first_touch_date: str = ""  # Дата первого касания
    last_touch_date: str = ""   # Дата последнего касания
    
    @property
    def zone_width_pct(self) -> float:
        """Ширина зоны в процентах"""
        if self.price == 0:
            return 0.0
        return (self.zone_upper - self.zone_lower) / self.price * 100
    
    @property
    def strength_score(self) -> int:
        """Оценка силы уровня (0-100)"""
        base_score = min(40, self.touch_count * 10)  # 3 касания = 30, 4+ = 40
        
        # Бонус за "усталость" уровня (много касаний = высокая вероятность пробоя)
        exhaustion_bonus = min(30, (self.touch_count - 2) * 5) if self.touch_count > 2 else 0
        
        # Бонус за узкую зону (чёткий уровень)
        precision_bonus = max(0, 15 - int(self.zone_width_pct * 2))
        
        return min(100, base_score + exhaustion_bonus + precision_bonus)
    
    @property
    def breakout_probability(self) -> float:
        """Вероятность пробоя уровня"""
        # 1 касание = 15%, 2 касания = 35%, 3+ = 60%+
        if self.touch_count == 1:
            return 0.15
        elif self.touch_count == 2:
            return 0.35
        elif self.touch_count >= 3:
            return min(0.85, 0.60 + (self.touch_count - 3) * 0.08)
        return 0.0
    
    def is_price_near(self, price: float, tolerance_pct: float = 0.5) -> bool:
        """Проверить, находится ли цена рядом с уровнем"""
        zone_mid = (self.zone_lower + self.zone_upper) / 2
        deviation = abs(price - zone_mid) / zone_mid * 100
        return deviation <= tolerance_pct


@dataclass
class LevelAnalysisResult:
    """Результат анализа уровней для символа"""
    symbol: str
    supports: List[HistoricalLevel]
    resistances: List[HistoricalLevel]
    current_price: float
    
    def get_nearest_support(self) -> Optional[HistoricalLevel]:
        """Ближайшая поддержка ниже текущей цены"""
        valid = [s for s in self.supports if s.zone_upper < self.current_price]
        if not valid:
            return None
        return min(valid, key=lambda x: abs(self.current_price - x.price))
    
    def get_nearest_resistance(self) -> Optional[HistoricalLevel]:
        """Ближайшее сопротивление выше текущей цены"""
        valid = [r for r in self.resistances if r.zone_lower > self.current_price]
        if not valid:
            return None
        return min(valid, key=lambda x: abs(self.current_price - x.price))
    
    def get_active_levels_near_price(self, tolerance_pct: float = 1.0) -> List[HistoricalLevel]:
        """Все уровни рядом с текущей ценой"""
        nearby = []
        for level in self.supports + self.resistances:
            if level.is_price_near(self.current_price, tolerance_pct):
                nearby.append(level)
        return sorted(nearby, key=lambda x: x.strength_score, reverse=True)


class HistoricalLevelAnalyzer:
    """
    Анализатор исторических уровней
    
    Использование:
        analyzer = HistoricalLevelAnalyzer()
        result = await analyzer.analyze_symbol(
            symbol="BTCUSDT",
            binance_client=state.binance,
            lookback_days=90,
            timeframe="1h"
        )
        
        # Для LONG входа:
        support = result.get_nearest_support()
        if support and support.touch_count >= 3:
            print(f"Сильная поддержка: {support.price} (касаний: {support.touch_count})")
    """
    
    def __init__(self):
        self.cache: Dict[str, LevelAnalysisResult] = {}
        self.cache_ttl_minutes = 30
        self._last_update: Dict[str, float] = {}
    
    async def analyze_symbol(
        self,
        symbol: str,
        binance_client,
        current_price: float,
        lookback_days: int = 90,
        timeframe: str = "1h",
        zone_tolerance_pct: float = 0.3,
        min_touch_count: int = 2
    ) -> LevelAnalysisResult:
        """
        Проанализировать исторические уровни для символа
        
        Args:
            symbol: Торговая пара
            binance_client: Клиент для получения свечей
            current_price: Текущая цена
            lookback_days: На сколько дней назад смотреть
            timeframe: Таймфрейм ("1h", "4h")
            zone_tolerance_pct: Допуск для определения зоны уровня
            min_touch_count: Минимальное количество касаний для включения
        """
        # Проверяем кэш
        cache_key = f"{symbol}_{timeframe}_{lookback_days}"
        import time
        now = time.time()
        if cache_key in self.cache:
            last_update = self._last_update.get(cache_key, 0)
            if (now - last_update) / 60 < self.cache_ttl_minutes:
                cached = self.cache[cache_key]
                cached.current_price = current_price  # Обновляем текущую цену
                return cached
        
        # Получаем исторические свечи
        limit = lookback_days * 24 if timeframe == "1h" else lookback_days * 6
        klines = await binance_client.get_klines(symbol, timeframe, min(limit, 1000))
        
        if not klines or len(klines) < 50:
            return LevelAnalysisResult(symbol, [], [], current_price)
        
        # Находим свинг-хаи и свинг-лоу
        swing_highs = self._find_swing_points(klines, "high", lookback=3)
        swing_lows = self._find_swing_points(klines, "low", lookback=3)
        
        # Группируем близкие уровни
        resistance_levels = self._cluster_levels(
            swing_highs, 
            "resistance", 
            zone_tolerance_pct,
            min_touch_count
        )
        support_levels = self._cluster_levels(
            swing_lows, 
            "support", 
            zone_tolerance_pct,
            min_touch_count
        )
        
        result = LevelAnalysisResult(
            symbol=symbol,
            supports=support_levels,
            resistances=resistance_levels,
            current_price=current_price
        )
        
        # Сохраняем в кэш
        self.cache[cache_key] = result
        self._last_update[cache_key] = now
        
        return result
    
    def _find_swing_points(
        self, 
        klines: List, 
        point_type: str, 
        lookback: int = 3
    ) -> List[Dict]:
        """
        Найти свинг-хаи или свинг-лоу
        
        Returns:
            Список точек: [{"price": float, "index": int, "time": str}]
        """
        points = []
        highs = [k.high for k in klines]
        lows = [k.low for k in klines]
        times = [getattr(k, 'open_time', getattr(k, 'timestamp', i)) 
                 for i, k in enumerate(klines)]
        
        for i in range(lookback, len(klines) - lookback):
            if point_type == "high":
                # Свинг-хай: выше всех соседей
                if all(highs[i] >= highs[j] for j in range(i-lookback, i)) and \
                   all(highs[i] >= highs[j] for j in range(i+1, i+lookback+1)):
                    points.append({
                        "price": highs[i],
                        "index": i,
                        "time": str(times[i])
                    })
            else:
                # Свинг-лоу: ниже всех соседей
                if all(lows[i] <= lows[j] for j in range(i-lookback, i)) and \
                   all(lows[i] <= lows[j] for j in range(i+1, i+lookback+1)):
                    points.append({
                        "price": lows[i],
                        "index": i,
                        "time": str(times[i])
                    })
        
        return points
    
    def _cluster_levels(
        self,
        points: List[Dict],
        level_type: str,
        tolerance_pct: float,
        min_touch_count: int
    ) -> List[HistoricalLevel]:
        """
        Сгруппировать близкие точки в уровни
        """
        if not points:
            return []
        
        # Сортируем по цене
        sorted_points = sorted(points, key=lambda x: x["price"])
        
        clusters = []
        current_cluster = [sorted_points[0]]
        
        for point in sorted_points[1:]:
            # Проверяем, близка ли точка к текущему кластеру
            cluster_avg = sum(p["price"] for p in current_cluster) / len(current_cluster)
            deviation = abs(point["price"] - cluster_avg) / cluster_avg * 100
            
            if deviation <= tolerance_pct:
                current_cluster.append(point)
            else:
                # Сохраняем текущий кластер и начинаем новый
                if len(current_cluster) >= min_touch_count:
                    levels = self._create_level_from_cluster(current_cluster, level_type)
                    clusters.append(levels)
                current_cluster = [point]
        
        # Не забываем последний кластер
        if len(current_cluster) >= min_touch_count:
            levels = self._create_level_from_cluster(current_cluster, level_type)
            clusters.append(levels)
        
        # Сортируем по силе (количество касаний)
        return sorted(clusters, key=lambda x: x.touch_count, reverse=True)
    
    def _create_level_from_cluster(
        self,
        cluster: List[Dict],
        level_type: str
    ) -> HistoricalLevel:
        """Создать HistoricalLevel из кластера точек"""
        prices = [p["price"] for p in cluster]
        times = [p["time"] for p in cluster]
        
        avg_price = sum(prices) / len(prices)
        zone_lower = min(prices)
        zone_upper = max(prices)
        
        return HistoricalLevel(
            level_type=level_type,
            price=avg_price,
            zone_lower=zone_lower,
            zone_upper=zone_upper,
            touch_count=len(cluster),
            touches=cluster,
            first_touch_date=min(times) if times else "",
            last_touch_date=max(times) if times else ""
        )
    
    def calculate_level_score_for_long(
        self,
        result: LevelAnalysisResult,
        entry_price: float
    ) -> Tuple[int, List[str]]:
        """
        Рассчитать бонус к скору для LONG входа на основе уровней
        
        Returns:
            (score_bonus, reasons)
        """
        score = 0
        reasons = []
        
        # Ищем поддержку рядом с ценой входа
        nearest_support = result.get_nearest_support()
        if nearest_support:
            distance_pct = abs(entry_price - nearest_support.price) / nearest_support.price * 100
            
            # Цена входа близка к сильной поддержке (3+ касания)
            if nearest_support.touch_count >= 3 and distance_pct < 1.0:
                score += 8
                reasons.append(
                    f"Уровень поддержки {nearest_support.price:.4f} "
                    f"({nearest_support.touch_count} касаний) — сильный отскок"
                )
            elif nearest_support.touch_count >= 2 and distance_pct < 0.5:
                score += 5
                reasons.append(
                    f"Поддержка {nearest_support.price:.4f} ({nearest_support.touch_count}к) рядом"
                )
            
            # Предупреждение о возможном пробое (усталый уровень)
            if nearest_support.touch_count >= 4:
                prob = nearest_support.breakout_probability
                if prob > 0.7:
                    score -= 5
                    reasons.append(
                        f"⚠️ Уровень устал ({nearest_support.touch_count}к), "
                        f"вероятность пробоя {prob*100:.0f}%"
                    )
        
        # Проверяем близость к сопротивлению (плохо для LONG)
        nearest_resistance = result.get_nearest_resistance()
        if nearest_resistance:
            distance_to_r = abs(nearest_resistance.price - entry_price) / entry_price * 100
            if distance_to_r < 2.0 and nearest_resistance.touch_count >= 2:
                score -= 3
                reasons.append(
                    f"Близко к сопротивлению {nearest_resistance.price:.4f} "
                    f"({nearest_resistance.touch_count}к) — риск отката"
                )
        
        return score, reasons
    
    def calculate_level_score_for_short(
        self,
        result: LevelAnalysisResult,
        entry_price: float
    ) -> Tuple[int, List[str]]:
        """
        Рассчитать бонус к скору для SHORT входа на основе уровней
        """
        score = 0
        reasons = []
        
        # Ищем сопротивление рядом с ценой входа
        nearest_resistance = result.get_nearest_resistance()
        if nearest_resistance:
            distance_pct = abs(entry_price - nearest_resistance.price) / nearest_resistance.price * 100
            
            # Цена входа близка к сильному сопротивлению
            if nearest_resistance.touch_count >= 3 and distance_pct < 1.0:
                score += 8
                reasons.append(
                    f"Уровень сопротивления {nearest_resistance.price:.4f} "
                    f"({nearest_resistance.touch_count} касаний) — сильный отскок вниз"
                )
            elif nearest_resistance.touch_count >= 2 and distance_pct < 0.5:
                score += 5
                reasons.append(
                    f"Сопротивление {nearest_resistance.price:.4f} ({nearest_resistance.touch_count}к) рядом"
                )
        
        # Проверяем близость к поддержке (плохо для SHORT)
        nearest_support = result.get_nearest_support()
        if nearest_support:
            distance_to_s = abs(entry_price - nearest_support.price) / entry_price * 100
            if distance_to_s < 2.0 and nearest_support.touch_count >= 2:
                score -= 3
                reasons.append(
                    f"Близко к поддержке {nearest_support.price:.4f} "
                    f"({nearest_support.touch_count}к) — риск отскока"
                )
        
        return score, reasons


# ============================================================================
# SINGLETON
# ============================================================================

_analyzer_instance: Optional[HistoricalLevelAnalyzer] = None


def get_historical_analyzer() -> HistoricalLevelAnalyzer:
    """Получить singleton экземпляр анализатора"""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = HistoricalLevelAnalyzer()
    return _analyzer_instance
