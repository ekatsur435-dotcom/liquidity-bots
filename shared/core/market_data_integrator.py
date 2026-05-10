"""
Market Data Integrator
Интеграция OKX данных (OI, Order Book, Trades) в скоринг систему бота
С логированием всех метрик для анализа
"""

import asyncio
import time
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
import json


@dataclass
class MarketContext:
    """Рыночный контекст для принятия решений"""
    symbol: str
    timestamp: int
    
    # OI данные
    oi: float = 0.0
    oi_change_24h: float = 0.0
    oi_change_1h: float = 0.0
    oi_signal: str = "neutral"  # 'increasing', 'decreasing', 'neutral'
    
    # Order Book
    spread_pct: float = 0.0
    bid_ask_imbalance: float = 0.0
    big_walls_count: int = 0
    support_levels: List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    ob_signal: str = "neutral"  # 'bullish', 'bearish', 'neutral'
    
    # Trades
    trade_pressure: float = 0.0  # -1.0 to 1.0
    large_trade_count: int = 0
    dominant_side: str = "neutral"
    trade_signal: str = "neutral"
    
    # Funding
    funding_rate: float = 0.0
    funding_signal: str = "neutral"  # 'extreme_positive', 'positive', 'neutral', 'negative', 'extreme_negative'
    
    # Liquidations
    liq_1h_usd: float = 0.0
    liq_dominant_side: Optional[str] = None
    
    # Итоговый скор
    data_quality_score: int = 0  # 0-100, насколько полные данные
    confidence: str = "low"  # 'high', 'medium', 'low'


