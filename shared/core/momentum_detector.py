"""
Momentum Detector Module v1.0
Детектор ценового импульса для трендовой торговли
Поддерживает LONG и SHORT направления

Отличия от обычной стратегии:
- Обычная: ловим отскоки от перепроданности (RSI < 40)
- Momentum: ловим продолжение тренда (RSI 40-75, цена растёт)
"""

import asyncio
import statistics
from typing import Optional, Dict, List, Literal
from dataclasses import dataclass
from datetime import datetime

from utils.binance_client import CandleData


@dataclass
class MomentumConfig:
    """Конфигурация momentum детектора"""
    enabled: bool = True
    direction: Literal["long", "short"] = "long"
    
    # Пороги изменения цены (%)
    min_1m_change: float = 0.8      # Минимум за 1 минуту
    min_5m_change: float = 2.0       # Минимум за 5 минут
    
    # Объём
    volume_spike_min: float = 2.5     # Минимум X к среднему
    
    # RSI фильтры (для long: не перекуплено, для short: перекуплено)
    rsi_min: float = 40.0
    rsi_max: float = 75.0
    
    # EMA тренд
    ema_fast: int = 9
    ema_slow: int = 21
    
    # Скоринг
    min_score: float = 50.0
    score_boost_per_1m_pct: float = 10.0  # +10 score за каждый % в минуту
    
    # Риск-менеджмент (отдельный от основного!)
    risk_per_trade: float = 0.0003    # 0.03% = $60 на 200K
    sl_buffer: float = 1.0            # Уже стоп (быстрый выход)
    max_hold_minutes: int = 30        # Макс время удержания
    trail_start_pct: float = 0.5      # Трейлинг с 0.5%
    
    # Лимиты
    max_concurrent_positions: int = 5  # Макс momentum позиций


@dataclass
class MomentumSignal:
    """Сигнал momentum"""
    symbol: str
    direction: Literal["long", "short"]
    entry_price: float
    score: float
    change_1m: float
    change_5m: float
    volume_spike: float
    rsi: float
    ema_fast: float
    ema_slow: float
    factors: List[str]
    timestamp: datetime
    
    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "score": round(self.score, 2),
            "change_1m": round(self.change_1m, 2),
            "change_5m": round(self.change_5m, 2),
            "volume_spike": round(self.volume_spike, 2),
            "rsi": round(self.rsi, 2),
            "factors": self.factors,
            "timestamp": self.timestamp.isoformat(),
            "signal_type": "momentum"
        }


