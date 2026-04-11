"""
Pattern Detection System
8 паттернов: 4 для SHORT + 4 для LONG
Все детектируются на 15-минутном таймфрейме
"""

from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
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
    
    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Расчёт ATR (Average True Range)"""
        if len(df) < period:
            return (df['high'] - df['low']).mean()
        
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        
        return atr if not pd.isna(atr) else high_low.mean()
    
    def _get_price_trend(self, df: pd.DataFrame, lookback: int = 5) -> str:
        """Определить тренд цены"""
        if len(df) < lookback:
            return 'sideways'
        
        recent = df.tail(lookback)
        first_price = recent['close'].iloc[0]
        last_price = recent['close'].iloc[-1]
        
        change = (last_price - first_price) / first_price * 100
        
        if change > 2:
            return 'rising'
        elif change < -2:
            return 'falling'
        else:
            return 'sideways'
    
    def _get_volume_trend(self, df: pd.DataFrame, lookback: int = 5) -> str:
        """Определить тренд объёма"""
        if len(df) < lookback + 5:
            return 'stable'
        
        recent_vol = df['volume'].tail(lookback).mean()
        prev_vol = df['volume'].tail(lookback + 5).head(5).mean()
        
        ratio = recent_vol / prev_vol if prev_vol > 0 else 1
        
        if ratio > 1.5:
            return 'rising'
        elif ratio < 0.7:
            return 'falling'
        else:
            return 'stable'


class ShortPatternDetector(BasePatternDetector):
    """
    Детектор паттернов для SHORT позиций
    """
    
    def detect_all(self, df_15m: pd.DataFrame, delta_15m: List[float],
                  ohlcv_1h: pd.DataFrame = None) -> List[Pattern]:
        """
        Обнаружить все SHORT паттерны
        
        Args:
            df_15m: DataFrame с 15m свечами (OHLCV)
            delta_15m: Дельта для каждой 15m свечи
            ohlcv_1h: 1h свечи для контекста (опционально)
        
        Returns:
            Список обнаруженных паттернов
        """
        patterns = []
        
        # Проверяем каждый паттерн
        rejection = self.detect_rejection_short(df_15m, delta_15m)
        if rejection:
            patterns.append(rejection)
        
        trap = self.detect_trap_long(df_15m, delta_15m)
        if trap:
            patterns.append(trap)
        
        mega = self.detect_mega_short(df_15m, delta_15m)
        if mega:
            patterns.append(mega)
        
        distribution = self.detect_distribution(df_15m, delta_15m)
        if distribution:
            patterns.append(distribution)
        
        return patterns
    
    def detect_rejection_short(self, df: pd.DataFrame, 
                              delta_data: List[float]) -> Optional[Pattern]:
        """
        REJECTION SHORT: Цена отбилась от сопротивления вниз
        
        Признаки:
        - Свеча с длинным верхним фитилем
        - Верхняя тень > 50% от диапазона
        - Закрытие в нижней части свечи
        - Высокий объём
        - Отрицательная дельта
        """
        if len(df) < 3:
            return None
        
        current = df.iloc[-1]
        avg_volume = df['volume'].tail(20).mean()
        
        # Анализ свечи
        total_range = current['high'] - current['low']
        if total_range == 0:
            return None
        
        upper_wick = current['high'] - max(current['open'], current['close'])
        lower_wick = min(current['open'], current['close']) - current['low']
        body_size = abs(current['close'] - current['open'])
        
        # Критерии shooting star / bearish pin bar
        is_shooting_star = (
            upper_wick > total_range * 0.6 and      # Длинная верхняя тень
            body_size < total_range * 0.3 and        # Маленькое тело
            current['close'] < current['open'] and # Медвежье закрытие
            current['close'] < current['low'] + total_range * 0.3  # Закрытие внизу
        )
        
        is_bearish_engulfing = False
        if len(df) >= 2:
            prev = df.iloc[-2]
            is_bearish_engulfing = (
                current['open'] > prev['close'] and
                current['close'] < prev['open'] and
                current['close'] < current['open'] and  # Медвежья свеча
                current['high'] > prev['high'] and      # Пробой максимума
                current['close'] < prev['low']          # Закрытие ниже минимума пред.
            )
        
        if not (is_shooting_star or is_bearish_engulfing):
            return None
        
        # Проверка объёма
        if current['volume'] < avg_volume * self.min_volume_mult:
            return None
        
        # Проверка дельты
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta > 0:  # Положительная дельта на откате — плохо
            return None
        
        # Уверенность
        if is_bearish_engulfing:
            confidence = 'very_strong'
            strength = 20
        elif upper_wick > total_range * 0.7:
            confidence = 'strong'
            strength = 18
        else:
            confidence = 'moderate'
            strength = 15
        
        # SL за свечой
        sl = current['high'] + (current['high'] - current['low']) * 0.2
        
        return Pattern(
            name='REJECTION_SHORT',
            direction='short',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=current['volume'] / avg_volume if avg_volume > 0 else 1,
            delta_at_trigger=current_delta,
            entry_price=current['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'Price rejected from high with {"engulfing" if is_bearish_engulfing else "shooting star"} pattern'
        )
    
    def detect_trap_long(self, df: pd.DataFrame, 
                        delta_data: List[float]) -> Optional[Pattern]:
        """
        TRAP LONG: Ловушка для лонгистов
        
        Признаки:
        - Пробой сопротивления (вынос стопов)
        - Возврат ниже уровня
        - Медвежье закрытие
        - Высокий объём на пробое
        """
        if len(df) < 2:
            return None
        
        current = df.iloc[-1]
        prev = df.iloc[-2]
        
        # Ищем resistance из недавних максимумов
        recent_highs = df['high'].tail(10).tolist()
        resistance = max(recent_highs[:-1]) if len(recent_highs) > 1 else prev['high']
        
        # Фаза 1: Пробой (предыдущая свеча)
        breakout = prev['high'] > resistance * 1.002
        high_volume_break = prev['volume'] > df['volume'].tail(20).mean() * 1.8
        
        if not (breakout and high_volume_break):
            return None
        
        # Фаза 2: Возврат (текущая свеча)
        returned = current['close'] < resistance
        bearish_close = current['close'] < current['open']
        
        if not (returned and bearish_close):
            return None
        
        # Проверка дельты (отрицательная на возврате)
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta > 0:
            return None
        
        # Уверенность
        if current['close'] < prev['low']:
            confidence = 'very_strong'  # Engulfing предыдущей
            strength = 22
        else:
            confidence = 'strong'
            strength = 18
        
        sl = max(prev['high'], current['high']) * 1.005
        
        return Pattern(
            name='TRAP_LONG',
            direction='short',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=prev['volume'] / df['volume'].tail(20).mean(),
            delta_at_trigger=current_delta,
            entry_price=current['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'Trap for longs: fake breakout above {resistance:.2f} then reversal'
        )
    
    def detect_mega_short(self, df: pd.DataFrame, 
                         delta_data: List[float]) -> Optional[Pattern]:
        """
        MEGA SHORT: Доминация продавцов
        
        Признаки:
        - 4-5 красных свечей подряд
        - Отрицательная дельта на каждой
        - Растущий объём
        - Ускорение падения
        """
        if len(df) < 6:
            return None
        
        recent = df.tail(5)
        
        # Считаем красные свечи
        red_candles = sum(1 for _, c in recent.iterrows() if c['close'] < c['open'])
        
        if red_candles < 4:
            return None
        
        # Проверяем дельту
        recent_delta = delta_data[-5:] if len(delta_data) >= 5 else []
        negative_delta_count = sum(1 for d in recent_delta if d < 0)
        
        if negative_delta_count < 4:
            return None
        
        # Проверяем тренд объёма (растёт?)
        volumes = recent['volume'].tolist()
        rising_volume = all(volumes[i] > volumes[i-1] * 0.9 for i in range(1, len(volumes)))
        
        # Проверяем ускорение падения
        price_changes = []
        for i in range(len(recent) - 1):
            change = (recent.iloc[i+1]['close'] - recent.iloc[i]['close']) / recent.iloc[i]['close'] * 100
            price_changes.append(change)
        
        accelerating = all(price_changes[i] <= price_changes[i-1] for i in range(1, len(price_changes)))
        
        avg_volume = df['volume'].tail(20).mean()
        current_vol_mult = volumes[-1] / avg_volume if avg_volume > 0 else 1
        
        # Уверенность
        if red_candles == 5 and negative_delta_count == 5 and rising_volume:
            confidence = 'very_strong'
            strength = 28
        elif red_candles >= 4 and negative_delta_count >= 4:
            confidence = 'strong'
            strength = 25
        else:
            confidence = 'moderate'
            strength = 20
        
        sl = df['high'].tail(5).max() * 1.008
        
        return Pattern(
            name='MEGA_SHORT',
            direction='short',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=current_vol_mult,
            delta_at_trigger=recent_delta[-1] if recent_delta else 0,
            entry_price=df.iloc[-1]['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'MEGA SHORT: {red_candles}/5 red candles, sellers dominating'
        )
    
    def detect_distribution(self, df: pd.DataFrame, 
                           delta_data: List[float]) -> Optional[Pattern]:
        """
        DISTRIBUTION: Крупный игрок распродаёт позицию
        
        Признаки:
        - Цена боковит или медленно растёт
        - Огромный объём
        - Отрицательная дельта (скрытые продажи)
        - Маленькие тела свечей
        """
        if len(df) < 10:
            return None
        
        recent = df.tail(10)
        avg_volume = df['volume'].tail(40).mean()
        
        # Проверка объёма
        high_vol_candles = sum(1 for _, c in recent.iterrows() if c['volume'] > avg_volume * 2)
        
        if high_vol_candles < 6:  # Минимум 6 свечей с высоким объёмом
            return None
        
        # Проверка ценового диапазона (боковик)
        price_range = (recent['high'].max() - recent['low'].min()) / recent['low'].min() * 100
        
        if price_range > 3:  # Слишком много движения
            return None
        
        # Проверка размеров тел (маленькие)
        body_sizes = []
        for _, c in recent.iterrows():
            body = abs(c['close'] - c['open'])
            range_size = c['high'] - c['low']
            if range_size > 0:
                body_sizes.append(body / range_size)
        
        avg_body_ratio = sum(body_sizes) / len(body_sizes) if body_sizes else 1
        
        if avg_body_ratio > 0.4:  # Тела слишком большие
            return None
        
        # Проверка дельты (отрицательная)
        recent_delta = delta_data[-10:] if len(delta_data) >= 10 else []
        avg_delta = sum(recent_delta) / len(recent_delta) if recent_delta else 0
        
        if avg_delta > -0.01:  # Должна быть отрицательная
            return None
        
        # Уверенность
        if high_vol_candles >= 8 and avg_body_ratio < 0.25:
            confidence = 'very_strong'
            strength = 30
        elif high_vol_candles >= 6:
            confidence = 'strong'
            strength = 27
        else:
            confidence = 'moderate'
            strength = 24
        
        sl = recent['high'].max() * 1.005
        
        return Pattern(
            name='DISTRIBUTION',
            direction='short',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=recent.iloc[-1]['volume'] / avg_volume,
            delta_at_trigger=avg_delta,
            entry_price=df.iloc[-1]['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'Whale distribution: {high_vol_candles}/10 high vol candles, avg delta {avg_delta:.2f}%'
        )


class LongPatternDetector(BasePatternDetector):
    """
    Детектор паттернов для LONG позиций
    Зеркальные SHORT паттернам
    """
    
    def detect_all(self, df_15m: pd.DataFrame, delta_15m: List[float],
                  ohlcv_1h: pd.DataFrame = None) -> List[Pattern]:
        """Обнаружить все LONG паттерны"""
        patterns = []
        
        rejection = self.detect_rejection_long(df_15m, delta_15m)
        if rejection:
            patterns.append(rejection)
        
        trap = self.detect_trap_short(df_15m, delta_15m)
        if trap:
            patterns.append(trap)
        
        mega = self.detect_mega_long(df_15m, delta_15m)
        if mega:
            patterns.append(mega)
        
        accumulation = self.detect_accumulation(df_15m, delta_15m)
        if accumulation:
            patterns.append(accumulation)
        
        return patterns
    
    def detect_rejection_long(self, df: pd.DataFrame, 
                             delta_data: List[float]) -> Optional[Pattern]:
        """
        REJECTION LONG: Отбой от поддержки вверх
        
        Признаки:
        - Свеча с длинным нижним фитилем
        - Бычье закрытие
        - Положительная дельта
        """
        if len(df) < 3:
            return None
        
        current = df.iloc[-1]
        avg_volume = df['volume'].tail(20).mean()
        
        total_range = current['high'] - current['low']
        if total_range == 0:
            return None
        
        lower_wick = min(current['open'], current['close']) - current['low']
        body_size = abs(current['close'] - current['open'])
        
        # Hammer / Dragonfly Doji
        is_hammer = (
            lower_wick > total_range * 0.6 and
            body_size < total_range * 0.3 and
            current['close'] > current['open'] and  # Бычье закрытие
            current['close'] > current['low'] + total_range * 0.7  # Закрытие вверху
        )
        
        is_bullish_engulfing = False
        if len(df) >= 2:
            prev = df.iloc[-2]
            is_bullish_engulfing = (
                current['open'] < prev['close'] and
                current['close'] > prev['open'] and
                current['close'] > current['open'] and
                current['low'] < prev['low'] and
                current['close'] > prev['high']
            )
        
        if not (is_hammer or is_bullish_engulfing):
            return None
        
        if current['volume'] < avg_volume * self.min_volume_mult:
            return None
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta < 0:  # Отрицательная дельта — плохо
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
        
        sl = current['low'] - (current['high'] - current['low']) * 0.2
        
        return Pattern(
            name='REJECTION_LONG',
            direction='long',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=current['volume'] / avg_volume if avg_volume > 0 else 1,
            delta_at_trigger=current_delta,
            entry_price=current['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'Price rejected low with {"engulfing" if is_bullish_engulfing else "hammer"} pattern'
        )
    
    def detect_trap_short(self, df: pd.DataFrame, 
                         delta_data: List[float]) -> Optional[Pattern]:
        """
        TRAP SHORT: Ловушка для шортистов
        
        Признаки:
        - Пробой поддержки вниз
        - Возврат выше уровня
        - Бычье закрытие
        """
        if len(df) < 2:
            return None
        
        current = df.iloc[-1]
        prev = df.iloc[-2]
        
        recent_lows = df['low'].tail(10).tolist()
        support = min(recent_lows[:-1]) if len(recent_lows) > 1 else prev['low']
        
        # Пробой вниз
        breakdown = prev['low'] < support * 0.998
        high_volume_break = prev['volume'] > df['volume'].tail(20).mean() * 1.8
        
        if not (breakdown and high_volume_break):
            return None
        
        # Возврат
        returned = current['close'] > support
        bullish_close = current['close'] > current['open']
        
        if not (returned and bullish_close):
            return None
        
        current_delta = delta_data[-1] if delta_data else 0
        if current_delta < 0:
            return None
        
        if current['close'] > prev['high']:
            confidence = 'very_strong'
            strength = 22
        else:
            confidence = 'strong'
            strength = 18
        
        sl = min(prev['low'], current['low']) * 0.995
        
        return Pattern(
            name='TRAP_SHORT',
            direction='long',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=prev['volume'] / df['volume'].tail(20).mean(),
            delta_at_trigger=current_delta,
            entry_price=current['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'Trap for shorts: fake breakdown below {support:.2f} then recovery'
        )
    
    def detect_mega_long(self, df: pd.DataFrame, 
                        delta_data: List[float]) -> Optional[Pattern]:
        """
        MEGA LONG: Доминация покупателей
        
        Признаки:
        - 4-5 зелёных свечей подряд
        - Положительная дельта
        - Растущий объём
        """
        if len(df) < 6:
            return None
        
        recent = df.tail(5)
        
        green_candles = sum(1 for _, c in recent.iterrows() if c['close'] > c['open'])
        
        if green_candles < 4:
            return None
        
        recent_delta = delta_data[-5:] if len(delta_data) >= 5 else []
        positive_delta_count = sum(1 for d in recent_delta if d > 0)
        
        if positive_delta_count < 4:
            return None
        
        volumes = recent['volume'].tolist()
        rising_volume = all(volumes[i] > volumes[i-1] * 0.9 for i in range(1, len(volumes)))
        
        avg_volume = df['volume'].tail(20).mean()
        current_vol_mult = volumes[-1] / avg_volume if avg_volume > 0 else 1
        
        if green_candles == 5 and positive_delta_count == 5 and rising_volume:
            confidence = 'very_strong'
            strength = 28
        elif green_candles >= 4 and positive_delta_count >= 4:
            confidence = 'strong'
            strength = 25
        else:
            confidence = 'moderate'
            strength = 20
        
        sl = df['low'].tail(5).min() * 0.992
        
        return Pattern(
            name='MEGA_LONG',
            direction='long',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=current_vol_mult,
            delta_at_trigger=recent_delta[-1] if recent_delta else 0,
            entry_price=df.iloc[-1]['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'MEGA LONG: {green_candles}/5 green candles, buyers dominating'
        )
    
    def detect_accumulation(self, df: pd.DataFrame, 
                           delta_data: List[float]) -> Optional[Pattern]:
        """
        ACCUMULATION: Крупный игрок накапливает
        
        Признаки:
        - Цена боковит или медленно падает
        - Огромный объём
        - Положительная дельта
        """
        if len(df) < 10:
            return None
        
        recent = df.tail(10)
        avg_volume = df['volume'].tail(40).mean()
        
        high_vol_candles = sum(1 for _, c in recent.iterrows() if c['volume'] > avg_volume * 2)
        
        if high_vol_candles < 6:
            return None
        
        price_range = (recent['high'].max() - recent['low'].min()) / recent['low'].min() * 100
        
        if price_range > 3:
            return None
        
        body_sizes = []
        for _, c in recent.iterrows():
            body = abs(c['close'] - c['open'])
            range_size = c['high'] - c['low']
            if range_size > 0:
                body_sizes.append(body / range_size)
        
        avg_body_ratio = sum(body_sizes) / len(body_sizes) if body_sizes else 1
        
        if avg_body_ratio > 0.4:
            return None
        
        recent_delta = delta_data[-10:] if len(delta_data) >= 10 else []
        avg_delta = sum(recent_delta) / len(recent_delta) if recent_delta else 0
        
        if avg_delta < 0.01:  # Должна быть положительная
            return None
        
        if high_vol_candles >= 8 and avg_body_ratio < 0.25:
            confidence = 'very_strong'
            strength = 30
        elif high_vol_candles >= 6:
            confidence = 'strong'
            strength = 27
        else:
            confidence = 'moderate'
            strength = 24
        
        sl = recent['low'].min() * 0.995
        
        return Pattern(
            name='ACCUMULATION',
            direction='long',
            strength=strength,
            candles_ago=0,
            freshness=0,
            volume_multiplier=recent.iloc[-1]['volume'] / avg_volume,
            delta_at_trigger=avg_delta,
            entry_price=df.iloc[-1]['close'],
            stop_loss=sl,
            confidence=confidence,
            description=f'Whale accumulation: {high_vol_candles}/10 high vol candles, avg delta +{avg_delta:.2f}%'
        )


# ============================================================================
# FACTORY
# ============================================================================

def get_pattern_detector(direction: str) -> BasePatternDetector:
    """
    Получить детектор паттернов для направления
    
    Args:
        direction: 'short' или 'long'
    
    Returns:
        Экземпляр детектора
    """
    if direction == 'short':
        return ShortPatternDetector()
    elif direction == 'long':
        return LongPatternDetector()
    else:
        raise ValueError(f"Unknown direction: {direction}")


# ============================================================================
# EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Создаём тестовые данные
    import numpy as np
    
    # Генерируем свечи для теста
    np.random.seed(42)
    n = 50
    
    base_price = 70000
    data = {
        'open': base_price + np.random.randn(n).cumsum() * 100,
        'high': base_price + np.random.randn(n).cumsum() * 100 + 200,
        'low': base_price + np.random.randn(n).cumsum() * 100 - 200,
        'close': base_price + np.random.randn(n).cumsum() * 100,
        'volume': np.random.rand(n) * 1000000 + 500000
    }
    
    # Корректируем чтобы high > low
    for i in range(n):
        data['high'][i] = max(data['high'][i], data['open'][i], data['close'][i]) + 100
        data['low'][i] = min(data['low'][i], data['open'][i], data['close'][i]) - 100
    
    df = pd.DataFrame(data)
    delta = np.random.randn(n) * 2  # Случайная дельта
    
    print("SHORT PATTERNS:")
    print("=" * 50)
    short_detector = ShortPatternDetector()
    short_patterns = short_detector.detect_all(df, delta.tolist())
    
    for p in short_patterns:
        print(f"✓ {p.name}: strength={p.strength}, confidence={p.confidence}")
        print(f"  {p.description}")
    
    if not short_patterns:
        print("Нет паттернов (тестовые данные случайные)")
    
    print("\nLONG PATTERNS:")
    print("=" * 50)
    long_detector = LongPatternDetector()
    long_patterns = long_detector.detect_all(df, delta.tolist())
    
    for p in long_patterns:
        print(f"✓ {p.name}: strength={p.strength}, confidence={p.confidence}")
        print(f"  {p.description}")
    
    if not long_patterns:
        print("Нет паттернов (тестовые данные случайные)")
