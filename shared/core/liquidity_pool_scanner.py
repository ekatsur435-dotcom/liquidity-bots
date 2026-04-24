"""
🌊 EQH/EQL Scanner v1.0 — Детекция пулов ликвидности

EQH = Equal Highs (равные максимумы) — ликвидность сверху
EQL = Equal Lows (равные минимумы) — ликвидность снизу

Обнаруживает:
- Двойные вершины/днища
- Тройные вершины/днища  
- Линии ликвидности с 2+ касаниями
- Sweep зон (пробой ложный/настоящий)

Environment:
- USE_EQH_EQL_SCANNER: "true" — включить сканер
- EQH_EQL_LOOKBACK: 100 — свечей для анализа
- EQH_EQL_TOLERANCE_PCT: 0.5 — допуск для "равных" уровней (0.5%)
- MIN_TOUCHES: 2 — минимум касаний для признания уровня
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
from enum import Enum


class LiquidityType(Enum):
    EQH = "equal_highs"      # Равные максимумы
    EQL = "equal_lows"       # Равные минимумы
    TRIPLE_TOP = "triple_top"
    TRIPLE_BOTTOM = "triple_bottom"
    LIQUIDITY_SWEEP_HIGH = "sweep_high"  # Пробой high с возвратом
    LIQUIDITY_SWEEP_LOW = "sweep_low"    # Пробой low с возвратом


@dataclass
class LiquidityPool:
    """Пул ликвидности — зона интереса SMC"""
    type: LiquidityType
    level: float
    touches: List[Dict] = field(default_factory=list)
    strength: int = 0  # 0-100
    volume_at_level: float = 0.0
    created_at: datetime = None
    is_swept: bool = False
    swept_at: Optional[datetime] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()


@dataclass
class LiquidityScanResult:
    """Результат сканирования"""
    symbol: str
    timeframe: str
    eqh_levels: List[LiquidityPool] = field(default_factory=list)
    eql_levels: List[LiquidityPool] = field(default_factory=list)
    active_sweeps: List[LiquidityPool] = field(default_factory=list)
    nearest_eqh: Optional[LiquidityPool] = None
    nearest_eql: Optional[LiquidityPool] = None
    
    def best_opportunity(self, direction: str) -> Optional[LiquidityPool]:
        """Лучшая возможность для направления"""
        if direction == "long":
            # Для LONG ищем EQL (liquidity sweep low)
            return self.nearest_eql
        else:
            # Для SHORT ищем EQH (liquidity sweep high)
            return self.nearest_eqh


class LiquidityPoolScanner:
    """
    🌊 Сканер пулов ликвидности (EQH/EQL)
    
    Логика:
    1. Находим уровни с 2+ касаниями (wicks/tails)
    2. Группируем "равные" уровни с допуском
    3. Определяем силу (volume + touches + recency)
    4. Детектируем sweeps (пробой с возвратом)
    """
    
    def __init__(self):
        self.enabled = os.getenv("USE_EQH_EQL_SCANNER", "true").lower() == "true"
        self.lookback = int(os.getenv("EQH_EQL_LOOKBACK", "100"))
        self.tolerance_pct = float(os.getenv("EQH_EQL_TOLERANCE_PCT", "0.5"))
        self.min_touches = int(os.getenv("EQH_EQL_MIN_TOUCHES", "2"))
        
        # Кэш результатов
        self.cache: Dict[str, LiquidityScanResult] = {}
        
        print(f"🌊 LiquidityPoolScanner: enabled={self.enabled}, "
              f"lookback={self.lookback}, tolerance={self.tolerance_pct}%")
    
    def scan(self, 
             ohlcv: List[List[float]], 
             symbol: str,
             timeframe: str = "30m") -> LiquidityScanResult:
        """
        Сканирование OHLCV на EQH/EQL
        
        Args:
            ohlcv: [[timestamp, open, high, low, close, volume], ...]
            symbol: Тикер
            timeframe: Таймфрейм
        
        Returns:
            LiquidityScanResult с найденными пулами
        """
        result = LiquidityScanResult(symbol=symbol, timeframe=timeframe)
        
        if not ohlcv or len(ohlcv) < 20:
            return result
        
        try:
            # 1. Собираем хаи и лоу
            highs = []
            lows = []
            volumes = []
            
            for i, candle in enumerate(ohlcv[-self.lookback:]):
                if len(candle) >= 5:
                    highs.append((candle[2], i))  # (high, index)
                    lows.append((candle[3], i))   # (low, index)
                    volumes.append(candle[4] if len(candle) > 4 else 0)
            
            # 2. Группируем "равные" уровни
            eqh_groups = self._group_equal_levels(highs, is_highs=True)
            eql_groups = self._group_equal_levels(lows, is_highs=False)
            
            # 3. Создаем LiquidityPool из групп
            for level, touches in eqh_groups.items():
                if len(touches) >= self.min_touches:
                    pool = LiquidityPool(
                        type=LiquidityType.EQH if len(touches) == 2 else LiquidityType.TRIPLE_TOP,
                        level=level,
                        touches=touches,
                        strength=min(100, len(touches) * 25 + 20),
                        volume_at_level=sum(volumes[t["index"]] for t in touches)
                    )
                    result.eqh_levels.append(pool)
            
            for level, touches in eql_groups.items():
                if len(touches) >= self.min_touches:
                    pool = LiquidityPool(
                        type=LiquidityType.EQL if len(touches) == 2 else LiquidityType.TRIPLE_BOTTOM,
                        level=level,
                        touches=touches,
                        strength=min(100, len(touches) * 25 + 20),
                        volume_at_level=sum(volumes[t["index"]] for t in touches)
                    )
                    result.eql_levels.append(pool)
            
            # 4. Определяем ближайшие уровни
            current_price = ohlcv[-1][4] if ohlcv else 0
            
            if result.eqh_levels:
                result.nearest_eqh = min(
                    result.eqh_levels,
                    key=lambda x: abs(x.level - current_price)
                )
            
            if result.eql_levels:
                result.nearest_eql = min(
                    result.eql_levels,
                    key=lambda x: abs(x.level - current_price)
                )
            
            # 5. Детектируем sweeps
            result.active_sweeps = self._detect_sweeps(ohlcv, result, current_price)
            
            # Кэшируем
            self.cache[f"{symbol}_{timeframe}"] = result
            
            print(f"🌊 [{symbol}] EQH: {len(result.eqh_levels)} | "
                  f"EQL: {len(result.eql_levels)} | "
                  f"Sweeps: {len(result.active_sweeps)}")
            
        except Exception as e:
            print(f"❌ LiquidityPoolScanner [{symbol}]: {e}")
        
        return result
    
    def _group_equal_levels(self, 
                            levels: List[Tuple[float, int]], 
                            is_highs: bool) -> Dict[float, List[Dict]]:
        """
        Группировка "равных" уровней с учетом tolerance
        """
        groups = {}
        
        for level, idx in levels:
            found_group = False
            
            for group_level in list(groups.keys()):
                diff_pct = abs(level - group_level) / group_level * 100
                
                if diff_pct <= self.tolerance_pct:
                    # Добавляем к существующей группе
                    groups[group_level].append({
                        "price": level,
                        "index": idx,
                        "diff_pct": diff_pct
                    })
                    found_group = True
                    break
            
            if not found_group:
                # Создаем новую группу
                groups[level] = [{
                    "price": level,
                    "index": idx,
                    "diff_pct": 0
                }]
        
        return groups
    
    def _detect_sweeps(self,
                       ohlcv: List[List[float]],
                       scan_result: LiquidityScanResult,
                       current_price: float) -> List[LiquidityPool]:
        """
        Детекция sweeps — пробой уровня с возвратом
        """
        sweeps = []
        
        if not ohlcv or len(ohlcv) < 3:
            return sweeps
        
        last_candle = ohlcv[-1]
        prev_candle = ohlcv[-2] if len(ohlcv) > 1 else None
        
        # Проверяем EQH sweeps (пробой high)
        for eqh in scan_result.eqh_levels:
            if eqh.level > current_price * 0.99:  # Недалеко от текущей цены
                # Пробили high и вернулись ниже?
                if prev_candle and prev_candle[2] > eqh.level and last_candle[4] < eqh.level:
                    eqh.is_swept = True
                    eqh.swept_at = datetime.utcnow()
                    sweeps.append(eqh)
        
        # Проверяем EQL sweeps (пробой low)
        for eql in scan_result.eql_levels:
            if eql.level < current_price * 1.01:  # Недалеко от текущей цены
                # Пробили low и вернулись выше?
                if prev_candle and prev_candle[3] < eql.level and last_candle[4] > eql.level:
                    eql.is_swept = True
                    eql.swept_at = datetime.utcnow()
                    sweeps.append(eql)
        
        return sweeps
    
    def get_liquidation_zones(self, 
                              symbol: str,
                              current_price: float,
                              direction: str) -> Dict[str, float]:
        """
        Получить зоны ликвидации для позиции
        """
        cache_key = f"{symbol}_30m"
        scan = self.cache.get(cache_key)
        
        if not scan:
            return {}
        
        zones = {}
        
        if direction == "long":
            # Для LONG: EQL = стоп-зона, EQH = тейк-профит
            if scan.nearest_eql:
                zones["liquidity_sl"] = scan.nearest_eql.level * 0.998
            if scan.nearest_eqh:
                zones["liquidity_tp"] = scan.nearest_eqh.level * 0.995
        else:
            # Для SHORT: EQH = стоп-зона, EQL = тейк-профит
            if scan.nearest_eqh:
                zones["liquidity_sl"] = scan.nearest_eqh.level * 1.002
            if scan.nearest_eql:
                zones["liquidity_tp"] = scan.nearest_eql.level * 1.005
        
        return zones


# Singleton instance
_scanner_instance: Optional[LiquidityPoolScanner] = None


def get_liquidity_scanner() -> LiquidityPoolScanner:
    global _scanner_instance
    if _scanner_instance is None:
        _scanner_instance = LiquidityPoolScanner()
    return _scanner_instance


def scan_liquidity_pools(ohlcv: List[List[float]], 
                         symbol: str,
                         timeframe: str = "30m") -> LiquidityScanResult:
    """Удобная функция для быстрого сканирования"""
    scanner = get_liquidity_scanner()
    return scanner.scan(ohlcv, symbol, timeframe)
