"""
Dump Detector v1.0
Детектор резких дампов (flash crashes) и панических продаж.

Логика:
  1. Отслеживаем резкие падения цены (>3% за N свечей)
  2. Анализируем объём (всплеск ликвидаций)
  3. CVD показывает панические продажи
  4. Ищем точки разворота после дампа

Сигналы:
  FLASH_DUMP:    -5% за 1 свечу + объём 5x + ликвидации
  PANIC_SELL:    -8% за 3 свечи + последовательное понижение
  LIQUIDATION_CASCADE: Массовые ликвидации лонгистов
  BOTTOMING:     Признаки разворота после дампа

Интеграция:
  - Для LONG: ждём дамп + признаки разворота = вход
  - Для SHORT: дамп уже идёт = не входим, ждём отскок
  - Защита: блокировать вход при LIQUIDATION_CASCADE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("dump_detector")


class DumpType(Enum):
    """Типы дампов"""
    NONE = "none"
    FLASH_DUMP = "flash_dump"           # Мгновенный дамп (-5% за свечу)
    PANIC_SELL = "panic_sell"           # Панические продажи (-8% за 3 свечи)
    LIQUIDATION_CASCADE = "liquidation_cascade"  # Каскад ликвидаций
    GRADUAL_DECLINE = "gradual_decline"  # Постепенное снижение
    BOTTOMING = "bottoming"             # Признаки дна


@dataclass
class DumpConfig:
    """Конфигурация Dump Detector"""
    # Пороги дампа (%)
    flash_dump_1candle: float = 5.0     # -5% за 1 свечу
    flash_dump_3candles: float = 8.0    # -8% за 3 свечи
    panic_threshold: float = 10.0       # -10% за 5 свечей
    
    # Объём
    volume_spike_flash: float = 5.0     # 5x средний объём
    volume_spike_panic: float = 3.0     # 3x средний объём
    
    # Ликвидации
    liquidation_threshold: float = 1000000  # $1M ликвидаций
    liquidation_spike: float = 3.0      # 3x среднее
    
    # CVD (давление продаж)
    cvd_panic_threshold: float = 20.0     # CVD < 20 = сильное давление
    
    # Признаки дна
    bottom_bounce_min: float = 1.5      # Мин отскок для дна (%)
    bottom_volume_drop: float = 0.5     # Снижение объёма (должно быть < 0.5x от пика)
    bottom_cvd_recovery: float = 40.0   # CVD восстановился до 40+


@dataclass
class DumpResult:
    """Результат детекции дампа"""
    detected: bool
    dump_type: DumpType
    confidence: float                 # 0-100
    
    price_change_pct: float           # Изменение цены (%)
    duration_candles: int               # Свечей дампа
    
    volume_spike: float               # Всплеск объёма (x)
    liquidation_usd: Optional[float] = None
    
    cvd_value: Optional[float] = None # Давление (0-100)
    
    is_bottoming: bool = False         # Признаки дна
    bottom_signals: List[str] = field(default_factory=list)
    
    reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CandleData:
    """Данные свечи"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    liquidations_usd: float = 0.0


