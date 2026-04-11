"""
SMC + ICT Pattern Detector
Smart Money Concepts + Inner Circle Trader
На основе методологии Статхэма (из Pine Script v130)
"""

import pandas as pd
import numpy as np
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
from enum import Enum


class SMCType(Enum):
    """Типы SMC структур"""
    ORDER_BLOCK_BULLISH = "ob_bullish"
    ORDER_BLOCK_BEARISH = "ob_bearish"
    FAIR_VALUE_GAP_BULLISH = "fvg_bullish"
    FAIR_VALUE_GAP_BEARISH = "fvg_bearish"
    BREAKER_BLOCK = "breaker"
    MITIGATION_BLOCK = "mitigation"
    LIQUIDITY_SWEEP_HIGH = "sweep_high"
    LIQUIDITY_SWEEP_LOW = "sweep_low"
    CHoCH_BULLISH = "choch_bullish"
    CHoCH_BEARISH = "choch_bearish"
    BOS_BULLISH = "bos_bullish"
    BOS_BEARISH = "bos_bearish"


@dataclass
class SMCStructure:
    """SMC структура (Order Block, FVG, etc.)"""
    type: SMCType
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    top: float
    bottom: float
    volume: float
    strength: int  # 1-10
    is_active: bool
    mitigation_price: Optional[float] = None
    
    @property
    def height(self) -> float:
        return self.top - self.bottom
    
    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class LiquidityZone:
    """Зона ликвидности (EQH/EQL, PWH/PWL)"""
    level: float
    type: str  # 'equal_highs', 'equal_lows', 'pwh', 'pwl'
    touches: int
    volume_at_level: float
    is_swept: bool = False
    sweep_time: Optional[pd.Timestamp] = None


@dataclass
class ICTSession:
    """ICT Trading Session"""
    name: str  # 'asia', 'london', 'ny', 'ny_pm'
    start_hour: int
    end_hour: int
    high: float
    low: float
    open: float
    close: float
    displacement: float  # Движение внутри сессии


