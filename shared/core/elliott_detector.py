"""
Elliott Wave Detector v3.0
Детекция волн Эллиотта для точных входов

Логика:
- Волна 2, B = ЛОВУШКИ (блокируем)
- Волна 4, C = ИДЕАЛЬНЫЕ точки входа (буст скора)
- Волна 3 = Тренд (небольшой буст)
- Волна 5 = Финал (осторожно, тight SL)
"""

import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class WaveType(Enum):
    IMPULSE = "impulse"      # 1-2-3-4-5
    CORRECTION = "correction"  # A-B-C
    UNKNOWN = "unknown"


class WavePosition(Enum):
    EARLY = "early"        # 1, A
    TRAP = "trap"          # 2, B ⚠️ Опасно!
    IDEAL = "ideal"        # 4, C 🎯 Лучшие точки!
    TREND = "trend"        # 3 Импульс
    FINAL = "final"        # 5, конец движения


@dataclass
class WaveResult:
    """Результат детекции волны"""
    wave: str              # "1", "2", "3", "4", "5", "A", "B", "C"
    wave_type: WaveType    # impulse/correction
    position: WavePosition # early/trap/ideal/trend/final
    confidence: float      # 0.0-1.0
    ideal_entry: bool      # True если волна 4 или C
    is_trap: bool          # True если волна 2 или B
    fib_ratio: float       # Текущий фибо-уровень
    next_target: str       # Следующая ожидаемая волна
    structure_quality: int # 0-100 качество структуры
    
    # Для логирования
    details: Dict = None
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


