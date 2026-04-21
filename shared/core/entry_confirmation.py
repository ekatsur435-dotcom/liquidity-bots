"""
Entry Confirmation v2.6
- Multi-timeframe confirmation (2+ TF must agree)
- Volume confirmation (spike detection)
- ATR volatility filter
- Support/Resistance levels
"""
from typing import List, Dict, Optional, Tuple, Any
import numpy as np


def _get_price(candle: Any, attr: str) -> float:
    """Универсальный доступ к цене: поддерживает и dict и object и namedtuple"""
    # Сначала проверяем dict (быстрее всего)
    if isinstance(candle, dict):
        return candle.get(attr, candle.get('close', 0))
    # Затем list (индексация)
    elif isinstance(candle, list):
        mapping = {'open': 0, 'high': 1, 'low': 2, 'close': 3, 'volume': 4}
        return candle[mapping.get(attr, 3)]
    # Объекты с атрибутами (CandleData, namedtuple) — hasattr сработает
    elif hasattr(candle, attr):
        return getattr(candle, attr)
    return 0.0


class EntryConfirmation:
    """
    Комплексная проверка перед входом в сделку.
    ВСЕ фильтры должны пройти для входа.
    """
    
    @staticmethod
    def multi_tf_confirmation(tf_data: Dict[str, List[List[float]]], 
                             direction: str = "short",
                             min_confirmations: int = 2) -> Tuple[bool, List[str]]:
        """
        ✅ v2.6: Мульти-ТФ подтверждение (2+ ТФ должны подтвердить)
        
        Args:
            tf_data: {"4h": ohlcv_4h, "2h": ohlcv_2h, "1h": ohlcv_1h, ...}
            direction: "short" | "long"
            min_confirmations: минимум совпадающих ТФ (2 по умолчанию)
        
        Returns: (passed, reasons)
        """
        confirmations = 0
        reasons = []
        
        for tf_name, ohlcv in tf_data.items():
            if len(ohlcv) < 20:
                continue
                
            # Проверяем тренд на этом ТФ
            ema_20 = EntryConfirmation._calc_ema(ohlcv, 20)
            ema_50 = EntryConfirmation._calc_ema(ohlcv, 50)
            current_price = _get_price(ohlcv[-1], 'close')
            
            if direction == "short":
                # Для шорта: цена < EMA20 < EMA50 = нисходящий
                if current_price < ema_20[-1] < ema_50[-1]:
                    confirmations += 1
                    reasons.append(f"📉 {tf_name}: Нисходящий тренд")
            else:
                # Для лонга: цена > EMA20 > EMA50 = восходящий
                if current_price > ema_20[-1] > ema_50[-1]:
                    confirmations += 1
                    reasons.append(f"📈 {tf_name}: Восходящий тренд")
        
        passed = confirmations >= min_confirmations
        return passed, reasons
    
    @staticmethod
    def volume_confirmation(ohlcv: List[List[float]], 
                           min_spike: float = 2.0,
                           lookback: int = 20) -> Tuple[bool, str]:
        """
        3️⃣ Объёмный подтверждение
        
        Требования:
        - Объём выше среднего на min_spike (2x)
        - На свече разворота высокий объём
        """
        if len(ohlcv) < lookback + 5:
            return False, "❌ Мало данных"
        
        # Средний объём (без последних 5)
        avg_vol = sum(_get_price(ohlcv[i], 'volume') for i in range(-lookback-5, -5)) / lookback
        
        # Текущий объём (3 последние свечи)
        current_vol = sum(_get_price(ohlcv[i], 'volume') for i in range(-3, 0)) / 3
        
        volume_spike = current_vol / avg_vol if avg_vol > 0 else 0
        
        if volume_spike < min_spike:
            return False, f"❌ Объём {volume_spike:.1f}x (нужно {min_spike}x)"
        
        return True, f"✅ Объём {volume_spike:.1f}x выше среднего"
    
    @staticmethod
    def atr_filter(ohlcv: List[List[float]],
                   min_atr: float = 1.5,
                   max_atr: float = 8.0,
                   period: int = 14) -> Tuple[bool, str, float]:
        """
        4️⃣ ATR Фильтр волатильности
        
        Исключаем:
        - Слишком спокойный рынок (< 1.5% ATR)
        - Слишком волатильный (> 8% ATR) — новости, памп/дамп
        """
        if len(ohlcv) < period + 1:
            return False, "❌ Мало данных", 0.0
        
        atr = EntryConfirmation._calc_atr(ohlcv, period)
        current_price = _get_price(ohlcv[-1], 'close')
        atr_pct = (atr / current_price) * 100
        
        if atr_pct < min_atr:
            return False, f"❌ ATR {atr_pct:.1f}% слишком низкий (мин {min_atr}%)", atr_pct
        
        if atr_pct > max_atr:
            return False, f"❌ ATR {atr_pct:.1f}% слишком высокий (макс {max_atr}%)", atr_pct
        
        return True, f"✅ ATR {atr_pct:.1f}% оптимален", atr_pct
    
    @staticmethod
    def sr_levels_filter(ohlcv: List[List[float]],
                        current_price: float,
                        tolerance: float = 0.02) -> Tuple[bool, str, Dict]:
        """
        5️⃣ Уровни поддержки/сопротивления
        
        Вход только у ключевых уровней, не посреди диапазона.
        """
        if len(ohlcv) < 50:
            return False, "❌ Мало истории", {}
        
        # Находим уровни за последние 50 свечей
        highs = [_get_price(ohlcv[i], 'high') for i in range(-50, 0)]
        lows = [_get_price(ohlcv[i], 'low') for i in range(-50, 0)]
        
        resistance = max(highs[-20:])  # Недавний максимум
        support = min(lows[-20:])       # Недавний минимум
        
        # Проверяем близость к уровням
        near_resistance = abs(current_price - resistance) / current_price < tolerance
        near_support = abs(current_price - support) / current_price < tolerance
        
        # Средняя цена диапазона
        range_mid = (max(highs) + min(lows)) / 2
        near_mid = abs(current_price - range_mid) / current_price < tolerance
        
        if near_mid and not (near_support or near_resistance):
            return False, "❌ Цена посреди диапазона (не у разворотного уровня)", {
                "resistance": resistance,
                "support": support,
                "mid": range_mid
            }
        
        level_type = ""
        if near_resistance:
            level_type = "Resistance"
        elif near_support:
            level_type = "Support"
        
        return True, f"✅ Цена у {level_type}: ${resistance if near_resistance else support:.4f}", {
            "resistance": resistance,
            "support": support,
            "type": level_type,
            "distance_pct": min(
                abs(current_price - resistance) / current_price,
                abs(current_price - support) / current_price
            ) * 100
        }
    
    @staticmethod
    def comprehensive_check(ohlcv: List[List[float]],
                          tf_data: Optional[Dict] = None,
                          direction: str = "short") -> Dict:
        """
        ✅ v2.6: Полная проверка всех фильтров
        """
        results = {
            "passed": True,
            "score": 0,
            "checks": {},
            "reasons": [],
            "entry_price": _get_price(ohlcv[-1], 'close') if ohlcv else 0
        }
        
        # 1. Мульти-ТФ (если есть данные)
        if tf_data:
            passed, reasons = EntryConfirmation.multi_tf_confirmation(tf_data, direction)
            results["checks"]["multi_tf"] = {"passed": passed, "reasons": reasons}
            if not passed:
                results["passed"] = False
            results["reasons"].extend(reasons)
        
        # 2. Объём
        vol_passed, vol_reason = EntryConfirmation.volume_confirmation(ohlcv)
        results["checks"]["volume"] = {"passed": vol_passed, "reason": vol_reason}
        if not vol_passed:
            results["passed"] = False
        results["reasons"].append(vol_reason)
        
        # 3. ATR
        atr_passed, atr_reason, atr_value = EntryConfirmation.atr_filter(ohlcv)
        results["checks"]["atr"] = {"passed": atr_passed, "reason": atr_reason, "value": atr_value}
        if not atr_passed:
            results["passed"] = False
        results["reasons"].append(atr_reason)
        
        # 4. S/R уровни
        sr_passed, sr_reason, sr_data = EntryConfirmation.sr_levels_filter(
            ohlcv, results["entry_price"]
        )
        results["checks"]["sr_levels"] = {"passed": sr_passed, "reason": sr_reason, "data": sr_data}
        if not sr_passed:
            results["passed"] = False
        results["reasons"].append(sr_reason)
        
        # Итоговый score (каждый пройденный фильтр +25)
        score = sum(25 for check in results["checks"].values() if check.get("passed", False))
        results["score"] = min(100, score)
        
        return results
    
    # =========================================================================
    # HELPER METHODS
    # =========================================================================
    
    @staticmethod
    def _calc_ema(ohlcv: List[Any], period: int) -> List[float]:
        """Расчёт EMA"""
        closes = [_get_price(c, 'close') for c in ohlcv]
        if len(closes) < period:
            return closes
        
        multiplier = 2 / (period + 1)
        ema = [sum(closes[:period]) / period]  # Начинаем с SMA
        
        for price in closes[period:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])
        
        # Дополняем до длины closes
        ema = [ema[0]] * (period - 1) + ema
        return ema
    
    @staticmethod
    def _calc_atr(ohlcv: List[Any], period: int = 14) -> float:
        """Расчёт Average True Range"""
        if len(ohlcv) < period + 1:
            return 0.0
        
        tr_values = []
        for i in range(1, len(ohlcv)):
            high = _get_price(ohlcv[i], 'high')
            low = _get_price(ohlcv[i], 'low')
            prev_close = _get_price(ohlcv[i-1], 'close')
            
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            
            tr_values.append(max(tr1, tr2, tr3))
        
        return sum(tr_values[-period:]) / period if tr_values else 0.0
