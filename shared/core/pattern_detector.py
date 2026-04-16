"""
Pattern Detection System - БЕЗ PANDAS
8 паттернов: 4 для SHORT + 4 для LONG
Все детектируются на 15-минутном таймфрейме
"""

from typing import List, Optional, Dict, Tuple, Any
from dataclasses import dataclass
from datetime import datetime
import numpy as np


@dataclass
class Pattern:
    """Торговый паттерн"""
    name: str
    direction: str  # 'short' или 'long'
    strength: int     # 10-30 очков
    candles_ago: int  # На какой свече найден (0 = текущая)
    freshness: int    # Минут назад
    volume_multiplier: float
    delta_at_trigger: float
    entry_price: float
    stop_loss: float
    confidence: str   # 'weak', 'moderate', 'strong', 'very_strong'
    description: str


class BasePatternDetector:
    """Базовый класс для детекторов паттернов"""
    
    def __init__(self, min_volume_mult: float = 1.5):
        self.min_volume_mult = min_volume_mult
    
    def _calculate_atr(self, data: List[Dict], period: int = 14) -> float:
        """Расчёт ATR (Average True Range)"""
        if len(data) < 2:
            return 0.0
        
        highs = [self._get_high(candle) for candle in data[-period:]]
        lows = [self._get_low(candle) for candle in data[-period:]]
        
        ranges = [h - l for h, l in zip(highs, lows)]
        return np.mean(ranges) if ranges else 0.0
    
    def _get_price_trend(self, data: List[Any], lookback: int = 5) -> str:
        """Определить тренд цены"""
        if len(data) < lookback:
            return 'sideways'
        
        first_price = self._get_close(data[-lookback])
        last_price = self._get_close(data[-1])
        
        change = (last_price - first_price) / first_price * 100
        
        if change > 2:
            return 'rising'
        elif change < -2:
            return 'falling'
        else:
            return 'sideways'
    
    def _get_volume_trend(self, data: List[Any], lookback: int = 5) -> str:
        """Определить тренд объёма"""
        if len(data) < lookback + 5:
            return 'stable'
        
        recent_vol = np.mean([self._get_volume(d) for d in data[-lookback:]])
        prev_vol = np.mean([self._get_volume(d) for d in data[-lookback-5:-lookback]])
        
        ratio = recent_vol / prev_vol if prev_vol > 0 else 1
        
        if ratio > 1.5:
            return 'rising'
        elif ratio < 0.7:
            return 'falling'
        else:
            return 'stable'

    # =========================================================================
    # HELPER METHODS — общие для SHORT и LONG детекторов
    # =========================================================================

    def _get_candle(self, data: List[Any], idx: int) -> Optional[Any]:
        """Безопасно получить свечу по индексу"""
        if 0 <= idx < len(data):
            return data[idx]
        return None

    def _get_open(self, candle: Any) -> float:
        return candle['open'] if isinstance(candle, dict) else candle.open

    def _get_high(self, candle: Any) -> float:
        return candle['high'] if isinstance(candle, dict) else candle.high

    def _get_low(self, candle: Any) -> float:
        return candle['low'] if isinstance(candle, dict) else candle.low

    def _get_close(self, candle: Any) -> float:
        return candle['close'] if isinstance(candle, dict) else candle.close

    def _get_volume(self, candle: Any) -> float:
        return candle['volume'] if isinstance(candle, dict) else candle.volume

    def _get_timestamp(self, candle: Any) -> datetime:
        ts = candle.get('timestamp', datetime.now()) if isinstance(candle, dict) else getattr(candle, 'timestamp', datetime.now())
        if isinstance(ts, int):
            return datetime.fromtimestamp(ts / 1000)
        return ts

    def _is_bullish(self, candle: Any) -> bool:
        return self._get_close(candle) > self._get_open(candle)

    def _is_bearish(self, candle: Any) -> bool:
        return self._get_close(candle) < self._get_open(candle)

    def _get_body(self, candle: Any) -> float:
        return abs(self._get_close(candle) - self._get_open(candle))

    def _get_range(self, candle: Any) -> float:
        return self._get_high(candle) - self._get_low(candle)


