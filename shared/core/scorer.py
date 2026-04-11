"""
Dual Scoring System: Short Scorer + Long Scorer
Оценка сигналов для обоих направлений
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum
from datetime import datetime


class Direction(Enum):
    """Направление торговли"""
    SHORT = "short"
    LONG = "long"


class Confidence(Enum):
    """Уровень уверенности в сигнале"""
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass
class ScoreComponent:
    """Компонент скора"""
    name: str
    score: int
    max_score: int
    description: str
    raw_value: Optional[float] = None


@dataclass
class ScoreResult:
    """Результат оценки"""
    total_score: int
    max_possible: int
    direction: Direction
    is_valid: bool
    confidence: Confidence
    grade: str  # F, D, C, B, A, S
    components: List[ScoreComponent]
    reasons: List[str]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def percentage(self) -> float:
        """Процент от максимума"""
        if self.max_possible == 0:
            return 0.0
        return round(self.total_score / self.max_possible * 100, 1)


@dataclass
class Pattern:
    """Торговый паттерн"""
    name: str
    direction: Direction
    strength: int
    candles_ago: int
    freshness: int  # минут назад
    volume_multiplier: float
    delta_at_trigger: float
    entry_price: float
    stop_loss: float
    confidence: str
    description: str


class BaseScorer:
    """Базовый класс для скоринга"""
    
    # Компоненты и их веса
    COMPONENT_WEIGHTS = {
        "rsi": 20,
        "funding": 15,
        "long_short_ratio": 15,
        "open_interest": 15,
        "delta": 20,
        "pattern": 30
    }
    
    def __init__(self, min_score: int = 65, direction: Direction = Direction.SHORT):
        self.min_score = min_score
        self.direction = direction
    
    def calculate_grade(self, score: int) -> str:
        """Расчёт буквенной оценки"""
        if score >= 90:
            return "S"
        elif score >= 80:
            return "A"
        elif score >= 70:
            return "B"
        elif score >= 60:
            return "C"
        elif score >= 50:
            return "D"
        else:
            return "F"
    
    def determine_confidence(self, score: int) -> Confidence:
        """Определение уровня уверенности"""
        if score >= 85:
            return Confidence.VERY_HIGH
        elif score >= 75:
            return Confidence.HIGH
        elif score >= 65:
            return Confidence.MEDIUM
        elif score >= 50:
            return Confidence.LOW
        else:
            return Confidence.VERY_LOW


class ShortScorer(BaseScorer):
    """
    Скорер для SHORT позиций
    Оценивает перекупленность монеты
    """
    
    def __init__(self, min_score: int = 65):
        super().__init__(min_score, Direction.SHORT)
    
    def calculate_rsi_component(self, rsi_1h: float) -> ScoreComponent:
        """
        RSI компонент (0-20 очков)
        Выше RSI = лучше для шорта
        """
        if rsi_1h >= 80:
            score = 20
            desc = f"RSI {rsi_1h:.1f} - Экстремальная перекупленность"
        elif rsi_1h >= 75:
            score = 18
            desc = f"RSI {rsi_1h:.1f} - Сильная перекупленность"
        elif rsi_1h >= 70:
            score = 15
            desc = f"RSI {rsi_1h:.1f} - Перекупленность"
        elif rsi_1h >= 65:
            score = 12
            desc = f"RSI {rsi_1h:.1f} - Начало перекупленности"
        elif rsi_1h >= 60:
            score = 8
            desc = f"RSI {rsi_1h:.1f} - Близко к перекупленности"
        elif rsi_1h >= 55:
            score = 4
            desc = f"RSI {rsi_1h:.1f} - Нейтрально-бullish"
        elif rsi_1h < 30:
            score = 0
            desc = f"RSI {rsi_1h:.1f} - Перепроданность (плохо для шорта)"
        else:
            score = 2
            desc = f"RSI {rsi_1h:.1f} - Нейтральная зона"
        
        return ScoreComponent(
            name="RSI",
            score=score,
            max_score=20,
            description=desc,
            raw_value=rsi_1h
        )
    
    def calculate_funding_component(self, current_funding: float,
                                   accumulated_4d: float) -> ScoreComponent:
        """
        Фандинг компонент (0-15 очков)
        Высокий положительный = лонги платят шортам = хорошо для шорта
        """
        score = 0
        reasons = []
        
        # Текущий фандинг
        if current_funding >= 0.1:
            score += 8
            reasons.append(f"Высокий фандинг {current_funding:.3f}%")
        elif current_funding >= 0.05:
            score += 5
            reasons.append(f"Повышенный фандинг {current_funding:.3f}%")
        elif current_funding > 0:
            score += 3
            reasons.append(f"Положительный фандинг {current_funding:.3f}%")
        elif current_funding <= -0.05:
            score += 0
            reasons.append(f"Отрицательный фандинг {current_funding:.3f}% (плохо)")
        else:
            score += 1
            reasons.append(f"Нейтральный фандинг {current_funding:.3f}%")
        
        # Накопленный фандинг
        if accumulated_4d >= 0.5:
            score += 7
            reasons.append(f"Высокий накопленный фандинг {accumulated_4d:.2f}%")
        elif accumulated_4d >= 0.3:
            score += 5
            reasons.append(f"Накопленный фандинг {accumulated_4d:.2f}%")
        elif accumulated_4d >= 0.1:
            score += 3
        elif accumulated_4d < 0:
            score += 0
            reasons.append(f"Отрицательный накопленный {accumulated_4d:.2f}% (плохо)")
        else:
            score += 1
        
        return ScoreComponent(
            name="Funding",
            score=min(score, 15),
            max_score=15,
            description=" | ".join(reasons) if reasons else "Нейтральный фандинг",
            raw_value=current_funding
        )
    
    def calculate_ratio_component(self, long_ratio: float) -> ScoreComponent:
        """
        L/S Ratio компонент (0-15 очков)
        Больше лонгов = толпа в лонгах = хорошо для шорта
        """
        short_ratio = 100 - long_ratio
        
        if long_ratio >= 70:
            score = 15
            desc = f"{long_ratio:.0f}% лонгов (толпа в лонгах, риск сжатия)"
        elif long_ratio >= 65:
            score = 12
            desc = f"{long_ratio:.0f}% лонгов (много лонгистов)"
        elif long_ratio >= 60:
            score = 10
            desc = f"{long_ratio:.0f}% лонгов (лонгисты доминируют)"
        elif long_ratio >= 55:
            score = 7
            desc = f"{long_ratio:.0f}% лонгов (лонг перевес)"
        elif long_ratio >= 50:
            score = 4
            desc = f"{long_ratio:.0f}% лонгов (лёгкий лонг перевес)"
        elif long_ratio >= 45:
            score = 2
            desc = f"{long_ratio:.0f}% лонгов (баланс)"
        elif long_ratio >= 35:
            score = 1
            desc = f"{long_ratio:.0f}% лонгов (шортисты доминируют, плохо)"
        else:
            score = 0
            desc = f"{long_ratio:.0f}% лонгов (толпа в шортах, избегать)"
        
        return ScoreComponent(
            name="L/S Ratio",
            score=score,
            max_score=15,
            description=desc,
            raw_value=long_ratio
        )
    
    def calculate_oi_component(self, oi_change_4d: float,
                              price_change_4d: float) -> ScoreComponent:
        """
        Open Interest компонент (0-15 очков)
        OI растёт с ценой = лонги перегружены = хорошо для шорта
        """
        if oi_change_4d >= 15 and price_change_4d >= 5:
            score = 15
            desc = f"OI +{oi_change_4d:.1f}% при росте +{price_change_4d:.1f}% (перегруз лонгов)"
        elif oi_change_4d >= 10 and price_change_4d >= 3:
            score = 12
            desc = f"OI +{oi_change_4d:.1f}% при росте +{price_change_4d:.1f}%"
        elif oi_change_4d >= 5 and price_change_4d >= 0:
            score = 8
            desc = f"OI +{oi_change_4d:.1f}% с ростом цены"
        elif oi_change_4d >= 0 and price_change_4d >= 0:
            score = 4
            desc = f"OI +{oi_change_4d:.1f}% (слабый рост)"
        elif oi_change_4d < -10 and price_change_4d < -5:
            score = 5
            desc = f"OI {oi_change_4d:.1f}% при падении (лонги закрываются, возможен отскок)"
        elif oi_change_4d < 0 and price_change_4d < 0:
            score = 2
            desc = f"OI {oi_change_4d:.1f}% при падении"
        else:
            score = 0
            desc = f"OI {oi_change_4d:.1f}% без ясной картины"
        
        return ScoreComponent(
            name="Open Interest",
            score=score,
            max_score=15,
            description=desc,
            raw_value=oi_change_4d
        )
    
    def calculate_delta_component(self, hourly_deltas: List[float],
                                 price_trend: str) -> ScoreComponent:
        """
        Дельта компонент (0-20 очков)
        Отрицательная дельта на росте = скрытые продажи = хорошо для шорта
        """
        score = 0
        reasons = []
        
        # Подсчёт отрицательных часов
        negative_hours = sum(1 for d in hourly_deltas if d < 0)
        positive_hours = sum(1 for d in hourly_deltas if d > 0)
        total_hours = len(hourly_deltas)
        
        # Пункты за отрицательные часы (макс 8)
        if negative_hours >= 5 and total_hours >= 6:
            score += 8
            reasons.append(f"{negative_hours}/{total_hours} часов отрицательной дельты")
        elif negative_hours >= 4:
            score += 6
            reasons.append(f"{negative_hours} часов отрицательной дельты")
        elif negative_hours >= 3:
            score += 4
            reasons.append(f"{negative_hours} часов отрицательной дельты")
        elif negative_hours >= 2:
            score += 2
        
        # Дивергенция: цена растёт, дельта падает (макс 12)
        if price_trend == "rising" and negative_hours >= 3:
            score += 12
            reasons.append("Медвежья дивергенция (цена растёт, дельта падает)")
        elif price_trend == "rising" and negative_hours >= 2:
            score += 8
            reasons.append("Слабая медвежья дивергенция")
        elif price_trend == "sideways" and negative_hours >= 4:
            score += 6
            reasons.append("Давление продавцов в боковике")
        
        return ScoreComponent(
            name="Delta",
            score=min(score, 20),
            max_score=20,
            description=" | ".join(reasons) if reasons else "Нейтральная дельта",
            raw_value=sum(hourly_deltas) if hourly_deltas else 0
        )
    
    def calculate_pattern_component(self, patterns: List[Pattern]) -> Tuple[ScoreComponent, List[str]]:
        """
        Паттерн компонент (0-30 очков) + список названий паттернов
        """
        if not patterns:
            return ScoreComponent(
                name="Patterns",
                score=0,
                max_score=30,
                description="Нет паттернов"
            ), []
        
        # Берём самый сильный паттерн
        best_pattern = max(patterns, key=lambda p: p.strength)
        base_score = best_pattern.strength
        pattern_names = [p.name for p in patterns]
        
        # Бонус за комбинацию паттернов
        bonus = 0
        if len(patterns) >= 2:
            bonus += 3
        if len(patterns) >= 3:
            bonus += 5
        
        # Бонус за свежесть
        freshness_bonus = 0
        if best_pattern.candles_ago == 0:
            freshness_bonus = 2
        elif best_pattern.candles_ago == 1:
            freshness_bonus = 1
        
        total_score = min(base_score + bonus + freshness_bonus, 30)
        
        desc = f"{best_pattern.name} (сила: {best_pattern.strength})"
        if len(patterns) > 1:
            desc += f" + {len(patterns)-1} паттернов (+{bonus})"
        if freshness_bonus > 0:
            desc += f", свежий (+{freshness_bonus})"
        
        return ScoreComponent(
            name="Patterns",
            score=total_score,
            max_score=30,
            description=desc
        ), pattern_names
    
    def calculate_score(self,
                       rsi_1h: float,
                       funding_current: float,
                       funding_accumulated: float,
                       long_ratio: float,
                       oi_change_4d: float,
                       price_change_4d: float,
                       hourly_deltas: List[float],
                       price_trend: str,
                       patterns: List[Pattern]) -> ScoreResult:
        """
        Расчёт полного Short Score
        
        Args:
            rsi_1h: RSI на 1h таймфрейме
            funding_current: Текущий фандинг (0.01 = 1%)
            funding_accumulated: Накопленный фандинг за 4 дня
            long_ratio: Процент лонгов (50 = баланс)
            oi_change_4d: Изменение OI за 4 дня в процентах
            price_change_4d: Изменение цены за 4 дня в процентах
            hourly_deltas: Дельта по часам (список)
            price_trend: 'rising', 'falling', 'sideways'
            patterns: Список обнаруженных паттернов
        
        Returns:
            ScoreResult с полной оценкой
        """
        # Расчёт компонентов
        components = []
        
        rsi_comp = self.calculate_rsi_component(rsi_1h)
        components.append(rsi_comp)
        
        funding_comp = self.calculate_funding_component(funding_current, funding_accumulated)
        components.append(funding_comp)
        
        ratio_comp = self.calculate_ratio_component(long_ratio)
        components.append(ratio_comp)
        
        oi_comp = self.calculate_oi_component(oi_change_4d, price_change_4d)
        components.append(oi_comp)
        
        delta_comp = self.calculate_delta_component(hourly_deltas, price_trend)
        components.append(delta_comp)
        
        pattern_comp, pattern_names = self.calculate_pattern_component(patterns)
        components.append(pattern_comp)
        
        # Итоговый скор
        total_score = sum(c.score for c in components)
        max_possible = sum(c.max_score for c in components)
        
        # Бонус за конfluence (3+ сильных компонентов)
        strong_components = sum(1 for c in components if c.score >= c.max_score * 0.6)
        if strong_components >= 4:
            total_score += 5
        elif strong_components >= 3:
            total_score += 3
        
        # Cap at 100
        total_score = min(total_score, 100)
        
        # Проверка валидности
        is_valid = total_score >= self.min_score
        
        # Уверенность и оценка
        confidence = self.determine_confidence(total_score)
        grade = self.calculate_grade(total_score)
        
        # Причины (для отображения)
        reasons = []
        if rsi_comp.score >= 15:
            reasons.append(f"RSI перекуплен ({rsi_1h:.1f})")
        if funding_comp.score >= 8:
            reasons.append("Высокий фандинг (лонги платят)")
        if ratio_comp.score >= 10:
            reasons.append(f"Толпа в лонгах ({long_ratio:.0f}%)")
        if oi_comp.score >= 10:
            reasons.append("OI растёт с ценой (перегруз)")
        if delta_comp.score >= 10:
            reasons.append("Медвежья дивергенция")
        if pattern_comp.score >= 20:
            reasons.append(f"Сильный паттерн: {pattern_names[0] if pattern_names else 'N/A'}")
        
        return ScoreResult(
            total_score=total_score,
            max_possible=max_possible,
            direction=Direction.SHORT,
            is_valid=is_valid,
            confidence=confidence,
            grade=grade,
            components=components,
            reasons=reasons
        )


class LongScorer(BaseScorer):
    """
    Скорер для LONG позиций
    Оценивает перепроданность монеты
    """
    
    def __init__(self, min_score: int = 65):
        super().__init__(min_score, Direction.LONG)
    
    def calculate_rsi_component(self, rsi_1h: float) -> ScoreComponent:
        """
        RSI компонент (0-20 очков)
        Ниже RSI = лучше для лонга
        """
        if rsi_1h <= 20:
            score = 20
            desc = f"RSI {rsi_1h:.1f} - Экстремальная перепроданность"
        elif rsi_1h <= 25:
            score = 18
            desc = f"RSI {rsi_1h:.1f} - Очень перепродан"
        elif rsi_1h <= 30:
            score = 15
            desc = f"RSI {rsi_1h:.1f} - Перепроданность"
        elif rsi_1h <= 35:
            score = 12
            desc = f"RSI {rsi_1h:.1f} - Сильная перепроданность"
        elif rsi_1h <= 40:
            score = 8
            desc = f"RSI {rsi_1h:.1f} - Начало перепроданности"
        elif rsi_1h <= 45:
            score = 4
            desc = f"RSI {rsi_1h:.1f} - Близко к перепроданности"
        elif rsi_1h >= 70:
            score = 0
            desc = f"RSI {rsi_1h:.1f} - Перекупленность (плохо для лонга)"
        else:
            score = 2
            desc = f"RSI {rsi_1h:.1f} - Нейтральная зона"
        
        return ScoreComponent(
            name="RSI",
            score=score,
            max_score=20,
            description=desc,
            raw_value=rsi_1h
        )
    
    def calculate_funding_component(self, current_funding: float,
                                   accumulated_4d: float) -> ScoreComponent:
        """
        Фандинг компонент (0-15 очков)
        Отрицательный = шорты платят лонгам = хорошо для лонга
        """
        score = 0
        reasons = []
        
        # Текущий фандинг
        if current_funding <= -0.1:
            score += 8
            reasons.append(f"Отрицательный фандинг {current_funding:.3f}% (шорты платят)")
        elif current_funding <= -0.05:
            score += 5
            reasons.append(f"Фандинг {current_funding:.3f}% (шорты платят)")
        elif current_funding < 0:
            score += 3
            reasons.append(f"Небольшой отрицательный фандинг {current_funding:.3f}%")
        elif current_funding >= 0.05:
            score += 0
            reasons.append(f"Положительный фандинг {current_funding:.3f}% (плохо)")
        else:
            score += 1
        
        # Накопленный фандинг
        if accumulated_4d <= -0.5:
            score += 7
            reasons.append(f"Накопленный отрицательный {accumulated_4d:.2f}%")
        elif accumulated_4d <= -0.3:
            score += 5
        elif accumulated_4d <= -0.1:
            score += 3
        elif accumulated_4d > 0.3:
            score += 0
            reasons.append(f"Положительный накопленный {accumulated_4d:.2f}% (плохо)")
        else:
            score += 1
        
        return ScoreComponent(
            name="Funding",
            score=min(score, 15),
            max_score=15,
            description=" | ".join(reasons) if reasons else "Нейтральный фандинг",
            raw_value=current_funding
        )
    
    def calculate_ratio_component(self, long_ratio: float) -> ScoreComponent:
        """
        L/S Ratio компонент (0-15 очков)
        Меньше лонгов = больше шортов = хорошо для лонга
        """
        short_ratio = 100 - long_ratio
        
        if long_ratio <= 25:
            score = 15
            desc = f"{long_ratio:.0f}% лонгов ({short_ratio:.0f}% шортов, толпа ошибается)"
        elif long_ratio <= 30:
            score = 12
            desc = f"{long_ratio:.0f}% лонгов (много шортистов)"
        elif long_ratio <= 35:
            score = 10
            desc = f"{long_ratio:.0f}% лонгов (шортисты доминируют)"
        elif long_ratio <= 40:
            score = 7
            desc = f"{long_ratio:.0f}% лонгов (перевес шортов)"
        elif long_ratio <= 45:
            score = 4
            desc = f"{long_ratio:.0f}% лонгов (больше шортов)"
        elif long_ratio <= 50:
            score = 2
            desc = f"{long_ratio:.0f}% лонгов (баланс)"
        elif long_ratio >= 65:
            score = 0
            desc = f"{long_ratio:.0f}% лонгов (толпа в лонгах, плохо)"
        else:
            score = 1
            desc = f"{long_ratio:.0f}% лонгов (лёгкий лонг перевес)"
        
        return ScoreComponent(
            name="L/S Ratio",
            score=score,
            max_score=15,
            description=desc,
            raw_value=long_ratio
        )
    
    def calculate_oi_component(self, oi_change_4d: float,
                              price_change_4d: float) -> ScoreComponent:
        """
        Open Interest компонент (0-15 очков)
        OI растёт при падении = шорты накапливаются = хорошо для лонга
        """
        if oi_change_4d >= 10 and price_change_4d <= -5:
            score = 15
            desc = f"OI +{oi_change_4d:.1f}% при падении {price_change_4d:.1f}% (шорты перегружены)"
        elif oi_change_4d >= 5 and price_change_4d <= -3:
            score = 12
            desc = f"OI +{oi_change_4d:.1f}% при падении {price_change_4d:.1f}%"
        elif oi_change_4d >= 0 and price_change_4d <= 0:
            score = 8
            desc = f"OI +{oi_change_4d:.1f}% с падением цены"
        elif oi_change_4d < -10 and price_change_4d <= -5:
            score = 6
            desc = f"OI {oi_change_4d:.1f}% при падении (шорты закрываются, возможен разворот)"
        elif oi_change_4d < -5 and price_change_4d >= 5:
            score = 10
            desc = f"OI {oi_change_4d:.1f}% при росте (шорт squeeze)"
        elif oi_change_4d < 0 and price_change_4d < 0:
            score = 3
            desc = f"OI {oi_change_4d:.1f}% при падении"
        else:
            score = 0
            desc = f"OI {oi_change_4d:.1f}% без ясной картины"
        
        return ScoreComponent(
            name="Open Interest",
            score=score,
            max_score=15,
            description=desc,
            raw_value=oi_change_4d
        )
    
    def calculate_delta_component(self, hourly_deltas: List[float],
                                 price_trend: str) -> ScoreComponent:
        """
        Дельта компонент (0-20 очков)
        Положительная дельта на падении = скрытые покупки = хорошо для лонга
        """
        score = 0
        reasons = []
        
        total_hours = len(hourly_deltas)
        positive_hours = sum(1 for d in hourly_deltas if d > 0)
        
        # Пункты за положительные часы (макс 8)
        if positive_hours >= 5 and total_hours >= 6:
            score += 8
            reasons.append(f"{positive_hours}/{total_hours} часов положительной дельты")
        elif positive_hours >= 4:
            score += 6
            reasons.append(f"{positive_hours} часов положительной дельты")
        elif positive_hours >= 3:
            score += 4
        elif positive_hours >= 2:
            score += 2
        
        # Бычья дивергенция (макс 12)
        if price_trend == "falling" and positive_hours >= 3:
            score += 12
            reasons.append("Бычья дивергенция (цена падает, дельта растёт)")
        elif price_trend == "falling" and positive_hours >= 2:
            score += 8
            reasons.append("Слабая бычья дивергенция")
        elif price_trend == "sideways" and positive_hours >= 4:
            score += 6
            reasons.append("Накопление в боковике")
        
        return ScoreComponent(
            name="Delta",
            score=min(score, 20),
            max_score=20,
            description=" | ".join(reasons) if reasons else "Нейтральная дельта",
            raw_value=sum(hourly_deltas) if hourly_deltas else 0
        )
    
    def calculate_pattern_component(self, patterns: List[Pattern]) -> Tuple[ScoreComponent, List[str]]:
        """Такой же как в ShortScorer"""
        if not patterns:
            return ScoreComponent(
                name="Patterns",
                score=0,
                max_score=30,
                description="Нет паттернов"
            ), []
        
        best_pattern = max(patterns, key=lambda p: p.strength)
        base_score = best_pattern.strength
        pattern_names = [p.name for p in patterns]
        
        bonus = 0
        if len(patterns) >= 2:
            bonus += 3
        if len(patterns) >= 3:
            bonus += 5
        
        freshness_bonus = 0
        if best_pattern.candles_ago == 0:
            freshness_bonus = 2
        elif best_pattern.candles_ago == 1:
            freshness_bonus = 1
        
        total_score = min(base_score + bonus + freshness_bonus, 30)
        
        desc = f"{best_pattern.name} (сила: {best_pattern.strength})"
        if len(patterns) > 1:
            desc += f" + {len(patterns)-1} паттернов (+{bonus})"
        if freshness_bonus > 0:
            desc += f", свежий (+{freshness_bonus})"
        
        return ScoreComponent(
            name="Patterns",
            score=total_score,
            max_score=30,
            description=desc
        ), pattern_names
    
    def calculate_score(self,
                       rsi_1h: float,
                       funding_current: float,
                       funding_accumulated: float,
                       long_ratio: float,
                       oi_change_4d: float,
                       price_change_4d: float,
                       hourly_deltas: List[float],
                       price_trend: str,
                       patterns: List[Pattern]) -> ScoreResult:
        """Расчёт полного Long Score (зеркальный Short)"""
        
        components = []
        
        rsi_comp = self.calculate_rsi_component(rsi_1h)
        components.append(rsi_comp)
        
        funding_comp = self.calculate_funding_component(funding_current, funding_accumulated)
        components.append(funding_comp)
        
        ratio_comp = self.calculate_ratio_component(long_ratio)
        components.append(ratio_comp)
        
        oi_comp = self.calculate_oi_component(oi_change_4d, price_change_4d)
        components.append(oi_comp)
        
        delta_comp = self.calculate_delta_component(hourly_deltas, price_trend)
        components.append(delta_comp)
        
        pattern_comp, pattern_names = self.calculate_pattern_component(patterns)
        components.append(pattern_comp)
        
        total_score = sum(c.score for c in components)
        max_possible = sum(c.max_score for c in components)
        
        # Бонус за confluence
        strong_components = sum(1 for c in components if c.score >= c.max_score * 0.6)
        if strong_components >= 4:
            total_score += 5
        elif strong_components >= 3:
            total_score += 3
        
        total_score = min(total_score, 100)
        
        is_valid = total_score >= self.min_score
        confidence = self.determine_confidence(total_score)
        grade = self.calculate_grade(total_score)
        
        # Причины
        reasons = []
        if rsi_comp.score >= 15:
            reasons.append(f"RSI перепродан ({rsi_1h:.1f})")
        if funding_comp.score >= 8:
            reasons.append("Шорты платят фандинг")
        if ratio_comp.score >= 10:
            reasons.append(f"Толпа в шортах ({100-long_ratio:.0f}%)")
        if oi_comp.score >= 10:
            reasons.append("Шорты перегружены (OI растёт)")
        if delta_comp.score >= 10:
            reasons.append("Бычья дивергенция")
        if pattern_comp.score >= 20:
            reasons.append(f"Сильный паттерн: {pattern_names[0] if pattern_names else 'N/A'}")
        
        return ScoreResult(
            total_score=total_score,
            max_possible=max_possible,
            direction=Direction.LONG,
            is_valid=is_valid,
            confidence=confidence,
            grade=grade,
            components=components,
            reasons=reasons
        )


# ============================================================================
# SINGLETON
# ============================================================================

_short_scorer = None
_long_scorer = None

def get_short_scorer(min_score: int = 65) -> ShortScorer:
    """Получить Short Scorer singleton"""
    global _short_scorer
    if _short_scorer is None:
        _short_scorer = ShortScorer(min_score)
    return _short_scorer

def get_long_scorer(min_score: int = 65) -> LongScorer:
    """Получить Long Scorer singleton"""
    global _long_scorer
    if _long_scorer is None:
        _long_scorer = LongScorer(min_score)
    return _long_scorer


# ============================================================================
# EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Тест Short Scorer
    print("=" * 60)
    print("SHORT SCORER TEST")
    print("=" * 60)
    
    short_scorer = ShortScorer(min_score=65)
    
    # Пример данных для SHORT (BTC на вершине)
    result = short_scorer.calculate_score(
        rsi_1h=78.5,
        funding_current=0.08,
        funding_accumulated=0.42,
        long_ratio=72,
        oi_change_4d=18.5,
        price_change_4d=12.3,
        hourly_deltas=[-2.1, -1.8, 0.5, -3.2, -2.5, -1.9, 0.3],  # Много отрицательной
        price_trend="rising",
        patterns=[Pattern(
            name="MEGA_SHORT",
            direction=Direction.SHORT,
            strength=25,
            candles_ago=0,
            freshness=0,
            volume_multiplier=2.5,
            delta_at_trigger=-5.2,
            entry_price=73500,
            stop_loss=74200,
            confidence="strong",
            description="Mega short pattern"
        )]
    )
    
    print(f"Score: {result.total_score}/100 ({result.percentage}%)")
    print(f"Grade: {result.grade}")
    print(f"Valid: {result.is_valid}")
    print(f"Confidence: {result.confidence.value}")
    print("Components:")
    for comp in result.components:
        print(f"  {comp.name}: {comp.score}/{comp.max_score} - {comp.description}")
    print(f"Reasons: {result.reasons}")
    
    # Тест Long Scorer
    print("\n" + "=" * 60)
    print("LONG SCORER TEST")
    print("=" * 60)
    
    long_scorer = LongScorer(min_score=65)
    
    # Пример данных для LONG (ETH после дампа)
    result = long_scorer.calculate_score(
        rsi_1h=24.3,
        funding_current=-0.05,
        funding_accumulated=-0.35,
        long_ratio=28,
        oi_change_4d=15.2,
        price_change_4d=-8.5,
        hourly_deltas=[1.2, 2.1, 0.8, 3.5, 2.9, 1.8, 2.2],  # Много положительной
        price_trend="falling",
        patterns=[Pattern(
            name="ACCUMULATION",
            direction=Direction.LONG,
            strength=30,
            candles_ago=0,
            freshness=0,
            volume_multiplier=3.2,
            delta_at_trigger=8.5,
            entry_price=3250,
            stop_loss=3180,
            confidence="very_strong",
            description="Whale accumulation"
        )]
    )
    
    print(f"Score: {result.total_score}/100 ({result.percentage}%)")
    print(f"Grade: {result.grade}")
    print(f"Valid: {result.is_valid}")
    print(f"Confidence: {result.confidence.value}")
    print("Components:")
    for comp in result.components:
        print(f"  {comp.name}: {comp.score}/{comp.max_score} - {comp.description}")
    print(f"Reasons: {result.reasons}")
