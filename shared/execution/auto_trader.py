"""
Auto Trading Module
Автоматическое исполнение сигналов через BingX
"""

import os
import asyncio
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime
import json

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from api.bingx_client import BingXClient, BingXPosition, BingXOrder
from upstash.redis_client import get_redis_client


@dataclass
class TradeConfig:
    """Конфигурация торговли"""
    enabled: bool = True
    demo_mode: bool = True  # True = DEMO, False = REAL
    
    # Риск-менеджмент
    max_positions: int = 5
    risk_per_trade: float = 0.015  # 1.5%
    max_daily_risk: float = 0.05  # 5%
    
    # Плечо
    default_leverage: int = 5
    min_leverage: int = 3
    max_leverage: int = 10
    
    # Вход
    use_limit_orders: bool = True  # Использовать лимитные ордера
    limit_order_offset: float = 0.001  # 0.1% отступ от цены
    
    # Выход
    use_trailing_stop: bool = True
    trailing_activation: float = 0.015  # Активация после +1.5%
    trailing_distance: float = 0.008  # Дистанция трейлинга 0.8%
    
    # Частичное закрытие
    partial_tp_enabled: bool = True
    tp1_size: float = 0.40  # 40%
    tp2_size: float = 0.35  # 35%
    tp3_size: float = 0.25  # 25%
    
    # Фильтры
    min_score_for_trade: int = 75
    require_smc_confirmation: bool = True
    min_smc_score: int = 50


