"""
Upstash Redis Client для Dual Bot System
Бесплатный tier: 10,000 запросов/день, 256MB
"""

import os
import json
import redis
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
from dataclasses import asdict


class UpstashRedisClient:
    """Клиент для Upstash Redis (бесплатный tier)"""
    
    def __init__(self, redis_url: Optional[str] = None):
        """
        Инициализация клиента
        
        Args:
            redis_url: Redis URL из Upstash (redis://default:pass@host:port)
                    Если None, берётся из переменной окружения REDIS_URL
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        if not self.redis_url:
            raise ValueError("REDIS_URL not provided and not in environment")
        
        # SSL detection from URL scheme (rediss:// = SSL)
        use_ssl = self.redis_url.startswith("rediss://")
        
        conn_kwargs = {
            "decode_responses": True,
            "socket_timeout": 5,
            "socket_connect_timeout": 5
        }
        
        if use_ssl:
            conn_kwargs["ssl_cert_reqs"] = None  # Disable cert verification for Upstash
        
        self.client = redis.from_url(self.redis_url, **conn_kwargs)
        
        # TTL для разных типов данных (в секундах)
        self.TTL = {
            "signal": 86400,        # 24 часа для сигналов
            "position": 604800,     # 7 дней для позиций
            "state": 3600,          # 1 час для состояния
            "stats": 2592000,       # 30 дней для статистики
            "cache": 300            # 5 минут для кэша API
        }
    
    def health_check(self) -> bool:
        """Проверка соединения с Redis"""
        try:
            return self.client.ping()
        except Exception as e:
            print(f"Redis health check failed: {e}")
            return False
    
    # =========================================================================
    # SIGNALS (Сигналы)
    # =========================================================================
    
    def save_signal(self, bot_type: str, symbol: str, signal_data: Dict) -> bool:
        """
        Сохранить сигнал в Redis
        
        Args:
            bot_type: 'short' или 'long'
            symbol: Торговая пара (BTCUSDT)
            signal_data: Данные сигнала
        """
        try:
            key = f"{bot_type}:signals:{symbol}"
            
            # Добавляем timestamp если нет
            if "timestamp" not in signal_data:
                signal_data["timestamp"] = datetime.utcnow().isoformat()
            
            # Сохраняем как JSON в список (LPUSH - в начало)
            self.client.lpush(key, json.dumps(signal_data))
            
            # Устанавливаем TTL
            self.client.expire(key, self.TTL["signal"])
            
            # Ограничиваем историю (оставляем последние 50)
            self.client.ltrim(key, 0, 49)
            
            return True
        except Exception as e:
            print(f"Error saving signal: {e}")
            return False
    
    def get_signals(self, bot_type: str, symbol: str, limit: int = 10) -> List[Dict]:
        """Получить сигналы для пары"""
        try:
            key = f"{bot_type}:signals:{symbol}"
            signals = self.client.lrange(key, 0, limit - 1)
            return [json.loads(s) for s in signals]
        except Exception as e:
            print(f"Error getting signals: {e}")
            return []
    
    def get_active_signals(self, bot_type: str) -> List[Dict]:
        """Получить все активные сигналы (не expired)"""
        try:
            pattern = f"{bot_type}:signals:*"
            keys = self.client.keys(pattern)
            
            active_signals = []
            for key in keys:
                signals = self.client.lrange(key, 0, 0)  # Последний сигнал
                if signals:
                    signal = json.loads(signals[0])
                    # Проверяем не expired ли
                    if signal.get("status") == "active":
                        signal["symbol"] = key.split(":")[-1]
                        active_signals.append(signal)
            
            return active_signals
        except Exception as e:
            print(f"Error getting active signals: {e}")
            return []
    
    def update_signal_status(self, bot_type: str, symbol: str, 
                            timestamp: str, new_status: str) -> bool:
        """Обновить статус сигнала (active → executed/expired)"""
        try:
            key = f"{bot_type}:signals:{symbol}"
            signals = self.client.lrange(key, 0, -1)
            
            for i, signal_json in enumerate(signals):
                signal = json.loads(signal_json)
                if signal.get("timestamp") == timestamp:
                    signal["status"] = new_status
                    signal["updated_at"] = datetime.utcnow().isoformat()
                    self.client.lset(key, i, json.dumps(signal))
                    return True
            
            return False
        except Exception as e:
            print(f"Error updating signal status: {e}")
            return False
    
    # =========================================================================
    # POSITIONS (Позиции для авто-торговли)
    # =========================================================================
    
    def save_position(self, bot_type: str, symbol: str, position_data: Dict) -> bool:
        """Сохранить открытую позицию"""
        try:
            key = f"{bot_type}:positions:{symbol}"
            self.client.setex(
                key,
                self.TTL["position"],
                json.dumps(position_data)
            )
            return True
        except Exception as e:
            print(f"Error saving position: {e}")
            return False
    
    def get_position(self, bot_type: str, symbol: str) -> Optional[Dict]:
        """Получить позицию по символу"""
        try:
            key = f"{bot_type}:positions:{symbol}"
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            print(f"Error getting position: {e}")
            return None
    
    def get_all_positions(self, bot_type: str) -> List[Dict]:
        """Получить все позиции бота"""
        try:
            pattern = f"{bot_type}:positions:*"
            keys = self.client.keys(pattern)
            
            positions = []
            for key in keys:
                data = self.client.get(key)
                if data:
                    pos = json.loads(data)
                    pos["symbol"] = key.split(":")[-1]
                    positions.append(pos)
            
            return positions
        except Exception as e:
            print(f"Error getting positions: {e}")
            return []
    
    def close_position(self, bot_type: str, symbol: str, 
                      pnl: float, close_price: float) -> bool:
        """Закрыть позицию и сохранить результат"""
        try:
            key = f"{bot_type}:positions:{symbol}"
            data = self.client.get(key)
            
            if data:
                position = json.loads(data)
                position["status"] = "closed"
                position["close_price"] = close_price
                position["pnl"] = pnl
                position["closed_at"] = datetime.utcnow().isoformat()
                
                # Сохраняем в историю
                history_key = f"{bot_type}:history:{symbol}"
                self.client.lpush(history_key, json.dumps(position))
                self.client.ltrim(history_key, 0, 99)  # Последние 100
                
                # Удаляем из активных
                self.client.delete(key)
                
                return True
            
            return False
        except Exception as e:
            print(f"Error closing position: {e}")
            return False
    
    # =========================================================================
    # BOT STATE (Состояние бота)
    # =========================================================================
    
    def update_bot_state(self, bot_type: str, state_data: Dict) -> bool:
        """Обновить состояние бота"""
        try:
            key = f"{bot_type}:bot:state"
            state_data["updated_at"] = datetime.utcnow().isoformat()
            
            self.client.setex(
                key,
                self.TTL["state"],
                json.dumps(state_data)
            )
            return True
        except Exception as e:
            print(f"Error updating bot state: {e}")
            return False
    
    def get_bot_state(self, bot_type: str) -> Optional[Dict]:
        """Получить текущее состояние бота"""
        try:
            key = f"{bot_type}:bot:state"
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            print(f"Error getting bot state: {e}")
            return None
    
    # =========================================================================
    # STATISTICS (Статистика)
    # =========================================================================
    
    def update_daily_stats(self, bot_type: str, date: str, stats: Dict) -> bool:
        """Обновить дневную статистику"""
        try:
            key = f"{bot_type}:stats:daily:{date}"
            
            self.client.setex(
                key,
                self.TTL["stats"],
                json.dumps(stats)
            )
            return True
        except Exception as e:
            print(f"Error updating stats: {e}")
            return False
    
    def get_daily_stats(self, bot_type: str, date: str) -> Optional[Dict]:
        """Получить статистику за день"""
        try:
            key = f"{bot_type}:stats:daily:{date}"
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            print(f"Error getting stats: {e}")
            return None
    
    def get_stats_range(self, bot_type: str, days: int = 30) -> List[Dict]:
        """Получить статистику за период"""
        try:
            stats = []
            for i in range(days):
                date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
                day_stats = self.get_daily_stats(bot_type, date)
                if day_stats:
                    day_stats["date"] = date
                    stats.append(day_stats)
            return stats
        except Exception as e:
            print(f"Error getting stats range: {e}")
            return []
    
    # =========================================================================
    # API CACHE (Кэширование запросов к биржам)
    # =========================================================================
    
    def cache_set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Сохранить в кэш"""
        try:
            ttl = ttl or self.TTL["cache"]
            self.client.setex(key, ttl, json.dumps(value))
            return True
        except Exception as e:
            print(f"Error setting cache: {e}")
            return False
    
    def cache_get(self, key: str) -> Optional[Any]:
        """Получить из кэша"""
        try:
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            print(f"Error getting cache: {e}")
            return None
    
    # =========================================================================
    # CROSS-BOT SYNC (Синхронизация между ботами)
    # =========================================================================
    
    def check_opposite_signal(self, symbol: str, bot_type: str) -> Optional[Dict]:
        """
        Проверить есть ли противоположный сигнал
        (если SHORT активен, а LONG даёт сигнал — конфликт)
        """
        try:
            opposite = "long" if bot_type == "short" else "short"
            return self.get_active_signals(opposite)
        except Exception as e:
            print(f"Error checking opposite signal: {e}")
            return None
    
    def get_shared_market_data(self, symbol: str) -> Optional[Dict]:
        """Получить общие рыночные данные (оба бота видят одно и то же)"""
        try:
            key = f"shared:market:{symbol}"
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            print(f"Error getting shared market data: {e}")
            return None
    
    def set_shared_market_data(self, symbol: str, data: Dict) -> bool:
        """Сохранить общие рыночные данные"""
        try:
            key = f"shared:market:{symbol}"
            self.client.setex(key, 60, json.dumps(data))  # 1 минута
            return True
        except Exception as e:
            print(f"Error setting shared market data: {e}")
            return False
    
    # =========================================================================
    # RATE LIMITING (Ограничение запросов)
    # =========================================================================
    
    def check_rate_limit(self, action: str, max_requests: int = 10, 
                        window: int = 60) -> bool:
        """
        Проверить не превышен ли rate limit
        
        Args:
            action: Тип действия (telegram, bybit, scan)
            max_requests: Максимум запросов
            window: Окно времени в секундах
        
        Returns:
            True если можно продолжать, False если лимит превышен
        """
        try:
            key = f"ratelimit:{action}:{datetime.utcnow().strftime('%Y%m%d%H%M')}"
            current = self.client.incr(key)
            
            if current == 1:
                # Устанавливаем TTL для нового ключа
                self.client.expire(key, window)
            
            return current <= max_requests
        except Exception as e:
            print(f"Error checking rate limit: {e}")
            return True  # В случае ошибки — пропускаем
    
    # =========================================================================
    # INFO & METRICS
    # =========================================================================
    
    def get_info(self) -> Dict:
        """Получить информацию о Redis"""
        try:
            info = self.client.info()
            return {
                "used_memory": info.get("used_memory_human", "unknown"),
                "connected_clients": info.get("connected_clients", 0),
                "uptime": info.get("uptime_in_seconds", 0),
                "version": info.get("redis_version", "unknown")
            }
        except Exception as e:
            print(f"Error getting Redis info: {e}")
            return {}
    
    def get_memory_usage(self) -> Dict:
        """Получить использование памяти (важно для бесплатного tier)"""
        try:
            info = self.client.info("memory")
            used = info.get("used_memory", 0)
            peak = info.get("used_memory_peak", 0)
            
            # Upstash free: 256MB limit
            limit = 256 * 1024 * 1024  # 256MB in bytes
            
            return {
                "used_bytes": used,
                "used_mb": round(used / 1024 / 1024, 2),
                "peak_bytes": peak,
                "peak_mb": round(peak / 1024 / 1024, 2),
                "limit_mb": 256,
                "usage_percent": round(used / limit * 100, 2)
            }
        except Exception as e:
            print(f"Error getting memory usage: {e}")
            return {}


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

_redis_client = None

def get_redis_client() -> UpstashRedisClient:
    """Получить singleton instance Redis клиента"""
    global _redis_client
    if _redis_client is None:
        _redis_client = UpstashRedisClient()
    return _redis_client


# ============================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ
# ============================================================================

if __name__ == "__main__":
    # Пример использования
    redis = UpstashRedisClient()
    
    # Проверка соединения
    if redis.health_check():
        print("✅ Redis connection OK")
    else:
        print("❌ Redis connection failed")
    
    # Сохранение сигнала
    signal = {
        "score": 78,
        "price": 73500.0,
        "pattern": "MEGA_SHORT",
        "status": "active"
    }
    redis.save_signal("short", "BTCUSDT", signal)
    
    # Получение сигналов
    signals = redis.get_signals("short", "BTCUSDT")
    print(f"Signals: {signals}")
    
    # Информация о памяти
    mem = redis.get_memory_usage()
    print(f"Memory usage: {mem['usage_percent']}% ({mem['used_mb']}MB / 256MB)")
