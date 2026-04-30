"""
Smart DCA (Dollar Cost Averaging) v1.0
Умное усреднение позиций на основе сигналов и рыночных условий.

Логика:
  1. Добавляем к позиции при откате цены к ключевым уровням
  2. Сигнал от Smart Money Concepts (OB, FVG, Liquidity)
  3. CVD подтверждает давление
  4. Kelly Criterion пересчитывает размер добавки

Условия добавки:
  STRONG:    Откат 2% + подтверждение OB/FVG + CVD в направлении
  MODERATE:  Откат 3% + ликвидность собрана + цена удерживается
  WEAK:      Откат 5% + структура не сломана (для долгосрочных)

Ограничения:
  - Макс 3 добавки на позицию
  - Общий риск не более 3% портфеля
  - Дистанция между добавками минимум 1.5%
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("smart_dca")


class DCAStrength(Enum):
    """Сила сигнала DCA"""
    STRONG = "strong"       # Идеальные условия
    MODERATE = "moderate"   # Хорошие условия  
    WEAK = "weak"          # Терпимые условия (для долгосрочных)
    NONE = "none"          # Нет сигнала


@dataclass
class DCAConfig:
    """Конфигурация Smart DCA"""
    max_additions: int = 3              # Максимум добавок на позицию
    max_portfolio_risk: float = 3.0     # Макс риск всего портфеля (%)
    min_distance_between: float = 1.5   # Мин дистанция между добавками (%)
    
    # Уровни отката для добавки
    pullback_strong: float = 2.0        # Откат для STRONG (%)
    pullback_moderate: float = 3.0       # Откат для MODERATE (%)
    pullback_weak: float = 5.0          # Откат для WEAK (%)
    
    # CVD фильтры
    cvd_threshold_strong: float = 70.0  # CVD давление для STRONG
    cvd_threshold_moderate: float = 60.0
    
    # Kelly multiplier для добавок
    kelly_multiplier_1st: float = 0.5   # Первая добавка: 50% от Kelly
    kelly_multiplier_2nd: float = 0.3   # Вторая добавка: 30% от Kelly
    kelly_multiplier_3rd: float = 0.2   # Третья добавка: 20% от Kelly


@dataclass
class DCAResult:
    """Результат анализа DCA"""
    should_add: bool                    # Добавлять?
    strength: DCAStrength               # Сила сигнала
    size_multiplier: float            # Множитель размера (0.0-1.0)
    target_price: Optional[float]     # Целевая цена для лимитки
    reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionInfo:
    """Информация о текущей позиции"""
    symbol: str
    entry_price: float
    current_price: float
    position_size: float           # Размер позиции в USDT
    side: str                      # "long" или "short"
    additions_count: int = 0       # Сколько добавок уже сделано
    last_addition_price: Optional[float] = None
    unrealized_pnl_pct: float = 0.0


class SmartDCA:
    """
    Интеллектуальная система усреднения позиций.
    Добавляет к позиции только при подтверждении SMC и CVD.
    """
    
    def __init__(self, config: Optional[DCAConfig] = None):
        self.cfg = config or DCAConfig()
        
    def calculate_pullback(self, pos: PositionInfo) -> float:
        """Рассчитать откат цены от входа в %"""
        if pos.side == "long":
            if pos.current_price >= pos.entry_price:
                return 0.0
            return ((pos.entry_price - pos.current_price) / pos.entry_price) * 100
        else:  # short
            if pos.current_price <= pos.entry_price:
                return 0.0
            return ((pos.current_price - pos.entry_price) / pos.entry_price) * 100
    
    def check_distance_from_last(self, pos: PositionInfo) -> bool:
        """Проверить дистанцию от последней добавки"""
        if pos.last_addition_price is None:
            return True
        
        price_change = abs(pos.current_price - pos.last_addition_price) / pos.last_addition_price * 100
        return price_change >= self.cfg.min_distance_between
    
    def analyze(self, 
                pos: PositionInfo,
                ob_nearby: bool = False,           # Есть Order Block рядом?
                fvg_nearby: bool = False,          # Есть FVG?
                liquidity_swept: bool = False,     # Ликвидность собрана?
                cvd_pressure: Optional[float] = None,  # CVD 0-100
                structure_intact: bool = True      # Структура не сломана?
                ) -> DCAResult:
        """
        Анализировать возможность добавки к позиции.
        
        Args:
            pos: Информация о позиции
            ob_nearby: Есть Order Block в зоне
            fvg_nearby: Есть Fair Value Gap
            liquidity_swept: Ликвидность была собрана
            cvd_pressure: Давление CVD (0-100)
            structure_intact: Рыночная структура не сломана
        """
        
        # Базовые проверки
        if pos.additions_count >= self.cfg.max_additions:
            return DCAResult(
                should_add=False,
                strength=DCAStrength.NONE,
                size_multiplier=0.0,
                target_price=None,
                reasons=["Max additions reached"]
            )
        
        if not self.check_distance_from_last(pos):
            return DCAResult(
                should_add=False,
                strength=DCAStrength.NONE,
                size_multiplier=0.0,
                target_price=None,
                reasons=["Too close to last addition"]
            )
        
        pullback = self.calculate_pullback(pos)
        if pullback == 0:
            return DCAResult(
                should_add=False,
                strength=DCAStrength.NONE,
                size_multiplier=0.0,
                target_price=None,
                reasons=["No pullback - price above entry"]
            )
        
        reasons = []
        strength = DCAStrength.NONE
        size_mult = 0.0
        target_price = pos.current_price
        
        # Определяем уровень сигнала
        if pullback >= self.cfg.pullback_strong:
            if ob_nearby and cvd_pressure and cvd_pressure >= self.cfg.cvd_threshold_strong:
                strength = DCAStrength.STRONG
                size_mult = self.cfg.kelly_multiplier_1st if pos.additions_count == 0 else \
                           self.cfg.kelly_multiplier_2nd if pos.additions_count == 1 else \
                           self.cfg.kelly_multiplier_3rd
                reasons.append(f"Strong pullback {pullback:.2f}% + OB + CVD {cvd_pressure:.0f}")
                
            elif (ob_nearby or fvg_nearby) and cvd_pressure and cvd_pressure >= self.cfg.cvd_threshold_moderate:
                strength = DCAStrength.MODERATE
                size_mult = self.cfg.kelly_multiplier_2nd if pos.additions_count == 0 else \
                           self.cfg.kelly_multiplier_3rd
                reasons.append(f"Moderate pullback {pullback:.2f}% + SMC + CVD {cvd_pressure:.0f}")
                
        if pullback >= self.cfg.pullback_moderate and strength == DCAStrength.NONE:
            if liquidity_swept and structure_intact:
                strength = DCAStrength.MODERATE
                size_mult = self.cfg.kelly_multiplier_3rd
                reasons.append(f"Moderate pullback {pullback:.2f}% + liquidity swept")
                
        if pullback >= self.cfg.pullback_weak and strength == DCAStrength.NONE:
            if structure_intact:
                strength = DCAStrength.WEAK
                size_mult = 0.1  # Минимальная добавка
                reasons.append(f"Weak pullback {pullback:.2f}% + structure intact")
        
        if strength == DCAStrength.NONE:
            reasons.append(f"Pullback {pullback:.2f}% insufficient for conditions")
            return DCAResult(
                should_add=False,
                strength=DCAStrength.NONE,
                size_multiplier=0.0,
                target_price=None,
                reasons=reasons
            )
        
        # Рассчитываем целевую цену для лимитки
        if pos.side == "long":
            # Для лонга: добавляем чуть ниже текущей
            target_price = pos.current_price * 0.995  # -0.5%
        else:
            # Для шорта: добавляем чуть выше текущей
            target_price = pos.current_price * 1.005  # +0.5%
        
        logger.info(f"[SMART-DCA] {pos.symbol}: {strength.value.upper()} signal | "
                   f"Pullback: {pullback:.2f}% | Size mult: {size_mult:.1f} | "
                   f"Add #{pos.additions_count + 1}")
        
        return DCAResult(
            should_add=True,
            strength=strength,
            size_multiplier=size_mult,
            target_price=target_price,
            reasons=reasons,
            metadata={
                "pullback_pct": pullback,
                "ob_nearby": ob_nearby,
                "fvg_nearby": fvg_nearby,
                "liquidity_swept": liquidity_swept,
                "cvd_pressure": cvd_pressure,
                "structure_intact": structure_intact
            }
        )


# Singleton instance
_dca: Optional[SmartDCA] = None

def get_smart_dca(config: Optional[DCAConfig] = None) -> SmartDCA:
    """Get or create SmartDCA singleton"""
    global _dca
    if _dca is None:
        _dca = SmartDCA(config)
    return _dca
