"""
Grid DCA (Grid Dollar Cost Averaging) v1.0
Сеточное усреднение позиций - "до кубка"

Логика:
  1. Создаём сетку уровней от текущей цены до целевой (кубка)
  2. Каждый уровень = точка добавки фиксированного размера
  3. Kelly Criterion распределяет общий бюджет по уровням
  4. Используем только лимитные ордера

Сетка (пример для лонга, цена 100, кубок 80):
  Уровень 1: 96  (добавка 25% от Kelly)
  Уровень 2: 92  (добавка 25% от Kelly)
  Уровень 3: 88  (добавка 25% от Kelly)
  Уровень 4: 84  (добавка 25% от Kelly)
  Уровень 5: 80  (STOP - не добавляем, это кубок)

Фильтры:
  - Структура не сломана (swing high/low)
  - CVD не показывает сильное противодавление
  - Объём не аномально высокий (не ловим нож)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("grid_dca")


@dataclass
class GridDCAConfig:
    """Конфигурация Grid DCA"""
    max_levels: int = 5                # Максимум уровней сетки
    grid_step_pct: float = 2.0         # Шаг сетки в процентах
    
    # Распределение размеров (должно суммироваться в 1.0)
    level_weights: List[float] = field(default_factory=lambda: [0.35, 0.30, 0.20, 0.10, 0.05])
    
    # Фильтры
    min_distance_from_entry: float = 1.0  # Мин откат от входа для старта сетки (%)
    max_total_risk: float = 3.0          # Макс общий риск (% от портфеля)
    
    # CVD фильтр
    cvd_max_opposite: float = 70.0       # Макс противоположное CVD давление
    
    # Объём фильтр (не ловим нож)
    max_volume_spike: float = 3.0        # Макс всплеск объёма относительно среднего


@dataclass
class GridLevel:
    """Один уровень сетки"""
    level: int                         # Номер уровня (1-based)
    price: float                       # Цена уровня
    weight: float                      # Вес уровня (0.0-1.0)
    size_usd: float                  # Размер в USDT
    active: bool = True              # Активен ли уровень
    filled: bool = False             # Заполнен ли
    order_id: Optional[str] = None   # ID ордера на бирже


@dataclass
class GridDCAResult:
    """Результат создания сетки"""
    symbol: str
    side: str                          # "long" или "short"
    levels: List[GridLevel] = field(default_factory=list)
    total_size_usd: float = 0.0
    total_risk_pct: float = 0.0
    is_valid: bool = False
    reasons: List[str] = field(default_factory=list)


@dataclass
class MarketContext:
    """Рыночный контекст для сетки"""
    current_price: float
    swing_low: Optional[float] = None   # Для лонга - куда планируем дойти
    swing_high: Optional[float] = None  # Для шорта
    cvd_value: Optional[float] = None # 0-100 давление
    volume_spike: Optional[float] = None  # Отношение текущего к среднему
    structure_broken: bool = False      # Сломана ли структура


class GridDCA:
    """
    Сеточная система усреднения "до кубка".
    Создаёт уровни от текущей цены до целевой с равномерным распределением.
    """
    
    def __init__(self, config: Optional[GridDCAConfig] = None):
        self.cfg = config or GridDCAConfig()
        
    def calculate_grid_levels(self,
                              symbol: str,
                              side: str,
                              entry_price: float,
                              context: MarketContext,
                              kelly_size: float,  # Размер по Kelly
                              sl_distance: float  # Дистанция до SL в %
                              ) -> GridDCAResult:
        """
        Создать сетку уровней для усреднения.
        
        Args:
            symbol: Символ
            side: "long" или "short"
            entry_price: Цена входа
            context: Рыночный контекст
            kelly_size: Рекомендуемый размер по Kelly
            sl_distance: Дистанция до стопа в процентах
        """
        
        reasons = []
        levels = []
        
        # Определяем целевую цену (кубок)
        if side == "long":
            target_price = context.swing_low or (entry_price * (1 - sl_distance/100))
        else:
            target_price = context.swing_high or (entry_price * (1 + sl_distance/100))
        
        # Проверяем минимальное расстояние от входа
        price_change = abs(context.current_price - entry_price) / entry_price * 100
        if price_change < self.cfg.min_distance_from_entry:
            reasons.append(f"Price change {price_change:.2f}% < min {self.cfg.min_distance_from_entry}%")
            return GridDCAResult(
                symbol=symbol, side=side, levels=[], 
                is_valid=False, reasons=reasons
            )
        
        # Проверяем CVD
        if context.cvd_value is not None:
            if side == "long" and context.cvd_value < 30:  # Сильное sell pressure
                reasons.append(f"CVD sell pressure {context.cvd_value:.0f} < 30")
                return GridDCAResult(symbol=symbol, side=side, levels=[], is_valid=False, reasons=reasons)
            if side == "short" and context.cvd_value > 70:  # Сильное buy pressure
                reasons.append(f"CVD buy pressure {context.cvd_value:.0f} > 70")
                return GridDCAResult(symbol=symbol, side=side, levels=[], is_valid=False, reasons=reasons)
        
        # Проверяем объём (не ловим нож)
        if context.volume_spike and context.volume_spike > self.cfg.max_volume_spike:
            reasons.append(f"Volume spike {context.volume_spike:.1f}x > max {self.cfg.max_volume_spike}")
            return GridDCAResult(symbol=symbol, side=side, levels=[], is_valid=False, reasons=reasons)
        
        # Создаём уровни
        total_distance = abs(target_price - context.current_price)
        step = total_distance / self.cfg.max_levels
        
        for i in range(self.cfg.max_levels):
            level_num = i + 1
            
            # Цена уровня
            if side == "long":
                level_price = context.current_price - (step * i)
            else:
                level_price = context.current_price + (step * i)
            
            # Не создаём уровень если цена ниже/выше кубка
            if side == "long" and level_price <= target_price:
                break
            if side == "short" and level_price >= target_price:
                break
            
            # Вес уровня
            weight = self.cfg.level_weights[i] if i < len(self.cfg.level_weights) else 0.05
            size = kelly_size * weight
            
            level = GridLevel(
                level=level_num,
                price=round(level_price, 8),
                weight=weight,
                size_usd=round(size, 2)
            )
            levels.append(level)
        
        if not levels:
            reasons.append("No valid levels created")
            return GridDCAResult(symbol=symbol, side=side, levels=[], is_valid=False, reasons=reasons)
        
        total_size = sum(l.size_usd for l in levels)
        total_risk = sum(l.size_usd for l in levels) * (sl_distance / 100)
        
        if total_risk > self.cfg.max_total_risk:
            reasons.append(f"Total risk {total_risk:.2f}% > max {self.cfg.max_total_risk}%")
            # Масштабируем размеры
            scale = self.cfg.max_total_risk / total_risk
            for level in levels:
                level.size_usd = round(level.size_usd * scale, 2)
            total_size = sum(l.size_usd for l in levels)
            total_risk = self.cfg.max_total_risk
            reasons.append(f"Scaled down by {scale:.2f}x")
        
        reasons.append(f"Created {len(levels)} levels from {context.current_price:.4f} to {target_price:.4f}")
        
        logger.info(f"[GRID-DCA] {symbol}: {len(levels)} levels | Total: ${total_size:.0f} | Risk: {total_risk:.2f}%")
        
        return GridDCAResult(
            symbol=symbol,
            side=side,
            levels=levels,
            total_size_usd=total_size,
            total_risk_pct=total_risk,
            is_valid=True,
            reasons=reasons
        )
    
    def get_active_levels(self, result: GridDCAResult) -> List[GridLevel]:
        """Получить активные (не заполненные) уровни"""
        return [l for l in result.levels if l.active and not l.filled]
    
    def mark_level_filled(self, result: GridDCAResult, level_num: int, order_id: str):
        """Отметить уровень как заполненный"""
        for level in result.levels:
            if level.level == level_num:
                level.filled = True
                level.order_id = order_id
                logger.info(f"[GRID-DCA] Level {level_num} filled @ {level.price:.4f}")
                break


# Singleton instance
_grid_dca: Optional[GridDCA] = None

def get_grid_dca(config: Optional[GridDCAConfig] = None) -> GridDCA:
    """Get or create GridDCA singleton"""
    global _grid_dca
    if _grid_dca is None:
        _grid_dca = GridDCA(config)
    return _grid_dca
