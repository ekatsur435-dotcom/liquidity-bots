"""
Candle History Manager v1.0
Менеджер истории свечей для мульти-таймфреймового анализа

Оптимальные настройки истории:
- 4h: 20-40 свечей (3-7 дней) - HTF структура, TP зоны
- 2h: 30-60 свечей (2.5-5 дней) - промежуточный контекст  
- 1h: 50-100 свечей (2-4 дня) - структурный TF
- 30m: 80-150 свечей (40-75 часов) - основной рабочий TF
- 15m: 100-200 свечей (25-50 часов) - входы, Smart SL
- 5m: 200-500 свечей (17-42 часа) - микро-структура
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import numpy as np
from collections import deque
import asyncio


@dataclass
class Candle:
    """Структура свечи"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    taker_buy_volume: float = 0.0
    trades_count: int = 0
    
    @property
    def body(self) -> float:
        """Тело свечи"""
        return abs(self.close - self.open)
    
    @property
    def upper_wick(self) -> float:
        """Верхняя тень"""
        return self.high - max(self.open, self.close)
    
    @property
    def lower_wick(self) -> float:
        """Нижняя тень"""
        return min(self.open, self.close) - self.low
    
    @property
    def total_range(self) -> float:
        """Общий диапазон"""
        return self.high - self.low
    
    @property
    def is_bullish(self) -> bool:
        """Бычья свеча"""
        return self.close > self.open
    
    @property
    def is_bearish(self) -> bool:
        """Медвежья свеча"""
        return self.close < self.open
    
    @property
    def delta(self) -> float:
        """Дельта (taker buy - taker sell)"""
        if self.taker_buy_volume:
            taker_sell = self.volume - self.taker_buy_volume
            return self.taker_buy_volume - taker_sell
        return 0.0
    
    def wick_ratio(self, direction: str = "upper") -> float:
        """Соотношение тени к телу"""
        if self.body == 0:
            return float('inf')
        if direction == "upper":
            return self.upper_wick / self.body
        return self.lower_wick / self.body


@dataclass
class TFConfig:
    """Конфигурация таймфрейма"""
    timeframe: str  # "5m", "15m", "30m", "1h", "2h", "4h"
    candles_count: int  # Сколько свечей хранить
    minutes: int  # Сколько минут в одной свече
    priority: int  # Приоритет (1 - highest)
    
    @property
    def hours_history(self) -> float:
        """Часов истории"""
        return (self.candles_count * self.minutes) / 60
    
    @property
    def days_history(self) -> float:
        """Дней истории"""
        return self.hours_history / 24


@dataclass
class CandleHistory:
    """История свечей для одного символа/ТФ"""
    symbol: str
    timeframe: str
    candles: deque  # deque для эффективного хранения
    last_update: datetime = field(default_factory=datetime.utcnow)
    
    def __post_init__(self):
        if not isinstance(self.candles, deque):
            self.candles = deque(maxlen=1000)
    
    def add_candle(self, candle: Candle):
        """Добавить свечу"""
        self.candles.append(candle)
        self.last_update = datetime.utcnow()
    
    def get_ohlcv(self, n: int = None) -> np.ndarray:
        """Получить OHLCV массив"""
        candles = list(self.candles)[-n:] if n else list(self.candles)
        if not candles:
            return np.array([])
        return np.array([
            [c.timestamp, c.open, c.high, c.low, c.close, c.volume]
            for c in candles
        ])
    
    def get_recent(self, n: int = 20) -> List[Candle]:
        """Получить N последних свечей"""
        return list(self.candles)[-n:]
    
    def get_range(self, start_idx: int, end_idx: int) -> List[Candle]:
        """Получить диапазон свечей"""
        candles = list(self.candles)
        return candles[start_idx:end_idx]
    
    @property
    def count(self) -> int:
        """Количество свечей"""
        return len(self.candles)
    
    def get_highs(self, n: int = None) -> List[float]:
        """Получить highs"""
        candles = list(self.candles)[-n:] if n else list(self.candles)
        return [c.high for c in candles]
    
    def get_lows(self, n: int = None) -> List[float]:
        """Получить lows"""
        candles = list(self.candles)[-n:] if n else list(self.candles)
        return [c.low for c in candles]
    
    def get_closes(self, n: int = None) -> List[float]:
        """Получить closes"""
        candles = list(self.candles)[-n:] if n else list(self.candles)
        return [c.close for c in candles]
    
    def get_volumes(self, n: int = None) -> List[float]:
        """Получить volumes"""
        candles = list(self.candles)[-n:] if n else list(self.candles)
        return [c.volume for c in candles]


