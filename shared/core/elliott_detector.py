"""
Elliott Wave Detector v4.0
Детекция волн Эллиотта для точных входов

Логика:
- Волна 2, B = ЛОВУШКИ (блокируем)
- Волна 4, C = ИДЕАЛЬНЫЕ точки входа (буст скора)
- Волна 3 = Тренд (небольшой буст)
- Волна 5 = Финал (осторожно, tight SL)

Fix v4.0: Полностью переписан _find_swing_points — старая версия почти никогда
не генерировала HL/LH, поэтому is_uptrend/is_downtrend всегда был False,
и каждый символ падал в "unknown" (wave=?, confidence=30%).
Новая версия: сначала находим чередующиеся pivot-максимумы и минимумы,
затем классифицируем каждый как HH/HL/LH/LL.
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
    1. Фракталов (чередующиеся pivot high / pivot low)
    2. Фибоначчи откатов
    3. Соотношений волн
    """

    def __init__(self):
        self.min_swing_pct = 0.012  # Минимум 1.2% для свинга

    def detect(self, ohlcv: List[Dict], direction: str = "long") -> WaveResult:
        """
        Основной метод детекции волны.

        Args:
            ohlcv: список свечей [{open, high, low, close, ...}] или NamedTuple
            direction: "long" или "short"

        Returns:
            WaveResult с информацией о текущей волне
        """
        if len(ohlcv) < 20:
            return self._empty_result("Недостаточно данных")

        def get_val(c, key):
            if hasattr(c, '_fields') and key in c._fields:
                return getattr(c, key)
            elif hasattr(c, key):
                return getattr(c, key)
            elif isinstance(c, dict):
                return c.get(key, c.get(key.upper(), 0))
            return 0

        try:
            closes = np.array([float(get_val(c, "close")) for c in ohlcv])
            highs  = np.array([float(get_val(c, "high"))  for c in ohlcv])
            lows   = np.array([float(get_val(c, "low"))   for c in ohlcv])
        except Exception as e:
            return self._empty_result(f"Ошибка парсинга OHLCV: {e}")

        swings = self._find_swing_points(highs, lows, closes)

        if len(swings) < 3:
            return self._empty_result("Недостаточно свингов")

        return self._analyze_wave_structure(swings, closes, direction)

    # ------------------------------------------------------------------
    def _find_swing_points(self, highs: np.ndarray, lows: np.ndarray,
                           closes: np.ndarray, window: int = 3) -> List[Dict]:
        """
        Находит чередующиеся pivot-максимумы и минимумы, затем классифицирует
        каждый как HH / LH (для хаёв) или HL / LL (для лоёв).

        Старая версия пыталась делать всё за один проход и почти никогда не
        порождала HL/LH — поэтому is_uptrend и is_downtrend всегда были False.
        """
        n = len(closes)

        # 1. Собираем ВСЕ pivot highs и pivot lows
        raw_highs: List[Dict] = []
        raw_lows:  List[Dict] = []

        for i in range(window, n - window):
            lo_slice = lows[i - window: i + window + 1]
            hi_slice = highs[i - window: i + window + 1]

            if highs[i] >= np.max(hi_slice):
                raw_highs.append({"idx": i, "price": float(highs[i]), "is_high": True})
            elif lows[i] <= np.min(lo_slice):
                raw_lows.append({"idx": i, "price": float(lows[i]), "is_high": False})

        # 2. Объединяем и сортируем по индексу
        all_pivots = sorted(raw_highs + raw_lows, key=lambda x: x["idx"])

        # 3. Оставляем только чередующиеся (убираем подряд идущие одного типа,
        #    сохраняем наиболее экстремальный)
        alternating: List[Dict] = []
        for p in all_pivots:
            if not alternating:
                alternating.append(dict(p))
            elif alternating[-1]["is_high"] == p["is_high"]:
                # Тот же тип — заменяем если более экстремальный
                if p["is_high"] and p["price"] > alternating[-1]["price"]:
                    alternating[-1] = dict(p)
                elif not p["is_high"] and p["price"] < alternating[-1]["price"]:
                    alternating[-1] = dict(p)
            else:
                alternating.append(dict(p))

        # 4. Классифицируем: HH/LH для хаёв, HL/LL для лоёв
        result: List[Dict] = []
        prev_high_price: Optional[float] = None
        prev_low_price:  Optional[float] = None

        for p in alternating:
            p = dict(p)
            if p["is_high"]:
                if prev_high_price is None:
                    p["type"] = "HH"
                elif p["price"] > prev_high_price:
                    p["type"] = "HH"
                else:
                    p["type"] = "LH"
                prev_high_price = p["price"]
            else:
                if prev_low_price is None:
                    p["type"] = "LL"
                elif p["price"] > prev_low_price:
                    p["type"] = "HL"
                else:
                    p["type"] = "LL"
                prev_low_price = p["price"]
            result.append(p)

        return result

    # ------------------------------------------------------------------
    def _analyze_wave_structure(self, swings: List[Dict], closes: np.ndarray,
                                direction: str) -> WaveResult:
        """Анализирует структуру волн по свингам"""

        # Берём последние 6 свингов для анализа
        recent = swings[-6:] if len(swings) >= 6 else swings
        current_price = float(closes[-1])

        hh = sum(1 for s in recent if s["type"] == "HH")
        hl = sum(1 for s in recent if s["type"] == "HL")
        lh = sum(1 for s in recent if s["type"] == "LH")
        ll = sum(1 for s in recent if s["type"] == "LL")

        # Тренд определяется по последним 6 свингам
        is_uptrend   = (hh + hl) > (lh + ll) and (hh >= 1 or hl >= 1)
        is_downtrend = (lh + ll) > (hh + hl) and (lh >= 1 or ll >= 1)

        details = {
            "swing_sequence": [s["type"] for s in recent],
            "hh": hh, "hl": hl, "lh": lh, "ll": ll,
            "is_uptrend": is_uptrend,
            "is_downtrend": is_downtrend,
            "last_swing_price": recent[-1]["price"] if recent else 0,
            "current_price": current_price
        }

        if direction == "long":
            return self._analyze_long_waves(recent, current_price, is_uptrend, is_downtrend, details)
        else:
            return self._analyze_short_waves(recent, current_price, is_uptrend, is_downtrend, details)

    # ------------------------------------------------------------------
    def _analyze_long_waves(self, swings: List[Dict], current_price: float,
                            is_uptrend: bool, is_downtrend: bool, details: Dict) -> WaveResult:
        """Анализ волн для LONG входов"""

        types = details["swing_sequence"]

        # Волна C — коррекция ABC заканчивается → ИДЕАЛ для LONG
        # Признак: is_downtrend, 2+ LL, 1+ LH, и цена начинает РАСТИ от последнего LL
        if is_downtrend and len(swings) >= 3:
            if types.count("LL") >= 2 and types.count("LH") >= 1:
                last_ll = next((s for s in reversed(swings) if s["type"] == "LL"), None)
                if last_ll and current_price > last_ll["price"] * 1.008:
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

        # Волна 4 — откат внутри восходящего тренда → ИДЕАЛ для LONG
        if is_uptrend and len(swings) >= 3:
            last_hh = next((s for s in reversed(swings) if s["type"] == "HH"), None)
            last_hl = next((s for s in reversed(swings) if s["type"] == "HL"), None)
            hh_count = types.count("HH")
            # Case 1: confirmed HL + HH pattern (classic wave 4)
            if hh_count >= 2 and last_hl and last_hh:
                if current_price < last_hh["price"] * 0.985:
                    fib = self._calc_fib_ratio(last_hl["price"], last_hh["price"], current_price)
                    if 0.25 <= fib <= 0.70:
                        return WaveResult(
                            wave="4",
                            wave_type=WaveType.IMPULSE,
                            position=WavePosition.IDEAL,
                            confidence=0.80,
                            ideal_entry=True,
                            is_trap=False,
                            fib_ratio=fib,
                            next_target="5 (финал импульса)",
                            structure_quality=80,
                            details={**details, "reason": f"Коррекция волны 4, фибо {fib:.1%}"}
                        )
            # Case 2: 2+ HH, currently pulling back (HL not yet confirmed = swing forming)
            elif hh_count >= 2 and last_hh:
                prev_hh = next((s for s in swings if s["type"] == "HH" and s["idx"] != last_hh["idx"]), None)
                if prev_hh:
                    pullback_pct = (last_hh["price"] - current_price) / last_hh["price"] * 100
                    wave_size = abs(last_hh["price"] - prev_hh["price"])
                    if pullback_pct >= 1.5 and current_price > prev_hh["price"]:
                        fib = pullback_pct / (wave_size / last_hh["price"] * 100) if wave_size > 0 else 0.4
                        if fib <= 0.70:
                            return WaveResult(
                                wave="4",
                                wave_type=WaveType.IMPULSE,
                                position=WavePosition.IDEAL,
                                confidence=0.72,
                                ideal_entry=True,
                                is_trap=False,
                                fib_ratio=round(fib, 3),
                                next_target="5 (финал импульса)",
                                structure_quality=72,
                                details={**details, "reason": f"Коррекция волны 4 (откат -{pullback_pct:.1f}% от HH), фибо {fib:.1%}"}
                            )

        # Волна 3 — тренд продолжается
        if is_uptrend and details["hh"] >= 2:
            return WaveResult(
                wave="3",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.TREND,
                confidence=0.68,
                ideal_entry=False,
                is_trap=False,
                fib_ratio=1.5,
                next_target="4 (коррекция)",
                structure_quality=68,
                details={**details, "reason": "Волна 3 тренда, можно входить с осторожностью"}
            )

        # Волна 5 — финал
        if is_uptrend and details["hh"] >= 3:
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
                details={**details, "reason": "Возможная волна 5 — финал импульса"}
            )

        # Волна 2 — ловушка
        if is_uptrend and details["hh"] == 1 and details["hl"] == 1:
            if current_price < swings[-1]["price"] * 0.985:
                return WaveResult(
                    wave="2?",
                    wave_type=WaveType.IMPULSE,
                    position=WavePosition.TRAP,
                    confidence=0.60,
                    ideal_entry=False,
                    is_trap=True,
                    fib_ratio=0.5,
                    next_target="3 (неопределённость)",
                    structure_quality=40,
                    details={**details, "reason": "Возможная волна 2 — ловушка, слишком рано"}
                )

        # Волна B — ловушка
        if is_downtrend and details["lh"] >= 1 and details["ll"] >= 1:
            if current_price > swings[-1]["price"] * 1.008:
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
                    details={**details, "reason": "Возможная волна B — фейковый отскок"}
                )

        # Боковик / начало — неопределённость, не блокируем
        return WaveResult(
            wave="?",
            wave_type=WaveType.UNKNOWN,
            position=WavePosition.EARLY,
            confidence=0.30,
            ideal_entry=False,
            is_trap=False,  # v4: не блокируем неизвестные структуры
            fib_ratio=0.5,
            next_target="?",
            structure_quality=25,
            details={**details, "reason": "Неопределённая структура — нейтрально"}
        )

    # ------------------------------------------------------------------
    def _analyze_short_waves(self, swings: List[Dict], current_price: float,
                             is_uptrend: bool, is_downtrend: bool, details: Dict) -> WaveResult:
        """Анализ волн для SHORT входов (зеркально LONG)"""

        types = details["swing_sequence"]

        # Волна C вверх заканчивается → ИДЕАЛ для SHORT
        # Признак: is_uptrend (коррекция вверх), 2+ HH, 1+ HL, цена НАЧИНАЕТ ПАДАТЬ от последнего HH
        if is_uptrend and len(swings) >= 3:
            if types.count("HH") >= 2 and types.count("HL") >= 1:
                last_hh = next((s for s in reversed(swings) if s["type"] == "HH"), None)
                if last_hh and current_price < last_hh["price"] * 0.992:
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

        # Волна 4 вниз — откат в нисходящем тренде → ИДЕАЛ для SHORT
        if is_downtrend and len(swings) >= 3:
            last_ll = next((s for s in reversed(swings) if s["type"] == "LL"), None)
            last_lh = next((s for s in reversed(swings) if s["type"] == "LH"), None)
            ll_count = types.count("LL")
            # Case 1: confirmed LH + LL pattern (classic wave 4)
            if ll_count >= 2 and last_lh and last_ll:
                if current_price > last_ll["price"] * 1.015:
                    fib = self._calc_fib_ratio(last_lh["price"], last_ll["price"], current_price)
                    if 0.25 <= fib <= 0.70:
                        return WaveResult(
                            wave="4",
                            wave_type=WaveType.IMPULSE,
                            position=WavePosition.IDEAL,
                            confidence=0.80,
                            ideal_entry=True,
                            is_trap=False,
                            fib_ratio=fib,
                            next_target="5 (финал импульса вниз)",
                            structure_quality=80,
                            details={**details, "reason": f"Коррекция волны 4 вниз, фибо {fib:.1%}"}
                        )
            # Case 2: 2+ LL, currently bouncing (LH not yet confirmed = swing still forming)
            elif ll_count >= 2 and last_ll:
                prev_ll = next((s for s in swings if s["type"] == "LL" and s["idx"] != last_ll["idx"]), None)
                if prev_ll:
                    bounce_pct = (current_price - last_ll["price"]) / last_ll["price"] * 100
                    wave_size = abs(prev_ll["price"] - last_ll["price"])
                    if bounce_pct >= 1.5 and current_price < prev_ll["price"]:
                        fib = bounce_pct / (wave_size / last_ll["price"] * 100) if wave_size > 0 else 0.4
                        if fib <= 0.70:
                            return WaveResult(
                                wave="4",
                                wave_type=WaveType.IMPULSE,
                                position=WavePosition.IDEAL,
                                confidence=0.72,
                                ideal_entry=True,
                                is_trap=False,
                                fib_ratio=round(fib, 3),
                                next_target="5 (финал импульса вниз)",
                                structure_quality=72,
                                details={**details, "reason": f"Коррекция волны 4 (отскок +{bounce_pct:.1f}% от LL), фибо {fib:.1%}"}
                            )

        # Волна 3 вниз — тренд
        if is_downtrend and details["ll"] >= 2:
            return WaveResult(
                wave="3",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.TREND,
                confidence=0.68,
                ideal_entry=False,
                is_trap=False,
                fib_ratio=1.5,
                next_target="4 (коррекция вверх)",
                structure_quality=68,
                details={**details, "reason": "Волна 3 тренда вниз"}
            )

        # Волна 5 вниз — финал
        if is_downtrend and details["ll"] >= 3:
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
                details={**details, "reason": "Возможная волна 5 вниз — финал"}
            )

        # Волна 2 вниз — ловушка
        if is_downtrend and details["ll"] == 1 and details["lh"] == 1:
            return WaveResult(
                wave="2?",
                wave_type=WaveType.IMPULSE,
                position=WavePosition.TRAP,
                confidence=0.60,
                ideal_entry=False,
                is_trap=True,
                fib_ratio=0.5,
                next_target="3 (неопределённость)",
                structure_quality=40,
                details={**details, "reason": "Возможная волна 2 вниз — ловушка"}
            )

        # Волна B вверх — ловушка для SHORT
        if is_uptrend and details["hh"] >= 1 and details["hl"] >= 1:
            if current_price > swings[-1]["price"] * 1.008:
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
                    details={**details, "reason": "Возможная волна B вверх — фейк, будет C"}
                )

        return WaveResult(
            wave="?",
            wave_type=WaveType.UNKNOWN,
            position=WavePosition.EARLY,
            confidence=0.30,
            ideal_entry=False,
            is_trap=False,  # v4: не блокируем неизвестные структуры
            fib_ratio=0.5,
            next_target="?",
            structure_quality=25,
            details={**details, "reason": "Неопределённая структура — нейтрально"}
        )

    # ------------------------------------------------------------------
    def _calc_fib_ratio(self, wave_start: float, wave_end: float,
                        current_price: float) -> float:
        """Процент отката от импульса"""
        impulse = abs(wave_end - wave_start)
        if impulse < 1e-10:
            return 0.5
        pullback = abs(current_price - wave_end)
        return round(min(pullback / impulse, 2.0), 3)

    def _empty_result(self, reason: str) -> WaveResult:
        return WaveResult(
            wave="?",
            wave_type=WaveType.UNKNOWN,
            position=WavePosition.EARLY,
            confidence=0.0,
            ideal_entry=False,
            is_trap=False,
            fib_ratio=0.5,
            next_target="?",
            structure_quality=0,
            details={"reason": reason}
        )


# Глобальный инстанс для переиспользования
_elliott_detector = None

def get_elliott_detector() -> ElliottWaveDetector:
    global _elliott_detector
    if _elliott_detector is None:
        _elliott_detector = ElliottWaveDetector()
    return _elliott_detector


def detect_elliott_wave(ohlcv: List[Dict], direction: str = "long") -> WaveResult:
    """Быстрая функция детекции волны"""
    return get_elliott_detector().detect(ohlcv, direction)