class SMCICTDetector:
    """
    Детектор SMC+ICT паттернов
    На основе концепций: ICT, Algo Forex, Smart Money Concepts
    """
    
    def __init__(self, 
                 ob_lookback: int = 5,
                 fvg_min_size: float = 0.001,  # 0.1%
                 min_volume_mult: float = 1.5):
        self.ob_lookback = ob_lookback
        self.fvg_min_size = fvg_min_size
        self.min_volume_mult = min_volume_mult
    
    # =========================================================================
    # ORDER BLOCKS (OB)
    # =========================================================================
    
    def detect_order_blocks(self, df: pd.DataFrame) -> List[SMCStructure]:
        """
        Детекция Order Blocks
        
        Bullish OB: Последняя медвежья свеча перед импульсом вверх
        Bearish OB: Последняя бычья свеча перед импульсом вниз
        """
        obs = []
        
        for i in range(self.ob_lookback, len(df) - 1):
            window = df.iloc[i-self.ob_lookback:i+1]
            
            # Bullish Order Block
            bullish_ob = self._detect_bullish_ob(window, i, df)
            if bullish_ob:
                obs.append(bullish_ob)
            
            # Bearish Order Block
            bearish_ob = self._detect_bearish_ob(window, i, df)
            if bearish_ob:
                obs.append(bearish_ob)
        
        # Фильтруем и сортируем по силе
        obs = self._filter_quality_obs(obs)
        
        return obs
    
    def _detect_bullish_ob(self, window: pd.DataFrame, 
                          idx: int, df: pd.DataFrame) -> Optional[SMCStructure]:
        """Детекция бычьего Order Block"""
        
        # Ищем последнюю красную свечу перед импульсом вверх
        for j in range(len(window) - 1, -1, -1):
            candle = window.iloc[j]
            
            # Должна быть медвежья свеча
            if candle['close'] >= candle['open']:  # Зелёная или дожи
                continue
            
            # Проверяем что после неё идёт импульс вверх
            if idx + 1 < len(df):
                next_candles = df.iloc[idx+1:min(idx+4, len(df))]
                
                # Должен быть рост минимум на 0.5%
                price_change = (next_candles['close'].iloc[-1] - candle['close']) / candle['close']
                
                if price_change > 0.005:  # 0.5% импульс
                    # Проверяем объём
                    avg_vol = window['volume'].mean()
                    if candle['volume'] > avg_vol * 0.8:  # Не слишком маленький объём
                        return SMCStructure(
                            type=SMCType.ORDER_BLOCK_BULLISH,
                            start_time=candle.name if isinstance(candle.name, pd.Timestamp) else pd.Timestamp.now(),
                            end_time=candle.name if isinstance(candle.name, pd.Timestamp) else pd.Timestamp.now(),
                            top=candle['open'],  # Верх OB - открытие медвежьей свечи
                            bottom=candle['low'],  # Низ OB - минимум
                            volume=candle['volume'],
                            strength=min(10, int(price_change * 1000)),  # Сила от импульса
                            is_active=True,
                            mitigation_price=None
                        )
        
        return None
    
    def _detect_bearish_ob(self, window: pd.DataFrame,
                          idx: int, df: pd.DataFrame) -> Optional[SMCStructure]:
        """Детекция медвежьего Order Block"""
        
        for j in range(len(window) - 1, -1, -1):
            candle = window.iloc[j]
            
            # Должна быть бычья свеча
            if candle['close'] <= candle['open']:  # Красная или дожи
                continue
            
            # Проверяем что после неё идёт импульс вниз
            if idx + 1 < len(df):
                next_candles = df.iloc[idx+1:min(idx+4, len(df))]
                
                price_change = (next_candles['close'].iloc[-1] - candle['close']) / candle['close']
                
                if price_change < -0.005:  # -0.5% импульс
                    avg_vol = window['volume'].mean()
                    if candle['volume'] > avg_vol * 0.8:
                        return SMCStructure(
                            type=SMCType.ORDER_BLOCK_BEARISH,
                            start_time=candle.name if isinstance(candle.name, pd.Timestamp) else pd.Timestamp.now(),
                            end_time=candle.name if isinstance(candle.name, pd.Timestamp) else pd.Timestamp.now(),
                            top=candle['high'],
                            bottom=candle['open'],  # Низ OB - открытие бычьей свечи
                            volume=candle['volume'],
                            strength=min(10, int(abs(price_change) * 1000)),
                            is_active=True,
                            mitigation_price=None
                        )
        
        return None
    
    def _filter_quality_obs(self, obs: List[SMCStructure]) -> List[SMCStructure]:
        """Фильтрация качественных OB"""
        # Убираем слишком близкие друг к другу
        filtered = []
        last_ob_end = None
        
        for ob in sorted(obs, key=lambda x: x.strength, reverse=True):
            if last_ob_end is None or (ob.start_time - last_ob_end).total_seconds() > 3600:  # 1 час
                filtered.append(ob)
                last_ob_end = ob.end_time
        
        return filtered[:10]  # Максимум 10 OB
    
    # =========================================================================
    # FAIR VALUE GAPS (FVG)
    # =========================================================================
    
    def detect_fvgs(self, df: pd.DataFrame) -> List[SMCStructure]:
        """
        Детекция Fair Value Gaps
        
        Bullish FVG: Low текущей > High двух свечей назад (импульс вверх)
        Bearish FVG: High текущей < Low двух свечей назад (импульс вниз)
        """
        fvgs = []
        
        for i in range(3, len(df)):
            current = df.iloc[i]
            prev1 = df.iloc[i-1]  # Первая свеча импульса
            prev2 = df.iloc[i-2]  # Вторая свеча (зазор)
            
            # Bullish FVG
            if current['low'] > prev2['high']:
                gap_size = current['low'] - prev2['high']
                gap_pct = gap_size / prev2['high']
                
                if gap_pct > self.fvg_min_size:
                    # Проверяем что импульс был сильным
                    impulse = (current['close'] - prev2['open']) / prev2['open']
                    if impulse > 0.01:  # 1% импульс
                        fvgs.append(SMCStructure(
                            type=SMCType.FAIR_VALUE_GAP_BULLISH,
                            start_time=prev2.name,
                            end_time=current.name,
                            top=current['low'],
                            bottom=prev2['high'],
                            volume=current['volume'],
                            strength=min(10, int(gap_pct * 10000)),
                            is_active=True
                        ))
            
            # Bearish FVG
            if current['high'] < prev2['low']:
                gap_size = prev2['low'] - current['high']
                gap_pct = gap_size / prev2['low']
                
                if gap_pct > self.fvg_min_size:
                    impulse = (current['close'] - prev2['open']) / prev2['open']
                    if impulse < -0.01:  # -1% импульс
                        fvgs.append(SMCStructure(
                            type=SMCType.FAIR_VALUE_GAP_BEARISH,
                            start_time=prev2.name,
                            end_time=current.name,
                            top=prev2['low'],
                            bottom=current['high'],
                            volume=current['volume'],
                            strength=min(10, int(gap_pct * 10000)),
                            is_active=True
                        ))
        
        return fvgs[:15]  # Максимум 15 FVG
    
    # =========================================================================
    # LIQUIDITY SWEEPS
    # =========================================================================
    
    def detect_liquidity_sweeps(self, df: pd.DataFrame, 
                                lookback: int = 20) -> List[LiquidityZone]:
        """
        Детекция ликвидности (Equal Highs/Lows, PWH/PWL)
        
        Sweep: Цена берёт ликвидность за уровнем и возвращается
        """
        sweeps = []
        
        # Ищем Equal Highs (два примерно одинаковых максимума)
        highs = df['high'].rolling(window=5).max()
        
        for i in range(lookback, len(df) - 1):
            current_high = df.iloc[i]['high']
            
            # Ищем предыдущий похожий максимум
            for j in range(i - lookback, i - 2):
                prev_high = df.iloc[j]['high']
                
                # Если максимумы примерно равны (0.1% разница)
                if abs(current_high - prev_high) / prev_high < 0.001:
                    # Проверяем был ли sweep (цена взяла ликвидность и вернулась)
                    if i + 1 < len(df):
                        next_close = df.iloc[i + 1]['close']
                        if next_close < current_high * 0.998:  # Вернулась ниже
                            sweeps.append(LiquidityZone(
                                level=current_high,
                                type='equal_highs',
                                touches=2,
                                volume_at_level=df.iloc[i]['volume'],
                                is_swept=True,
                                sweep_time=df.iloc[i].name if isinstance(df.iloc[i].name, pd.Timestamp) else None
                            ))
        
        # Ищем Equal Lows (такая же логика для минимумов)
        for i in range(lookback, len(df) - 1):
            current_low = df.iloc[i]['low']
            
            for j in range(i - lookback, i - 2):
                prev_low = df.iloc[j]['low']
                
                if abs(current_low - prev_low) / prev_low < 0.001:
                    if i + 1 < len(df):
                        next_close = df.iloc[i + 1]['close']
                        if next_close > current_low * 1.002:  # Вернулась выше
                            sweeps.append(LiquidityZone(
                                level=current_low,
                                type='equal_lows',
                                touches=2,
                                volume_at_level=df.iloc[i]['volume'],
                                is_swept=True,
                                sweep_time=df.iloc[i].name if isinstance(df.iloc[i].name, pd.Timestamp) else None
                            ))
        
        return sweeps[:10]
    
    # =========================================================================
    # CHoCH / BOS (Change of Character / Break of Structure)
    # =========================================================================
    
    def detect_structure_breaks(self, df: pd.DataFrame) -> List[SMCStructure]:
        """
        Детекция CHoCH и BOS
        
        CHoCH: Смена характера (бычий → медвежий или наоборот)
        BOS: Пробой структуры (продолжение тренда)
        """
        breaks = []
        
        # Определяем структуру через swing highs/lows
        swing_highs = self._find_swing_highs(df)
        swing_lows = self._find_swing_lows(df)
        
        # Ищем CHoCH (смена)
        for i in range(2, len(swing_highs)):
            # Bearish CHoCH: новый максимум ниже предыдущего
            if swing_highs[i] < swing_highs[i-1] and swing_highs[i-1] > swing_highs[i-2]:
                breaks.append(SMCStructure(
                    type=SMCType.CHoch_BEARISH,
                    start_time=df.iloc[swing_highs[i]].name,
                    end_time=df.iloc[swing_highs[i]].name,
                    top=df.iloc[swing_highs[i]]['high'],
                    bottom=df.iloc[swing_lows[i-1]]['low'] if i-1 < len(swing_lows) else df.iloc[swing_highs[i]]['low'],
                    volume=df.iloc[swing_highs[i]]['volume'],
                    strength=7,
                    is_active=True
                ))
        
        for i in range(2, len(swing_lows)):
            # Bullish CHoCH: новый минимум выше предыдущего
            if swing_lows[i] > swing_lows[i-1] and swing_lows[i-1] < swing_lows[i-2]:
                breaks.append(SMCStructure(
                    type=SMCType.CHoch_BULLISH,
                    start_time=df.iloc[swing_lows[i]].name,
                    end_time=df.iloc[swing_lows[i]].name,
                    top=df.iloc[swing_highs[i-1]]['high'] if i-1 < len(swing_highs) else df.iloc[swing_lows[i]]['high'],
                    bottom=df.iloc[swing_lows[i]]['low'],
                    volume=df.iloc[swing_lows[i]]['volume'],
                    strength=7,
                    is_active=True
                ))
        
        return breaks
    
    def _find_swing_highs(self, df: pd.DataFrame, window: int = 5) -> List[int]:
        """Находит индексы swing highs"""
        highs = []
        for i in range(window, len(df) - window):
            if df.iloc[i]['high'] == df.iloc[i-window:i+window+1]['high'].max():
                highs.append(i)
        return highs
    
    def _find_swing_lows(self, df: pd.DataFrame, window: int = 5) -> List[int]:
        """Находит индексы swing lows"""
        lows = []
        for i in range(window, len(df) - window):
            if df.iloc[i]['low'] == df.iloc[i-window:i+window+1]['low'].min():
                lows.append(i)
        return lows
    
    # =========================================================================
    # ICT TIME CONCEPTS
    # =========================================================================
    
    def get_ict_session(self, df: pd.DataFrame, timestamp: pd.Timestamp) -> ICTSession:
        """
        Определяет ICT сессию по времени
        
        Asia: 00:00 - 08:00 UTC
        London: 08:00 - 12:00 UTC
        NY AM: 12:00 - 16:00 UTC
        NY PM: 16:00 - 20:00 UTC
        """
        hour = timestamp.hour
        
        if 0 <= hour < 8:
            name = 'asia'
            start, end = 0, 8
        elif 8 <= hour < 12:
            name = 'london'
            start, end = 8, 12
        elif 12 <= hour < 16:
            name = 'ny'
            start, end = 12, 16
        else:
            name = 'ny_pm'
            start, end = 16, 20
        
        # Рассчитываем статистику сессии
        session_data = df[(df.index.hour >= start) & (df.index.hour < end)]
        
        if len(session_data) == 0:
            return ICTSession(name, start, end, 0, 0, 0, 0, 0)
        
        return ICTSession(
            name=name,
            start_hour=start,
            end_hour=end,
            high=session_data['high'].max(),
            low=session_data['low'].min(),
            open=session_data['open'].iloc[0],
            close=session_data['close'].iloc[-1],
            displacement=(session_data['close'].iloc[-1] - session_data['open'].iloc[0]) / session_data['open'].iloc[0]
        )
    
    def is_ict_killzone(self, timestamp: pd.Timestamp) -> bool:
        """Проверяет находимся ли в ICT Killzone (лучшее время для входов)"""
        hour = timestamp.hour
        minute = timestamp.minute
        
        # London Open Killzone: 08:00 - 10:00 UTC
        if hour == 8 or (hour == 9 and minute < 30):
            return True
        
        # NY Open Killzone: 12:00 - 14:00 UTC
        if hour == 12 or (hour == 13 and minute < 30):
            return True
        
        # NY Close Killzone: 19:00 - 21:00 UTC
        if hour == 19 or hour == 20:
            return True
        
        return False
    
    # =========================================================================
    # PREMIUM / DISCOUNT (ICT Concept)
    # =========================================================================
    
    def calculate_fibonacci_levels(self, df: pd.DataFrame, 
                                   lookback: int = 20) -> Dict[str, float]:
        """
        Рассчитывает уровни Фибоначчи (Premium/Discount)
        
        Premium: Выше 50% (дорого)
        Discount: Ниже 50% (дешево)
        """
        recent = df.tail(lookback)
        
        high = recent['high'].max()
        low = recent['low'].min()
        range_size = high - low
        
        return {
            'fib_0': high,
            'fib_236': high - range_size * 0.236,
            'fib_382': high - range_size * 0.382,
            'fib_50': high - range_size * 0.5,  # Equilibrium
            'fib_618': high - range_size * 0.618,  # OTE (Optimal Trade Entry)
            'fib_786': high - range_size * 0.786,
            'fib_100': low
        }
    
    def is_price_at_ote(self, price: float, fib_levels: Dict[str, float],
                       tolerance: float = 0.005) -> bool:
        """Проверяет находится ли цена в зоне OTE (0.618-0.786)"""
        ote_high = fib_levels['fib_618']
        ote_low = fib_levels['fib_786']
        
        return (ote_low * (1 - tolerance)) <= price <= (ote_high * (1 + tolerance))
    
    # =========================================================================
    # SMC SIGNAL SCORING
    # =========================================================================
    
    def calculate_smc_score(self, 
                           symbol: str,
                           direction: str,
                           obs: List[SMCStructure],
                           fvgs: List[SMCStructure],
                           sweeps: List[LiquidityZone],
                           breaks: List[SMCStructure],
                           fib_levels: Dict[str, float],
                           current_price: float,
                           timestamp: pd.Timestamp) -> Dict:
        """
        Рассчитывает SMC-Score для текущей ситуации
        
        Returns:
            {
                'score': int (0-100),
                'factors': List[str],
                'entry_zone': Tuple[float, float],
                'stop_loss': float,
                'is_valid': bool
            }
        """
        score = 0
        factors = []
        
        # 1. Order Block в зоне (макс 25 очков)
        relevant_obs = [ob for ob in obs if ob.is_active]
        for ob in relevant_obs:
            if direction == 'long' and ob.type == SMCType.ORDER_BLOCK_BULLISH:
                if ob.bottom * 0.995 <= current_price <= ob.top * 1.005:
                    score += 20 + ob.strength
                    factors.append(f"Bullish OB (strength {ob.strength})")
                    break
            elif direction == 'short' and ob.type == SMCType.ORDER_BLOCK_BEARISH:
                if ob.bottom * 0.995 <= current_price <= ob.top * 1.005:
                    score += 20 + ob.strength
                    factors.append(f"Bearish OB (strength {ob.strength})")
                    break
        
        # 2. FVG в зоне (макс 20 очков)
        for fvg in fvgs:
            if fvg.is_active:
                if direction == 'long' and fvg.type == SMCType.FAIR_VALUE_GAP_BULLISH:
                    if fvg.bottom <= current_price <= fvg.top:
                        score += 15 + fvg.strength // 2
                        factors.append(f"Bullish FVG")
                        break
                elif direction == 'short' and fvg.type == SMCType.FAIR_VALUE_GAP_BEARISH:
                    if fvg.bottom <= current_price <= fvg.top:
                        score += 15 + fvg.strength // 2
                        factors.append(f"Bearish FVG")
                        break
        
        # 3. Ликвидность взята (макс 15 очков)
        for sweep in sweeps:
            if sweep.is_swept:
                if direction == 'long' and sweep.type == 'equal_lows':
                    if abs(current_price - sweep.level) / sweep.level < 0.01:
                        score += 15
                        factors.append("Equal Lows swept (liquidity taken)")
                        break
                elif direction == 'short' and sweep.type == 'equal_highs':
                    if abs(current_price - sweep.level) / sweep.level < 0.01:
                        score += 15
                        factors.append("Equal Highs swept (liquidity taken)")
                        break
        
        # 4. CHoCH/BOS подтверждает (макс 15 очков)
        for br in breaks:
            if direction == 'long' and br.type in [SMCType.CHoch_BULLISH, SMCType.BOS_BULLISH]:
                if (timestamp - br.start_time).total_seconds() < 7200:  # 2 часа
                    score += 15
                    factors.append(f"{br.type.value} confirms bullish bias")
                    break
            elif direction == 'short' and br.type in [SMCType.CHoch_BEARISH, SMCType.BOS_BEARISH]:
                if (timestamp - br.start_time).total_seconds() < 7200:
                    score += 15
                    factors.append(f"{br.type.value} confirms bearish bias")
                    break
        
        # 5. OTE зона (макс 15 очков)
        if self.is_price_at_ote(current_price, fib_levels):
            score += 15
            factors.append("Price at OTE (Optimal Trade Entry)")
        
        # 6. ICT Killzone (макс 10 очков)
        if self.is_ict_killzone(timestamp):
            score += 10
            factors.append("ICT Killzone (optimal time)")
        
        # Cap at 100
        score = min(score, 100)
        
        # Рассчитываем зоны входа и SL
        entry_zone, stop_loss = self._calculate_zones(
            direction, relevant_obs, fvgs, fib_levels, current_price
        )
        
        return {
            'score': score,
            'factors': factors,
            'entry_zone': entry_zone,
            'stop_loss': stop_loss,
            'is_valid': score >= 60,  # Минимум 60 для SMC-сигнала
            'smc_structures': {
                'obs': len(relevant_obs),
                'fvgs': len(fvgs),
                'sweeps': len(sweeps),
                'breaks': len(breaks)
            }
        }
    
    def _calculate_zones(self, direction: str, obs: List[SMCStructure],
                        fvgs: List[SMCStructure], 
                        fib_levels: Dict[str, float],
                        current_price: float) -> Tuple[Tuple[float, float], float]:
        """Рассчитывает зону входа и стоп-лосс на основе SMC"""
        
        entry_high = current_price * 1.002
        entry_low = current_price * 0.998
        
        if direction == 'long':
            # SL за последним OB или FVG
            stop_loss = current_price * 0.985  # -1.5% по умолчанию
            
            for ob in obs:
                if ob.type == SMCType.ORDER_BLOCK_BULLISH:
                    stop_loss = min(stop_loss, ob.bottom * 0.995)
                    entry_low = max(entry_low, ob.bottom)
                    entry_high = min(entry_high, ob.top)
                    break
            
            for fvg in fvgs:
                if fvg.type == SMCType.FAIR_VALUE_GAP_BULLISH:
                    stop_loss = min(stop_loss, fvg.bottom * 0.997)
                    break
        
        else:  # short
            stop_loss = current_price * 1.015  # +1.5% по умолчанию
            
            for ob in obs:
                if ob.type == SMCType.ORDER_BLOCK_BEARISH:
                    stop_loss = max(stop_loss, ob.top * 1.005)
                    entry_low = max(entry_low, ob.bottom)
                    entry_high = min(entry_high, ob.top)
                    break
            
            for fvg in fvgs:
                if fvg.type == SMCType.FAIR_VALUE_GAP_BEARISH:
                    stop_loss = max(stop_loss, fvg.top * 1.003)
                    break
        
        return ((entry_low, entry_high), stop_loss)


