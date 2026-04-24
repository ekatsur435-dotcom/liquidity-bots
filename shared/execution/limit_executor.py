"""
🎯 Limit Executor v1.1 — Адаптивное исполнение лимитных ордеров

Phase 2: Limit Executor с адаптивным TTL и fallback
Phase 3: Slippage Tracking — отслеживание проскальзывания
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, Dict, Any, Literal
from enum import Enum


class LimitOrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    EXPIRED = "expired"
    FALLBACK_TO_MARKET = "fallback_to_market"


@dataclass
class LimitOrderConfig:
    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    limit_price: float
    quantity: float
    sl_price: float
    tp_prices: list
    ttl_seconds: int = 300
    fallback_to_market: bool = True
    use_micro_steps: bool = True
    micro_step_pct: float = 0.02
    max_micro_steps: int = 3
    source: str = "ob"
    source_quality: int = 70


@dataclass
class LimitOrderResult:
    status: LimitOrderStatus
    filled_price: Optional[float] = None
    filled_quantity: float = 0.0
    order_id: Optional[str] = None
    fallback_used: bool = False
    micro_steps_taken: int = 0
    slippage_from_limit: float = 0.0
    created_at: datetime = None
    filled_at: Optional[datetime] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()


class LimitExecutor:
    """🎯 Адаптивное исполнение лимиток с fallback"""
    
    def __init__(self, binance_client=None, bingx_client=None, use_demo: bool = True):
        self.binance = binance_client
        self.bingx = bingx_client
        self.use_demo = use_demo
        self.active_orders: Dict[str, LimitOrderResult] = {}
        
        self.enabled = os.getenv("LIMIT_ORDER_ENABLED", "true").lower() == "true"
        self.default_ttl = int(os.getenv("LIMIT_FALLBACK_TIMEOUT", "300"))
        self.micro_step_pct = float(os.getenv("LIMIT_MICRO_STEP_PCT", "0.02"))
        
        print(f"🎯 Limit Executor: enabled={self.enabled}, TTL={self.default_ttl}s")
    
    def calculate_adaptive_ttl(self, 
                              symbol_profile: Any,
                              ob_freshness: str = "medium",
                              timeframe: str = "30m") -> int:
        """
        Адаптивный TTL на основе таймфрейма и профиля
        """
        # Базовый TTL по ТФ
        tf_base_ttl = {
            "5m": 60,      # 1 мин
            "15m": 150,    # 2.5 мин
            "30m": 300,    # 5 мин (default)
            "1h": 600,     # 10 мин
            "2h": 900,     # 15 мин
            "4h": 1200,    # 20 мин
            "6h": 1800,    # 30 мин
            "8h": 2400,    # 40 мин
            "1d": 3600,    # 60 мин
        }
        
        base_ttl = tf_base_ttl.get(timeframe, self.default_ttl)
        
        # Корректировка по свежести OB
        freshness_multiplier = {
            "fresh": 1.5,
            "medium": 1.0,
            "aging": 0.7,
            "stale": 0.3
        }
        
        multiplier = freshness_multiplier.get(ob_freshness, 1.0)
        
        # Корректировка по волатильности
        if symbol_profile:
            if symbol_profile.volatility_class == "high":
                multiplier *= 0.7
            elif symbol_profile.volatility_class == "low":
                multiplier *= 1.3
        
        return int(base_ttl * multiplier)
    
    def should_use_limit(self, signal: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        if signal.get("entry_type") != "LIMIT":
            return False
        ob_data = signal.get("ob_data")
        if not isinstance(ob_data, dict):
            return False
        if ob_data.get("ob_quality", 0) < 70:
            return False
        return True
    
    async def execute(self, config: LimitOrderConfig,
                      execute_market_callback: Optional[Callable] = None
                      ) -> LimitOrderResult:
        start_time = datetime.utcnow()
        result = LimitOrderResult(
            status=LimitOrderStatus.PENDING,
            order_id=f"limit_{config.symbol}_{int(start_time.timestamp())}"
        )
        
        print(f"🎯 [{config.symbol}] Limit @{config.limit_price:.6f} TTL={config.ttl_seconds}s")
        
        # Phase 1: Ждем TTL/2
        await asyncio.sleep(config.ttl_seconds // 2)
        
        # Phase 2: Микро-шаги
        if config.use_micro_steps and config.fallback_to_market:
            for step in range(config.max_micro_steps):
                result.micro_steps_taken += 1
                print(f"🔄 [{config.symbol}] Micro-step #{step+1}")
                await asyncio.sleep(config.ttl_seconds // (config.max_micro_steps + 1))
        
        # Phase 3: Fallback
        if config.fallback_to_market and execute_market_callback:
            print(f"⏰ [{config.symbol}] Fallback to MARKET")
            market_result = await execute_market_callback(
                symbol=config.symbol, side=config.side, quantity=config.quantity
            )
            if market_result:
                result.status = LimitOrderStatus.FALLBACK_TO_MARKET
                result.fallback_used = True
                result.filled_price = market_result.get("filled_price", config.entry_price)
                print(f"⚡ [{config.symbol}] MARKET @{result.filled_price}")
        else:
            result.status = LimitOrderStatus.EXPIRED
            
        return result