class MarketDataIntegrator:
    """
    Интегратор рыночных данных
    Собирает данные из OKX и преобразует в торговые сигналы
    """
    
    def __init__(self, okx_client=None, binance_client=None):
        self.okx = okx_client
        self.binance = binance_client
        
        # Кэш для снижения нагрузки на API
        self._cache: Dict[str, Dict] = {}
        self._cache_ttl = 30  # 30 секунд
        
        # Статистика
        self._request_count = 0
        self._error_count = 0
        self._success_count = 0
        
        # История для анализа трендов
        self._oi_history: Dict[str, List[Dict]] = defaultdict(list)
        self._max_history = 10
        
        print("📊 Market Data Integrator initialized")
        print("   ✅ OKX: OI, Order Book, Trades, Funding, Liquidations")
        print("   ✅ Cache TTL: 30s")
        print("   ✅ Historical tracking: enabled")
    
    def _get_cache_key(self, symbol: str, data_type: str) -> str:
        """Создать ключ кэша"""
        return f"{symbol}:{data_type}"
    
    def _get_cached(self, symbol: str, data_type: str) -> Optional[Any]:
        """Получить из кэша"""
        key = self._get_cache_key(symbol, data_type)
        if key in self._cache:
            entry = self._cache[key]
            if time.time() - entry["timestamp"] < self._cache_ttl:
                return entry["data"]
        return None
    
    def _set_cache(self, symbol: str, data_type: str, data: Any):
        """Сохранить в кэш"""
        key = self._get_cache_key(symbol, data_type)
        self._cache[key] = {
            "data": data,
            "timestamp": time.time()
        }
    
    async def get_full_context(self, symbol: str) -> MarketContext:
        """
        Получить полный рыночный контекст
        
        Returns:
            MarketContext со всеми данными
        """
        context = MarketContext(
            symbol=symbol,
            timestamp=int(time.time() * 1000)
        )
        
        data_points = 0
        errors = []
        
        # 1. OI данные
        try:
            if self.okx:
                oi_data = await self.okx.get_open_interest(symbol)
                if oi_data:
                    context.oi = oi_data.oi
                    context.oi_change_24h = oi_data.oi_change_24h
                    data_points += 1
                    
                    # Сигнал OI
                    if context.oi_change_24h > 5:
                        context.oi_signal = "increasing"
                    elif context.oi_change_24h < -5:
                        context.oi_signal = "decreasing"
                    
                    # Сохраняем в историю
                    self._oi_history[symbol].append({
                        "oi": context.oi,
                        "timestamp": context.timestamp
                    })
                    if len(self._oi_history[symbol]) > self._max_history:
                        self._oi_history[symbol] = self._oi_history[symbol][-self._max_history:]
        except Exception as e:
            errors.append(f"OI: {e}")
        
        # 2. Order Book
        try:
            if self.okx:
                cached = self._get_cached(symbol, "ob")
                if cached:
                    ob_analysis = cached
                else:
                    ob_analysis = await self.okx.analyze_order_book(symbol)
                    if ob_analysis:
                        self._set_cache(symbol, "ob", ob_analysis)
                
                if ob_analysis:
                    context.spread_pct = ob_analysis.get("spread_pct", 0)
                    context.bid_ask_imbalance = ob_analysis.get("bid_ask_imbalance", 0)
                    context.big_walls_count = len(ob_analysis.get("big_walls", []))
                    context.support_levels = ob_analysis.get("support_levels", [])
                    context.resistance_levels = ob_analysis.get("resistance_levels", [])
                    data_points += 1
                    
                    # Сигнал Order Book
                    imbalance = context.bid_ask_imbalance
                    if imbalance > 0.2:
                        context.ob_signal = "bullish"
                    elif imbalance < -0.2:
                        context.ob_signal = "bearish"
        except Exception as e:
            errors.append(f"OB: {e}")
        
        # 3. Trades
        try:
            if self.okx:
                cached = self._get_cached(symbol, "trades")
                if cached:
                    trade_analysis = cached
                else:
                    trade_analysis = await self.okx.analyze_recent_trades(symbol, 100)
                    if trade_analysis:
                        self._set_cache(symbol, "trades", trade_analysis)
                
                if trade_analysis:
                    context.trade_pressure = trade_analysis.get("pressure", 0)
                    context.large_trade_count = trade_analysis.get("large_trade_count", 0)
                    context.dominant_side = trade_analysis.get("dominant_side", "neutral")
                    context.trade_signal = trade_analysis.get("signal", "neutral")
                    data_points += 1
        except Exception as e:
            errors.append(f"Trades: {e}")
        
        # 4. Funding
        try:
            if self.okx:
                funding = await self.okx.get_funding_rate(symbol)
                if funding:
                    context.funding_rate = funding.funding_rate
                    data_points += 1
                    
                    # Сигнал фандинга
                    fr = context.funding_rate
                    if fr > 0.1:
                        context.funding_signal = "extreme_positive"
                    elif fr > 0.05:
                        context.funding_signal = "positive"
                    elif fr < -0.1:
                        context.funding_signal = "extreme_negative"
                    elif fr < -0.05:
                        context.funding_signal = "negative"
        except Exception as e:
            errors.append(f"Funding: {e}")
        
        # 5. Liquidations (из Binance или OKX)
        try:
            if self.binance:
                liq = await self.binance.get_liquidations(symbol)
                if liq:
                    context.liq_1h_usd = liq.get("total_usd", 0)
                    context.liq_dominant_side = liq.get("dominant_side")
                    data_points += 1
        except Exception as e:
            errors.append(f"Liq: {e}")
        
        # Оценка качества данных
        context.data_quality_score = int((data_points / 5) * 100)
        
        if context.data_quality_score >= 80:
            context.confidence = "high"
        elif context.data_quality_score >= 50:
            context.confidence = "medium"
        else:
            context.confidence = "low"
        
        # Статистика
        self._request_count += 1
        if errors:
            self._error_count += len(errors)
        else:
            self._success_count += 1
        
        return context
    
    def calculate_score_adjustment(self, context: MarketContext, direction: str) -> Dict:
        """
        Рассчитать корректировку скора на основе рыночного контекста
        
        Args:
            context: Рыночный контекст
            direction: 'long' или 'short'
        
        Returns:
            {
                "score_delta": int,  # На сколько изменить скор
                "reasons": List[str],  # Почему
                "confidence": str,  # Насколько уверены
                "should_block": bool  # Блокировать ли вход
            }
        """
        score_delta = 0
        reasons = []
        should_block = False
        
        # Проверка качества данных
        if context.confidence == "low":
            return {
                "score_delta": 0,
                "reasons": ["⚠️ Недостаточно данных для анализа"],
                "confidence": "low",
                "should_block": False
            }
        
        # 1. Анализ Order Book
        imbalance = context.bid_ask_imbalance
        
        if direction == "long":
            if imbalance > 0.3:
                score_delta += 5
                reasons.append(f"📊 Стакан: сильный buy imbalance ({imbalance:+.2f})")
            elif imbalance > 0.15:
                score_delta += 3
                reasons.append(f"📊 Стакан: buy imbalance ({imbalance:+.2f})")
            elif imbalance < -0.3:
                score_delta -= 5
                reasons.append(f"📊 Стакан: сильный sell pressure ({imbalance:+.2f})")
            elif imbalance < -0.15:
                score_delta -= 3
                reasons.append(f"📊 Стакан: sell pressure ({imbalance:+.2f})")
            
            # Проверка стен продаж
            if context.big_walls_count > 0:
                # Если есть большие стены продаж — снижаем скор
                score_delta -= 2
                reasons.append(f"🧱 Обнаружены стены продаж ({context.big_walls_count})")
        
        else:  # direction == "short"
            if imbalance < -0.3:
                score_delta += 5
                reasons.append(f"📊 Стакан: сильный sell imbalance ({imbalance:+.2f})")
            elif imbalance < -0.15:
                score_delta += 3
                reasons.append(f"📊 Стакан: sell imbalance ({imbalance:+.2f})")
            elif imbalance > 0.3:
                score_delta -= 5
                reasons.append(f"📊 Стакан: сильный buy pressure ({imbalance:+.2f})")
            elif imbalance > 0.15:
                score_delta -= 3
                reasons.append(f"📊 Стакан: buy pressure ({imbalance:+.2f})")
            
            # Проверка стен покупок
            if context.big_walls_count > 0:
                score_delta -= 2
                reasons.append(f"🧱 Обнаружены стены покупок ({context.big_walls_count})")
        
        # 2. Анализ ленты сделок
        pressure = context.trade_pressure
        
        if direction == "long":
            if pressure > 0.5:
                score_delta += 4
                reasons.append(f"🔄 Лента: сильный buy pressure ({pressure:+.2f})")
            elif pressure > 0.2:
                score_delta += 2
                reasons.append(f"🔄 Лента: buy pressure ({pressure:+.2f})")
            elif pressure < -0.5:
                score_delta -= 4
                reasons.append(f"🔄 Лента: сильный sell pressure ({pressure:+.2f})")
        
        else:  # short
            if pressure < -0.5:
                score_delta += 4
                reasons.append(f"🔄 Лента: сильный sell pressure ({pressure:+.2f})")
            elif pressure < -0.2:
                score_delta += 2
                reasons.append(f"🔄 Лента: sell pressure ({pressure:+.2f})")
            elif pressure > 0.5:
                score_delta -= 4
                reasons.append(f"🔄 Лента: сильный buy pressure ({pressure:+.2f})")
        
        # Крупные сделки
        if context.large_trade_count >= 5:
            reasons.append(f"🐋 Крупные сделки: {context.large_trade_count} шт")
        
        return {
            "score_delta": score_delta,
            "reasons": reasons,
            "confidence": context.confidence,
            "should_block": should_block
        }
    
    async def log_context(self, symbol: str, context: MarketContext, direction: str):
        """
        Логирование полного контекста для анализа
        """
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "direction": direction,
            "context": {
                "oi": context.oi,
                "oi_change_24h": context.oi_change_24h,
                "oi_signal": context.oi_signal,
                "spread_pct": context.spread_pct,
                "bid_ask_imbalance": context.bid_ask_imbalance,
                "big_walls_count": context.big_walls_count,
                "trade_pressure": context.trade_pressure,
                "large_trade_count": context.large_trade_count,
                "dominant_side": context.dominant_side,
                "funding_rate": context.funding_rate,
                "funding_signal": context.funding_signal,
                "liq_1h_usd": context.liq_1h_usd,
                "liq_dominant_side": context.liq_dominant_side,
                "data_quality_score": context.data_quality_score,
                "confidence": context.confidence
            }
        }
        
        # Вывод в консоль
        print(f"\n📊 MARKET CONTEXT [{symbol}] {direction.upper()}")
        print(f"   Quality: {context.data_quality_score}% ({context.confidence})")
        print(f"   OI: {context.oi:,.0f} ({context.oi_change_24h:+.2f}%)")
        print(f"   OB Imbalance: {context.bid_ask_imbalance:+.3f}")
        print(f"   Trade Pressure: {context.trade_pressure:+.3f}")
        print(f"   Funding: {context.funding_rate:.4f}%")
        print(f"   Liq 1h: ${context.liq_1h_usd:,.0f}")
        
        return log_data
    
    def get_stats(self) -> Dict:
        """Получить статистику интегратора"""
        total = self._request_count
        success_rate = (self._success_count / max(total, 1)) * 100
        
        return {
            "total_requests": total,
            "success_count": self._success_count,
            "error_count": self._error_count,
            "success_rate": round(success_rate, 2),
            "cache_size": len(self._cache),
            "tracked_symbols": len(self._oi_history)
        }


# Singleton
_integrator: Optional[MarketDataIntegrator] = None


def get_market_data_integrator(okx_client=None, binance_client=None) -> MarketDataIntegrator:
    """Получить singleton интегратора"""
    global _integrator
    if _integrator is None:
        _integrator = MarketDataIntegrator(okx_client, binance_client)
    return _integrator
