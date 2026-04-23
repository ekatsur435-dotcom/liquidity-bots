"""
Liquidity Detector v2.6
- Stop Loss sweeps (hunting)
- Liquidity grabs above/below key levels
- Historical zones analysis
- Smart money liquidity targeting
"""
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass


@dataclass
class LiquidityZone:
    """Зона ликвидности — где стоят стопы толпы"""
    zone_type: str        # "equal_highs" | "equal_lows" | "swing_high" | "swing_low"
    price_level: float    # Уровень ликвидности
    strength: int          # 0-100 (сколько раз тестировался)
    volume_at_level: float # Объём на пробое
    swept: bool           # Была ли уже собрана
    sweep_price: Optional[float] = None  # Цена пробоя


@dataclass
class LiquiditySweepResult:
    """Результат анализа ликвидности"""
    found_sweep: bool
    sweep_type: str        # "long_liquidity" | "short_liquidity" | "none"
    zone: Optional[LiquidityZone]
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: int       # 0-100
    reasons: List[str]
    # ✅ v2.7: Данные для точного стопа (хвост свечи sweep)
    sweep_low: float = 0.0    # Минимум свечи sweep (для LONG стопа)
    sweep_high: float = 0.0   # Максимум свечи sweep (для SHORT стопа)
    fvg_low: float = 0.0      # Нижняя граница FVG
    fvg_high: float = 0.0     # Верхняя граница FVG