class MomentumDetector:
    """
    Детектор ценового импульса
    Работает НЕЗАВИСИМО от основного скорера!
    """
    
    def __init__(self, config: MomentumConfig, bot_type: str = "long"):
        self.config = config
        self.bot_type = bot_type
        self.active_signals: Dict[str, MomentumSignal] = {}
        
        print(f"🚀 MomentumDetector initialized for {bot_type.upper()}")
        print(f"   Enabled: {config.enabled}")
        print(f"   Direction: {config.direction}")
        print(f"   Min 1m change: {config.min_1m_change}%")
        print(f"   Volume spike: {config.volume_spike_min}x")
        print(f"   RSI range: {config.rsi_min}-{config.rsi_max}")
    
    @property
    def direction(self) -> str:
        """Expose direction from config"""
        return self.config.direction
    
    def _calc_ema(self, prices: List[float], period: int) -> float:
        """Расчёт EMA"""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period  # SMA для начала
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    def _calc_rsi(self, candles: List[CandleData], period: int = 14) -> float:
        """Расчёт RSI"""
        if len(candles) < period + 1:
            return 50.0
        
        gains = []
        losses = []
        
        for i in range(1, len(candles)):
            change = candles[i].close - candles[i-1].close
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        if len(gains) < period:
            return 50.0
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def detect_momentum(
        self,
        symbol: str,
        candles_1m: List[CandleData],
        candles_5m: Optional[List[CandleData]] = None
    ) -> Optional[MomentumSignal]:
        """
        Основной метод детекции momentum
        
        Args:
            symbol: Тикер
            candles_1m: 1-минутные свечи (минимум 25 для EMA21)
            candles_5m: 5-минутные свечи (опционально)
        
        Returns:
            MomentumSignal или None
        """
        if not self.config.enabled:
            return None
        
        if len(candles_1m) < 25:
            return None
        
        # Проверка: не превышен ли лимит momentum позиций
        if len(self.active_signals) >= self.config.max_concurrent_positions:
            return None
        
        # Уже есть активный сигнал по этой монете?
        if symbol in self.active_signals:
            return None
        
        # --- РАСЧЁТ ИЗМЕНЕНИЯ ЦЕНЫ ---
        price_now = candles_1m[-1].close
        price_1m_ago = candles_1m[-2].close if len(candles_1m) >= 2 else candles_1m[0].close
        price_5m_ago = candles_1m[-6].close if len(candles_1m) >= 6 else candles_1m[0].close
        
        change_1m = (price_now - price_1m_ago) / price_1m_ago * 100
        change_5m = (price_now - price_5m_ago) / price_5m_ago * 100
        
        # Для SHORT инвертируем (ищем падение)
        if self.config.direction == "short":
            change_1m = -change_1m
            change_5m = -change_5m
        
        # Проверка порогов изменения цены
        if change_1m < self.config.min_1m_change:
            return None
        if change_5m < self.config.min_5m_change:
            return None
        
        # --- ОБЪЁМНЫЙ ВСПЛЕСК ---
        avg_volume = sum(c.quote_volume for c in candles_1m[-21:-1]) / 20
        current_volume = candles_1m[-1].quote_volume
        volume_spike = current_volume / avg_volume if avg_volume > 0 else 1.0
        
        if volume_spike < self.config.volume_spike_min:
            return None
        
        # --- RSI ПРОВЕРКА ---
        rsi = self._calc_rsi(candles_1m)
        
        if rsi < self.config.rsi_min or rsi > self.config.rsi_max:
            return None
        
        # --- EMA ТРЕНД ---
        closes = [c.close for c in candles_1m]
        ema_fast = self._calc_ema(closes, self.config.ema_fast)
        ema_slow = self._calc_ema(closes, self.config.ema_slow)
        
        # Для LONG: EMA9 > EMA21 (тренд вверх)
        # Для SHORT: EMA9 < EMA21 (тренд вниз)
        if self.config.direction == "long":
            if ema_fast <= ema_slow:
                return None
        else:  # short
            if ema_fast >= ema_slow:
                return None
        
        # --- РАСЧЁТ СКОРА ---
        base_score = self.config.min_score
        velocity_bonus = min(change_1m * self.config.score_boost_per_1m_pct, 30)
        volume_bonus = min((volume_spike - 2.5) * 5, 10)  # До +10 за сильный всплеск
        
        final_score = base_score + velocity_bonus + volume_bonus
        final_score = min(final_score, 95)  # Кап на 95
        
        # --- ФАКТОРЫ ДЛЯ ЛОГА ---
        factors = [
            f"🚀 Velocity +{change_1m:.2f}%/min" if self.config.direction == "long" else f"🚀 Velocity -{change_1m:.2f}%/min",
            f"📊 Volume {volume_spike:.1f}x",
            f"📈 RSI {rsi:.1f}",
            f"EMA{self.config.ema_fast}>{self.config.ema_slow}" if self.config.direction == "long" else f"EMA{self.config.ema_fast}<{self.config.ema_slow}"
        ]
        
        signal = MomentumSignal(
            symbol=symbol,
            direction=self.config.direction,
            entry_price=price_now,
            score=final_score,
            change_1m=change_1m if self.config.direction == "long" else -change_1m,
            change_5m=change_5m if self.config.direction == "long" else -change_5m,
            volume_spike=volume_spike,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            factors=factors,
            timestamp=datetime.utcnow()
        )
        
        # Сохраняем активный сигнал
        self.active_signals[symbol] = signal
        
        print(f"🎯 [{self.bot_type.upper()} MOMENTUM] {symbol}: "
              f"Score={final_score:.1f}, "
              f"{'+' if self.config.direction == 'long' else '-'}{change_1m:.2f}%/min, "
              f"Vol={volume_spike:.1f}x")
        
        return signal
    
    def remove_signal(self, symbol: str):
        """Удалить сигнал (при закрытии позиции)"""
        if symbol in self.active_signals:
            del self.active_signals[symbol]
    
    async def scan_watchlist(
        self,
        watchlist: List[str],
        get_candles_func,
        min_volume_usdt: float = 100000
    ) -> List[MomentumSignal]:
        """
        Сканирование вотчлиста на momentum сигналы
        
        Args:
            watchlist: Список тикеров
            get_candles_func: Функция для получения свечей (symbol, tf) -> candles
            min_volume_usdt: Минимальный объём для фильтра
        
        Returns:
            Список сигналов
        """
        if not self.config.enabled:
            return []
        
        signals = []
        
        for symbol in watchlist:
            try:
                # Получаем 1-минутные свечи
                candles = await get_candles_func(symbol, "1m", limit=30)
                
                if not candles or len(candles) < 25:
                    continue
                
                # Фильтр по объёму (последняя свеча)
                if candles[-1].quote_volume < min_volume_usdt:
                    continue
                
                signal = self.detect_momentum(symbol, candles)
                
                if signal:
                    signals.append(signal)
                    
            except Exception as e:
                # Не логируем каждую ошибку чтобы не спамить
                pass
        
        return signals


# Фабрика для создания детекторов
def get_momentum_detector(
    direction: Literal["long", "short"],
    **kwargs
) -> MomentumDetector:
    """
    Создаёт momentum детектор с настройками из kwargs или ENV
    
    Args:
        direction: "long" или "short"
        **kwargs: Переопределение настроек
    
    Returns:
        MomentumDetector
    """
    import os
    
    prefix = "MOMENTUM_LONG_" if direction == "long" else "MOMENTUM_SHORT_"
    
    config = MomentumConfig(
        enabled=os.getenv(f"{prefix}ENABLED", "true").lower() == "true",
        direction=direction,
        min_1m_change=float(os.getenv(f"{prefix}MIN_1MIN_CHANGE", "0.8")),
        min_5m_change=float(os.getenv(f"{prefix}MIN_5MIN_CHANGE", "2.0")),
        volume_spike_min=float(os.getenv(f"{prefix}VOLUME_SPIKE", "2.5")),
        rsi_min=float(os.getenv(f"{prefix}RSI_MIN", "40" if direction == "long" else "60")),
        rsi_max=float(os.getenv(f"{prefix}RSI_MAX", "75" if direction == "long" else "85")),
        min_score=float(os.getenv(f"{prefix}SCORE_MIN", "50")),
        risk_per_trade=float(os.getenv(f"{prefix}RISK_PER_TRADE", "0.0003")),
        sl_buffer=float(os.getenv(f"{prefix}SL_BUFFER", "1.0")),
        max_hold_minutes=int(os.getenv(f"{prefix}MAX_HOLD_MINUTES", "30")),
        trail_start_pct=float(os.getenv(f"{prefix}TRAIL_START", "0.5")),
        max_concurrent_positions=int(os.getenv(f"{prefix}MAX_POSITIONS", "5")),
    )
    
    # Переопределение из kwargs
    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)
    
    bot_type = "long" if direction == "long" else "short"
    return MomentumDetector(config, bot_type)
