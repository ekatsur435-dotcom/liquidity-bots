"""
Volume Profile Analysis — POC (Point of Control) и зоны объема
Для определения ключевых уровней TP/SL
"""

from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
import numpy as np


@dataclass
class VolumeProfile:
    """Профиль объема"""
    poc_price: float  # Point of Control (максимальный объем)
    value_area_high: float  # Верхняя граница Value Area (70% объема)
    value_area_low: float  # Нижняя граница Value Area
    high_volume_nodes: List[float]  # Все HVN (High Volume Nodes)
    low_volume_nodes: List[float]  # LVN (Low Volume Nodes — пробойные зоны)
    volume_profile: Dict[float, float]  # {price_level: volume}


class VolumeProfileAnalyzer:
    """Анализатор профиля объема"""
    
    def __init__(self, num_bins: int = 24):
        self.num_bins = num_bins  # Количество уровней цены
    
    def calculate(self, candles: List) -> Optional[VolumeProfile]:
        """
        Рассчитать Volume Profile из OHLCV данных
        
        Args:
            candles: Список свечей с атрибутами high, low, close, volume
        
        Returns:
            VolumeProfile или None если недостаточно данных
        """
        if len(candles) < 20:
            return None
        
        try:
            # Извлекаем данные
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            closes = [c.close for c in candles]
            volumes = [c.volume for c in candles]
            
            # Диапазон цен
            min_price = min(lows)
            max_price = max(highs)
            price_range = max_price - min_price
            
            if price_range == 0:
                return None
            
            # Создаем бины (уровни цены)
            bin_size = price_range / self.num_bins
            volume_by_level = {}
            
            for i, candle in enumerate(candles):
                # Распределяем объем по уровням внутри диапазона свечи
                candle_min = candle.low
                candle_max = candle.high
                candle_volume = candle.volume
                
                # Определяем какие бины пересекает свеча
                bin_start = int((candle_min - min_price) / bin_size)
                bin_end = int((candle_max - min_price) / bin_size)
                
                bin_start = max(0, min(bin_start, self.num_bins - 1))
                bin_end = max(0, min(bin_end, self.num_bins - 1))
                
                # Распределяем объем равномерно
                bins_covered = bin_end - bin_start + 1
                vol_per_bin = candle_volume / bins_covered
                
                for bin_idx in range(bin_start, bin_end + 1):
                    price_level = min_price + (bin_idx + 0.5) * bin_size
                    volume_by_level[price_level] = volume_by_level.get(price_level, 0) + vol_per_bin
            
            if not volume_by_level:
                return None
            
            # Находим POC (максимальный объем)
            poc_price = max(volume_by_level, key=volume_by_level.get)
            total_volume = sum(volume_by_level.values())
            
            # Сортируем уровни по объему
            sorted_levels = sorted(volume_by_level.items(), key=lambda x: x[1], reverse=True)
            
            # Value Area (70% объема)
            cumulative_volume = 0
            value_area_levels = []
            for price, vol in sorted_levels:
                cumulative_volume += vol
                value_area_levels.append(price)
                if cumulative_volume >= total_volume * 0.7:
                    break
            
            value_area_high = max(value_area_levels) if value_area_levels else max_price
            value_area_low = min(value_area_levels) if value_area_levels else min_price
            
            # HVN — топ 30% уровней по объему
            num_hvn = max(1, len(sorted_levels) // 3)
            high_volume_nodes = [p for p, v in sorted_levels[:num_hvn]]
            
            # LVN — нижние 30% (редкие объемы = зоны пробоя)
            low_volume_nodes = [p for p, v in sorted_levels[-num_hvn:]]
            
            return VolumeProfile(
                poc_price=poc_price,
                value_area_high=value_area_high,
                value_area_low=value_area_low,
                high_volume_nodes=high_volume_nodes,
                low_volume_nodes=low_volume_nodes,
                volume_profile=volume_by_level
            )
            
        except Exception as e:
            print(f"[VolumeProfile] Error: {e}")
            return None
    
    def get_poc_based_levels(self, vp: VolumeProfile, current_price: float, 
                             direction: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Получить уровни TP и SL на основе POC
        
        Args:
            vp: VolumeProfile
            current_price: Текущая цена
            direction: "long" или "short"
        
        Returns:
            (tp_price, sl_price) или (None, None)
        """
        try:
            if direction == "long":
                # TP: следующий HVN выше цены или Value Area High
                tp_candidates = [p for p in vp.high_volume_nodes if p > current_price]
                if tp_candidates:
                    tp_price = min(tp_candidates)  # Ближайший HVN сверху
                else:
                    tp_price = vp.value_area_high
                
                # SL: POC или ближайший HVN ниже
                sl_candidates = [p for p in vp.high_volume_nodes if p < current_price]
                if sl_candidates:
                    sl_price = max(sl_candidates)  # Ближайший HVN снизу
                else:
                    sl_price = vp.value_area_low
                    
            else:  # short
                # TP: следующий HVN ниже цены
                tp_candidates = [p for p in vp.high_volume_nodes if p < current_price]
                if tp_candidates:
                    tp_price = max(tp_candidates)
                else:
                    tp_price = vp.value_area_low
                
                # SL: POC или ближайший HVN выше
                sl_candidates = [p for p in vp.high_volume_nodes if p > current_price]
                if sl_candidates:
                    sl_price = min(sl_candidates)
                else:
                    sl_price = vp.value_area_high
            
            return tp_price, sl_price
            
        except Exception:
            return None, None
    
    def is_price_at_poc(self, vp: VolumeProfile, current_price: float, 
                        tolerance: float = 0.005) -> bool:
        """Проверить, находится ли цена около POC (±0.5% по умолчанию)"""
        return abs(current_price - vp.poc_price) / vp.poc_price < tolerance


# Синглтон инстанс
_analyzer: Optional[VolumeProfileAnalyzer] = None


def get_volume_profile_analyzer() -> VolumeProfileAnalyzer:
    """Получить анализатор"""
    global _analyzer
    if _analyzer is None:
        _analyzer = VolumeProfileAnalyzer()
    return _analyzer
