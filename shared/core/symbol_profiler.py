"""
Symbol Profiler v2.8 - Индивидуальный анализ каждой монеты

Анализирует исторические данные для определения:
- Оптимального таймфрейма для анализа
- Волатильности (ATR %)
- Паттернов фандинга
- Времени жизни лимитных ордеров (TTL)
"""

from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
import statistics
from datetime import datetime, timedelta


@dataclass
class SymbolProfile:
    """Индивидуальный профиль торгового инструмента"""
    symbol: str
    
    # Волатильность
    atr_14_pct: float = 0.0          # ATR как % от цены
    daily_range_avg: float = 0.0      # Средний дневной диапазон %
    volatility_class: str = "medium"  # low / medium / high / extreme
    
    # Оптимальный таймфрейм
    ideal_tf: str = "15m"            # 5m / 15m / 1h / 4h
    
    # Фандинг
    funding_bias: float = 0.0          # Средний фандинг (положительный = шорты платят)
    funding_volatility: float = 0.0   # Волатильность фандинга
    
    # OI паттерны
    oi_trend: str = "neutral"         # rising / falling / neutral
    oi_price_corr: float = 0.0        # Корреляция OI с ценой
    
    # Ликвидность
    avg_volume_24h: float = 0.0       # Средний объём 24ч
    spread_avg: float = 0.0           # Средний спред
    
    # Параметры лимиток (динамические)
    limit_ttl_base: int = 900         # Базовое время жизни (сек)
    limit_slippage_max: float = 0.003  # Макс проскальзывание (0.3%)
    
    # Метаданные
    last_updated: Optional[datetime] = None
    data_points: int = 0
    
    def calculate_limit_ttl(self, ob_freshness: str = "fresh") -> int:
        """
        Расчёт времени жизни лимитки на основе профиля
        
        Args:
            ob_freshness: "fresh" (< 5 свечей), "medium" (5-10), "old" (> 10)
        """
        base = self.limit_ttl_base
        
        # Корректировка по волатильности
        if self.volatility_class == "extreme":
            # Для высокой волатильности — меньше времени (цена уйдёт быстро)
            base = int(base * 0.4)
        elif self.volatility_class == "high":
            base = int(base * 0.7)
        elif self.volatility_class == "low":
            # Для низкой волатильности — больше времени
            base = int(base * 1.3)
        
        # Корректировка по "свежести" OB
        freshness_mult = {
            "fresh": 1.0,    # Свежий OB — стандартное время
            "medium": 0.7,   # Средний — меньше
            "old": 0.4       # Старый — быстро отменяем если не взялось
        }
        base = int(base * freshness_mult.get(ob_freshness, 1.0))
        
        # Минимумы и максимумы
        min_ttl = {
            "5m": 180,      # 3 минуты
            "15m": 600,     # 10 минут
            "1h": 1800,     # 30 минут
            "4h": 7200      # 2 часа
        }
        max_ttl = {
            "5m": 600,      # 10 минут
            "15m": 1800,    # 30 минут
            "1h": 7200,     # 2 часа
            "4h": 18000     # 5 часов
        }
        
        return max(min_ttl.get(self.ideal_tf, 600), 
                   min(base, max_ttl.get(self.ideal_tf, 1800)))
    
    def should_use_limit_entry(self) -> bool:
        """Определяет, стоит ли использовать лимитные входы для этого символа"""
        # Для низкой ликвидности — лучше рынок (слишком долго ждать)
        if self.avg_volume_24h < 1_000_000:  # < $1M объёма
            return False
        # Для экстремальной волатильности — рынок (OB быстро сломается)
        if self.volatility_class == "extreme" and self.atr_14_pct > 5.0:
            return False
        return True
    
    def get_adaptive_sl_buffer(self) -> float:
        """Адаптивный буфер стоп-лосса на основе волатильности"""
        if self.volatility_class == "low":
            return 0.8  # 0.8%
        elif self.volatility_class == "medium":
            return 1.0  # 1.0%
        elif self.volatility_class == "high":
            return 1.5  # 1.5%
        else:  # extreme
            return 2.0  # 2.0%


