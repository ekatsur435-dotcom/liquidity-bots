"""
🎢 Micro-Step Trailing Stop v1.1 — Плавный трейлинг с Redis persistence

Проблема: Обычный трейлинг двигает стоп слишком далеко, выбивает сделки
Решение: Микро-шаги — двигать стоп чуть-чуть за каждым TP

Логика:
- После каждого TP сдвигаем стоп на SMALL_PCT (0.3-0.5%)
- Между TP не трогаем стоп
- При достижении TP2+ активируем "защитный" режим

Phase 3: Добавлена Redis persistence для сохранения состояния при перезапуске
- TRAIL_TP2_LOCK: 0.8 (0.8% в плюс после TP2)
- TRAIL_TP3_LOCK: 1.5 (1.5% в плюс после TP3)
"""

import os
import sys
from dataclasses import dataclass
from typing import Dict, Optional, List
from datetime import datetime

# ✅ FIX: импорт redis клиента (был не импортирован — NameError)
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from upstash.redis_client import get_redis_client
except ImportError:
    def get_redis_client():
        raise ImportError("upstash.redis_client not found")


@dataclass
class TrailingState:
    """Состояние трейлинга для позиции"""
    symbol: str
    direction: str  # "long" | "short"
    entry_price: float
    initial_sl: float
    current_sl: float
    taken_tps: int = 0
    max_tps: int = 6
    
    # Микро-шаги
    steps_taken: int = 0
    last_trail_at: Optional[datetime] = None
    total_sl_moved: float = 0.0  # Общее смещение SL в %


