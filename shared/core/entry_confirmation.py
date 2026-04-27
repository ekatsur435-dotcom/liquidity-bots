"""
Entry Confirmation v2.7
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
    Комплексная проверка перед входом в сделку v2.7
    Фильтры дают БОНУСЫ к скору, а не блокируют вход.
    Основная логика v2.5 сохранена + v2.7 как усиление сигнала.
    """
    
    @staticmethod
    def multi_tf_confirmation(tf_data: Dict[str, List[List[float]]], 
                             direction: str = "short",
                             min_confirmations: int = 2) -> Tuple[bool, List[str]]:
        """
        ✅ v2.7: Мульти-ТФ подтверждение (2+ ТФ должны подтвердить)
        
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
                           min_spike: float = 1.3,
                           lookback: int = 20) -> Tuple[bool, str, float]:
        """
        3️⃣ Объёмный подтверждение v2.7 (смягчено)
        
        Было: 2x объём = обязательно (блок)
        Стало: 1.3x объём = бонус, 2x+ = сильный бонус
        """
        if len(ohlcv) < lookback + 5:
            return True, "⚠️ Мало данных для объёма", 0.0
        
        try:
            # Средний объём (без последних 5)
            avg_vol = sum(_get_price(ohlcv[i], 'volume') for i in range(-lookback-5, -5)) / lookback
            # Текущий объём (3 последние свечи)
            current_vol = sum(_get_price(ohlcv[i], 'volume') for i in range(-3, 0)) / 3
            volume_spike = current_vol / avg_vol if avg_vol > 0 else 0
        except:
            return True, "⚠️ Ошибка расчёта объёма", 0.0
        
        # v2.7: Не блокируем, даём бонус за высокий объём
        if volume_spike >= 2.0:
            return True, f"✅ Объём {volume_spike:.1f}x (сильный импульс)", volume_spike
        elif volume_spike >= min_spike:
            return True, f"✅ Объём {volume_spike:.1f}x (есть активность)", volume_spike
        else:
            return True, f"⚠️ Объём {volume_spike:.1f}x (ниже среднего)", volume_spike
    
    @staticmethod
    def atr_filter(ohlcv: List[List[float]],
                   min_atr: float = 0.8,
                   max_atr: float = 15.0,
                   period: int = 14) -> Tuple[bool, str, float]:
        """
        4️⃣ ATR Фильтр волатильности v2.7 (смягчено)
        
        Было: 1.5-8% = блок, остальное нельзя
        Стало: 0.8-15% = норма, всё остальное = предупреждение но не блок
        """
        if len(ohlcv) < period + 1:
            return True, "⚠️ Мало данных для ATR", 0.0
        
        try:
            atr = EntryConfirmation._calc_atr(ohlcv, period)
            current_price = _get_price(ohlcv[-1], 'close')
            atr_pct = (atr / current_price) * 100 if current_price > 0 else 0
        except:
            return True, "⚠️ Ошибка расчёта ATR", 0.0
        
        # v2.7: Не блокируем, даём информацию
        if atr_pct < min_atr:
            return True, f"⚠️ ATR {atr_pct:.1f}% низкий (спокойный рынок)", atr_pct
        elif atr_pct > max_atr:
            return True, f"⚠️ ATR {atr_pct:.1f}% высокий (волатильность)", atr_pct
        else:
            return True, f"✅ ATR {atr_pct:.1f}% в рабочем диапазоне", atr_pct
    
    @staticmethod
    def sr_levels_filter(ohlcv: List[List[float]],
                        current_price: float,
                        direction: str = "long",
                        tolerance: float = 0.03) -> Tuple[bool, str, Dict]:
        """
        5️⃣ Уровни поддержки/сопротивления v2.7 (умная логика direction)
        
        LONG:  Хорошо у Support (покупаем на дне), плохо у Resistance
        SHORT: Хорошо у Resistance (продаём на пике), плохо у Support
        
        v2.7: Не блокируем, даём бонус за правильное расположение
        """
        # v2.7: Смягчено — 30 свечей достаточно для 30m, масштабируем по ТФ
        min_candles = 30  # Базовое требование
        
        if len(ohlcv) < min_candles:
            return True, "⚠️ Мало истории для S/R", {"resistance": 0, "support": 0}
        
        try:
            # Находим уровни за последние N свечей
            lookback = min(30, len(ohlcv))  # Адаптивно
            highs = [_get_price(ohlcv[i], 'high') for i in range(-lookback, 0)]
            lows = [_get_price(ohlcv[i], 'low') for i in range(-lookback, 0)]
            
            resistance = max(highs[-15:]) if len(highs) >= 15 else max(highs)
            support = min(lows[-15:]) if len(lows) >= 15 else min(lows)
            
            # Проверяем близость к уровням (расширенный tolerance 3%)
            near_resistance = abs(current_price - resistance) / current_price < tolerance
            near_support = abs(current_price - support) / current_price < tolerance
            
            # v2.7: Умная логика в зависимости от направления
            if direction == "long":
                if near_support:
                    return True, f"✅ У Support (хорошо для LONG): ${support:.4f}", {
                        "resistance": resistance, "support": support, 
                        "type": "support", "optimal": True
                    }
                elif near_resistance:
                    return True, f"⚠️ У Resistance (неидеально для LONG): ${resistance:.4f}", {
                        "resistance": resistance, "support": support, 
                        "type": "resistance", "optimal": False
                    }
                else:
                    # v2.7: Не блокируем посреди диапазона!
                    return True, f"📊 В диапазоне (нейтрально)", {
                        "resistance": resistance, "support": support, 
                        "type": "mid", "optimal": None
                    }
            else:  # short
                if near_resistance:
                    return True, f"✅ У Resistance (хорошо для SHORT): ${resistance:.4f}", {
                        "resistance": resistance, "support": support, 
                        "type": "resistance", "optimal": True
                    }
                elif near_support:
                    return True, f"⚠️ У Support (неидеально для SHORT): ${support:.4f}", {
                        "resistance": resistance, "support": support, 
                        "type": "support", "optimal": False
                    }
                else:
                    # v2.7: Не блокируем посреди диапазона!
                    return True, f"📊 В диапазоне (нейтрально)", {
                        "resistance": resistance, "support": support, 
                        "type": "mid", "optimal": None
                    }
        except Exception as e:
            return True, f"⚠️ Ошибка S/R: {str(e)[:20]}", {"resistance": 0, "support": 0}
    
    @staticmethod
    def comprehensive_check(ohlcv: List[List[float]],
                          tf_data: Optional[Dict] = None,
                          direction: str = "short",
                          min_history: int = 30) -> Dict:
        """
        ✅ v2.7: Полная проверка — фильтры дают БОНУСЫ, не блокируют
        
        Система начисления:
        - Базовый скор: 50 (просто за проверку)
        - +10 за каждый "хороший" сигнал
        - +5 за каждый "нейтральный"
        - 0 за "плохой" но не блокируем
        
        Минимум истории: 30 свечей (вместо 50), масштабируется по ТФ
        """
        results = {
            "passed": True,  # v2.7: Всегда True, не блокируем
            "score": 50,     # v2.7: Базовый скор 50 просто за проверку
            "checks": {},
            "reasons": [],
            "entry_price": _get_price(ohlcv[-1], 'close') if ohlcv else 0,
            "direction": direction
        }
        
        # 1. Мульти-ТФ (если есть данные) — бонус за согласие ТФ
        if tf_data and len(tf_data) >= 2:
            passed, reasons = EntryConfirmation.multi_tf_confirmation(tf_data, direction)
            results["checks"]["multi_tf"] = {"passed": passed, "reasons": reasons}
            if passed:
                results["score"] += 15  # +15 за согласие 2+ ТФ
                results["reasons"].append(f"✅ Мульти-ТФ: {len(reasons)} подтверждений")
            else:
                results["score"] += 5   # +5 за попытку
                results["reasons"].append(f"⚠️ Мульти-ТФ: недостаточно подтверждений")
        else:
            results["reasons"].append("ℹ️ Мульти-ТФ: нет данных")
        
        # 2. Объём — бонус за высокий объём
        vol_passed, vol_reason, vol_spike = EntryConfirmation.volume_confirmation(ohlcv)
        results["checks"]["volume"] = {"passed": vol_passed, "reason": vol_reason, "spike": vol_spike}
        if vol_spike >= 2.0:
            results["score"] += 15  # Сильный импульс
        elif vol_spike >= 1.3:
            results["score"] += 10  # Есть активность
        elif vol_spike > 0:
            results["score"] += 3   # Низкий но есть
        results["reasons"].append(vol_reason)
        
        # 3. ATR — бонус за оптимальную волатильность
        atr_passed, atr_reason, atr_value = EntryConfirmation.atr_filter(ohlcv)
        results["checks"]["atr"] = {"passed": atr_passed, "reason": atr_reason, "value": atr_value}
        if 1.0 <= atr_value <= 10.0:
            results["score"] += 10  # Оптимальный ATR
        elif 0.5 <= atr_value <= 15.0:
            results["score"] += 5   # Приемлемый
        results["reasons"].append(atr_reason)
        
        # 4. S/R уровни — бонус за правильное расположение
        sr_passed, sr_reason, sr_data = EntryConfirmation.sr_levels_filter(
            ohlcv, results["entry_price"], direction=direction
        )
        results["checks"]["sr_levels"] = {"passed": sr_passed, "reason": sr_reason, "data": sr_data}
        
        # v2.7: Умная логика бонусов S/R
        if sr_data.get("optimal") is True:
            results["score"] += 15  # Идеальное расположение (Support для LONG, Resistance для SHORT)
        elif sr_data.get("optimal") is False:
            results["score"] += 5   # Неидеально но допустимо
        else:
            results["score"] += 8   # В диапазоне (нейтрально)
        results["reasons"].append(sr_reason)
        
        # v2.7: Ограничиваем максимум 100
        results["score"] = min(100, results["score"])
        
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