# ============================================================================
# EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Создаём тестовые данные
    np.random.seed(42)
    
    dates = pd.date_range('2024-01-01', periods=100, freq='15min')
    data = {
        'open': 100 + np.cumsum(np.random.randn(100) * 0.5),
        'high': 100 + np.cumsum(np.random.randn(100) * 0.5) + 1,
        'low': 100 + np.cumsum(np.random.randn(100) * 0.5) - 1,
        'close': 100 + np.cumsum(np.random.randn(100) * 0.5),
        'volume': np.random.rand(100) * 1000000
    }
    
    df = pd.DataFrame(data, index=dates)
    
    # Корректируем high/low
    df['high'] = df[['open', 'close', 'high']].max(axis=1) + 0.5
    df['low'] = df[['open', 'close', 'low']].min(axis=1) - 0.5
    
    print("=" * 60)
    print("SMC+ICT DETECTOR TEST")
    print("=" * 60)
    
    detector = SMCICTDetector()
    
    # Детекция
    obs = detector.detect_order_blocks(df)
    fvgs = detector.detect_fvgs(df)
    sweeps = detector.detect_liquidity_sweeps(df)
    breaks = detector.detect_structure_breaks(df)
    fib = detector.calculate_fibonacci_levels(df)
    
    print(f"\nOrder Blocks found: {len(obs)}")
    for ob in obs[:3]:
        print(f"  {ob.type.value}: {ob.bottom:.2f} - {ob.top:.2f} (strength: {ob.strength})")
    
    print(f"\nFVGs found: {len(fvgs)}")
    for fvg in fvgs[:3]:
        print(f"  {fvg.type.value}: {fvg.bottom:.2f} - {fvg.top:.2f}")
    
    print(f"\nLiquidity Sweeps: {len(sweeps)}")
    print(f"Structure Breaks: {len(breaks)}")
    
    print(f"\nFibonacci Levels:")
    for level, price in fib.items():
        print(f"  {level}: {price:.2f}")
    
    # Рассчитываем SMC Score
    current_price = df['close'].iloc[-1]
    current_time = df.index[-1]
    
    score_result = detector.calculate_smc_score(
        symbol="TESTUSDT",
        direction="long",
        obs=obs,
        fvgs=fvgs,
        sweeps=sweeps,
        breaks=breaks,
        fib_levels=fib,
        current_price=current_price,
        timestamp=current_time
    )
    
    print(f"\n{'=' * 60}")
    print(f"SMC Score: {score_result['score']}/100")
    print(f"Valid: {score_result['is_valid']}")
    print(f"Factors:")
    for factor in score_result['factors']:
        print(f"  ✓ {factor}")
    print(f"Entry Zone: {score_result['entry_zone'][0]:.2f} - {score_result['entry_zone'][1]:.2f}")
    print(f"Stop Loss: {score_result['stop_loss']:.2f}")