class CandleHistoryManager:
    """
    Менеджер истории свечей
    Хранит и управляет историей для всех символов и таймфреймов
    """
    
    # Оптимальные конфигурации по умолчанию
    DEFAULT_TF_CONFIGS = {
        "5m": TFConfig("5m", candles_count=300, minutes=5, priority=6),
        "15m": TFConfig("15m", candles_count=150, minutes=15, priority=5),
        "30m": TFConfig("30m", candles_count=120, minutes=30, priority=4),
        "1h": TFConfig("1h", candles_count=80, minutes=60, priority=3),
        "2h": TFConfig("2h", candles_count=50, minutes=120, priority=2),
        "4h": TFConfig("4h", candles_count=30, minutes=240, priority=1),
    }
    
    def __init__(self, tf_configs: Dict[str, TFConfig] = None):
        """
        Инициализация менеджера
        
        Args:
            tf_configs: Конфигурации таймфреймов (или дефолтные)
        """
        self.tf_configs = tf_configs or self.DEFAULT_TF_CONFIGS.copy()
        
        # Хранилище: symbol -> timeframe -> CandleHistory
        self._history: Dict[str, Dict[str, CandleHistory]] = {}
        
        # Статистика
        self._stats = {
            "updates": 0,
            "symbols": set(),
            "errors": 0
        }
        
        print(f"📊 CandleHistoryManager initialized")
        print(f"   Timeframes: {list(self.tf_configs.keys())}")
        for tf, cfg in self.tf_configs.items():
            print(f"   {tf}: {cfg.candles_count} candles ({cfg.hours_history:.1f}h / {cfg.days_history:.1f}d)")
    
    def configure_tf(self, timeframe: str, candles_count: int):
        """Изменить конфигурацию таймфрейма"""
        if timeframe in self.tf_configs:
            old_cfg = self.tf_configs[timeframe]
            self.tf_configs[timeframe] = TFConfig(
                timeframe=timeframe,
                candles_count=candles_count,
                minutes=old_cfg.minutes,
                priority=old_cfg.priority
            )
            print(f"✅ {timeframe} reconfigured: {candles_count} candles")
    
    def get_or_create_history(self, symbol: str, timeframe: str) -> CandleHistory:
        """Получить или создать историю для символа/ТФ"""
        if symbol not in self._history:
            self._history[symbol] = {}
            self._stats["symbols"].add(symbol)
        
        if timeframe not in self._history[symbol]:
            cfg = self.tf_configs.get(timeframe)
            max_len = cfg.candles_count if cfg else 100
            self._history[symbol][timeframe] = CandleHistory(
                symbol=symbol,
                timeframe=timeframe,
                candles=deque(maxlen=max_len + 50)  # +50 буфер
            )
        
        return self._history[symbol][timeframe]
    
    def update_candles(self, symbol: str, timeframe: str, ohlcv_data: List[List]):
        """
        Обновить свечи из OHLCV данных
        
        Args:
            ohlcv_data: [[timestamp, open, high, low, close, volume], ...]
        """
        try:
            history = self.get_or_create_history(symbol, timeframe)
            
            # Проверка типа данных
            if not isinstance(ohlcv_data, (list, tuple)):
                print(f"⚠️ [CHM] Invalid data type for {symbol} {timeframe}: {type(ohlcv_data)}")
                return
            
            for candle_data in ohlcv_data:
                # Пропускаем если это не список/кортеж (например, CandleData объект)
                if not isinstance(candle_data, (list, tuple)):
                    continue
                if len(candle_data) >= 6:
                    candle = Candle(
                        timestamp=int(candle_data[0]),
                        open=float(candle_data[1]),
                        high=float(candle_data[2]),
                        low=float(candle_data[3]),
                        close=float(candle_data[4]),
                        volume=float(candle_data[5])
                    )
                    history.add_candle(candle)
            
            self._stats["updates"] += 1
            
        except Exception as e:
            self._stats["errors"] += 1
            print(f"❌ Error updating candles for {symbol} {timeframe}: {e}")
    
    def get_candles(self, symbol: str, timeframe: str, n: int = None) -> List[Candle]:
        """Получить свечи для анализа"""
        history = self._history.get(symbol, {}).get(timeframe)
        if not history:
            return []
        return history.get_recent(n) if n else list(history.candles)
    
    def get_ohlcv(self, symbol: str, timeframe: str, n: int = None) -> np.ndarray:
        """Получить OHLCV массив для numpy-анализа"""
        history = self._history.get(symbol, {}).get(timeframe)
        if not history:
            return np.array([])
        return history.get_ohlcv(n)
    
    def has_enough_data(self, symbol: str, timeframe: str, min_candles: int = 20) -> bool:
        """Проверить достаточно ли данных для анализа"""
        history = self._history.get(symbol, {}).get(timeframe)
        if not history:
            return False
        return history.count >= min_candles
    
    def get_data_quality(self, symbol: str, timeframe: str) -> Dict:
        """Получить качество данных"""
        history = self._history.get(symbol, {}).get(timeframe)
        cfg = self.tf_configs.get(timeframe)
        
        if not history or not cfg:
            return {"status": "no_data", "percent": 0}
        
        actual = history.count
        required = cfg.candles_count
        percent = min(100, (actual / required) * 100)
        
        status = "optimal" if percent >= 90 else "good" if percent >= 70 else "insufficient"
        
        return {
            "status": status,
            "actual": actual,
            "required": required,
            "percent": round(percent, 1),
            "hours": cfg.hours_history,
            "last_update": history.last_update.isoformat() if history.last_update else None
        }
    
    def get_all_data_quality(self, symbol: str) -> Dict[str, Dict]:
        """Получить качество данных по всем ТФ"""
        return {
            tf: self.get_data_quality(symbol, tf)
            for tf in self.tf_configs.keys()
        }
    
    def get_swing_highs(self, symbol: str, timeframe: str, lookback: int = 10, 
                        pivot_bars: int = 2) -> List[Tuple[int, float]]:
        """
        Найти Swing Highs (для Smart SL и TP)
        
        Returns: [(index, price), ...]
        """
        candles = self.get_candles(symbol, timeframe, lookback + pivot_bars * 2)
        if len(candles) < lookback:
            return []
        
        swing_highs = []
        for i in range(pivot_bars, len(candles) - pivot_bars):
            current_high = candles[i].high
            is_swing = all(
                candles[j].high < current_high
                for j in range(i - pivot_bars, i + pivot_bars + 1)
                if j != i
            )
            if is_swing:
                swing_highs.append((i, current_high))
        
        return swing_highs
    
    def get_swing_lows(self, symbol: str, timeframe: str, lookback: int = 10,
                       pivot_bars: int = 2) -> List[Tuple[int, float]]:
        """Найти Swing Lows"""
        candles = self.get_candles(symbol, timeframe, lookback + pivot_bars * 2)
        if len(candles) < lookback:
            return []
        
        swing_lows = []
        for i in range(pivot_bars, len(candles) - pivot_bars):
            current_low = candles[i].low
            is_swing = all(
                candles[j].low > current_low
                for j in range(i - pivot_bars, i + pivot_bars + 1)
                if j != i
            )
            if is_swing:
                swing_lows.append((i, current_low))
        
        return swing_lows
    
    def get_manipulation_candles(self, symbol: str, timeframe: str, 
                                  min_wick_ratio: float = 1.5,
                                  lookback: int = 20) -> List[Tuple[int, Candle, str]]:
        """
        Найти свечи манипуляции (для Smart SL)
        
        Returns: [(index, candle, direction), ...]
        """
        candles = self.get_candles(symbol, timeframe, lookback)
        if len(candles) < 3:
            return []
        
        manipulations = []
        for i, candle in enumerate(candles):
            if candle.body == 0:
                continue
            
            upper_ratio = candle.wick_ratio("upper")
            lower_ratio = candle.wick_ratio("lower")
            
            if upper_ratio >= min_wick_ratio:
                manipulations.append((i, candle, "upper"))
            elif lower_ratio >= min_wick_ratio:
                manipulations.append((i, candle, "lower"))
        
        return manipulations
    
    def clear_old_data(self, max_age_hours: float = 24):
        """Очистить старые данные"""
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        cleared = 0
        
        for symbol in list(self._history.keys()):
            for tf in list(self._history[symbol].keys()):
                history = self._history[symbol][tf]
                if history.last_update < cutoff:
                    del self._history[symbol][tf]
                    cleared += 1
            
            if not self._history[symbol]:
                del self._history[symbol]
                self._stats["symbols"].discard(symbol)
        
        if cleared > 0:
            print(f"🧹 Cleared {cleared} old histories (> {max_age_hours}h)")
    
    def get_stats(self) -> Dict:
        """Получить статистику менеджера"""
        total_candles = sum(
            h.count
            for symbol_histories in self._history.values()
            for h in symbol_histories.values()
        )
        
        return {
            "symbols": len(self._stats["symbols"]),
            "total_candles": total_candles,
            "updates": self._stats["updates"],
            "errors": self._stats["errors"],
            "memory_estimate_mb": round(total_candles * 64 / 1024 / 1024, 2)
        }


