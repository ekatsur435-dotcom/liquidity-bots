"""
Grid Entry System v1.0
Сеточный вход в позицию с лимитными ордерами.

Логика:
  1. Вместо одного рыночного входа - создаём сетку лимиток
  2. Распределяем позицию по уровням с разными весами
  3. Часть может заполниться по лимитке (лучшая цена)
  4. Остаток докупаем рынком если нужно

Сетка (для лонга):
  - Цена сигнала: 100
  - Уровень 1: 99.5 (40% от позиции)
  - Уровень 2: 99.0 (30% от позиции)
  - Уровень 3: 98.5 (20% от позиции)
  - Fallback: Рынок (10% если выше 100.5)

Преимущества:
  - Лучшее средневзвешенное входа
  - Уменьшаем просадку при резком движении
  - Часть позиции может не заполниться (меньше риск)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum
from datetime import datetime, timedelta

logger = logging.getLogger("grid_entry")


class GridEntryStatus(Enum):
    """Статус уровня сетки"""
    PENDING = "pending"     # Ожидает заполнения
    FILLED = "filled"       # Заполнен
    EXPIRED = "expired"     # Истёк срок
    CANCELLED = "cancelled" # Отменён


@dataclass
class GridEntryConfig:
    """Конфигурация Grid Entry"""
    levels: int = 3                     # Количество уровней
    
    # Распределение позиции по уровням
    level_weights: List[float] = field(default_factory=lambda: [0.40, 0.30, 0.20])
    fallback_weight: float = 0.10       # Остаток на рынок
    
    # Отступы от цены сигнала (%)
    level_offsets: List[float] = field(default_factory=lambda: [-0.5, -1.0, -1.5])
    fallback_offset: float = 0.5        # Для fallback (+0.5% выше сигнала)
    
    # TTL (Time To Live)
    limit_ttl_seconds: int = 300          # 5 минут на заполнение лимитки
    
    # Фильтры
    min_spread_pct: float = 0.05          # Мин спред для лимитки
    max_slippage_pct: float = 0.3        # Макс проскальзывание


@dataclass
class GridLevel:
    """Один уровень сетки входа"""
    level: int
    price: float
    size_usd: float
    weight: float
    status: GridEntryStatus = GridEntryStatus.PENDING
    order_id: Optional[str] = None
    filled_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def is_expired(self, ttl_seconds: int) -> bool:
        """Проверить истёк ли срок"""
        age = (datetime.utcnow() - self.created_at).total_seconds()
        return age > ttl_seconds and self.status == GridEntryStatus.PENDING


@dataclass
class GridEntryResult:
    """Результат сеточного входа"""
    symbol: str
    side: str
    signal_price: float
    total_target_size: float
    
    levels: List[GridLevel] = field(default_factory=list)
    fallback_level: Optional[GridLevel] = None
    
    filled_size: float = 0.0
    filled_value: float = 0.0
    avg_price: float = 0.0
    
    is_complete: bool = False
    is_valid: bool = False
    reasons: List[str] = field(default_factory=list)


@dataclass
class MarketCondition:
    """Рыночные условия для входа"""
    current_price: float
    bid_ask_spread: float              # Спред в процентах
    volatility_1m: float               # Волатильность за минуту
    order_book_depth: float            # Глубина стакана в % от цены


class GridEntry:
    """
    Сеточная система входа в позицию.
    Распределяет позицию по лимитным уровням для лучшего средневзвешенного.
    """
    
    def __init__(self, config: Optional[GridEntryConfig] = None):
        self.cfg = config or GridEntryConfig()
        
    def create_grid(self,
                    symbol: str,
                    side: str,
                    signal_price: float,
                    target_size_usd: float,
                    market: MarketCondition
                   ) -> GridEntryResult:
        """
        Создать сетку входа.
        
        Args:
            symbol: Торговая пара
            side: "long" или "short"
            signal_price: Цена сигнала (откуда считаем уровни)
            target_size_usd: Целевой размер позиции в USD
            market: Рыночные условия
        """
        
        reasons = []
        levels = []
        
        # Проверяем спред
        if market.bid_ask_spread < self.cfg.min_spread_pct:
            reasons.append(f"Spread {market.bid_ask_spread:.3f}% too small for grid")
            # Делаем простой рыночный вход
            return GridEntryResult(
                symbol=symbol, side=side, signal_price=signal_price,
                total_target_size=target_size_usd,
                is_valid=True, is_complete=False,
                reasons=reasons
            )
        
        # Создаём уровни
        total_weight = sum(self.cfg.level_weights[:self.cfg.levels])
        
        for i in range(self.cfg.levels):
            offset = self.cfg.level_offsets[i] if i < len(self.cfg.level_offsets) else -2.0
            
            # Цена уровня
            if side == "long":
                level_price = signal_price * (1 + offset/100)
            else:
                level_price = signal_price * (1 - offset/100)
            
            # Размер уровня
            weight = self.cfg.level_weights[i] if i < len(self.cfg.level_weights) else 0.1
            size = target_size_usd * (weight / total_weight) * (1 - self.cfg.fallback_weight)
            
            level = GridLevel(
                level=i+1,
                price=round(level_price, 8),
                size_usd=round(size, 2),
                weight=weight
            )
            levels.append(level)
        
        # Fallback уровень (рыночный вход если цена ушла вверх/вниз)
        if side == "long":
            fallback_price = signal_price * (1 + self.cfg.fallback_offset/100)
        else:
            fallback_price = signal_price * (1 - self.cfg.fallback_offset/100)
        
        fallback_size = target_size_usd * self.cfg.fallback_weight
        fallback = GridLevel(
            level=99,  # Специальный номер
            price=round(fallback_price, 8),
            size_usd=round(fallback_size, 2),
            weight=self.cfg.fallback_weight
        )
        
        logger.info(f"[GRID-ENTRY] {symbol}: {len(levels)} levels + fallback | "
                   f"Total: ${target_size_usd:.0f}")
        
        reasons.append(f"Created {len(levels)} grid levels + fallback")
        
        return GridEntryResult(
            symbol=symbol,
            side=side,
            signal_price=signal_price,
            total_target_size=target_size_usd,
            levels=levels,
            fallback_level=fallback,
            is_valid=True,
            is_complete=False,
            reasons=reasons
        )
    
    def check_and_fill(self, 
                       result: GridEntryResult, 
                       current_price: float,
                       order_executor=None
                      ) -> GridEntryResult:
        """
        Проверить и заполнить уровни при достижении цены.
        
        Args:
            result: Результат сетки
            current_price: Текущая цена рынка
            order_executor: Функция для отправки ордера (symbol, price, size)
        """
        
        filled_total = 0
        filled_value = 0
        
        # Проверяем лимитные уровни
        for level in result.levels:
            if level.status != GridEntryStatus.PENDING:
                if level.status == GridEntryStatus.FILLED:
                    filled_total += level.size_usd
                    filled_value += level.size_usd * level.price
                continue
            
            # Проверяем истечение срока
            if level.is_expired(self.cfg.limit_ttl_seconds):
                level.status = GridEntryStatus.EXPIRED
                logger.info(f"[GRID-ENTRY] Level {level.level} expired")
                continue
            
            # Проверяем достижение цены
            price_hit = False
            if result.side == "long" and current_price <= level.price:
                price_hit = True
            elif result.side == "short" and current_price >= level.price:
                price_hit = True
            
            if price_hit and order_executor:
                # Отправляем ордер
                order_id = order_executor(result.symbol, level.price, level.size_usd)
                if order_id:
                    level.status = GridEntryStatus.FILLED
                    level.filled_at = datetime.utcnow()
                    level.order_id = order_id
                    filled_total += level.size_usd
                    filled_value += level.size_usd * level.price
                    logger.info(f"[GRID-ENTRY] Level {level.level} filled @ {level.price:.4f}")
        
        # Проверяем fallback
        if result.fallback_level and result.fallback_level.status == GridEntryStatus.PENDING:
            fallback_hit = False
            if result.side == "long" and current_price >= result.fallback_level.price:
                fallback_hit = True
            elif result.side == "short" and current_price <= result.fallback_level.price:
                fallback_hit = True
            
            if fallback_hit and order_executor:
                order_id = order_executor(result.symbol, current_price, result.fallback_level.size_usd)
                if order_id:
                    result.fallback_level.status = GridEntryStatus.FILLED
                    result.fallback_level.filled_at = datetime.utcnow()
                    result.fallback_level.order_id = order_id
                    filled_total += result.fallback_level.size_usd
                    filled_value += result.fallback_level.size_usd * current_price
                    logger.info(f"[GRID-ENTRY] Fallback filled @ {current_price:.4f}")
        
        # Обновляем статистику
        result.filled_size = filled_total
        result.filled_value = filled_value
        if filled_total > 0:
            result.avg_price = filled_value / filled_total
        
        # Проверяем завершение
        total_levels = len([l for l in result.levels if l.status != GridEntryStatus.PENDING])
        filled_levels = len([l for l in result.levels if l.status == GridEntryStatus.FILLED])
        
        if filled_levels >= len(result.levels) * 0.7:  # 70% заполнено
            result.is_complete = True
        
        return result
    
    def get_pending_levels(self, result: GridEntryResult) -> List[GridLevel]:
        """Получить ожидающие уровни"""
        return [l for l in result.levels if l.status == GridEntryStatus.PENDING]
    
    def get_fill_percentage(self, result: GridEntryResult) -> float:
        """Получить процент заполнения"""
        if result.total_target_size == 0:
            return 0.0
        return (result.filled_size / result.total_target_size) * 100


# Singleton instance
_grid_entry: Optional[GridEntry] = None

def get_grid_entry(config: Optional[GridEntryConfig] = None) -> GridEntry:
    """Get or create GridEntry singleton"""
    global _grid_entry
    if _grid_entry is None:
        _grid_entry = GridEntry(config)
    return _grid_entry