class LiquidityDetector:
    """
    Детектор сборов ликвидности и стоп-лоссов.
    
    Ищет:
    1. Equal Highs/Lows — двойные вершины/основания
    2. Swing High/Low — экстремумы где стопы
    3. Liquidity Sweep — пробой с возвратом
    4. Historical zones — зоны из прошлого
    """
    
    def __init__(self, ohlcv: List[Any], lookback: int = 100):
        self.ohlcv = ohlcv
        self.n = len(ohlcv)
        self.lookback = lookback
        
    def _get_price(self, i: int, attr: str) -> float:
        """Универсальный доступ к данным свечи"""
        candle = self.ohlcv[i]
        if isinstance(candle, dict):
            return candle.get(attr, candle.get('close', 0))
        elif isinstance(candle, list):
            mapping = {'open': 0, 'high': 1, 'low': 2, 'close': 3, 'volume': 4}
            return candle[mapping.get(attr, 3)]
        elif hasattr(candle, attr):
            return getattr(candle, attr)
        return 0.0
    
    def _h(self, i: int) -> float: return self._get_price(i, 'high')
    def _l(self, i: int) -> float: return self._get_price(i, 'low')
    def _c(self, i: int) -> float: return self._get_price(i, 'close')
    def _v(self, i: int) -> float: 
        try:
            return self._get_price(i, 'volume')
        except:
            return 0
    
    def find_equal_levels(self, tolerance: float = 0.005) -> List[LiquidityZone]:
        """
        Ищет Equal Highs и Equal Lows — зоны где цена касалась 2+ раз.
        Там стопы трейдеров.
        """
        zones = []
        highs = [(i, self._h(i)) for i in range(max(0, self.n-self.lookback), self.n)]
        lows = [(i, self._l(i)) for i in range(max(0, self.n-self.lookback), self.n)]
        
        # Equal Highs (двойная вершина)
        for i in range(len(highs)-1):
            for j in range(i+1, len(highs)):
                if abs(highs[i][1] - highs[j][1]) / highs[i][1] < tolerance:
                    zones.append(LiquidityZone(
                        zone_type="equal_highs",
                        price_level=highs[i][1],
                        strength=80,
                        volume_at_level=self._v(highs[i][0]),
                        swept=False
                    ))
        
        # Equal Lows (двойное дно)
        for i in range(len(lows)-1):
            for j in range(i+1, len(lows)):
                if abs(lows[i][1] - lows[j][1]) / lows[i][1] < tolerance:
                    zones.append(LiquidityZone(
                        zone_type="equal_lows",
                        price_level=lows[i][1],
                        strength=80,
                        volume_at_level=self._v(lows[i][0]),
                        swept=False
                    ))
        
        return zones
    
    def find_swing_points(self) -> List[LiquidityZone]:
        """
        Находит Swing Highs/Lows — локальные экстремумы.
        Там стопы breakout трейдеров.
        """
        zones = []
        window = 5  # Свечи слева/справа для подтверждения экстремума
        
        for i in range(window, self.n - window):
            # Swing High (вершина)
            if all(self._h(i) > self._h(j) for j in range(i-window, i) if j != i) and \
               all(self._h(i) > self._h(j) for j in range(i+1, i+window+1)):
                zones.append(LiquidityZone(
                    zone_type="swing_high",
                    price_level=self._h(i),
                    strength=60,
                    volume_at_level=self._v(i),
                    swept=False
                ))
            
            # Swing Low (дно)
            if all(self._l(i) < self._l(j) for j in range(i-window, i) if j != i) and \
               all(self._l(i) < self._l(j) for j in range(i+1, i+window+1)):
                zones.append(LiquidityZone(
                    zone_type="swing_low",
                    price_level=self._l(i),
                    strength=60,
                    volume_at_level=self._v(i),
                    swept=False
                ))
        
        return zones
    
    def detect_sweep(self, direction: str = "short", 
                     sweep_buffer: float = 0.01) -> LiquiditySweepResult:
        """
        Детектор Liquidity Sweep:
        - Цена пробивает уровень (собирает стопы)
        - Но тут же возвращается (фейк)
        - Это вход для разворота!
        
        Args:
            direction: "short" | "long"
            sweep_buffer: насколько пробить уровень (1% по умолчанию)
        """
        if self.n < 10:
            return LiquiditySweepResult(False, "none", None, 0, 0, 0, 0, [])
        
        # Текущая цена
        current_price = self._c(self.n - 1)
        prev_price = self._c(self.n - 2)
        
        # Находим зоны
        zones = self.find_equal_levels() + self.find_swing_points()
        
        reasons = []
        
        for zone in zones:
            # Проверяем Sweep для SHORT (лонг ликвидность собрана, можно шортить)
            if direction == "short" and zone.zone_type in ["equal_highs", "swing_high"]:
                # Цена пробила выше (собрала стопы шортистов)
                if prev_price > zone.price_level * (1 + sweep_buffer) and \
                   current_price < zone.price_level:  # Но вернулась!
                    
                    zone.swept = True
                    zone.sweep_price = prev_price
                    
                    # ✅ v2.7: Хвост свечи sweep для точного стопа
                    sweep_low = self._l(self.n - 2)   # Минимум свечи sweep
                    sweep_high = self._h(self.n - 2)  # Максимум свечи sweep (для SHORT)
                    
                    reasons.extend([
                        f"🎯 Liquidity Sweep Above ${zone.price_level:.4f}",
                        f"🧹 Собраны стопы шортистов (лонг ликвидность)",
                        f"↩️ Возврат под уровень = фейк!",
                        f"📊 Сила зоны: {zone.strength}%",
                        f"🕯️ Свип high: ${sweep_high:.6f} (хвост свечи)"
                    ])
                    
                    return LiquiditySweepResult(
                        found_sweep=True,
                        sweep_type="long_liquidity",
                        zone=zone,
                        entry_price=current_price,
                        stop_loss=sweep_high * 1.005,  # ✅ За хвост свечи sweep
                        take_profit=zone.price_level * 0.97,  # К ближайшей зоне
                        confidence=min(90, zone.strength + 20),
                        reasons=reasons,
                        sweep_low=sweep_low,
                        sweep_high=sweep_high  # ✅ Для стопа SHORT
                    )
            
            # Проверяем Sweep для LONG (шорт ликвидность собрана, можно лонгить)
            elif direction == "long" and zone.zone_type in ["equal_lows", "swing_low"]:
                # Цена пробила ниже (собрала стопы лонгистов)
                if prev_price < zone.price_level * (1 - sweep_buffer) and \
                   current_price > zone.price_level:  # Но вернулась!
                    
                    zone.swept = True
                    zone.sweep_price = prev_price
                    
                    # ✅ v2.7: Хвост свечи sweep для точного стопа
                    sweep_low = self._l(self.n - 2)  # Минимум свечи sweep
                    sweep_high = self._h(self.n - 2)  # Максимум свечи sweep
                    
                    reasons.extend([
                        f"🎯 Liquidity Sweep Below ${zone.price_level:.4f}",
                        f"🧹 Собраны стопы лонгистов (шорт ликвидность)",
                        f"↩️ Возврат над уровнем = фейк!",
                        f"📊 Сила зоны: {zone.strength}%",
                        f"🕯️ Свип low: ${sweep_low:.6f} (хвост свечи)"
                    ])
                    
                    return LiquiditySweepResult(
                        found_sweep=True,
                        sweep_type="short_liquidity",
                        zone=zone,
                        entry_price=current_price,
                        stop_loss=sweep_low * 0.995,  # ✅ За хвост свечи sweep
                        take_profit=zone.price_level * 1.03,  # К ближайшей зоне
                        confidence=min(90, zone.strength + 20),
                        reasons=reasons,
                        sweep_low=sweep_low,      # ✅ Для стопа
                        sweep_high=sweep_high   # Для анализа
                    )
        
        return LiquiditySweepResult(False, "none", None, 0, 0, 0, 0, [])
    
    def analyze_historical_zones(self) -> Dict[str, List[float]]:
        """
        Анализ исторических зон — где цена часто реагировала.
        Возвращает support/resistance зоны.
        """
        if self.n < 50:
            return {"support": [], "resistance": []}
        
        # Собираем все хаи/лоу за период
        highs = [self._h(i) for i in range(max(0, self.n-100), self.n)]
        lows = [self._l(i) for i in range(max(0, self.n-100), self.n)]
        
        # Находим частые уровни (где цена "тупила")
        from collections import defaultdict
        level_counts = defaultdict(int)
        
        for price in highs + lows:
            # Округляем до значимых цифр
            rounded = round(price, max(0, 4 - len(str(int(price)))))
            level_counts[rounded] += 1
        
        # Уровни с 3+ касаниями = сильные
        strong_levels = [price for price, count in level_counts.items() if count >= 3]
        
        current_price = self._c(self.n - 1)
        
        support = [p for p in strong_levels if p < current_price * 0.98]
        resistance = [p for p in strong_levels if p > current_price * 1.02]
        
        return {
            "support": sorted(support, reverse=True)[:5],      # Ближайшие снизу
            "resistance": sorted(resistance)[:5]              # Ближайшие сверху
        }