# Глобальный инстанс
_candle_manager: Optional[CandleHistoryManager] = None


def get_candle_manager() -> CandleHistoryManager:
    """Получить глобальный инстанс менеджера"""
    global _candle_manager
    if _candle_manager is None:
        _candle_manager = CandleHistoryManager()
    return _candle_manager


def init_candle_manager(tf_configs: Dict[str, TFConfig] = None) -> CandleHistoryManager:
    """Инициализировать менеджер с кастомными настройками"""
    global _candle_manager
    _candle_manager = CandleHistoryManager(tf_configs)
    return _candle_manager


# Утилиты для интеграции

async def fetch_and_store_candles(symbol: str, timeframe: str, 
                                   client, limit: int = None):
    """
    Загрузить свечи из API и сохранить в менеджер
    
    Args:
        client: Binance/Bybit клиент с методом get_klines
    """
    manager = get_candle_manager()
    cfg = manager.tf_configs.get(timeframe)
    
    if not limit and cfg:
        limit = cfg.candles_count
    
    try:
        # Получаем свечи из API
        ohlcv = await client.get_klines(symbol, timeframe, limit=limit)
        
        if ohlcv and len(ohlcv) > 0:
            manager.update_candles(symbol, timeframe, ohlcv)
            
            quality = manager.get_data_quality(symbol, timeframe)
            print(f"📈 {symbol} {timeframe}: {quality['actual']}/{quality['required']} "
                  f"({quality['percent']}%)")
            
            return True
        
        return False
        
    except Exception as e:
        print(f"❌ Error fetching {symbol} {timeframe}: {e}")
        return False