class ShortPatternDetector(BasePatternDetector):
    """Детектор паттернов для SHORT позиций"""
    
    def detect_all(self, data_15m: List[Dict], delta_15m: List[float],
                  ohlcv_1h: List[Dict] = None) -> List[Pattern]:
        """Обнаружить все SHORT паттерны"""
        patterns = []
        
        rejection = self.detect_rejection_short(data_15m, delta_15m)
        if rejection:
            patterns.append(rejection)
        
        trap = self.detect_trap_long(data_15m, delta_15m)
        if trap:
            patterns.append(trap)
        
        mega = self.detect_mega_short(data_15m, delta_15m)
        if mega:
            patterns.append(mega)
        
        distribution = self.detect_distribution(data_15m, delta_15m)
        if distribution:
            patterns.append(distribution)
        
        return patterns
    
    def detect_rejection_short(self, data: List[Any], 
                              delta_data: List[float]) -> Optional[Pattern]:
        """REJECTION SHORT: Цена отбилась от сопротивления вниз"""
        if len(data) < 3:
            return None
        
        current = data[-1]
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        
        total_range = self._get_high(current) - self._get_low(current)
        if total_range == 0:
            return None
        
        upper_wick = self._get_high(current) - max(self._get_open(current), self._get_close(current))
        lower_wick = min(self._get_open(current), self._get_close(current)) - self._get_low(current)
        body_size = abs(self._get_close(current) - self._get_open(current))
        
        is_shooting_star = (
            upper_wick > total_range * 0.6 and
            body_size < total_range * 0.3 and
            self._get_close(current) < self._get_open(current) and
            self._get_close(current) < self._get_low(current) + total_range * 0.3
        )
        
        is_bearish_engulfing = False
        if len(data) >= 2:
            prev = data[-2]
            is_bearish_engulfing = (
                self._get_open(current) > self._get_close(prev) and
                self._get_close(current) < self._get_open(prev) and
                self._is_bearish(current) and  # Медвежья свеча
                self._get_high(current) > self._get_high(prev) and      # Пробой максимума
                self._get_close(current) < self._get_low(prev)          # Закрытие ниже минимума пред.
            )
        
        if not (is_shooting_star or is_bearish_engulfing):
            return None
        
        if self._get_volume(current) < avg_volume * self.min_volume_mult:
            return None
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta > 0:
            return None
        
        if is_bearish_engulfing:
            confidence = 'very_strong'
            strength = 20
        elif upper_wick > total_range * 0.7:
            confidence = 'strong'
            strength = 18
        else:
            confidence = 'moderate'
            strength = 15
        
        sl = self._get_high(current) + (self._get_high(current) - self._get_low(current)) * 0.2
        
        return Pattern(
            name='REJECTION_SHORT',
            direction='short',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=current_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence=confidence,
            description=f'Price rejected from high with {"engulfing" if is_bearish_engulfing else "shooting star"} pattern'
        )
    
    def detect_trap_long(self, data: List[Any], 
                        delta_data: List[float]) -> Optional[Pattern]:
        """TRAP LONG: Ловушка для лонгистов"""
        if len(data) < 5:
            return None
        
        recent_5 = data[-5:]
        highs = [self._get_high(d) for d in recent_5]
        lows = [self._get_low(d) for d in recent_5]
        
        resistance = max(highs[:-1])  # Максимум за 4 свечи до текущей
        current = data[-1]
        
        fake_breakout = (
            self._get_high(current) > resistance and
            self._get_close(current) < resistance and
            self._is_bearish(current)
        )
        
        if not fake_breakout:
            return None
        
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        if self._get_volume(current) < avg_volume * self.min_volume_mult:
            return None
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta > -1:
            return None
        
        sl = self._get_high(current) + (self._get_high(current) - self._get_low(current)) * 0.3
        
        return Pattern(
            name='TRAP_LONG',
            direction='short',
            strength=22,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=current_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence='strong',
            description=f'Trap for longs: fake breakout above {resistance:.2f} then reversal'
        )
    
    def detect_mega_short(self, data: List[Any], 
                         delta_data: List[float]) -> Optional[Pattern]:
        """MEGA SHORT: Доминация продавцов"""
        if len(data) < 5:
            return None
        
        recent_5 = data[-5:]
        red_candles = sum(1 for d in recent_5 if self._is_bearish(d))
        
        if red_candles < 4:
            return None
        
        volumes = [self._get_volume(d) for d in recent_5]
        avg_vol = np.mean(volumes)
        high_vol = all(v > avg_vol * 0.8 for v in volumes)
        
        if not high_vol:
            return None
        
        current = data[-1]
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta > 0:
            return None
        
        sl = max(self._get_high(d) for d in recent_5) * 1.01
        
        return Pattern(
            name='MEGA_SHORT',
            direction='short',
            strength=25,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=current_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence='very_strong',
            description=f'MEGA SHORT: {red_candles}/5 red candles, sellers dominating'
        )
    
    def detect_distribution(self, data: List[Any], 
                           delta_data: List[float]) -> Optional[Pattern]:
        """DISTRIBUTION: Крупный игрок распродаёт позицию"""
        if len(data) < 10:
            return None
        
        recent_10 = data[-10:]
        highs = [self._get_high(d) for d in recent_10]
        lows = [self._get_low(d) for d in recent_10]
        
        range_high = max(highs)
        range_low = min(lows)
        total_range = range_high - range_low
        
        if total_range == 0:
            return None
        
        in_range = all(range_low * 0.98 <= self._get_close(d) <= range_high * 1.02 for d in recent_10)
        
        if not in_range:
            return None
        
        first_5_delta = sum(delta_data[-10:-5]) if len(delta_data) >= 10 else 0
        last_5_delta = sum(delta_data[-5:]) if len(delta_data) >= 5 else 0
        
        distribution_detected = first_5_delta > 0 and last_5_delta < -first_5_delta * 0.5
        
        if not distribution_detected:
            return None
        
        current = data[-1]
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        
        sl = range_high + total_range * 0.1
        
        return Pattern(
            name='DISTRIBUTION',
            direction='short',
            strength=28,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=last_5_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence='very_strong',
            description=f'Distribution detected: smart money selling in range {range_low:.2f}-{range_high:.2f}'
        )