class SymbolProfiler:
    """
    Профилировщик торговых инструментов
    Анализирует историю и создаёт индивидуальные профили
    """
    
    # TTL по таймфреймам (секунды)
    TTL_BY_TF = {
        "5m": 300,      # 5 минут
        "15m": 900,     # 15 минут
        "1h": 3600,     # 1 час
        "4h": 14400,    # 4 часа
    }
    
    def __init__(self, cache_duration_hours: int = 24):
        self._profiles: Dict[str, SymbolProfile] = {}
        self._cache_duration = timedelta(hours=cache_duration_hours)
    
    async def get_profile(self, symbol: str, binance_client) -> SymbolProfile:
        """Получить профиль символа (из кэша или пересчитать)"""
        cached = self._profiles.get(symbol)
        
        if cached and cached.last_updated:
            age = datetime.utcnow() - cached.last_updated
            if age < self._cache_duration:
                return cached
        
        # Пересчитываем профиль
        profile = await self._calculate_profile(symbol, binance_client)
        self._profiles[symbol] = profile
        return profile
    
    async def _calculate_profile(
        self, 
        symbol: str, 
        binance_client
    ) -> SymbolProfile:
        """Расчёт профиля на основе исторических данных"""
        
        profile = SymbolProfile(symbol=symbol)
        
        try:
            # Загружаем данные разных таймфреймов
            ohlcv_15m = await binance_client.get_klines(symbol, "15m", limit=96)  # 24 часа
            ohlcv_1h = await binance_client.get_klines(symbol, "1h", limit=72)    # 3 дня
            ohlcv_4h = await binance_client.get_klines(symbol, "4h", limit=42)    # 7 дней
            
            # 1. Определяем волатильность
            profile.atr_14_pct = self._calculate_atr_pct(ohlcv_15m, period=14)
            profile.daily_range_avg = self._calculate_avg_daily_range(ohlcv_1h)
            profile.volatility_class = self._classify_volatility(profile.atr_14_pct)
            
            # 2. Определяем оптимальный таймфрейм
            profile.ideal_tf = self._determine_ideal_tf(
                profile.volatility_class,
                profile.atr_14_pct,
                ohlcv_15m,
                ohlcv_1h
            )
            
            # 3. Устанавливаем базовый TTL
            profile.limit_ttl_base = self.TTL_BY_TF.get(profile.ideal_tf, 900)
            
            # 4. Анализ объёмов
            profile.avg_volume_24h = self._calculate_avg_volume(ohlcv_15m)
            
            # 5. Метаданные
            profile.last_updated = datetime.utcnow()
            profile.data_points = len(ohlcv_15m) if ohlcv_15m else 0
            
        except Exception as e:
            print(f"[SymbolProfiler] Error calculating profile for {symbol}: {e}")
            # Fallback на дефолты
            profile.volatility_class = "medium"
            profile.ideal_tf = "15m"
            profile.limit_ttl_base = 900
            profile.last_updated = datetime.utcnow()
        
        return profile
    
    def _calculate_atr_pct(self, ohlcv: List, period: int = 14) -> float:
        """Расчёт ATR как % от средней цены"""
        if not ohlcv or len(ohlcv) < period + 1:
            return 1.0  # Default 1%
        
        tr_values = []
        for i in range(1, len(ohlcv)):
            high = ohlcv[i].high
            low = ohlcv[i].low
            prev_close = ohlcv[i-1].close
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)
        
        if len(tr_values) < period:
            return 1.0
        
        # ATR
        atr = statistics.mean(tr_values[-period:])
        
        # Как % от цены
        avg_price = statistics.mean([c.close for c in ohlcv[-period:]])
        atr_pct = (atr / avg_price) * 100 if avg_price > 0 else 1.0
        
        return round(atr_pct, 2)
    
    def _calculate_avg_daily_range(self, ohlcv_1h: List) -> float:
        """Средний дневной диапазон %"""
        if not ohlcv_1h or len(ohlcv_1h) < 24:
            return 3.0  # Default 3%
        
        # Группируем по дням
        daily_ranges = []
        current_day_high = ohlcv_1h[0].high
        current_day_low = ohlcv_1h[0].low
        current_day_open = ohlcv_1h[0].open
        
        for candle in ohlcv_1h[1:]:
            # Проверяем новый день (упрощённо — по timestamp)
            if hasattr(candle, 'timestamp') and candle.timestamp:
                # Здесь должна быть логика определения нового дня
                pass
            
            current_day_high = max(current_day_high, candle.high)
            current_day_low = min(current_day_low, candle.low)
        
        # Для простоты — считаем range последних 24 свечей (1 день)
        recent = ohlcv_1h[-24:]
        day_high = max(c.high for c in recent)
        day_low = min(c.low for c in recent)
        day_open = recent[0].open
        
        if day_open > 0:
            day_range = ((day_high - day_low) / day_open) * 100
            return round(day_range, 2)
        
        return 3.0
    
    def _classify_volatility(self, atr_pct: float) -> str:
        """Классификация волатильности"""
        if atr_pct < 0.5:
            return "low"
        elif atr_pct < 1.5:
            return "medium"
        elif atr_pct < 3.0:
            return "high"
        else:
            return "extreme"
    
    def _determine_ideal_tf(
        self, 
        vol_class: str, 
        atr_pct: float,
        ohlcv_15m: List,
        ohlcv_1h: List
    ) -> str:
        """
        Определение оптимального таймфрейма
        
        Логика:
        - Низкая волатильность → 15m или 1h (больше данных)
        - Высокая волатильность → 5m или 15m (быстрее реагировать)
        - Очень высокая → 5m (скальп)
        """
        if vol_class == "extreme":
            return "5m"
        elif vol_class == "high":
            # Проверяем, есть ли достаточно движения на 15m
            if ohlcv_15m and len(ohlcv_15m) >= 20:
                recent_volatility = self._calculate_recent_volatility(ohlcv_15m[-20:])
                if recent_volatility > 2.0:
                    return "5m"
            return "15m"
        elif vol_class == "medium":
            # Для средней — 15m оптимально
            return "15m"
        else:  # low
            # Для низкой — можно 1h для стабильности
            if ohlcv_1h and len(ohlcv_1h) >= 20:
                return "1h"
            return "15m"
    
    def _calculate_recent_volatility(self, ohlcv: List) -> float:
        """Волатильность последних N свечей"""
        if not ohlcv or len(ohlcv) < 5:
            return 1.0
        
        ranges = []
        for c in ohlcv:
            if c.open > 0:
                range_pct = ((c.high - c.low) / c.open) * 100
                ranges.append(range_pct)
        
        return statistics.mean(ranges) if ranges else 1.0
    
    def _calculate_avg_volume(self, ohlcv: List) -> float:
        """Средний объём в долларах"""
        if not ohlcv:
            return 0.0
        
        volumes = []
        for c in ohlcv:
            # quote_volume если доступен, иначе примерный расчёт
            vol = getattr(c, 'quote_volume', 0) or (c.volume * c.close if hasattr(c, 'volume') else 0)
            if vol:
                volumes.append(vol)
        
        return statistics.mean(volumes) if volumes else 0.0
    
    def get_all_profiles(self) -> Dict[str, SymbolProfile]:
        """Получить все профили"""
        return self._profiles.copy()
    
    def invalidate_cache(self, symbol: Optional[str] = None):
        """Сбросить кэш (все или один символ)"""
        if symbol:
            self._profiles.pop(symbol, None)
        else:
            self._profiles.clear()


# Глобальный экземпляр
_profiler_instance: Optional[SymbolProfiler] = None


def get_symbol_profiler() -> SymbolProfiler:
    """Получить глобальный экземпляр профилировщика"""
    global _profiler_instance
    if _profiler_instance is None:
        _profiler_instance = SymbolProfiler()
    return _profiler_instance


async def get_profile(symbol: str, binance_client) -> SymbolProfile:
    """Удобная функция для получения профиля"""
    profiler = get_symbol_profiler()
    return await profiler.get_profile(symbol, binance_client)