async def prefetch_all_tfs(symbol: str, client, priority_tfs: List[str] = None):
    """Загрузить свечи для всех ТФ (или приоритетных)"""
    manager = get_candle_manager()
    
    tfs = priority_tfs or ["30m", "1h", "4h"]  # Основные по умолчанию
    
    results = {}
    for tf in tfs:
        success = await fetch_and_store_candles(symbol, tf, client)
        results[tf] = success
        
        # Небольшая задержка чтобы не перегружать API
        if not success:
            await asyncio.sleep(0.1)
    
    return results


def check_data_ready(symbol: str, required_tfs: List[str] = None,
                     min_candles: int = 20) -> bool:
    """Проверить готовность данных для анализа"""
    manager = get_candle_manager()
    
    tfs = required_tfs or ["30m", "1h", "4h"]
    
    for tf in tfs:
        if not manager.has_enough_data(symbol, tf, min_candles):
            quality = manager.get_data_quality(symbol, tf)
            print(f"⚠️ {symbol} {tf}: insufficient data ({quality['actual']}/{min_candles})")
            return False
    
    return True


if __name__ == "__main__":
    # Тест
    manager = CandleHistoryManager()
    
    # Тестовые данные
    test_ohlcv = [
        [1609459200000, 29000, 29500, 28800, 29200, 1000],
        [1609545600000, 29200, 29800, 29100, 29600, 1200],
        [1609632000000, 29600, 29700, 29300, 29400, 800],
    ]
    
    manager.update_candles("BTCUSDT", "1h", test_ohlcv)
    
    print(f"\nCandles: {manager.get_candles('BTCUSDT', '1h')}")
    print(f"Quality: {manager.get_data_quality('BTCUSDT', '1h')}")
    print(f"Stats: {manager.get_stats()}")
