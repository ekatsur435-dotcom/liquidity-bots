"""
SMC + ICT Detector — БЕЗ PANDAS
Smart Money Concepts + Inner Circle Trader
На основе методологии Статхэма (из Pine Script v130)
"""

import numpy as np
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class SMCType(Enum):
    """Типы SMC структур"""
    ORDER_BLOCK_BULLISH = "ob_bullish"
    ORDER_BLOCK_BEARISH = "ob_bearish"
    FVG_BULLISH = "fvg_bullish"
    FVG_BEARISH = "fvg_bearish"
    LIQUIDITY_SWING_HIGH = "liq_high"
    LIQUIDITY_SWING_LOW = "liq_low"
    CHoCH_BULLISH = "choch_bullish"
    CHoCH_BEARISH = "choch_bearish"
    BOS_BULLISH = "bos_bullish"
    BOS_BEARISH = "bos_bearish"


@dataclass
class SMCStructure:
    """SMC структура (Order Block, FVG, etc.)"""
    type: SMCType
    start_time: datetime
    end_time: datetime
    top: float
    bottom: float
    volume: float
    strength: int = 50  # 0-100
    is_valid: bool = True
    mitigated: bool = False
    mitigated_time: Optional[datetime] = None
    
    def get_mid(self) -> float:
        return (self.top + self.bottom) / 2
    
    def get_size(self) -> float:
        return abs(self.top - self.bottom)
    
    def contains_price(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass
class LiquidityZone:
    """Зона ликвидности"""
    price_level: float
    zone_type: str  # 'equal_highs', 'equal_lows', 'pwh', 'pwl', 'ssl', 'bsl'
    touches: int
    volume_at_level: float
    is_swept: bool = False
    sweep_time: Optional[datetime] = None


@dataclass
class ICTSession:
    """ICT торговая сессия"""
    name: str  # 'asia', 'london', 'ny_am', 'ny_pm'
    start_time: datetime
    end_time: datetime
    high: float
    low: float
    open: float
    close: float
    displacement: float  # % изменение


class SMCDetector:
    """
    Детектор SMC (Smart Money Concepts) паттернов
    Без pandas — только lists и dicts
    """
    
    def __init__(self, timeframe: str = "15m"):
        self.timeframe = timeframe
    
    def _get_candle(self, data: List[Dict], idx: int) -> Optional[Dict]:
        """Безопасно получить свечу по индексу"""
        if 0 <= idx < len(data):
            return data[idx]
        return None
    
    def _is_bullish(self, candle: Dict) -> bool:
        return candle['close'] > candle['open']
    
    def _is_bearish(self, candle: Dict) -> bool:
        return candle['close'] < candle['open']
    
    def _get_body(self, candle: Dict) -> float:
        return abs(candle['close'] - candle['open'])
    
    def _get_range(self, candle: Dict) -> float:
        return candle['high'] - candle['low']
    
    # =========================================================================
    # ORDER BLOCKS (OB)
    # =========================================================================
    
    def detect_order_blocks(self, data: List[Dict]) -> List[SMCStructure]:
        """Детекция Order Blocks"""
        obs = []
        
        if len(data) < 5:
            return obs
        
        # Ищем импульсы и OB перед ними
        for i in range(3, len(data) - 2):
            # Бычий импульс (3 зелёные свечи)
            if (self._is_bullish(data[i]) and 
                self._is_bullish(data[i+1]) and 
                self._is_bullish(data[i+2])):
                
                # Ищем последнюю красную свечу перед импульсом
                for j in range(i-1, max(i-5, -1), -1):
                    if self._is_bearish(data[j]):
                        avg_vol = np.mean([d['volume'] for d in data[max(0,j-5):j+1]])
                        if data[j]['volume'] > avg_vol * 0.8:
                            ob = SMCStructure(
                                type=SMCType.ORDER_BLOCK_BULLISH,
                                start_time=data[j].get('timestamp', datetime.now()),
                                end_time=data[j].get('timestamp', datetime.now()),
                                top=data[j]['open'],
                                bottom=data[j]['low'],
                                volume=data[j]['volume'],
                                strength=70
                            )
                            obs.append(ob)
                            break
            
            # Медвежий импульс (3 красные свечи)
            if (self._is_bearish(data[i]) and 
                self._is_bearish(data[i+1]) and 
                self._is_bearish(data[i+2])):
                
                for j in range(i-1, max(i-5, -1), -1):
                    if self._is_bullish(data[j]):
                        avg_vol = np.mean([d['volume'] for d in data[max(0,j-5):j+1]])
                        if data[j]['volume'] > avg_vol * 0.8:
                            ob = SMCStructure(
                                type=SMCType.ORDER_BLOCK_BEARISH,
                                start_time=data[j].get('timestamp', datetime.now()),
                                end_time=data[j].get('timestamp', datetime.now()),
                                top=data[j]['high'],
                                bottom=data[j]['open'],
                                volume=data[j]['volume'],
                                strength=70
                            )
                            obs.append(ob)
                            break
        
        return obs[-10:]  # Последние 10 OB
    
    # =========================================================================
    # FAIR VALUE GAPS (FVG)
    # =========================================================================
    
    def detect_fvgs(self, data: List[Dict]) -> List[SMCStructure]:
        """Детекция Fair Value Gaps"""
        fvgs = []
        
        if len(data) < 3:
            return fvgs
        
        for i in range(2, len(data)):
            prev2 = data[i-2]
            prev1 = data[i-1]
            curr = data[i]
            
            # Бычий FVG: high[i-2] < low[i]
            if prev2['high'] < curr['low']:
                fvg = SMCStructure(
                    type=SMCType.FVG_BULLISH,
                    start_time=curr.get('timestamp', datetime.now()),
                    end_time=curr.get('timestamp', datetime.now()),
                    top=curr['low'],
                    bottom=prev2['high'],
                    volume=curr['volume'],
                    strength=60
                )
                fvgs.append(fvg)
            
            # Медвежий FVG: low[i-2] > high[i]
            if prev2['low'] > curr['high']:
                fvg = SMCStructure(
                    type=SMCType.FVG_BEARISH,
                    start_time=curr.get('timestamp', datetime.now()),
                    end_time=curr.get('timestamp', datetime.now()),
                    top=prev2['low'],
                    bottom=curr['high'],
                    volume=curr['volume'],
                    strength=60
                )
                fvgs.append(fvg)
        
        return fvgs[-10:]
    
    # =========================================================================
    # LIQUIDITY SWEEPS
    # =========================================================================
    
    def detect_liquidity_sweeps(self, data: List[Dict], 
                                lookback: int = 20) -> List[LiquidityZone]:
        """Детекция ликвидности (Equal Highs/Lows, PWH/PWL)"""
        sweeps = []
        
        if len(data) < lookback:
            return sweeps
        
        # Ищем Equal Highs (два одинаковых максимума)
        highs = [(i, d['high']) for i, d in enumerate(data[-lookback:])]
        
        for i, (idx1, h1) in enumerate(highs):
            for idx2, h2 in highs[i+1:]:
                if abs(h1 - h2) / h1 < 0.001:  # 0.1% tolerance
                    # Проверяем был ли sweep
                    for k in range(idx2+1, len(data)):
                        if data[k]['high'] > h1:
                            sweeps.append(LiquidityZone(
                                price_level=h1,
                                zone_type='equal_highs',
                                touches=2,
                                volume_at_level=data[k]['volume'],
                                is_swept=True,
                                sweep_time=data[k].get('timestamp')
                            ))
                            break
                    break
        
        # Ищем Equal Lows (два одинаковых минимума)
        lows = [(i, d['low']) for i, d in enumerate(data[-lookback:])]
        
        for i, (idx1, l1) in enumerate(lows):
            for idx2, l2 in lows[i+1:]:
                if abs(l1 - l2) / l1 < 0.001:
                    for k in range(idx2+1, len(data)):
                        if data[k]['low'] < l1:
                            sweeps.append(LiquidityZone(
                                price_level=l1,
                                zone_type='equal_lows',
                                touches=2,
                                volume_at_level=data[k]['volume'],
                                is_swept=True,
                                sweep_time=data[k].get('timestamp')
                            ))
                            break
                    break
        
        return sweeps[:10]
    
    # =========================================================================
    # CHoCH / BOS
    # =========================================================================
    
    def detect_structure_breaks(self, data: List[Dict]) -> List[SMCStructure]:
        """Детекция CHoCH и BOS"""
        breaks = []
        
        if len(data) < 10:
            return breaks
        
        # Находим swing highs/lows
        swing_highs = self._find_swing_highs(data)
        swing_lows = self._find_swing_lows(data)
        
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return breaks
        
        # BOS (Break of Structure) - пробой последнего swing high/low
        last_sh = swing_highs[-1]
        prev_sh = swing_highs[-2]
        
        if data[-1]['close'] > data[last_sh]['high']:
            breaks.append(SMCStructure(
                type=SMCType.BOS_BULLISH,
                start_time=data[last_sh].get('timestamp', datetime.now()),
                end_time=data[-1].get('timestamp', datetime.now()),
                top=data[-1]['high'],
                bottom=data[prev_sh]['low'],
                volume=data[-1]['volume'],
                strength=75
            ))
        
        last_sl = swing_lows[-1]
        prev_sl = swing_lows[-2]
        
        if data[-1]['close'] < data[last_sl]['low']:
            breaks.append(SMCStructure(
                type=SMCType.BOS_BEARISH,
                start_time=data[last_sl].get('timestamp', datetime.now()),
                end_time=data[-1].get('timestamp', datetime.now()),
                top=data[prev_sl]['high'],
                bottom=data[-1]['low'],
                volume=data[-1]['volume'],
                strength=75
            ))
        
        return breaks
    
    def _find_swing_highs(self, data: List[Dict], window: int = 5) -> List[int]:
        """Находит индексы swing highs"""
        highs = []
        for i in range(window, len(data) - window):
            if all(data[i]['high'] > data[i-j]['high'] for j in range(1, window+1)) and \
               all(data[i]['high'] > data[i+j]['high'] for j in range(1, window+1)):
                highs.append(i)
        return highs
    
    def _find_swing_lows(self, data: List[Dict], window: int = 5) -> List[int]:
        """Находит индексы swing lows"""
        lows = []
        for i in range(window, len(data) - window):
            if all(data[i]['low'] < data[i-j]['low'] for j in range(1, window+1)) and \
               all(data[i]['low'] < data[i+j]['low'] for j in range(1, window+1)):
                lows.append(i)
        return lows
    
    # =========================================================================
    # ICT TIME CONCEPTS
    # =========================================================================
    
    def get_ict_session(self, timestamp: datetime) -> ICTSession:
        """Определяет ICT сессию по времени"""
        hour = timestamp.hour
        
        if 0 <= hour < 8:
            name = 'asia'
        elif 8 <= hour < 12:
            name = 'london'
        elif 12 <= hour < 16:
            name = 'ny_am'
        else:
            name = 'ny_pm'
        
        return ICTSession(
            name=name,
            start_time=timestamp.replace(hour=hour, minute=0),
            end_time=timestamp.replace(hour=hour+4, minute=0),
            high=0, low=0, open=0, close=0, displacement=0
        )
    
    def is_ict_killzone(self, timestamp: datetime) -> bool:
        """Проверяет находимся ли в ICT Killzone"""
        hour = timestamp.hour
        minute = timestamp.minute
        
        # London Open Killzone: 8:00-10:00
        london_kz = (8 <= hour < 10)
        
        # NY Open Killzone: 9:30-11:30 (NY time = 13:30-15:30 UTC)
        ny_kz = (13 <= hour < 16 and self.timeframe != "1h")
        
        # London Close Killzone: 16:00-17:00
        london_close = (15 <= hour < 17)
        
        return london_kz or ny_kz or london_close
    
    # =========================================================================
    # PREMIUM / DISCOUNT
    # =========================================================================
    
    def calculate_fibonacci_levels(self, data: List[Dict], 
                                   lookback: int = 20) -> Dict[str, float]:
        """Рассчитывает уровни Фибоначчи (Premium/Discount)"""
        if len(data) < lookback:
            lookback = len(data)
        
        recent = data[-lookback:]
        swing_high = max(d['high'] for d in recent)
        swing_low = min(d['low'] for d in recent)
        
        range_size = swing_high - swing_low
        
        return {
            'premium': swing_high - range_size * 0.25,  # 75% (short zone)
            'discount': swing_low + range_size * 0.25,   # 25% (long zone)
            'equilibrium': swing_low + range_size * 0.5, # 50%
            'swing_high': swing_high,
            'swing_low': swing_low,
            'range': range_size
        }
    
    # =========================================================================
    # SCORING
    # =========================================================================
    
    def calculate_smc_score(self, data: List[Dict],
                           obs: List[SMCStructure],
                           fvgs: List[SMCStructure],
                           sweeps: List[LiquidityZone],
                           breaks: List[SMCStructure],
                           fib_levels: Dict[str, float],
                           current_price: float,
                           timestamp: datetime) -> Dict:
        """Рассчитывает SMC-Score для текущей ситуации"""
        
        score = 0
        details = []
        
        # 1. Наличие ликвидности рядом (+20)
        near_liquidity = any(
            abs(zone.price_level - current_price) / current_price < 0.005
            for zone in sweeps if zone.is_swept
        )
        if near_liquidity:
            score += 20
            details.append("Liquidity swept nearby")
        
        # 2. Наличие OB рядом (+25)
        near_ob = any(
            ob.contains_price(current_price) and not ob.mitigated
            for ob in obs
        )
        if near_ob:
            score += 25
            details.append("In Order Block zone")
        
        # 3. FVG рядом (+20)
        near_fvg = any(
            fvg.contains_price(current_price)
            for fvg in fvgs
        )
        if near_fvg:
            score += 20
            details.append("In Fair Value Gap")
        
        # 4. Premium/Discount zone (+15)
        if current_price > fib_levels.get('premium', 0):
            score += 15
            details.append("Price in Premium zone (short)")
        elif current_price < fib_levels.get('discount', float('inf')):
            score += 15
            details.append("Price in Discount zone (long)")
        
        # 5. CHoCH/BOS недавно (+20)
        recent_break = any(
            (breaks[-1].end_time - b.end_time).total_seconds() < 3600
            for b in breaks[-1:]
        ) if breaks else False
        if recent_break:
            score += 20
            details.append("Recent structure break")
        
        # 6. ICT Killzone (+10)
        if self.is_ict_killzone(timestamp):
            score += 10
            details.append("In ICT Killzone")
        
        return {
            'total_score': min(score, 100),
            'max_possible': 100,
            'details': details,
            'confidence': 'high' if score >= 70 else 'medium' if score >= 50 else 'low',
            'entry_zone': self._get_optimal_entry_zone(obs, fvgs, current_price)
        }
    
    def _get_optimal_entry_zone(self, obs: List[SMCStructure], 
                                fvgs: List[SMCStructure],
                                current_price: float) -> Optional[Tuple[float, float]]:
        """Определяет оптимальную зону входа"""
        # Ищем ближайший OB или FVG
        candidates = obs + fvgs
        
        if not candidates:
            return None
        
        # Находим ближайшую к цене зону
        closest = min(candidates, 
                     key=lambda x: abs(x.get_mid() - current_price))
        
        return (closest.bottom, closest.top)
    
    # =========================================================================
    # MAIN ANALYSIS
    # =========================================================================
    
    def analyze(self, data: List[Dict], timestamp: datetime) -> Dict:
        """Полный анализ SMC для текущей ситуации"""
        
        if not data:
            return {'score': 0, 'confidence': 'none', 'signals': []}
        
        current_price = data[-1]['close']
        
        # Детектируем все паттерны
        obs = self.detect_order_blocks(data)
        fvgs = self.detect_fvgs(data)
        sweeps = self.detect_liquidity_sweeps(data)
        breaks = self.detect_structure_breaks(data)
        fib_levels = self.calculate_fibonacci_levels(data)
        
        # Считаем скор
        score_result = self.calculate_smc_score(
            data, obs, fvgs, sweeps, breaks, fib_levels,
            current_price, timestamp
        )
        
        # Определяем сигнал
        signal = 'neutral'
        if breaks and breaks[-1].type == SMCType.BOS_BULLISH:
            signal = 'bullish'
        elif breaks and breaks[-1].type == SMCType.BOS_BEARISH:
            signal = 'bearish'
        
        # Проверяем OB
        bullish_obs = [ob for ob in obs if ob.type == SMCType.ORDER_BLOCK_BULLISH]
        bearish_obs = [ob for ob in obs if ob.type == SMCType.ORDER_BLOCK_BEARISH]
        
        return {
            'score': score_result['total_score'],
            'max_score': 100,
            'confidence': score_result['confidence'],
            'signal': signal,
            'entry_zone': score_result['entry_zone'],
            'details': score_result['details'],
            'patterns': {
                'order_blocks': len(obs),
                'bullish_obs': len(bullish_obs),
                'bearish_obs': len(bearish_obs),
                'fvgs': len(fvgs),
                'sweeps': len(sweeps),
                'breaks': len(breaks)
            },
            'ict_context': {
                'in_killzone': self.is_ict_killzone(timestamp),
                'premium_zone': current_price > fib_levels.get('premium', 0),
                'discount_zone': current_price < fib_levels.get('discount', float('inf'))
            }
        }


# Example usage
if __name__ == "__main__":
    # Тестовые данные
    np.random.seed(42)
    
    base_price = 50000
    data = []
    for i in range(100):
        o = base_price + np.random.randn() * 100
        c = o + np.random.randn() * 200
        h = max(o, c) + abs(np.random.randn()) * 50
        l = min(o, c) - abs(np.random.randn()) * 50
        v = np.random.rand() * 1000000
        
        data.append({
            'open': o,
            'high': h,
            'low': l,
            'close': c,
            'volume': v,
            'timestamp': datetime.now() + timedelta(minutes=15*i)
        })
        base_price = c
    
    detector = SMCDetector()
    result = detector.analyze(data, datetime.now())
    
    print(f"SMC Score: {result['score']}/100")
    print(f"Confidence: {result['confidence']}")
    print(f"Signal: {result['signal']}")
    print(f"Patterns: {result['patterns']}")
    print(f"\nDetails: {result['details']}")
