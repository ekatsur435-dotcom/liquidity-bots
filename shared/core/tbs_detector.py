"""
TBS Detector v2.6
Test Before Strike / Tap Before Sweep
SMC концепция: цена делает ретест зоны перед основным движением
"""
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum


class TBSPhase(Enum):
    """Фазы TBS паттерна"""
    NONE = "none"
    INITIAL_MOVE = "initial_move"      # Первичное движение к зоне
    TEST = "test"                       # Тест/касание зоны
    RETEST = "retest"                   # Ретест (TBS фаза)
    STRIKE = "strike"                   # Основное движение (Strike)


@dataclass
class TBSResult:
    """Результат TBS анализа"""
    found: bool
    phase: TBSPhase
    zone_type: str                      # "order_block" | "breaker" | "liquidity"
    zone_price: float
    test_price: float                   # Цена теста
    retest_price: Optional[float]       # Цена ретеста (если был)
    entry_price: float                  # Рекомендуемый вход
    confidence: int                     # 0-100
    reasons: List[str]


class TBSDetector:
    """
    Детектор Test Before Strike (TBS) паттернов.
    
    SMC концепция:
    1. Цена подходит к ключевой зоне (Order Block, Liquidity)
    2. Делает первичный тест (Test)
    3. Уходит небольшое движение против тренда
    4. Возвращается и делает ретест зоны (Retest/TBS)
    5. После ретеста идёт основное движение (Strike)
    
    Это самая сильная точка входа — подтверждённый ретест!
    """
    
    def __init__(self, ohlcv: List[List[float]], lookback: int = 30):
        self.ohlcv = ohlcv
        self.n = len(ohlcv)
        self.lookback = lookback
        
    def _get_price(self, i: int, attr: str) -> float:
        """Универсальный доступ к данным свечи"""
        candle = self.ohlcv[i]
        if hasattr(candle, attr):
            return getattr(candle, attr)
        elif isinstance(candle, (list, tuple)):
            mapping = {'open': 0, 'high': 1, 'low': 2, 'close': 3, 'volume': 4}
            return candle[mapping.get(attr, 3)]
        return 0.0
    
    def _o(self, i: int) -> float: return self._get_price(i, 'open')
    def _h(self, i: int) -> float: return self._get_price(i, 'high')
    def _l(self, i: int) -> float: return self._get_price(i, 'low')
    def _c(self, i: int) -> float: return self._get_price(i, 'close')
    def _v(self, i: int) -> float: 
        try:
            return self._get_price(i, 'volume')
        except:
            return 0
    
    def find_order_blocks(self) -> List[Dict]:
        """
        Находит Order Blocks (свечи перед сильным движением).
        Это зоны где "умные деньги" набирали позиции.
        """
        obs = []
        
        for i in range(3, self.n - 3):
            # Bullish OB: свеча перед сильным ростом
            if self._c(i) > self._o(i) * 1.01:  # Бычья свеча
                # Следующие 2 свечи — сильный рост
                if (self._c(i+2) - self._c(i)) / self._c(i) > 0.02:
                    obs.append({
                        "type": "bullish_ob",
                        "high": self._h(i),
                        "low": self._l(i),
                        "mid": (self._h(i) + self._l(i)) / 2,
                        "index": i,
                        "strength": 70
                    })
            
            # Bearish OB: свеча перед сильным падением
            if self._c(i) < self._o(i) * 0.99:  # Медвежья свеча
                # Следующие 2 свечи — сильное падение
                if (self._c(i) - self._c(i+2)) / self._c(i) > 0.02:
                    obs.append({
                        "type": "bearish_ob",
                        "high": self._h(i),
                        "low": self._l(i),
                        "mid": (self._h(i) + self._l(i)) / 2,
                        "index": i,
                        "strength": 70
                    })
        
        return obs
    
    def detect_tbs_pattern(self, direction: str = "short",
                          tolerance: float = 0.01) -> TBSResult:
        """
        Детектор TBS (Test Before Strike) паттерна.
        
        Алгоритм для SHORT:
        1. Цена подходит к сопротивлению (Initial Move)
        2. Тестирует уровень (Test) — может пробить на 0.5-1%
        3. Делает небольшой откат вниз (имитация слабости)
        4. ВОЗВРАЩАЕТСЯ к уровню (Retest/TBS) — идеальный вход!
        5. После ретеста идёт Strike (основное падение)
        
        Args:
            direction: "short" | "long"
            tolerance: допуск для теста зоны (1%)
        """
        if self.n < 15:
            return TBSResult(False, TBSPhase.NONE, "", 0, 0, None, 0, 0, [])
        
        # Берём последние 10 свечей для анализа
        recent = self.ohlcv[-10:]
        
        # Находим ключевые уровни
        recent_highs = [c[1] for c in recent[:-3]]  # Без последних 3
        recent_lows = [c[2] for c in recent[:-3]]
        
        if direction == "short":
            resistance = max(recent_highs)
            
            # Проверяем TBS для SHORT
            # 1. Был ли тест сопротивления?
            test_candle = None
            for i in range(-7, -3):  # 4-7 свечей назад
                if self._h(i) >= resistance * (1 - tolerance):
                    test_candle = i
                    break
            
            if test_candle is None:
                return TBSResult(False, TBSPhase.NONE, "resistance", resistance, 0, None, 0, 0, 
                               ["Нет теста сопротивления"])
            
            # 2. Был ли откат после теста?
            pullback_low = min(self._l(i) for i in range(test_candle+1, -2))
            pullback_pct = (resistance - pullback_low) / resistance
            
            if pullback_pct < 0.005:  # Минимум 0.5% откат
                return TBSResult(False, TBSPhase.TEST, "resistance", resistance, 
                               self._h(test_candle), None, 0, 40,
                               [f"Тест был, но откат слишком мал ({pullback_pct*100:.1f}%)"])
            
            # 3. Был ли ретест (TBS)?
            current_high = self._h(-1)
            current_low = self._l(-1)
            
            # Цена сейчас у сопротивления снова (ретест)
            near_resistance = current_high >= resistance * (1 - tolerance * 2)
            
            if not near_resistance:
                return TBSResult(False, TBSPhase.INITIAL_MOVE, "resistance", resistance,
                               self._h(test_candle), pullback_low, 0, 60,
                               ["Тест и откат были, ждём ретеста"])
            
            # ✅ TBS НАЙДЕН! Идеальный вход для SHORT
            return TBSResult(
                found=True,
                phase=TBSPhase.RETEST,
                zone_type="resistance",
                zone_price=resistance,
                test_price=self._h(test_candle),
                retest_price=current_high,
                entry_price=current_low,  # Вход на ближайшем лоу
                confidence=85,
                reasons=[
                    f"🎯 TBS Pattern: Test → Pullback → Retest",
                    f"📊 Тест: ${self._h(test_candle):.4f}",
                    f"📉 Откат: {pullback_pct*100:.1f}%",
                    f"🔄 Ретест: ${current_high:.4f}",
                    f"⚡ Strike incoming!"
                ]
            )
        
        else:  # LONG
            support = min(recent_lows)
            
            # Проверяем TBS для LONG (зеркально)
            test_candle = None
            for i in range(-7, -3):
                if self._l(i) <= support * (1 + tolerance):
                    test_candle = i
                    break
            
            if test_candle is None:
                return TBSResult(False, TBSPhase.NONE, "support", support, 0, None, 0, 0,
                               ["Нет теста поддержки"])
            
            # Тест был найден — проверяем ретест
            pullback_high = max(self._h(i) for i in range(test_candle+1, -2))
            pullback_pct = (pullback_high - support) / support
            
            if pullback_pct < 0.005:
                return TBSResult(False, TBSPhase.TEST, "support", support,
                               self._l(test_candle), None, 0, 40,
                               [f"Тест был, откат слишком мал"])
            
            current_low = self._l(-1)
            near_support = current_low <= support * (1 + tolerance * 2)
            
            if not near_support:
                return TBSResult(False, TBSPhase.INITIAL_MOVE, "support", support,
                               self._l(test_candle), pullback_high, 0, 60,
                               ["Тест и откат были, ждём ретеста"])
            
            # ✅ TBS НАЙДЕН для LONG!
            return TBSResult(
                found=True,
                phase=TBSPhase.RETEST,
                zone_type="support",
                zone_price=support,
                test_price=self._l(test_candle),
                retest_price=current_low,
                entry_price=self._h(-1),
                confidence=85,
                reasons=[
                    f"🎯 TBS Pattern: Test → Pullback → Retest",
                    f"📊 Тест: ${self._l(test_candle):.4f}",
                    f"📈 Откат: {pullback_pct*100:.1f}%",
                    f"🔄 Ретест: ${current_low:.4f}",
                    f"⚡ Strike incoming!"
                ]
            )
    
    def get_entry_timing(self) -> Dict:
        """
        Определяет оптимальный тайминг входа по TBS.
        """
        short_tbs = self.detect_tbs_pattern("short")
        long_tbs = self.detect_tbs_pattern("long")
        
        if short_tbs.found and short_tbs.confidence > 80:
            return {
                "direction": "short",
                "confidence": short_tbs.confidence,
                "entry": short_tbs.entry_price,
                "sl": short_tbs.zone_price * 1.01,  # За зоной
                "tp": short_tbs.entry_price * 0.96,  # 4%
                "reasons": short_tbs.reasons
            }
        
        if long_tbs.found and long_tbs.confidence > 80:
            return {
                "direction": "long",
                "confidence": long_tbs.confidence,
                "entry": long_tbs.entry_price,
                "sl": long_tbs.zone_price * 0.99,
                "tp": long_tbs.entry_price * 1.04,
                "reasons": long_tbs.reasons
            }
        
        return {"direction": "none", "confidence": 0}


def detect_tbs_entry(ohlcv: List[Any], direction: str = "short") -> Optional[Dict]:
    """
    Упрощённая функция для быстрой проверки TBS.
    Принимает как List[List[float]] так и List[CandleData].
    """
    if not ohlcv or len(ohlcv) < 10:
        return None
    detector = TBSDetector(ohlcv)
    result = detector.detect_tbs_pattern(direction)
    
    if result.found and result.confidence >= 75:
        return {
            "found": True,
            "pattern": "TBS",
            "entry": result.entry_price,
            "zone": result.zone_price,
            "confidence": result.confidence,
            "reasons": result.reasons
        }
    
    return None
