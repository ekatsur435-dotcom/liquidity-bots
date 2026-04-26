"""
Market Regime Detector — определение режима рынка (bull/bear/sideways)
По BTC 1h и 4h для адаптации стратегии
"""

from typing import Optional, Dict, List
from dataclasses import dataclass
import asyncio


@dataclass
class MarketRegime:
    """Режим рынка"""
    trend: str  # "bull", "bear", "sideways"
    strength: float  # 0.0-1.0 сила тренда
    btc_price: float
    btc_ema_20: float
    btc_ema_50: float
    btc_rsi_14: float
    adx: float  # Average Directional Index
    recommendation: str  # "aggressive", "moderate", "conservative"


class MarketRegimeDetector:
    """Детектор режима рынка по BTC"""
    
    def __init__(self, market_data_client):
        self.client = market_data_client
        self._cache: Optional[MarketRegime] = None
        self._cache_time: float = 0
        self._cache_ttl: int = 300  # 5 минут
    
    async def detect(self) -> MarketRegime:
        """Определить текущий режим рынка"""
        # Кэш
        import time
        if self._cache and (time.time() - self._cache_time) < self._cache_ttl:
            return self._cache
        
        try:
            # Получаем данные BTC 1h
            btc_ohlcv_1h = await self.client.get_ohlcv("BTCUSDT", "1h", limit=100)
            btc_ohlcv_4h = await self.client.get_ohlcv("BTCUSDT", "4h", limit=50)
            
            if not btc_ohlcv_1h or len(btc_ohlcv_1h) < 50:
                return self._default_regime()
            
            # Расчет EMA
            closes_1h = [c.close for c in btc_ohlcv_1h]
            ema_20 = self._ema(closes_1h, 20)
            ema_50 = self._ema(closes_1h, 50)
            
            # RSI
            rsi_14 = self._rsi(closes_1h, 14)
            
            # ADX (трендовая сила)
            adx = self._calculate_adx(btc_ohlcv_1h[-20:])
            
            current_price = closes_1h[-1]
            
            # Определение тренда
            trend = "sideways"
            strength = 0.5
            
            if ema_20 and ema_50:
                # Bull: цена > EMA20 > EMA50
                if current_price > ema_20[-1] > ema_50[-1]:
                    trend = "bull"
                    strength = min(1.0, (current_price - ema_50[-1]) / ema_50[-1] * 100)
                # Bear: цена < EMA20 < EMA50
                elif current_price < ema_20[-1] < ema_50[-1]:
                    trend = "bear"
                    strength = min(1.0, (ema_50[-1] - current_price) / ema_50[-1] * 100)
            
            # Рекомендация по стратегии
            recommendation = self._get_recommendation(trend, strength, adx)
            
            regime = MarketRegime(
                trend=trend,
                strength=strength,
                btc_price=current_price,
                btc_ema_20=ema_20[-1] if ema_20 else current_price,
                btc_ema_50=ema_50[-1] if ema_50 else current_price,
                btc_rsi_14=rsi_14,
                adx=adx,
                recommendation=recommendation
            )
            
            self._cache = regime
            self._cache_time = time.time()
            return regime
            
        except Exception as e:
            print(f"[MarketRegime] Error: {e}")
            return self._default_regime()
    
    def _default_regime(self) -> MarketRegime:
        """Режим по умолчанию (sideways)"""
        return MarketRegime(
            trend="sideways",
            strength=0.5,
            btc_price=0,
            btc_ema_20=0,
            btc_ema_50=0,
            btc_rsi_14=50,
            adx=20,
            recommendation="moderate"
        )
    
    def _get_recommendation(self, trend: str, strength: float, adx: float) -> str:
        """Рекомендация по стратегии"""
        if adx > 25:  # Сильный тренд
            if trend == "bull":
                return "aggressive_long" if strength > 0.7 else "moderate_long"
            elif trend == "bear":
                return "aggressive_short" if strength > 0.7 else "moderate_short"
        elif adx < 20:  # Слабый тренд (sideways)
            return "conservative"
        return "moderate"
    
    def _ema(self, data: List[float], period: int) -> Optional[List[float]]:
        """Расчет EMA"""
        if len(data) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])
        return ema
    
    def _rsi(self, data: List[float], period: int = 14) -> float:
        """Расчет RSI"""
        if len(data) < period + 1:
            return 50.0
        
        deltas = [data[i] - data[i-1] for i in range(1, len(data))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _calculate_adx(self, candles: List) -> float:
        """Упрощенный расчет ADX"""
        try:
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            closes = [c.close for c in candles]
            
            tr_list = []
            for i in range(1, len(candles)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                tr_list.append(tr)
            
            atr = sum(tr_list) / len(tr_list) if tr_list else 1
            
            # Упрощенный расчет directional movement
            plus_dm = []
            minus_dm = []
            for i in range(1, len(candles)):
                up_move = highs[i] - highs[i-1]
                down_move = lows[i-1] - lows[i]
                
                if up_move > down_move and up_move > 0:
                    plus_dm.append(up_move)
                else:
                    plus_dm.append(0)
                
                if down_move > up_move and down_move > 0:
                    minus_dm.append(down_move)
                else:
                    minus_dm.append(0)
            
            avg_plus_dm = sum(plus_dm) / len(plus_dm) if plus_dm else 0
            avg_minus_dm = sum(minus_dm) / len(minus_dm) if minus_dm else 0
            
            plus_di = 100 * (avg_plus_dm / atr) if atr else 0
            minus_di = 100 * (avg_minus_dm / atr) if atr else 0
            
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
            return dx
            
        except Exception:
            return 20.0  # Default value


# Глобальный инстанс (singleton)
_regime_detector: Optional[MarketRegimeDetector] = None


def get_regime_detector(market_data_client) -> MarketRegimeDetector:
    """Получить инстанс детектора"""
    global _regime_detector
    if _regime_detector is None:
        _regime_detector = MarketRegimeDetector(market_data_client)
    return _regime_detector
