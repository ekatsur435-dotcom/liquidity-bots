"""
🛡️ Phase 3: Advanced Risk Manager

Расширенный риск-менеджмент:
- Корреляционная проверка позиций
- Динамический position sizing
- Circuit breaker при просадке
"""

import asyncio
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import numpy as np


@dataclass
class Position:
    """Структура позиции для анализа риска"""
    symbol: str
    direction: str  # "long" или "short"
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl_pct: float
    leverage: float


@dataclass
class RiskLimits:
    """Лимиты риска"""
    max_correlation: float = 0.7  # Макс корреляция между позициями
    max_portfolio_risk: float = 5.0  # Макс риск портфеля в %
    max_daily_loss: float = 10.0  # Макс дневная просадка в %
    circuit_breaker_threshold: float = 15.0  # Остановка торговли


class RiskManager:
    """
    🛡️ Phase 3: Умный Risk Manager
    
    Защищает от:
    1. Перекоррелированных позиций (не открывать BTC и ETH одновременно)
    2. Перегрузки портфеля (макс X% риска)
    3. Каскадных потерь (остановка при просадке)
    """
    
    # Корреляционные группы (монеты движутся вместе)
    CORRELATION_GROUPS = {
        "btc_family": ["BTCUSDT", "BTCUSD", "XBTUSDT", "BTC-PERP"],
        "eth_family": ["ETHUSDT", "ETHUSD", "ETH-PERP"],
        "sol_family": ["SOLUSDT", "SOL-PERP", "RAYUSDT", "SRMUSDT"],
        "meme_coins": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "FLOKIUSDT"],
        "ai_coins": ["AGIXUSDT", "FETUSDT", "OCEANUSDT", "NMRUSDT"],
        "gaming": ["SANDUSDT", "MANAUSDT", "AXSUSDT", "GALAUSDT"],
        "defi": ["AAVEUSDT", "UNIUSDT", "MKRUSDT", "CRVUSDT"],
    }
    
    def __init__(self, limits: RiskLimits = None):
        self.limits = limits or RiskLimits()
        self.daily_pnl_history: List[float] = []
        self.last_reset = datetime.utcnow().date()
        self.circuit_breaker_active = False
        self.circuit_breaker_reason = None
    
    def check_position_correlation(
        self,
        new_symbol: str,
        existing_positions: List[Position]
    ) -> Tuple[bool, str]:
        """
        Проверка корреляции новой позиции с существующими
        
        Returns: (allowed, reason)
        """
        # Находим группу нового символа
        new_group = None
        for group_name, symbols in self.CORRELATION_GROUPS.items():
            if any(s in new_symbol.upper() for s in symbols):
                new_group = group_name
                break
        
        if not new_group:
            return True, "No correlation group"
        
        # Считаем позиции в той же группе
        same_group_positions = [
            pos for pos in existing_positions
            if any(s in pos.symbol.upper() for s in self.CORRELATION_GROUPS[new_group])
        ]
        
        total_exposure = sum(pos.size for pos in same_group_positions)
        new_exposure_ratio = total_exposure / (total_exposure + 1) if total_exposure > 0 else 0
        
        if new_exposure_ratio > self.limits.max_correlation:
            return False, f"Correlation limit: {new_group} already has {len(same_group_positions)} positions"
        
        return True, f"Correlation OK: {new_group}"
    
    def calculate_position_size(
        self,
        symbol: str,
        portfolio_value: float,
        signal_score: float,
        volatility: float,
        existing_positions: List[Position]
    ) -> float:
        """
        Динамический расчет размера позиции
        
        Учитывает:
        - Силу сигнала (score)
        - Волатильность (меньше размер при высокой волатильности)
        - Текущую загрузку портфеля
        """
        # Базовый размер (1% от портфеля)
        base_size = portfolio_value * 0.01
        
        # Мультипликатор от силы сигнала (0.5 - 2.0)
        score_multiplier = max(0.5, min(2.0, signal_score / 50))
        
        # Мультипликатор от волатильности (обратная зависимость)
        # При vol=50% → 0.5x, при vol=10% → 1.5x
        vol_multiplier = max(0.3, min(1.5, 1.0 - (volatility / 100) * 0.8))
        
        # Мультипликатор от загрузки портфеля
        current_risk = sum(abs(pos.unrealized_pnl_pct) * pos.size for pos in existing_positions)
        risk_ratio = current_risk / portfolio_value if portfolio_value > 0 else 0
        load_multiplier = max(0.3, 1.0 - risk_ratio * 2)  # При высоком риске уменьшаем
        
        # Итоговый размер
        final_size = base_size * score_multiplier * vol_multiplier * load_multiplier
        
        # Макс 3% от портфеля на одну позицию
        max_size = portfolio_value * 0.03
        
        return min(final_size, max_size)
    
    def check_circuit_breaker(
        self,
        daily_pnl_pct: float,
        portfolio_value: float
    ) -> Tuple[bool, Optional[str]]:
        """
        Проверка circuit breaker
        
        Returns: (trading_allowed, reason_if_stopped)
        """
        # Сбрасываем ежедневно
        today = datetime.utcnow().date()
        if today != self.last_reset:
            self.daily_pnl_history = []
            self.last_reset = today
            self.circuit_breaker_active = False
            self.circuit_breaker_reason = None
        
        # Добавляем текущий P&L в историю
        self.daily_pnl_history.append(daily_pnl_pct)
        
        # Проверяем просадку за день
        total_daily_pnl = sum(self.daily_pnl_history)
        
        if total_daily_pnl <= -self.limits.circuit_breaker_threshold:
            self.circuit_breaker_active = True
            self.circuit_breaker_reason = (
                f"🔴 CIRCUIT BREAKER: Daily loss {total_daily_pnl:.1f}% "
                f"exceeds limit {self.limits.circuit_breaker_threshold}%"
            )
            return False, self.circuit_breaker_reason
        
        # Макс дневная просадка
        if total_daily_pnl <= -self.limits.max_daily_loss:
            return False, f"⚠️ Daily loss limit: {total_daily_pnl:.1f}%"
        
        return True, None
    
    def get_portfolio_risk_metrics(
        self,
        positions: List[Position]
    ) -> Dict:
        """Расчет метрик риска портфеля"""
        if not positions:
            return {
                "total_exposure": 0,
                "portfolio_heat": 0,
                "concentration_risk": 0,
                "var_95": 0,  # Value at Risk
            }
        
        # Общая экспозиция
        total_exposure = sum(pos.size for pos in positions)
        
        # "Жар" портфеля (средний убыток в %)
        portfolio_heat = sum(
            pos.unrealized_pnl_pct * (pos.size / total_exposure)
            for pos in positions
        ) if total_exposure > 0 else 0
        
        # Концентрационный риск (Herfindahl index)
        weights = [pos.size / total_exposure for pos in positions]
        concentration_risk = sum(w ** 2 for w in weights)
        
        # VaR (Value at Risk) — упрощенный расчет
        pnl_values = [pos.unrealized_pnl_pct for pos in positions]
        if len(pnl_values) > 1:
            var_95 = np.percentile(pnl_values, 5)  # 5-й перцентиль = 95% VaR
        else:
            var_95 = pnl_values[0] if pnl_values else 0
        
        return {
            "total_exposure": round(total_exposure, 2),
            "portfolio_heat": round(portfolio_heat, 2),
            "concentration_risk": round(concentration_risk, 3),
            "var_95": round(var_95, 2),
            "position_count": len(positions),
        }


# Singleton
_risk_manager: Optional[RiskManager] = None


def get_risk_manager(limits: RiskLimits = None) -> RiskManager:
    """Get or create singleton RiskManager"""
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager(limits)
    return _risk_manager