class DumpDetector:
    """
    Детектор дампов и панических продаж.
    Определяет когда идёт резкое падение и готовится ли разворот.
    """
    
    def __init__(self, config: Optional[DumpConfig] = None):
        self.cfg = config or DumpConfig()
        
    def analyze_candles(self, candles: List[Dict]) -> Tuple[float, float, int]:
        """
        Анализировать свечи на предмет дампа.
        
        Returns:
            (price_change_pct, volume_spike, duration)
        """
        if len(candles) < 3:
            return 0.0, 1.0, 0
        
        start_price = candles[0]["open"]
        end_price = candles[-1]["close"]
        
        price_change = (end_price - start_price) / start_price * 100
        
        # Объём
        avg_volume = sum(c.get("volume", 0) for c in candles[:-1]) / max(1, len(candles) - 1)
        recent_volume = candles[-1].get("volume", 0)
        volume_spike = recent_volume / avg_volume if avg_volume > 0 else 1.0
        
        # Длительность (сколько свечей падения)
        decline_candles = 0
        for c in candles:
            if c["close"] < c["open"]:  # Медвежья свеча
                decline_candles += 1
        
        return price_change, volume_spike, decline_candles
    
    def detect_liquidation_cascade(self, candles: List[Dict]) -> Tuple[bool, float]:
        """Детектировать каскад ликвидаций"""
        if not candles:
            return False, 0.0
        
        # Ищем свечу с аномальными ликвидациями
        total_liq = 0
        max_liq = 0
        
        for c in candles[-5:]:  # Последние 5 свечей
            liq = c.get("liquidations_usd", 0) or c.get("recent_liquidations_usd", 0)
            total_liq += liq
            max_liq = max(max_liq, liq)
        
        avg_liq = total_liq / 5
        
        if max_liq > self.cfg.liquidation_threshold and max_liq > avg_liq * self.cfg.liquidation_spike:
            return True, max_liq
        
        return False, max_liq
    
    def check_bottoming(self,
                        candles: List[Dict],
                        cvd_value: Optional[float] = None
                       ) -> Tuple[bool, List[str]]:
        """Проверить признаки дна после дампа"""
        if len(candles) < 3:
            return False, []
        
        signals = []
        
        # 1. Отскок от минимума
        recent_low = min(c["low"] for c in candles[-3:])
        current_price = candles[-1]["close"]
        bounce = (current_price - recent_low) / recent_low * 100
        
        if bounce >= self.cfg.bottom_bounce_min:
            signals.append(f"bounce_{bounce:.1f}%")
        
        # 2. Снижение объёма после пика
        peak_volume = max(c.get("volume", 0) for c in candles[-10:])
        current_volume = candles[-1].get("volume", 0)
        
        if peak_volume > 0 and current_volume < peak_volume * self.cfg.bottom_volume_drop:
            signals.append("volume_drop")
        
        # 3. CVD восстановление
        if cvd_value and cvd_value >= self.cfg.bottom_cvd_recovery:
            signals.append(f"cvd_recovery_{cvd_value:.0f}")
        
        # 4. Свеча с длинным нижним хвостом (поглощение продаж)
        last = candles[-1]
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        
        if body > 0 and lower_wick > body * 2:
            signals.append("long_lower_wick")
        
        # Дно если ≥2 сигнала
        is_bottom = len(signals) >= 2
        
        return is_bottom, signals
    
    def analyze(self,
                candles: List[Dict],
                cvd_value: Optional[float] = None,
                current_price: Optional[float] = None
               ) -> DumpResult:
        """
        Полный анализ на дамп.
        
        Args:
            candles: Свечи (минимум 5)
            cvd_value: Давление CVD (0-100)
            current_price: Текущая цена
        """
        
        if len(candles) < 5:
            return DumpResult(
                detected=False,
                dump_type=DumpType.NONE,
                confidence=0,
                price_change_pct=0,
                duration_candles=0,
                volume_spike=1.0,
                reasons=["Insufficient candles"]
            )
        
        reasons = []
        
        # Базовый анализ
        price_change, volume_spike, decline_candles = self.analyze_candles(candles)
        
        # Детекция типа дампа
        dump_type = DumpType.NONE
        confidence = 0
        
        # 1. FLASH DUMP - резкое падение за 1 свечу
        last_candle = candles[-1]
        last_change = (last_candle["close"] - last_candle["open"]) / last_candle["open"] * 100
        
        if last_change <= -self.cfg.flash_dump_1candle and volume_spike >= self.cfg.volume_spike_flash:
            dump_type = DumpType.FLASH_DUMP
            confidence = 80 + min(20, volume_spike * 2)
            reasons.append(f"Flash dump: {last_change:.1f}% in 1 candle, vol {volume_spike:.1f}x")
        
        # 2. PANIC SELL - падение за несколько свечей
        elif price_change <= -self.cfg.panic_threshold and decline_candles >= 3:
            dump_type = DumpType.PANIC_SELL
            confidence = 70 + min(20, abs(price_change))
            reasons.append(f"Panic sell: {price_change:.1f}% in {decline_candles} candles")
        
        # 3. GRADUAL DECLINE - постепенное снижение
        elif price_change <= -self.cfg.flash_dump_3candles:
            dump_type = DumpType.GRADUAL_DECLINE
            confidence = 50
            reasons.append(f"Gradual decline: {price_change:.1f}%")
        
        # Ликвидации (усиливают любой дамп)
        is_liq_cascade, liq_usd = self.detect_liquidation_cascade(candles)
        if is_liq_cascade:
            confidence = min(95, confidence + 15)
            reasons.append(f"Liquidation cascade: ${liq_usd:,.0f}")
            
            if dump_type in [DumpType.FLASH_DUMP, DumpType.PANIC_SELL]:
                dump_type = DumpType.LIQUIDATION_CASCADE
        
        # CVD давление продаж
        if cvd_value and cvd_value < self.cfg.cvd_panic_threshold:
            confidence = min(95, confidence + 10)
            reasons.append(f"Strong sell pressure: CVD {cvd_value:.0f}")
        
        # Признаки дна (только если дамп обнаружен)
        is_bottoming = False
        bottom_signals = []
        
        if dump_type != DumpType.NONE:
            is_bottoming, bottom_signals = self.check_bottoming(candles, cvd_value)
            if is_bottoming:
                reasons.append(f"Bottoming signals: {', '.join(bottom_signals)}")
        
        detected = dump_type != DumpType.NONE
        
        if detected:
            logger.warning(f"[DUMP-DETECTOR] {dump_type.value.upper()} | "
                          f"Price: {price_change:.1f}% | Vol: {volume_spike:.1f}x | "
                          f"Conf: {confidence:.0f}% | Bottoming: {is_bottoming}")
        
        return DumpResult(
            detected=detected,
            dump_type=dump_type,
            confidence=confidence,
            price_change_pct=price_change,
            duration_candles=decline_candles,
            volume_spike=volume_spike,
            liquidation_usd=liq_usd if is_liq_cascade else None,
            cvd_value=cvd_value,
            is_bottoming=is_bottoming,
            bottom_signals=bottom_signals,
            reasons=reasons,
            metadata={
                "last_candle_change": last_change,
                "avg_volume": sum(c.get("volume", 0) for c in candles) / len(candles),
                "max_volume": max(c.get("volume", 0) for c in candles)
            }
        )


# Singleton instance
_dump: Optional[DumpDetector] = None

def get_dump_detector(config: Optional[DumpConfig] = None) -> DumpDetector:
    """Get or create DumpDetector singleton"""
    global _dump
    if _dump is None:
        _dump = DumpDetector(config)
    return _dump