class AutoTrader:
    """Авто-трейдер для исполнения сигналов"""
    
    def __init__(self, 
                 bingx_client: Optional[BingXClient] = None,
                 config: Optional[TradeConfig] = None):
        """
        Инициализация авто-трейдера
        
        Args:
            bingx_client: Клиент BingX (создаётся автоматически если None)
            config: Конфигурация торговли
        """
        self.config = config or TradeConfig()
        self.bingx = bingx_client or BingXClient(demo=self.config.demo_mode)
        self.redis = get_redis_client()
        
        # Статистика
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.last_reset = datetime.utcnow().date()
        
        print(f"🤖 AutoTrader initialized ({'DEMO' if self.config.demo_mode else 'REAL'} mode)")
        print(f"   Max positions: {self.config.max_positions}")
        print(f"   Risk per trade: {self.config.risk_per_trade*100}%")
    
    async def can_open_position(self, symbol: str, direction: str, signal_score: int) -> bool:
        """
        Проверяем можно ли открыть позицию
        
        Returns:
            True если можно открыть, False если нет
        """
        # Проверяем включена ли автоторговля
        if not self.config.enabled:
            print(f"⏸️ Auto-trading disabled")
            return False
        
        # Проверяем минимальный Score
        if signal_score < self.config.min_score_for_trade:
            print(f"⏸️ Signal score {signal_score} < min {self.config.min_score_for_trade}")
            return False
        
        # Проверяем дневной лимит риска
        self._check_daily_reset()
        if self.daily_pnl <= -self.config.max_daily_risk:
            print(f"⏸️ Daily risk limit reached ({self.daily_pnl:.2%})")
            return False
        
        # Проверяем количество позиций
        current_positions = await self.bingx.get_positions()
        if len(current_positions) >= self.config.max_positions:
            print(f"⏸️ Max positions reached ({len(current_positions)}/{self.config.max_positions})")
            return False
        
        # Проверяем нет ли уже позиции по этой паре
        existing = [p for p in current_positions if p.symbol == symbol.replace('USDT', '-USDT') or p.symbol == symbol]
        if existing:
            print(f"⏸️ Position already exists for {symbol}")
            return False
        
        # Проверяем противоположную позицию
        opposite_direction = "SHORT" if direction == "long" else "LONG"
        opposite = [p for p in current_positions if p.symbol == symbol.replace('USDT', '-USDT') or p.symbol == symbol and p.side == opposite_direction]
        if opposite:
            print(f"⚠️ Opposite position exists for {symbol}, closing...")
            await self.close_position(symbol, opposite_direction)
        
        return True
    
    async def open_position(self, 
                           symbol: str,
                           direction: str,
                           entry_price: float,
                           stop_loss: float,
                           take_profits: List[tuple],
                           signal_score: int,
                           smc_data: Optional[Dict] = None) -> Optional[Dict]:
        """
        Открыть позицию на BingX
        
        Args:
            symbol: Торговая пара (BTCUSDT)
            direction: 'long' или 'short'
            entry_price: Цена входа
            stop_loss: Стоп-лосс
            take_profits: Список (цена, процент_закрытия)
            signal_score: Score сигнала
            smc_data: Данные SMC (опционально)
        
        Returns:
            Информация об открытой позиции или None
        """
        try:
            # Проверяем можно ли открыть
            if not await self.can_open_position(symbol, direction, signal_score):
                return None
            
            # Получаем баланс
            balance_data = await self.bingx.get_account_balance()
            if not balance_data:
                print("❌ Failed to get balance")
                return None
            
            # Рассчитываем размер позиции
            available_balance = float(balance_data.get("availableMargin", 1000))
            
            # Smart position sizing на основе Score
            if signal_score >= 85:
                risk_mult = 2.0  # 3% риска на снайперские сигналы
            elif signal_score >= 75:
                risk_mult = 1.5  # 2.25% риска
            else:
                risk_mult = 1.0  # 1.5% риска
            
            actual_risk = self.config.risk_per_trade * risk_mult
            risk_amount = available_balance * actual_risk
            
            # Рассчитываем размер позиции
            price_distance = abs(entry_price - stop_loss) / entry_price
            if price_distance == 0:
                print("❌ Invalid SL distance")
                return None
            
            position_value = risk_amount / price_distance
            
            # Определяем плечо
            leverage = self._calculate_leverage(signal_score, smc_data)
            
            # Рассчитываем количество монет
            size = position_value / entry_price
            
            # Корректируем под минимальный размер BingX
            min_size = 0.001  # Минимум 0.001 BTC
            if size < min_size:
                print(f"⚠️ Position size {size:.4f} < min {min_size}, adjusting...")
                size = min_size
            
            # Устанавливаем плечо
            bingx_symbol = symbol.replace('USDT', '-USDT')
            await self.bingx.set_leverage(bingx_symbol, leverage, position_side="BOTH")
            
            # Определяем стороны
            bingx_side = "BUY" if direction == "long" else "SELL"
            bingx_position_side = "LONG" if direction == "long" else "SHORT"
            
            # Используем лимитный или рыночный ордер
            if self.config.use_limit_orders and smc_data and smc_data.get('entry_zone'):
                # Лимитный ордер в зоне OB/FVG
                entry_zone = smc_data['entry_zone']
                limit_price = entry_zone[1] if direction == "short" else entry_zone[0]
                
                order = await self.bingx.place_order(
                    symbol=bingx_symbol,
                    side=bingx_side,
                    position_side=bingx_position_side,
                    order_type="LIMIT",
                    size=size,
                    price=limit_price,
                    stop_loss=stop_loss
                )
            else:
                # Рыночный ордер
                order = await self.bingx.place_market_order(
                    symbol=bingx_symbol,
                    side=bingx_side,
                    position_side=bingx_position_side,
                    size=size,
                    stop_loss=stop_loss
                )
            
            if not order:
                print("❌ Failed to place order")
                return None
            
            # Сохраняем позицию в Redis
            position_data = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "size": size,
                "leverage": leverage,
                "stop_loss": stop_loss,
                "take_profits": take_profits,
                "signal_score": signal_score,
                "smc_data": smc_data,
                "order_id": order.order_id,
                "opened_at": datetime.utcnow().isoformat(),
                "status": "open"
            }
            
            # Сохраняем в Redis
            bot_type = "short" if direction == "short" else "long"
            self.redis.save_position(bot_type, symbol, position_data)
            
            # Обновляем статистику
            self.daily_trades += 1
            
            print(f"✅ Position opened: {symbol} {direction.upper()}")
            print(f"   Size: {size:.4f}, Leverage: {leverage}x")
            print(f"   Entry: ${entry_price:.2f}, SL: ${stop_loss:.2f}")
            print(f"   Risk: {actual_risk*100:.2f}% of balance")
            
            return position_data
        
        except Exception as e:
            print(f"❌ Error opening position: {e}")
            return None
    
    async def close_position(self, symbol: str, position_side: str) -> bool:
        """Закрыть позицию"""
        try:
            bingx_symbol = symbol.replace('USDT', '-USDT')
            success = await self.bingx.close_position(bingx_symbol, position_side)
            
            if success:
                # Обновляем в Redis
                bot_type = "short" if position_side == "SHORT" else "long"
                self.redis.close_position(bot_type, symbol, 0.0, 0.0)
            
            return success
        except Exception as e:
            print(f"❌ Error closing position: {e}")
            return False
    
    async def update_positions(self):
        """
        Обновить статус позиций (проверить SL/TP, обновить PnL)
        Вызывается регулярно
        """
        try:
            # Получаем позиции с биржи
            bingx_positions = await self.bingx.get_positions()
            
            # Получаем позиции из Redis
            short_positions = self.redis.get_all_positions("short")
            long_positions = self.redis.get_all_positions("long")
            redis_positions = short_positions + long_positions
            
            # Синхронизируем
            for redis_pos in redis_positions:
                symbol = redis_pos.get("symbol", "")
                direction = redis_pos.get("direction", "")
                
                # Ищем соответствующую позицию на бирже
                bingx_symbol = symbol.replace('USDT', '-USDT')
                bingx_pos = next((p for p in bingx_positions if p.symbol == bingx_symbol), None)
                
                if not bingx_pos:
                    # Позиция закрыта на бирже
                    print(f"📊 Position closed on exchange: {symbol}")
                    
                    # Рассчитываем PnL (упрощённо)
                    entry = redis_pos.get("entry_price", 0)
                    size = redis_pos.get("size", 0)
                    
                    # Обновляем в Redis
                    bot_type = "short" if direction == "short" else "long"
                    self.redis.close_position(bot_type, symbol, 0.0, 0.0)
                else:
                    # Обновляем PnL
                    unrealized_pnl = bingx_pos.unrealized_pnl
                    redis_pos["unrealized_pnl"] = unrealized_pnl
                    redis_pos["current_price"] = bingx_pos.entry_price  # Обновим текущую цену
                    
                    # Сохраняем обновлённую позицию
                    bot_type = "short" if direction == "short" else "long"
                    self.redis.save_position(bot_type, symbol, redis_pos)
            
            return True
        
        except Exception as e:
            print(f"❌ Error updating positions: {e}")
            return False
    
    async def get_account_summary(self) -> Dict:
        """Получить сводку аккаунта"""
        try:
            balance = await self.bingx.get_account_balance()
            positions = await self.bingx.get_positions()
            
            total_unrealized = sum(p.unrealized_pnl for p in positions)
            
            return {
                "balance": balance,
                "open_positions": len(positions),
                "unrealized_pnl": total_unrealized,
                "daily_trades": self.daily_trades,
                "daily_pnl": self.daily_pnl,
                "mode": "DEMO" if self.config.demo_mode else "REAL"
            }
        except Exception as e:
            print(f"❌ Error getting summary: {e}")
            return {}
    
    def _calculate_leverage(self, signal_score: int, smc_data: Optional[Dict]) -> int:
        """Рассчитать оптимальное плечо"""
        base_leverage = self.config.default_leverage
        
        # Увеличиваем плечо на сильные сигналы
        if signal_score >= 85:
            leverage = min(self.config.max_leverage, base_leverage + 3)
        elif signal_score >= 75:
            leverage = min(self.config.max_leverage, base_leverage + 1)
        else:
            leverage = base_leverage
        
        # Уменьшаем если нет SMC подтверждения
        if self.config.require_smc_confirmation and (not smc_data or smc_data.get('score', 0) < 50):
            leverage = max(self.config.min_leverage, leverage - 2)
        
        return leverage
    
    def _check_daily_reset(self):
        """Сбросить дневную статистику если новый день"""
        today = datetime.utcnow().date()
        if today != self.last_reset:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset = today
            print("📅 Daily stats reset")


# ============================================================================
# EXAMPLE
# ============================================================================

async def test_auto_trader():
    """Тест авто-трейдера"""
    config = TradeConfig(
        enabled=True,
        demo_mode=True,
        max_positions=3,
        risk_per_trade=0.015
    )
    
    trader = AutoTrader(config=config)
    
    # Проверяем можно ли открыть
    can_trade = await trader.can_open_position("BTCUSDT", "long", 78)
    print(f"Can trade: {can_trade}")
    
    # Получаем сводку
    summary = await trader.get_account_summary()
    print(f"Account summary: {summary}")
    
    await trader.bingx.close()


if __name__ == "__main__":
    asyncio.run(test_auto_trader())