class ElliottWaveDetector:
    """
    Детектор волн Эллиотта на основе:
    1. Фракталов (5-bar HH/HL)
    2. Фибоначчи откатов
    3. Дивергенций
    4. Соотношений волн
    """
    
    def __init__(self):
        self.min_swing_pct = 0.015  # Минимум 1.5% для свинга
        
    def detect(self, ohlcv: List[Dict], direction: str = "long") -> WaveResult:
        """
        Основной метод детекции волны
        
        Args:
            ohlcv: список свечей [{open, high, low, close, timestamp}] или NamedTuple
            direction: "long" или "short"
        
        Returns:
            WaveResult с информацией о текущей волне
        """
        if len(ohlcv) < 20:
            return self._empty_result("Недостаточно данных")
        
        # 🔧 FIX: Поддержка и словарей и NamedTuple
        def get_val(c, key):
            # Для NamedTuple (CandleData)
            if hasattr(c, '_fields') and key in c._fields:
                return getattr(c, key)
            # Для обычного объекта
            elif hasattr(c, key):
                return getattr(c, key)
            # Для словаря
            elif isinstance(c, dict):
                return c.get(key, c.get(key.upper(), 0))
            return 0
        
        try:
            closes = np.array([get_val(c, "close") for c in ohlcv])
            highs = np.array([get_val(c, "high") for c in ohlcv])
            lows = np.array([get_val(c, "low") for c in ohlcv])
        except Exception as e:
            print(f"🌊 [ELLIOTT-DEBUG] Ошибка парсинга OHLCV: {e}")
            return self._empty_result(f"Ошибка парсинга OHLCV: {e}")
        
        # Находим свинги (точки разворота)
        swings = self._find_swing_points(highs, lows, closes)
        
        if len(swings) < 3:
            return self._empty_result("Недостаточно свингов")
        
        # Анализируем структуру волн
        wave_analysis = self._analyze_wave_structure(swings, closes, direction)
        
        return wave_analysis
    
    def _find_swing_points(self, highs: np.ndarray, lows: np.ndarray, 
                           closes: np.ndarray, window: int = 3) -> List[Dict]:
        """Находит точки разворота (HH, HL, LH, LL)"""
        swings = []
        
        for i in range(window, len(closes) - window):
            # Проверяем Higher High
            if highs[i] == max(highs[i-window:i+window+1]):
                swings.append({
                    "idx": i,
                    "price": highs[i],
                    "type": "HH",
                    "is_high": True
                })
            # Проверяем Lower Low
            elif lows[i] == min(lows[i-window:i+window+1]):
                swings.append({
                    "idx": i,
                    "price": lows[i],
                    "type": "LL",
                    "is_high": False
                })
            # Проверяем Higher Low (после LL)
            elif len(swings) > 0 and swings[-1]["type"] == "LL":
                if lows[i] > lows[i-1] and lows[i] > min(lows[i-window:i]):
                    swings.append({
                        "idx": i,
                        "price": lows[i],
                        "type": "HL",
                        "is_high": False
                    })
            # Проверяем Lower High (после HH)
            elif len(swings) > 0 and swings[-1]["type"] == "HH":
                if highs[i] < highs[i-1] and highs[i] < max(highs[i-window:i]):
                    swings.append({
                        "idx": i,
                        "price": highs[i],
                        "type": "LH",
                        "is_high": True
                    })
        
        return swings
    
    def _analyze_wave_structure(self, swings: List[Dict], closes: np.ndarray,
                                 direction: str) -> WaveResult:
        """Анализирует структуру волн по свингам"""
        
        if len(swings) < 3:
            return self._empty_result("Мало свингов для анализа")
        
        # Берем последние 4-5 свингов для анализа
        recent = swings[-5:] if len(swings) >= 5 else swings
        
        # Определяем текущую цену и последний свинг
        current_price = closes[-1]
        last_swing = recent[-1]
        
        # Анализируем паттерн
        wave_pattern = self._identify_pattern(recent, current_price, direction)
        
        return wave_pattern
    
    def _identify_pattern(self, swings: List[Dict], current_price: float,
                          direction: str) -> WaveResult:
        """Идентификация паттерна волн"""
        
        # Базовая логика определения волны по структуре свингов
        hh_count = sum(1 for s in swings if s["type"] == "HH")
        hl_count = sum(1 for s in swings if s["type"] == "HL")
        lh_count = sum(1 for s in swings if s["type"] == "LH")
        ll_count = sum(1 for s in swings if s["type"] == "LL")
        
        # Определяем тренд
        is_uptrend = hh_count > lh_count and hl_count > ll_count
        is_downtrend = lh_count > hh_count and ll_count > hl_count
        
        # Последовательность свингов
        types = [s["type"] for s in swings]
        
        details = {
            "swing_sequence": types,
            "hh_count": hh_count,
            "hl_count": hl_count,
            "lh_count": lh_count,
            "ll_count": ll_count,
            "is_uptrend": is_uptrend,
            "is_downtrend": is_downtrend,
            "last_swing_price": swings[-1]["price"] if swings else 0,
            "current_price": current_price
        }
        
        # === ЛОГИКА ОПРЕДЕЛЕНИЯ ВОЛН ===
        
        # Для LONG бота (ищем разворот вверх или продолжение)
        if direction == "long":
            return self._analyze_long_waves(swings, current_price, is_uptrend, is_downtrend, details)
        else:  # short
            if len(swings) < 4 or len(ohlcv) < 20:
                # Недостаточно свингов — упрощенный анализ
                details = {"swing_types": [s.get("type", "?") for s in swings], "swing_count": len(swings)}
                return self._analyze_short_waves(swings, current_price, is_uptrend, is_downtrend, details)
            return self._analyze_short_waves(swings, current_price, is_uptrend, is_downtrend, details)
    
    def _analyze_long_waves(self, swings: List[Dict], current_price: float,
                            is_uptrend: bool, is_downtrend: bool, details: Dict) -> WaveResult:
        """Анализ волн для LONG входов"""
        
        types = details["swing_sequence"]
        last_swing = swings[-1] if swings else None
        
        # Волна C (коррекция заканчивается) - ИДЕАЛЬНО для LONG
        if is_downtrend and len(swings) >= 3:
            # Последовательность: LL -> LH -> LL (снижающиеся минимумы)
            if types.count("LL") >= 2 and types.count("LH") >= 1:
                # Проверяем не вышли ли мы из коррекции
                if current_price > last_swing["price"] * 1.01:
                    return WaveResult(
                        wave="C",
                        wave_type=WaveType.CORRECTION,
                        position=WavePosition.IDEAL,
                        confidence=0.75,
                        ideal_entry=True,
                        is_trap=False,
                        fib_ratio=1.0,
                        next_target="1 (новый импульс)",
                        structure_quality=75,
                        details={**details, "reason": "Завершение коррекции ABC, начало нового роста"}
                    )
        
        # Волна 4 (коррекция в восходящем тренде) - ОТЛИЧНО для LONG
        if is_uptrend and len(swings) >= 4:
            # HH -> HL -> HH -> [текущая коррекция]
            if types.count("HH") >= 2 and types.count("HL") >= 1:
                # Проверяем что мы в откате от последнего HH
                last_hh = next((s for s in reversed(swings) if s["type"] == "HH"), None)
                if last_hh and current_price < last_hh["price"] * 0.98:
                    # Расчет фибо-отката
                    fib_ratio = self._calc_fib_ratio(swings, current_price)
                    if 0.382 <= fib_ratio <= 0.618:  # Идеальная зона волны 4
                        return WaveResult(
                            wave="4",
                            wave_type=WaveType.IMPULSE,
                            position=WavePosition.IDEAL,
                            confidence=0.80,
                            ideal_entry=True,
                            is_trap=False,
                            fib_ratio=fib_ratio,
                            next_target="5 (финал импульса)",
                            structure_quality=80,
                            details={**details, "reason": f"Коррекция волны 4, фибо {fib_ratio:.1%}"}
                        )
        
        # Волна 2 (ловушка!) - БЛОКИРУЕМ
        if is_uptrend and len(swings) >= 2:
            if types.count("HH") == 1 and types.count("HL") == 1:
                # Только начало тренда, может быть ловушкой
                if current_price < swings[-1]["price"] * 0.98:
                    return WaveResult(
                        wave="2?",
                        wave_type=WaveType.IMPULSE,
                        position=WavePosition.TRAP,
                        confidence=0.60,
                        ideal_entry=False,
                        is_trap=True,
                        fib_ratio=0.5,
                        next_target="3 (неопределенность)",
                        structure_quality=40,
                        details={**details, "reason": "Возможная волна 2 - ловушка, слишком рано"}
                    )
        
        # Волна B (ловушка!) - БЛОКИРУЕМ
        if is_downtrend and types.count("LH") >= 1 and types.count("LL") >= 1:
            return WaveResult(
                wave="B?",
                wave_type=WaveType.CORRECTION,
                position=WavePosition.TRAP,
                confidence=0.55,
                ideal_entry=False,
                is_trap=True,
                fib_ratio=0.5,
                next_target="C (продолжение падения)",
                structure_quality=35,
                details={**details, "reason": "Возможная волна B - фейковый отскок, будет C"}
            )
        
        # Волна 3 (тренд) - хорошо, но не идеально
        if is_uptrend and types.count("HH") >= 2:
            return WaveResult(
                wave="3",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.TREND,
                confidence=0.70,
                ideal_entry=False,
                is_trap=False,
                fib_ratio=1.5,
                next_target="4 (коррекция)",
                structure_quality=70,
                details={**details, "reason": "Волна 3 тренда, можно входить с осторожностью"}
            )
        
        # Волна 5 (финал) - осторожно
        if is_uptrend and types.count("HH") >= 3:
            return WaveResult(
                wave="5?",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.FINAL,
                confidence=0.50,
                ideal_entry=False,
                is_trap=False,
                fib_ratio=2.0,
                next_target="A (коррекция)",
                structure_quality=50,
                details={**details, "reason": "Возможная волна 5 - финал, дальше коррекция"}
            )
        
        # Неопределенность
        return WaveResult(
            wave="?",
            wave_type=WaveType.UNKNOWN,
            position=WavePosition.EARLY,
            confidence=0.30,
            ideal_entry=False,
            is_trap=True,  # Безопаснее считать ловушкой
            fib_ratio=0.5,
            next_target="?",
            structure_quality=20,
            details={**details, "reason": "Неопределенная структура"}
        )
    
    def _analyze_short_waves(self, swings: List[Dict], current_price: float,
                             is_uptrend: bool, is_downtrend: bool, details: Dict) -> WaveResult:
        """Анализ волн для SHORT входов (зеркально LONG)"""
        
        types = details["swing_sequence"]
        
        # Для SHORT ищем окончание роста (волна 5 или C завершена)
        
        # Волна C вверх (коррекция заканчивается) - ИДЕАЛЬНО для SHORT
        if is_uptrend and len(swings) >= 3:
            if types.count("HH") >= 2 and types.count("HL") >= 1:
                if current_price < swings[-1]["price"] * 0.99:
                    return WaveResult(
                        wave="C",
                        wave_type=WaveType.CORRECTION,
                        position=WavePosition.IDEAL,
                        confidence=0.75,
                        ideal_entry=True,
                        is_trap=False,
                        fib_ratio=1.0,
                        next_target="1 (новый импульс вниз)",
                        structure_quality=75,
                        details={**details, "reason": "Завершение коррекции ABC вверх, начало падения"}
                    )
        
        # Волна 4 вниз (коррекция в нисходящем тренде) - ОТЛИЧНО для SHORT  
        if is_downtrend and len(swings) >= 4:
            if types.count("LL") >= 2 and types.count("LH") >= 1:
                last_ll = next((s for s in reversed(swings) if s["type"] == "LL"), None)
                if last_ll and current_price > last_ll["price"] * 1.02:
                    fib_ratio = self._calc_fib_ratio(swings, current_price)
                    if 0.382 <= fib_ratio <= 0.618:
                        return WaveResult(
                            wave="4",
                            wave_type=WaveType.IMPULSE,
                            position=WavePosition.IDEAL,
                            confidence=0.80,
                            ideal_entry=True,
                            is_trap=False,
                            fib_ratio=fib_ratio,
                            next_target="5 (финал импульса вниз)",
                            structure_quality=80,
                            details={**details, "reason": f"Коррекция волны 4 вниз, фибо {fib_ratio:.1%}"}
                        )
        
        # Волна 2 вниз (ловушка!) - БЛОКИРУЕМ
        if is_downtrend and len(swings) >= 2:
            if types.count("LL") == 1 and types.count("LH") == 1:
                return WaveResult(
                    wave="2?",
                    wave_type=WaveType.IMPULSE,
                    position=WavePosition.TRAP,
                    confidence=0.60,
                    ideal_entry=False,
                    is_trap=True,
                    fib_ratio=0.5,
                    next_target="3 (неопределенность)",
                    structure_quality=40,
                    details={**details, "reason": "Возможная волна 2 вниз - ловушка"}
                )
        
        # Волна B вверх (ловушка!) - БЛОКИРУЕМ
        if is_uptrend and types.count("HL") >= 1 and types.count("HH") >= 1:
            return WaveResult(
                wave="B?",
                wave_type=WaveType.CORRECTION,
                position=WavePosition.TRAP,
                confidence=0.55,
                ideal_entry=False,
                is_trap=True,
                fib_ratio=0.5,
                next_target="C (продолжение роста)",
                structure_quality=35,
                details={**details, "reason": "Возможная волна B вверх - фейк, будет C"}
            )
        
        # Волна 3 вниз (тренд)
        if is_downtrend and types.count("LL") >= 2:
            return WaveResult(
                wave="3",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.TREND,
                confidence=0.70,
                ideal_entry=False,
                is_trap=False,
                fib_ratio=1.5,
                next_target="4 (коррекция вверх)",
                structure_quality=70,
                details={**details, "reason": "Волна 3 тренда вниз"}
            )
        
        # Волна 5 вниз (финал)
        if is_downtrend and types.count("LL") >= 3:
            return WaveResult(
                wave="5?",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.FINAL,
                confidence=0.50,
                ideal_entry=False,
                is_trap=False,
                fib_ratio=2.0,
                next_target="A (коррекция вверх)",
                structure_quality=50,
                details={**details, "reason": "Возможная волна 5 вниз - финал"}
            )
        
        return WaveResult(
            wave="?",
            wave_type=WaveType.UNKNOWN,
            position=WavePosition.EARLY,
            confidence=0.30,
            ideal_entry=False,
            is_trap=True,
            fib_ratio=0.5,
            next_target="?",
            structure_quality=20,
            details={**details, "reason": "Неопределенная структура"}
        )
    
    def _calc_fib_ratio(self, swings: List[Dict], current_price: float) -> float:
        """Расчет фибоначчи отката"""
        if len(swings) < 2:
            return 0.5
        
        # Берем последний импульс (от HL до HH или наоборот)
        last_two = swings[-2:]
        
        if len(last_two) >= 2:
            impulse_start = last_two[0]["price"]
            impulse_end = last_two[1]["price"]
            
            impulse_size = abs(impulse_end - impulse_start)
            if impulse_size == 0:
                return 0.5
            
            current_pullback = abs(current_price - impulse_end)
            ratio = current_pullback / impulse_size
            
            return round(ratio, 3)
        
        return 0.5
    
    def _empty_result(self, reason: str) -> WaveResult:
        """Возвращает пустой результат"""
        return WaveResult(
            wave="?",
            wave_type=WaveType.UNKNOWN,
            position=WavePosition.EARLY,
            confidence=0.0,
            ideal_entry=False,
            is_trap=True,
            fib_ratio=0.5,
            next_target="?",
            structure_quality=0,
            details={"reason": reason}
        )


# Глобальный инстанс для переиспользования
_elliott_detector = None

def get_elliott_detector() -> ElliottWaveDetector:
    """Получить инстанс детектора волн"""
    global _elliott_detector
    if _elliott_detector is None:
        _elliott_detector = ElliottWaveDetector()
    return _elliott_detector


# Удобная функция для быстрой проверки
def detect_elliott_wave(ohlcv: List[Dict], direction: str = "long") -> WaveResult:
    """Быстрая функция детекции волны"""
    detector = get_elliott_detector()
    return detector.detect(ohlcv, direction)