def _get_candle_price(candle: Any, attr: str = 'close') -> float:
    """Универсальный доступ к данным свечи"""
    if isinstance(candle, dict):
        return candle.get(attr, candle.get('close', 0))
    elif isinstance(candle, list):
        mapping = {'open': 0, 'high': 1, 'low': 2, 'close': 3, 'volume': 4}
        return candle[mapping.get(attr, 3)]
    elif hasattr(candle, attr):
        return getattr(candle, attr)
    return 0.0


def detect_smart_money_entry(ohlcv: List[Any], 
                              direction: str = "short") -> Optional[Dict]:
    """
    Комбинированный анализ для входа:
    1. Ликвидность собрана?
    2. Есть подтверждение?
    3. Объём есть?
    Принимает как List[List[float]] так и List[CandleData].
    """
    if not ohlcv or len(ohlcv) < 20:
        return None
    
    detector = LiquidityDetector(ohlcv)
    
    # 1. Проверяем sweep
    sweep = detector.detect_sweep(direction)
    if not sweep.found_sweep:
        return None
    
    # 2. Проверяем объём (должен быть выше среднего)
    avg_vol = sum(_get_candle_price(ohlcv[i], 'volume') for i in range(max(0, len(ohlcv)-20), len(ohlcv)-1)) / 20
    current_vol = _get_candle_price(ohlcv[-1], 'volume')
    if current_vol < avg_vol * 1.5:
        return None  # Мало объёма
    
    # 3. Исторические зоны
    zones = detector.analyze_historical_zones()
    
    return {
        "found": True,
        "entry": sweep.entry_price,
        "price": sweep.entry_price,  # Alias для совместимости
        "sl": sweep.stop_loss,
        "tp": sweep.take_profit,
        "confidence": sweep.confidence,
        "reasons": sweep.reasons,
        "zones": zones
    }
