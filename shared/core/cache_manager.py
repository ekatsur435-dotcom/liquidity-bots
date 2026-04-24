"""
⚡ Phase 3: Performance Optimization — Cache Manager

Кэширование для ускорения:
- OB detection (5 минут TTL)
- Market data (30 сек TTL)
- Liquidity pool scan (2 минуты TTL)
"""

import time
import hashlib
import json
from typing import Optional, Dict, Any, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass
from functools import wraps


@dataclass
class CacheEntry:
    """Запись в кэше"""
    data: Any
    timestamp: float
    ttl_seconds: int
    
    def is_valid(self) -> bool:
        """Проверка валидности кэша"""
        return (time.time() - self.timestamp) < self.ttl_seconds


class CacheManager:
    """
    📦 Phase 3: Универсальный кэш-менеджер
    
    Поддерживает:
    - In-memory cache (быстрый)
    - Redis cache (между рестартами)
    - TTL для каждого ключа
    """
    
    # TTL по умолчанию для разных типов данных
    DEFAULT_TTL = {
        "order_block": 300,      # 5 минут
        "market_data": 30,       # 30 секунд
        "liquidity_pool": 120,   # 2 минуты
        "symbol_profile": 600,   # 10 минут
        "indicator": 60,         # 1 минута
    }
    
    def __init__(self, max_size: int = 1000):
        self.memory_cache: Dict[str, CacheEntry] = {}
        self.max_size = max_size
        self._hit_count = 0
        self._miss_count = 0
        
        # Пробуем подключить Redis
        try:
            import sys
            import os
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            from upstash.redis_client import get_redis_client
            self.redis = get_redis_client()
        except:
            self.redis = None
    
    def _make_key(self, prefix: str, *args, **kwargs) -> str:
        """Создание ключа из аргументов"""
        key_data = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
        hash_key = hashlib.md5(key_data.encode()).hexdigest()
        return f"cache:{prefix}:{hash_key}"
    
    def get(self, prefix: str, *args, **kwargs) -> Optional[Any]:
        """Получить из кэша"""
        key = self._make_key(prefix, *args, **kwargs)
        
        # Сначала проверяем память (быстрее)
        if key in self.memory_cache:
            entry = self.memory_cache[key]
            if entry.is_valid():
                self._hit_count += 1
                return entry.data
            else:
                # Устарело — удаляем
                del self.memory_cache[key]
        
        # Проверяем Redis
        if self.redis:
            try:
                data = self.redis.get(key)
                if data:
                    # Восстанавливаем в память
                    entry = CacheEntry(
                        data=json.loads(data),
                        timestamp=time.time(),
                        ttl_seconds=self.DEFAULT_TTL.get(prefix, 300)
                    )
                    self._store_in_memory(key, entry)
                    self._hit_count += 1
                    return entry.data
            except Exception:
                pass
        
        self._miss_count += 1
        return None
    
    def set(self, prefix: str, data: Any, *args, **kwargs) -> None:
        """Сохранить в кэш"""
        key = self._make_key(prefix, *args, **kwargs)
        ttl = self.DEFAULT_TTL.get(prefix, 300)
        
        # Сохраняем в память
        entry = CacheEntry(data=data, timestamp=time.time(), ttl_seconds=ttl)
        self._store_in_memory(key, entry)
        
        # Сохраняем в Redis
        if self.redis:
            try:
                self.redis.setex(key, ttl, json.dumps(data, default=str))
            except Exception:
                pass
    
    def _store_in_memory(self, key: str, entry: CacheEntry) -> None:
        """Сохранение в память с ограничением размера"""
        # Если кэш переполнен — чистим старые
        if len(self.memory_cache) >= self.max_size:
            # Удаляем 20% старых записей
            sorted_keys = sorted(
                self.memory_cache.keys(),
                key=lambda k: self.memory_cache[k].timestamp
            )
            for old_key in sorted_keys[:int(self.max_size * 0.2)]:
                del self.memory_cache[old_key]
        
        self.memory_cache[key] = entry
    
    def invalidate(self, prefix: str = None) -> int:
        """Инвалидация кэша"""
        if prefix:
            # Удаляем только с префиксом
            keys_to_remove = [k for k in self.memory_cache.keys() if k.startswith(f"cache:{prefix}:")]
            for key in keys_to_remove:
                del self.memory_cache[key]
            
            # Удаляем из Redis
            if self.redis:
                try:
                    redis_keys = self.redis.keys(f"cache:{prefix}:*")
                    if redis_keys:
                        self.redis.delete(*redis_keys)
                except:
                    pass
            
            return len(keys_to_remove)
        else:
            # Полная очистка
            count = len(self.memory_cache)
            self.memory_cache.clear()
            
            if self.redis:
                try:
                    keys = self.redis.keys("cache:*")
                    if keys:
                        self.redis.delete(*keys)
                except:
                    pass
            
            return count
    
    def get_stats(self) -> Dict:
        """Статистика кэша"""
        total_requests = self._hit_count + self._miss_count
        hit_rate = self._hit_count / total_requests if total_requests > 0 else 0
        
        return {
            "memory_entries": len(self.memory_cache),
            "hit_count": self._hit_count,
            "miss_count": self._miss_count,
            "hit_rate": round(hit_rate * 100, 2),
        }


# Декоратор для кэширования функций
def cached(prefix: str, ttl: int = None):
    """
    Декоратор для кэширования результатов функции
    
    Usage:
        @cached("order_block", ttl=300)
        def detect_order_blocks(ohlcv, symbol):
            ...
    """
    def decorator(func: Callable) -> Callable:
        cache = CacheManager()
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Пробуем получить из кэша
            result = cache.get(prefix, *args, **kwargs)
            if result is not None:
                return result
            
            # Вызываем функцию
            result = func(*args, **kwargs)
            
            # Сохраняем в кэш
            cache.set(prefix, result, *args, **kwargs)
            
            return result
        
        return wrapper
    return decorator


# Singleton
_cache_manager: Optional[CacheManager] = None


def get_cache_manager() -> CacheManager:
    """Get or create singleton CacheManager"""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = CacheManager()
    return _cache_manager