class MicroTrailingStop:
    """
    🎢 Микро-шаг трейлинг — плавное движение стопа
    
    Правила:
    1. После TP1: сдвинуть SL в безубыток + 0.3%
    2. После TP2: сдвинуть SL ещё +0.5% (total +0.8%)
    3. После TP3: сдвинуть SL ещё +0.7% (total +1.5%)
    4. Между TP — НЕ трогаем стоп!
    """
    
    # Настройки микро-шагов (% от цены входа)
    TP1_LOCK_PCT = float(os.getenv("TRAIL_TP1_LOCK", "0.3"))
    TP2_LOCK_PCT = float(os.getenv("TRAIL_TP2_LOCK", "0.8"))
    TP3_LOCK_PCT = float(os.getenv("TRAIL_TP3_LOCK", "1.5"))
    TP4_LOCK_PCT = float(os.getenv("TRAIL_TP4_LOCK", "2.5"))
    TP5_LOCK_PCT = float(os.getenv("TRAIL_TP5_LOCK", "4.0"))
    TP6_LOCK_PCT = float(os.getenv("TRAIL_TP6_LOCK", "6.0"))
    
    # Дополнительные настройки
    MICRO_STEP_PCT = float(os.getenv("TRAIL_MICRO_STEP_PCT", "0.3"))
    MIN_PROFIT_TO_TRAIL = float(os.getenv("MIN_PROFIT_TO_TRAIL", "0.5"))
    
    def __init__(self):
        self.states: Dict[str, TrailingState] = {}
        # Phase 3: Redis persistence
        try:
            self.redis = get_redis_client()
            self._restore_all_states()
        except Exception as e:
            print(f"⚠️ [MicroTrailingStop] Redis not available: {e}")
            self.redis = None
        print(f"🎢 MicroTrailingStop: "
              f"TP1={self.TP1_LOCK_PCT}%, TP2={self.TP2_LOCK_PCT}%, "
              f"TP3={self.TP3_LOCK_PCT}%")
    
    def get_state(self, symbol: str) -> Optional[TrailingState]:
        return self.states.get(symbol)
    
    def initialize(self, 
                   symbol: str,
                   direction: str,
                   entry_price: float,
                   initial_sl: float) -> TrailingState:
        """Инициализация трейлинга для новой позиции"""
        state = TrailingState(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            initial_sl=initial_sl,
            current_sl=initial_sl,
            taken_tps=0,
            max_tps=6
        )
        self.states[symbol] = state
        # Phase 3: Сохраняем в Redis
        self._save_state(symbol, state)
        print(f"🎢 [{symbol}] Trailing initialized: SL={initial_sl:.6f}")
        return state
    
    def on_tp_taken(self, 
                    symbol: str,
                    tp_level: int,
                    current_price: float) -> Optional[float]:
        """
        Вызывается при взятии TP — возвращает новый SL или None
        """
        state = self.states.get(symbol)
        if not state:
            print(f"⚠️ [{symbol}] No trailing state found")
            return None
        
        # Обновляем количество взятых TP
        state.taken_tps = max(state.taken_tps, tp_level)
        
        # Рассчитываем новый SL на основе TP
        new_sl = self._calculate_new_sl(state, tp_level)
        
        if new_sl and self._should_update_sl(state, new_sl):
            old_sl = state.current_sl
            state.current_sl = new_sl
            state.steps_taken += 1
            state.last_trail_at = datetime.utcnow()
            
            # Считаем общее смещение
            if state.direction == "long":
                move_pct = (new_sl - state.initial_sl) / state.entry_price * 100
            else:
                move_pct = (state.initial_sl - new_sl) / state.entry_price * 100
            state.total_sl_moved = move_pct
            
            # Phase 3: Сохраняем обновленное состояние в Redis
            self._save_state(symbol, state)
            
            print(f"🎢 [{symbol}] TRAIL after TP{tp_level}: "
                  f"{old_sl:.6f} → {new_sl:.6f} "
                  f"(+{move_pct:.2f}% from initial)")
            
            return new_sl
        
        return None
    
    def _calculate_new_sl(self, state: TrailingState, tp_level: int) -> Optional[float]:
        """Расчет нового SL на основе уровня TP"""
        entry = state.entry_price
        direction = state.direction
        
        # Выбираем % блокировки
        lock_pct = {
            1: self.TP1_LOCK_PCT,
            2: self.TP2_LOCK_PCT,
            3: self.TP3_LOCK_PCT,
            4: self.TP4_LOCK_PCT,
            5: self.TP5_LOCK_PCT,
            6: self.TP6_LOCK_PCT,
        }.get(tp_level, self.TP1_LOCK_PCT)
        
        if direction == "long":
            # Для LONG: SL ниже входа, двигаем вверх
            new_sl = entry * (1 + lock_pct / 100)
            # Не двигаем ниже текущего SL
            return max(new_sl, state.current_sl)
        else:
            # Для SHORT: SL выше входа, двигаем вниз
            new_sl = entry * (1 - lock_pct / 100)
            # Не двигаем выше текущего SL
            return min(new_sl, state.current_sl)
    
    def _should_update_sl(self, state: TrailingState, new_sl: float) -> bool:
        """Проверка, стоит ли обновлять SL"""
        direction = state.direction
        current = state.current_sl
        
        # Минимальный шаг
        min_step = state.entry_price * self.MICRO_STEP_PCT / 100
        
        if direction == "long":
            # Для LONG: новый SL должен быть выше текущего
            return new_sl > current + min_step * 0.5
        else:
            # Для SHORT: новый SL должен быть ниже текущего
            return new_sl < current - min_step * 0.5
    
    def check_early_exit(self,
                         symbol: str,
                         current_price: float) -> Optional[str]:
        """
        Проверка досрочного выхода (если цена ушла против нас)
        """
        state = self.states.get(symbol)
        if not state:
            return None
        
        direction = state.direction
        current_sl = state.current_sl
        
        if direction == "long":
            if current_price < current_sl:
                return "sl_hit"
        else:
            if current_price > current_sl:
                return "sl_hit"
        
        return None
    
    def remove(self, symbol: str):
        """Удаление состояния (при закрытии позиции)"""
        if symbol in self.states:
            del self.states[symbol]
        # Phase 3: Очистка из Redis
        if self.redis:
            try:
                self.redis.delete(f"trailing:{symbol}")
            except Exception as e:
                print(f"⚠️ [MicroTrailingStop] Redis delete error: {e}")
    
    def _save_state(self, symbol: str, state: TrailingState):
        """Phase 3: Сохранение состояния в Redis"""
        if not self.redis:
            return
        try:
            data = {
                "symbol": state.symbol,
                "direction": state.direction,
                "entry_price": state.entry_price,
                "initial_sl": state.initial_sl,
                "current_sl": state.current_sl,
                "taken_tps": state.taken_tps,
                "steps_taken": state.steps_taken,
                "total_sl_moved": state.total_sl_moved,
                "last_trail_at": state.last_trail_at.isoformat() if state.last_trail_at else None
            }
            self.redis.set(f"trailing:{symbol}", json.dumps(data), ex=86400)  # TTL 24h
        except Exception as e:
            print(f"⚠️ [MicroTrailingStop] Redis save error: {e}")
    
    def _restore_all_states(self):
        """Phase 3: Восстановление всех состояний из Redis"""
        if not self.redis:
            return
        try:
            # Получаем все ключи trailing:*
            keys = self.redis.keys("trailing:*")
            if not keys:
                return
            print(f"🎢 [MicroTrailingStop] Restoring {len(keys)} states from Redis...")
            for key in keys:
                try:
                    data = self.redis.get(key)
                    if data:
                        state_dict = json.loads(data)
                        symbol = state_dict["symbol"]
                        state = TrailingState(
                            symbol=symbol,
                            direction=state_dict["direction"],
                            entry_price=state_dict["entry_price"],
                            initial_sl=state_dict["initial_sl"],
                            current_sl=state_dict["current_sl"],
                            taken_tps=state_dict.get("taken_tps", 0),
                            steps_taken=state_dict.get("steps_taken", 0),
                            total_sl_moved=state_dict.get("total_sl_moved", 0.0),
                            last_trail_at=datetime.fromisoformat(state_dict["last_trail_at"]) if state_dict.get("last_trail_at") else None
                        )
                        self.states[symbol] = state
                except Exception as e:
                    print(f"⚠️ [MicroTrailingStop] Failed to restore state for {key}: {e}")
            print(f"🎢 [MicroTrailingStop] Restored {len(self.states)} states")
        except Exception as e:
            print(f"⚠️ [MicroTrailingStop] Redis restore error: {e}")
    
    def get_summary(self, symbol: str) -> Dict:
        """Сводка по трейлингу позиции"""
        state = self.states.get(symbol)
        if not state:
            return {}
        
        return {
            "taken_tps": state.taken_tps,
            "steps_taken": state.steps_taken,
            "initial_sl": state.initial_sl,
            "current_sl": state.current_sl,
            "total_moved_pct": state.total_sl_moved,
            "last_trail": state.last_trail_at.isoformat() if state.last_trail_at else None
        }


# Singleton instance
_trailing_instance: Optional[MicroTrailingStop] = None


def get_micro_trailing() -> MicroTrailingStop:
    global _trailing_instance
    if _trailing_instance is None:
        _trailing_instance = MicroTrailingStop()
    return _trailing_instance
