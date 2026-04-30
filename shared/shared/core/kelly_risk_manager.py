"""
Kelly Criterion Risk Manager v1.0 (from Aegis HARD BOT)

Институциональный риск-менеджмент с Kelly Criterion для оптимального размера позиции.

Компоненты:
  1. Kelly Criterion (fractional) — оптимальный размер позиции
  2. Portfolio Heat Monitor — суммарная экспозиция
  3. Circuit Breakers — автостоп при просадке/серии стопов
  4. Signal-Based Sizing — размер зависит от качества сигнала

Лимиты (рекомендуемые):
  max_position_pct:     15% на позицию
  max_total_exposure:   60% всего капитала
  max_daily_drawdown:   5.0%
  max_consecutive_loss: 4
  kelly_fraction:       0.25 (Quarter-Kelly)
  min_rr_ratio:         1.5

Kelly Formula:
  f* = (bp - q) / b
  where:
    b = avg_win / avg_loss (odds)
    p = win_rate
    q = 1 - p

Quarter-Kelly:
  size = f* * 0.25 — более безопасно для крипто волатильности
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("kelly_risk_manager")


@dataclass
class RiskLimits:
    """Риск-параметры"""
    max_position_pct:     float = 0.15   # 15% капитала на 1 позицию
    max_total_exposure:   float = 0.60   # 60% суммарно открыто
    max_daily_drawdown:   float = 5.0    # 5% дневная просадка = стоп
    max_consecutive_loss: int   = 4      # 4 подряд стопа = стоп
    kelly_fraction:       float = 0.25   # Quarter-Kelly
    min_rr_ratio:         float = 1.5    # Минимум R:R 1:1.5
    max_corr_positions:   int   = 3      # Макс коррелирующих позиций


@dataclass
class CircuitBreakerState:
    """Состояние circuit breaker"""
    triggered:         bool    = False
    reason:            str     = ""
    triggered_at:      Optional[datetime] = None
    daily_pnl_pct:     float   = 0.0
    consecutive_losses: int    = 0
    daily_trades:      int     = 0
    daily_loss_usd:    float   = 0.0
    reset_date:        Optional[date] = None

    def is_triggered(self) -> bool:
        return self.triggered

    def reset_daily(self, today: date):
        """Дневной сброс статистики"""
        if self.reset_date != today:
            self.daily_pnl_pct  = 0.0
            self.daily_loss_usd = 0.0
            self.daily_trades   = 0
            self.reset_date     = today
            if self.triggered and "daily" in self.reason:
                self.triggered = False
                self.reason    = ""
                logger.info("Circuit breaker: daily drawdown reset")


@dataclass
class PositionSizeResult:
    """Результат расчёта размера позиции"""
    size_usd:        float
    size_pct:        float          # % от капитала
    kelly_pct:       float          # Исходный Kelly %
    adjusted_pct:    float          # После всех ограничений
    risk_usd:        float          # Максимальный убыток (в USD)
    blocked:         bool = False
    block_reason:    str  = ""
    signal_boost:    float = 0.0     # Буст от качества сигнала


@dataclass
class SignalQuality:
    """Качество сигнала для sizing"""
    score:           float   # 0-100
    has_tbs:         bool    # Test Before Strike
    ob_quality:      int     # 0-100
    is_sweep:        bool    # Liquidity sweep
    confidence:      str     # "LOW", "MEDIUM", "HIGH", "ULTRA"


class KellyRiskManager:
    """
    Институциональный риск-менеджер с Kelly Criterion.
    
    Usage:
        rm = KellyRiskManager(limits=RiskLimits(), capital=10000)
        size = rm.calculate_position_size(
            win_rate=0.65, avg_win=50, avg_loss=30,
            signal_quality=SignalQuality(score=78, ...),
            sl_pct=2.5
        )
    """

    def __init__(self, limits: Optional[RiskLimits] = None, capital: float = 10000.0,
                 redis_client=None):
        self.limits    = limits or RiskLimits()
        self.capital   = capital
        self.redis     = redis_client
        self.cb_state  = CircuitBreakerState()
        self._history: List[Dict] = []  # История сделок для расчёта win_rate

    def update_capital(self, new_capital: float):
        """Обновить капитал"""
        self.capital = new_capital

    def record_trade(self, pnl_pct: float, pnl_usd: float, symbol: str = ""):
        """Записать результат сделки для статистики"""
        self._history.append({
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "symbol": symbol,
            "time": datetime.utcnow()
        })
        
        # Circuit breaker tracking
        self.cb_state.daily_trades += 1
        self.cb_state.daily_pnl_pct += pnl_pct
        
        if pnl_pct < 0:
            self.cb_state.consecutive_losses += 1
            self.cb_state.daily_loss_usd += abs(pnl_usd)
        else:
            self.cb_state.consecutive_losses = 0  # Reset on win

    def get_win_rate(self, lookback: int = 20) -> float:
        """Расчёт win rate за последние N сделок"""
        if len(self._history) < 5:
            return 0.55  # Conservative default
        
        recent = self._history[-lookback:]
        wins = sum(1 for t in recent if t["pnl_pct"] > 0)
        return wins / len(recent)

    def get_avg_win_loss(self, lookback: int = 20) -> Tuple[float, float]:
        """Средний выигрыш и проигрыш"""
        if len(self._history) < 5:
            return 2.0, 1.0  # Default 2:1 R:R
        
        recent = self._history[-lookback:]
        wins = [t["pnl_pct"] for t in recent if t["pnl_pct"] > 0]
        losses = [abs(t["pnl_pct"]) for t in recent if t["pnl_pct"] < 0]
        
        avg_win = sum(wins) / len(wins) if wins else 2.0
        avg_loss = sum(losses) / len(losses) if losses else 1.0
        
        return avg_win, avg_loss

    def calculate_kelly(self, win_rate: Optional[float] = None,
                        avg_win: Optional[float] = None,
                        avg_loss: Optional[float] = None) -> float:
        """
        Расчёт Kelly Criterion.
        f* = (bp - q) / b
        Returns: fraction of capital (0.0 - 1.0)
        """
        if win_rate is None:
            win_rate = self.get_win_rate()
        
        if avg_win is None or avg_loss is None:
            avg_win, avg_loss = self.get_avg_win_loss()
        
        if avg_loss == 0:
            return 0.0
        
        b = avg_win / avg_loss  # Odds
        p = win_rate
        q = 1 - p
        
        kelly = (b * p - q) / b if b > 0 else 0.0
        
        # Apply Kelly fraction (Quarter-Kelly for safety)
        return max(0.0, min(kelly * self.limits.kelly_fraction, 0.5))

    def calculate_position_size(
        self,
        signal_quality: SignalQuality,
        win_rate: Optional[float] = None,
        avg_win: Optional[float] = None,
        avg_loss: Optional[float] = None,
        sl_pct: float = 2.0,
        current_exposure_pct: float = 0.0,
    ) -> PositionSizeResult:
        """
        Расчёт оптимального размера позиции.
        
        Args:
            signal_quality: Качество сигнала
            win_rate: Win rate (если None — из истории)
            avg_win: Средний выигрыш %
            avg_loss: Средний проигрыш %
            sl_pct: Stop Loss %
            current_exposure_pct: Текущая экспозиция %
        
        Returns:
            PositionSizeResult с size_usd и ограничениями
        """
        # Check circuit breakers first
        if self.cb_state.consecutive_losses >= self.limits.max_consecutive_loss:
            return PositionSizeResult(
                size_usd=0.0, size_pct=0.0, kelly_pct=0.0,
                adjusted_pct=0.0, risk_usd=0.0,
                blocked=True, block_reason=f"Circuit breaker: {self.cb_state.consecutive_losses} consecutive losses"
            )
        
        if self.cb_state.daily_pnl_pct <= -self.limits.max_daily_drawdown:
            return PositionSizeResult(
                size_usd=0.0, size_pct=0.0, kelly_pct=0.0,
                adjusted_pct=0.0, risk_usd=0.0,
                blocked=True, block_reason=f"Circuit breaker: daily drawdown {self.cb_state.daily_pnl_pct:.1f}%"
            )

        # Base Kelly calculation
        kelly_pct = self.calculate_kelly(win_rate, avg_win, avg_loss)
        
        # Signal quality boost
        signal_boost = 0.0
        if signal_quality.score >= 85:
            signal_boost = 0.05  # +5% for ULTRA signals
        elif signal_quality.score >= 70:
            signal_boost = 0.03  # +3% for STRONG
        elif signal_quality.has_tbs and signal_quality.ob_quality >= 60:
            signal_boost = 0.02  # +2% for TBS+OB
        
        # Apply signal boost
        adjusted_pct = kelly_pct + signal_boost
        
        # Hard limits
        adjusted_pct = min(adjusted_pct, self.limits.max_position_pct)
        
        # Portfolio heat check
        if current_exposure_pct + adjusted_pct > self.limits.max_total_exposure:
            adjusted_pct = self.limits.max_total_exposure - current_exposure_pct
            if adjusted_pct < 0.01:  # Less than 1%
                return PositionSizeResult(
                    size_usd=0.0, size_pct=0.0, kelly_pct=kelly_pct,
                    adjusted_pct=0.0, risk_usd=0.0,
                    blocked=True, block_reason="Max portfolio exposure reached",
                    signal_boost=signal_boost
                )
        
        # Calculate size
        size_usd = self.capital * adjusted_pct
        risk_usd = size_usd * (sl_pct / 100)
        
        return PositionSizeResult(
            size_usd=round(size_usd, 2),
            size_pct=round(adjusted_pct * 100, 2),
            kelly_pct=round(kelly_pct * 100, 2),
            adjusted_pct=round(adjusted_pct * 100, 2),
            risk_usd=round(risk_usd, 2),
            blocked=False,
            block_reason="",
            signal_boost=round(signal_boost * 100, 2)
        )

    def check_risk_limits(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        sl_pct: float,
        tp_levels: List[Tuple[float, int]]
    ) -> Tuple[bool, str]:
        """
        Проверка всех риск-лимитов перед входом.
        
        Returns:
            (allowed: bool, reason: str)
        """
        # R:R check
        if tp_levels:
            tp1_pct = tp_levels[0][0]  # First TP %
            rr_ratio = tp1_pct / sl_pct if sl_pct > 0 else 0.0
            if rr_ratio < self.limits.min_rr_ratio:
                return False, f"R:R {rr_ratio:.2f} < min {self.limits.min_rr_ratio}"
        
        # Size check
        size_pct = size_usd / self.capital
        if size_pct > self.limits.max_position_pct:
            return False, f"Size {size_pct:.1%} > max {self.limits.max_position_pct:.1%}"
        
        # Circuit breaker check
        if self.cb_state.is_triggered():
            return False, f"Circuit breaker active: {self.cb_state.reason}"
        
        return True, "OK"

    def get_status(self) -> Dict:
        """Получить статус риск-менеджера"""
        return {
            "capital": self.capital,
            "circuit_breaker": {
                "triggered": self.cb_state.triggered,
                "reason": self.cb_state.reason,
                "consecutive_losses": self.cb_state.consecutive_losses,
                "daily_pnl_pct": round(self.cb_state.daily_pnl_pct, 2),
                "daily_trades": self.cb_state.daily_trades,
            },
            "limits": {
                "max_position_pct": self.limits.max_position_pct,
                "max_total_exposure": self.limits.max_total_exposure,
                "max_daily_drawdown": self.limits.max_daily_drawdown,
                "kelly_fraction": self.limits.kelly_fraction,
            },
            "history_stats": {
                "total_trades": len(self._history),
                "win_rate": round(self.get_win_rate(), 2),
                "avg_win": round(self.get_avg_win_loss()[0], 2),
                "avg_loss": round(self.get_avg_win_loss()[1], 2),
            }
        }


# Singleton instance
_kelly_rm: Optional[KellyRiskManager] = None


def get_kelly_risk_manager(capital: float = 10000.0, redis_client=None) -> KellyRiskManager:
    """Получить singleton instance"""
    global _kelly_rm
    if _kelly_rm is None:
        _kelly_rm = KellyRiskManager(capital=capital, redis_client=redis_client)
    return _kelly_rm