class LongPatternDetector(BasePatternDetector):
    """Детектор паттернов для LONG позиций"""
    
    def detect_all(self, data_15m: List[Dict], delta_15m: List[float],
                  ohlcv_1h: List[Dict] = None) -> List[Pattern]:
        """Обнаружить все LONG паттерны"""
        patterns = []
        
        rejection = self.detect_rejection_long(data_15m, delta_15m)
        if rejection:
            patterns.append(rejection)
        
        trap = self.detect_trap_short(data_15m, delta_15m)
        if trap:
            patterns.append(trap)
        
        mega = self.detect_mega_long(data_15m, delta_15m)
        if mega:
            patterns.append(mega)
        
        accumulation = self.detect_accumulation(data_15m, delta_15m)
        if accumulation:
            patterns.append(accumulation)
        
        return patterns
    
    def detect_rejection_long(self, data: List[Any], 
                             delta_data: List[float]) -> Optional[Pattern]:
        """REJECTION LONG: Отбой от поддержки вверх"""
        if len(data) < 3:
            return None
        
        current = data[-1]
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        
        total_range = self._get_high(current) - self._get_low(current)
        if total_range == 0:
            return None
        
        upper_wick = self._get_high(current) - max(self._get_open(current), self._get_close(current))
        lower_wick = min(self._get_open(current), self._get_close(current)) - self._get_low(current)
        body_size = abs(self._get_close(current) - self._get_open(current))
        
        is_hammer = (
            lower_wick > total_range * 0.6 and
            body_size < total_range * 0.3 and
            self._is_bullish(current) and
            self._get_close(current) > self._get_low(current) + total_range * 0.7
        )
        
        is_bullish_engulfing = False
        if len(data) >= 2:
            prev = data[-2]
            is_bullish_engulfing = (
                self._get_open(current) < self._get_close(prev) and
                self._get_close(current) > self._get_open(prev) and
                self._is_bullish(current) and
                self._get_low(current) < self._get_low(prev) and
                self._get_close(current) > self._get_high(prev)
            )
        
        if not (is_hammer or is_bullish_engulfing):
            return None
        
        if self._get_volume(current) < avg_volume * self.min_volume_mult:
            return None
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta < 0:
            return None
        
        if is_bullish_engulfing:
            confidence = 'very_strong'
            strength = 20
        elif lower_wick > total_range * 0.7:
            confidence = 'strong'
            strength = 18
        else:
            confidence = 'moderate'
            strength = 15
        
        sl = self._get_low(current) - (self._get_high(current) - self._get_low(current)) * 0.2
        
        return Pattern(
            name='REJECTION_LONG',
            direction='long',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=current_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence=confidence,
            description=f'Price rejected low with {"engulfing" if is_bullish_engulfing else "hammer"} pattern'
        )
    
    def detect_trap_short(self, data: List[Any], 
                         delta_data: List[float]) -> Optional[Pattern]:
        """TRAP SHORT: Ловушка для шортистов"""
        if len(data) < 5:
            return None
        
        recent_5 = data[-5:]
        lows = [self._get_low(d) for d in recent_5]
        
        support = min(lows[:-1])
        current = data[-1]
        
        fake_breakdown = (
            self._get_low(current) < support and
            self._get_close(current) > support and
            self._get_close(current) > self._get_open(current)
        )
        
        if not fake_breakdown:
            return None
        
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        if self._get_volume(current) < avg_volume * self.min_volume_mult:
            return None
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta < 1:
            return None
        
        sl = self._get_low(current) - (self._get_high(current) - self._get_low(current)) * 0.3
        
        return Pattern(
            name='TRAP_SHORT',
            direction='long',
            strength=22,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=current_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence='strong',
            description=f'Trap for shorts: fake breakdown below {support:.2f} then recovery'
        )
    
    def detect_mega_long(self, data: List[Any], 
                        delta_data: List[float]) -> Optional[Pattern]:
        """MEGA LONG: Доминация покупателей"""
        if len(data) < 5:
            return None
        
        recent_5 = data[-5:]
        green_candles = sum(1 for d in recent_5 if self._is_bullish(d))
        
        if green_candles < 4:
            return None
        
        volumes = [self._get_volume(d) for d in recent_5]
        avg_vol = np.mean(volumes)
        high_vol = all(v > avg_vol * 0.8 for v in volumes)
        
        if not high_vol:
            return None
        
        current = data[-1]
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta < 0:
            return None
        
        sl = min(self._get_low(d) for d in recent_5) * 0.99
        
        return Pattern(
            name='MEGA_LONG',
            direction='long',
            strength=25,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=current_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence='very_strong',
            description=f'MEGA LONG: {green_candles}/5 green candles, buyers dominating'
        )
    
    def detect_accumulation(self, data: List[Any], 
                           delta_data: List[float]) -> Optional[Pattern]:
        """ACCUMULATION: Крупный игрок накапливает"""
        if len(data) < 10:
            return None
        
        recent_10 = data[-10:]
        highs = [self._get_high(d) for d in recent_10]
        lows = [self._get_low(d) for d in recent_10]
        
        range_high = max(highs)
        range_low = min(lows)
        total_range = range_high - range_low
        
        if total_range == 0:
            return None
        
        in_range = all(range_low * 0.98 <= self._get_close(d) <= range_high * 1.02 for d in recent_10)
        
        if not in_range:
            return None
        
        first_5_delta = sum(delta_data[-10:-5]) if len(delta_data) >= 10 else 0
        last_5_delta = sum(delta_data[-5:]) if len(delta_data) >= 5 else 0
        
        accumulation_detected = first_5_delta < 0 and last_5_delta > abs(first_5_delta) * 0.5
        
        if not accumulation_detected:
            return None
        
        current = data[-1]
        avg_volume = np.mean([self._get_volume(d) for d in data[-20:]]) if len(data) >= 20 else self._get_volume(current)
        
        sl = range_low - total_range * 0.1
        
        return Pattern(
            name='ACCUMULATION',
            direction='long',
            strength=28,
            candles_ago=0,
            freshness=0,
            volume_multiplier=self._get_volume(current) / avg_volume if avg_volume > 0 else 1.0,
            delta_at_trigger=last_5_delta,
            entry_price=self._get_close(current),
            stop_loss=sl,
            confidence='very_strong',
            description=f'Accumulation detected: smart money buying in range {range_low:.2f}-{range_high:.2f}'
        )


# Example usage
if __name__ == "__main__":
    # Тестовые данные
    data = [
        {'open': 100, 'high': 105, 'low': 98, 'close': 103, 'volume': 1000},
        {'open': 103, 'high': 108, 'low': 102, 'close': 104, 'volume': 1200},
        {'open': 104, 'high': 109, 'low': 100, 'close': 101, 'volume': 1500},
    ]
    delta = [0.5, -1.2, 2.0]
    
    short_detector = ShortPatternDetector()
    patterns = short_detector.detect_all(data, delta)
    
    for p in patterns:
        print(f"{p.name}: {p.confidence} (strength: {p.strength})")
