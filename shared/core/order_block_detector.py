"""
Order Block Detector v2.8 - Детекция институциональных зон

Определяет Order Blocks (OB) — зоны накопления лимитных ордеров маркетмейкеров.
Основан на импульсных свечах перед Break of Structure (BOS).
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Literal
from enum import Enum
import statistics
from datetime import datetime


class OBType(Enum):
    """Тип Order Block"""
    BULLISH = "bullish"    # Для лонгов (медвежья свеча перед ростом)
    BEARISH = "bearish"    # Для шортов (бычья свеча перед падением)


class OBFreshness(Enum):
    """Свежесть Order Block"""
    FRESH = "fresh"        # < 5 свечей назад
    MEDIUM = "medium"      # 5-10 свечей
    OLD = "old"            # > 10 свечей


@dataclass
class OrderBlock:
    """
    Структура Order Block
    
    Attributes:
        ob_type: Тип OB (бычий/медвежий)
        price_top: Верхняя граница OB (для входа)
        price_bottom: Нижняя граница OB
        price_optimal: Оптимальная цена входа (обычно 50% зоны)
        quality: Качество OB 0-100 (объем + импульс)
        freshness: Насколько "свеж" OB
        volume_ratio: Отношение объема к среднему
        impulse_strength: Сила импульса (0-100)
        timestamp: Время формирования
        candle_index: Индекс свечи в массиве (для отслеживания)
        sweep_data: Данные о sweep ликвидности (если был)
    """
    ob_type: OBType
    price_top: float
    price_bottom: float
    price_optimal: float
    quality: int  # 0-100
    freshness: OBFreshness
    volume_ratio: float
    impulse_strength: float
    timestamp: Optional[datetime] = None
    candle_index: int = 0
    sweep_data: Optional[Dict] = None
    
    def get_entry_zone(self, use_optimal: bool = True) -> float:
        """Получить цену входа"""
        if use_optimal:
            return self.price_optimal
        # Верхняя граница для лонга, нижняя для шорта
        if self.ob_type == OBType.BULLISH:
            return self.price_top
        else:
            return self.price_bottom
    
    def get_stop_loss_price(self, atr: float, buffer_pct: float = 0.2) -> float:
        """
        Расчёт стоп-лосса за пределами OB + буфер
        
        Для BULLISH: стоп ниже price_bottom
        Для BEARISH: стоп выше price_top
        """
        buffer = atr * buffer_pct
        
        if self.ob_type == OBType.BULLISH:
            return self.price_bottom - buffer
        else:
            return self.price_top + buffer
    
    def is_price_in_zone(self, price: float) -> bool:
        """Проверяет, находится ли цена внутри зоны OB"""
        return self.price_bottom <= price <= self.price_top
    
    def distance_from_price(self, current_price: float) -> float:
        """Расстояние от текущей цены до OB в %"""
        if self.ob_type == OBType.BULLISH:
            # Для лонга — расстояние до price_top (вход)
            return abs(self.price_top - current_price) / current_price
        else:
            # Для шорта — расстояние до price_bottom (вход)
            return abs(current_price - self.price_bottom) / current_price


@dataclass
class OBDetectionResult:
    """Результат детекции OB для символа"""
    symbol: str
    bullish_ob: Optional[OrderBlock] = None
    bearish_ob: Optional[OrderBlock] = None
    recent_sweep: Optional[Dict] = None
    fvg_zones: List[Tuple[float, float]] = field(default_factory=list)
    
    def has_valid_ob(self, direction: Literal["long", "short"], 
                     min_quality: int = 60) -> bool:
        """Проверяет наличие валидного OB для направления"""
        if direction == "long" and self.bullish_ob:
            return self.bullish_ob.quality >= min_quality
        elif direction == "short" and self.bearish_ob:
            return self.bearish_ob.quality >= min_quality
        return False
    
    def get_best_ob(self, direction: Literal["long", "short"]) -> Optional[OrderBlock]:
        """Получить лучший OB для направления"""
        if direction == "long":
            return self.bullish_ob
        return self.bearish_ob


class OrderBlockDetector:
    """
    Детектор Order Blocks
    
    Алгоритм:
    1. Находит Break of Structure (BOS)
    2. Определяет импульсную свечу перед BOS
    3. Маркирует её как Order Block
    4. Оценивает качество (объем, сила импульса)
    """
    
    def __init__(self, 
                 min_volume_ratio: float = 1.5,      # Мин отношение объема
                 min_impulse_body_pct: float = 60.0,  # Мин % тела свечи
                 max_ob_age_candles: int = 20,        # Макс "возраст" OB
                 use_sweep_confirmation: bool = True): # Требовать sweep?
        self.min_volume_ratio = min_volume_ratio
        self.min_impulse_body_pct = min_impulse_body_pct
        self.max_ob_age = max_ob_age_candles
        self.max_ob = max_ob_age_candles  # Alias для совместимости
        self.use_sweep_confirmation = use_sweep_confirmation
    
    def detect(self, 
               ohlcv: List, 
               direction: Literal["long", "short", "both"] = "both",
               current_price: Optional[float] = None) -> OBDetectionResult:
        """
        Основной метод детекции
        
        Args:
            ohlcv: Список свечей (должны иметь open, high, low, close, volume)
            direction: Какой тип OB искать (long/bullish, short/bearish, both)
            current_price: Текущая цена (для проверки расстояния)
        """
        if not ohlcv or len(ohlcv) < 10:
            return OBDetectionResult(symbol="unknown")
        
        symbol = getattr(ohlcv[0], 'symbol', 'unknown')
        result = OBDetectionResult(symbol=symbol)
        
        # Расчёт среднего объёма
        avg_volume = self._calculate_avg_volume(ohlcv)
        
        # Детекция BOS и OB
        if direction in ["long", "both"]:
            result.bullish_ob = self._find_bullish_ob(ohlcv, avg_volume, current_price)
        
        if direction in ["short", "both"]:
            result.bearish_ob = self._find_bearish_ob(ohlcv, avg_volume, current_price)
        
        # Поиск FVG зон (для ретеста)
        result.fvg_zones = self._find_fvg_zones(ohlcv)
        
        # Проверка sweep (если включено)
        if self.use_sweep_confirmation:
            result.recent_sweep = self._detect_recent_sweep(ohlcv)
        
        return result
    
    def _find_bullish_ob(self, 
                         ohlcv: List, 
                         avg_volume: float,
                         current_price: Optional[float]) -> Optional[OrderBlock]:
        """
        Поиск бычьего OB (для лонгов)
        
        Логика: Находим Bearish Engulfing перед ростом (BOS вверх)
        """
        best_ob = None
        best_quality = 0
        
        # Идём с конца, ищем свежие OB
        for i in range(len(ohlcv) - 5, max(3, len(ohlcv) - self.max_ob - 1), -1):
            if i < 0:
                continue
                
            # Текущая свеча
            candle = ohlcv[i]
            prev = ohlcv[i-1]
            next_c = ohlcv[i+1] if i+1 < len(ohlcv) else None
            
            # Проверяем: была ли медвежья свеча с большим объёмом
            if not self._is_bearish_impulse(candle, prev, avg_volume):
                continue
            
            # Проверяем: был ли после этого рост (BOS)
            if not next_c or not self._is_bullish_bos(candle, next_c, ohlcv[i+2:i+5]):
                continue
            
            # Оцениваем качество
            quality = self._calculate_ob_quality(candle, prev, avg_volume, "bullish")
            
            # Определяем свежесть
            candles_since = len(ohlcv) - i - 1
            freshness = self._determine_freshness(candles_since)
            
            # Проверяем расстояние от текущей цены
            if current_price and freshness in [OBFreshness.FRESH, OBFreshness.MEDIUM]:
                # OB должен быть ниже текущей цены (для ретеста)
                if candle.high >= current_price * 0.995:  # Слишком близко/выше
                    continue
            
            if quality > best_quality:
                best_quality = quality
                
                # Расчёт зоны OB
                price_top = candle.open  # Открытие медвежьей свечи
                price_bottom = candle.low  # Лой (сильная поддержка)
                price_optimal = (price_top + price_bottom) / 2  # 50% зоны
                
                best_ob = OrderBlock(
                    ob_type=OBType.BULLISH,
                    price_top=price_top,
                    price_bottom=price_bottom,
                    price_optimal=price_optimal,
                    quality=quality,
                    freshness=freshness,
                    volume_ratio=candle.volume / avg_volume if avg_volume > 0 else 1.0,
                    impulse_strength=self._calculate_impulse_strength(candle, "bearish"),
                    timestamp=getattr(candle, 'timestamp', None),
                    candle_index=i
                )
        
        return best_ob
    
    def _find_bearish_ob(self, 
                         ohlcv: List, 
                         avg_volume: float,
                         current_price: Optional[float]) -> Optional[OrderBlock]:
        """
        Поиск медвежьего OB (для шортов)
        
        Логика: Находим Bullish Engulfing перед падением (BOS вниз)
        """
        best_ob = None
        best_quality = 0
        
        for i in range(len(ohlcv) - 5, max(3, len(ohlcv) - self.max_ob - 1), -1):
            if i < 0:
                continue
                
            candle = ohlcv[i]
            prev = ohlcv[i-1]
            next_c = ohlcv[i+1] if i+1 < len(ohlcv) else None
            
            # Проверяем: была ли бычья свеча с большим объёмом
            if not self._is_bullish_impulse(candle, prev, avg_volume):
                continue
            
            # Проверяем: было ли после этого падение (BOS)
            if not next_c or not self._is_bearish_bos(candle, next_c, ohlcv[i+2:i+5]):
                continue
            
            quality = self._calculate_ob_quality(candle, prev, avg_volume, "bearish")
            
            candles_since = len(ohlcv) - i - 1
            freshness = self._determine_freshness(candles_since)
            
            # OB должен быть выше текущей цены (для ретеста)
            if current_price and freshness in [OBFreshness.FRESH, OBFreshness.MEDIUM]:
                if candle.low <= current_price * 1.005:  # Слишком близко/ниже
                    continue
            
            if quality > best_quality:
                best_quality = quality
                
                price_bottom = candle.open
                price_top = candle.high
                price_optimal = (price_top + price_bottom) / 2
                
                best_ob = OrderBlock(
                    ob_type=OBType.BEARISH,
                    price_top=price_top,
                    price_bottom=price_bottom,
                    price_optimal=price_optimal,
                    quality=quality,
                    freshness=freshness,
                    volume_ratio=candle.volume / avg_volume if avg_volume > 0 else 1.0,
                    impulse_strength=self._calculate_impulse_strength(candle, "bullish"),
                    timestamp=getattr(candle, 'timestamp', None),
                    candle_index=i
                )
        
        return best_ob
    
    def _is_bearish_impulse(self, candle, prev_candle, avg_volume) -> bool:
        """Проверка на медвежью импульсную свечу"""
        # Медвежья свеча
        if candle.close >= candle.open:
            return False
        
        # Тело больше min_impulse_body_pct от диапазона
        body = abs(candle.close - candle.open)
        range_c = candle.high - candle.low
        if range_c == 0:
            return False
        
        body_pct = (body / range_c) * 100
        if body_pct < self.min_impulse_body_pct:
            return False
        
        # Объём выше среднего
        if candle.volume < avg_volume * self.min_volume_ratio:
            return False
        
        return True
    
    def _is_bullish_impulse(self, candle, prev_candle, avg_volume) -> bool:
        """Проверка на бычью импульсную свечу"""
        if candle.close <= candle.open:
            return False
        
        body = candle.close - candle.open
        range_c = candle.high - candle.low
        if range_c == 0:
            return False
        
        body_pct = (body / range_c) * 100
        if body_pct < self.min_impulse_body_pct:
            return False
        
        if candle.volume < avg_volume * self.min_volume_ratio:
            return False
        
        return True
    
    def _is_bullish_bos(self, ob_candle, next_candle, following) -> bool:
        """Проверка бычьего BOS (пробой структуры вверх)"""
        # Цена вышла выше хая OB свечи
        if next_candle.close > ob_candle.high:
            return True
        
        # Или последующие свечи показывают рост
        if following and len(following) >= 2:
            avg_close = sum(c.close for c in following[:3]) / 3
            if avg_close > ob_candle.close * 1.01:  # +1%
                return True
        
        return False
    
    def _is_bearish_bos(self, ob_candle, next_candle, following) -> bool:
        """Проверка медвежьего BOS (пробой структуры вниз)"""
        if next_candle.close < ob_candle.low:
            return True
        
        if following and len(following) >= 2:
            avg_close = sum(c.close for c in following[:3]) / 3
            if avg_close < ob_candle.close * 0.99:  # -1%
                return True
        
        return False
    
    def _calculate_ob_quality(self, candle, prev, avg_volume, ob_type) -> int:
        """Оценка качества OB 0-100"""
        quality = 50  # Базовое
        
        # За объём
        vol_ratio = candle.volume / avg_volume if avg_volume > 0 else 1.0
        quality += min(30, int((vol_ratio - 1.0) * 20))
        
        # За силу импульса
        body = abs(candle.close - candle.open)
        range_c = candle.high - candle.low
        body_pct = (body / range_c) * 100 if range_c > 0 else 50
        quality += min(20, int((body_pct - 60) / 2))
        
        return min(100, max(0, quality))
    
    def _calculate_impulse_strength(self, candle, direction) -> float:
        """Расчёт силы импульса 0-100"""
        body = abs(candle.close - candle.open)
        range_c = candle.high - candle.low
        if range_c == 0:
            return 50.0
        
        body_pct = (body / range_c) * 100
        return min(100.0, body_pct)
    
    def _determine_freshness(self, candles_since: int) -> OBFreshness:
        """Определение свежести OB"""
        if candles_since < 5:
            return OBFreshness.FRESH
        elif candles_since < 10:
            return OBFreshness.MEDIUM
        else:
            return OBFreshness.OLD
    
    def _calculate_avg_volume(self, ohlcv: List) -> float:
        """Расчёт среднего объёма"""
        volumes = []
        for c in ohlcv:
            vol = getattr(c, 'volume', 0)
            if vol:
                volumes.append(vol)
        
        if len(volumes) < 5:
            return 1.0  # Default
        
        return statistics.mean(volumes[-20:])  # Последние 20 свечей
    
    def _find_fvg_zones(self, ohlcv: List) -> List[Tuple[float, float]]:
        """Поиск FVG (Fair Value Gap) зон"""
        fvg_zones = []
        
        for i in range(2, len(ohlcv)):
            # FVG bullish: low текущей > high двух свечей назад
            if ohlcv[i].low > ohlcv[i-2].high:
                fvg_zones.append((ohlcv[i-2].high, ohlcv[i].low))
            
            # FVG bearish: high текущей < low двух свечей назад
            elif ohlcv[i].high < ohlcv[i-2].low:
                fvg_zones.append((ohlcv[i].high, ohlcv[i-2].low))
        
        return fvg_zones[-5:]  # Последние 5 зон
    
    def _detect_recent_sweep(self, ohlcv: List) -> Optional[Dict]:
        """
        Детекция недавнего sweep ликвидности
        
        Возвращает: {"direction": "up"/"down", "level": price, "reversed": bool}
        """
        if len(ohlcv) < 5:
            return None
        
        recent = ohlcv[-5:]
        
        # Поиск sweep вверх (выбили хаи) и возврата
        # Упрощённая версия: резкий выход за диапазон и возврат
        for i in range(1, len(recent) - 1):
            prev_high = recent[i-1].high
            prev_low = recent[i-1].low
            
            # Sweep вверх
            if recent[i].high > prev_high * 1.005 and recent[i+1].close < prev_high:
                return {
                    "direction": "up",
                    "level": recent[i].high,
                    "reversed": True,
                    "sweep_candle": i
                }
            
            # Sweep вниз
            if recent[i].low < prev_low * 0.995 and recent[i+1].close > prev_low:
                return {
                    "direction": "down",
                    "level": recent[i].low,
                    "reversed": True,
                    "sweep_candle": i
                }
        
        return None


# Удобные функции для использования
_detector_instance: Optional[OrderBlockDetector] = None


def get_ob_detector(
    min_volume_ratio: float = 1.5,
    min_impulse_body_pct: float = 60.0
) -> OrderBlockDetector:
    """Получить глобальный экземпляр детектора"""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = OrderBlockDetector(
            min_volume_ratio=min_volume_ratio,
            min_impulse_body_pct=min_impulse_body_pct
        )
    return _detector_instance


def detect_order_blocks(
    ohlcv: List,
    direction: Literal["long", "short", "both"] = "both",
    current_price: Optional[float] = None,
    min_quality: int = 60
) -> OBDetectionResult:
    """Удобная функция для быстрой детекции"""
    detector = get_ob_detector()
    result = detector.detect(ohlcv, direction, current_price)
    return result


def format_ob_for_signal(ob: OrderBlock) -> Dict:
    """Форматирование OB для интеграции в сигнал"""
    return {
        "entry_type": "LIMIT",
        "limit_price": ob.price_optimal,
        "ob_zone_top": ob.price_top,
        "ob_zone_bottom": ob.price_bottom,
        "ob_quality": ob.quality,
        "ob_freshness": ob.freshness.value,
        "sl_suggested": ob.price_bottom if ob.ob_type == OBType.BULLISH else ob.price_top,
        "volume_ratio": round(ob.volume_ratio, 2),
    }
