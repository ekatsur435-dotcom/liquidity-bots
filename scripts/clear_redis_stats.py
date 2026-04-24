"""
🧹 Очистка статистики в Redis
Вариант A: Начать с чистого листа
"""

import os
import sys

# Добавляем путь к shared
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shared.core.redis_client import get_redis_short, get_redis_long

def clear_all_stats():
    """Очистить всю статистику торговли"""
    print("🧹 Очистка статистики Redis...")
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Удаляем all_trades
            all_trades_key = f"{prefix}:all_trades"
            result = redis.delete(all_trades_key)
            print(f"  ✅ {all_trades_key}: {'удалено' if result else 'не найдено'}")
            
            # Удаляем stats:daily:*
            pattern = f"{prefix}:stats:daily:*"
            keys = redis.keys(pattern) or []
            for key in keys:
                redis.delete(key)
            print(f"  ✅ {pattern}: удалено {len(keys)} ключей")
            
            # Удаляем micro_step:saved_trades
            micro_key = f"{prefix}:micro_step:saved_trades"
            result = redis.delete(micro_key)
            print(f"  ✅ {micro_key}: {'удалено' if result else 'не найдено'}")
            
            # Удаляем active_positions
            active_key = f"{prefix}:positions:active"
            result = redis.delete(active_key)
            print(f"  ✅ {active_key}: {'удалено' if result else 'не найдено'}")
            
            # Удаляем историю позиций
            history_keys = redis.keys(f"{prefix}:history:*") or []
            for key in history_keys:
                redis.delete(key)
            print(f"  ✅ {prefix}:history:*: удалено {len(history_keys)} ключей")
            
        except Exception as e:
            print(f"  ❌ {bot_name} ошибка: {e}")
    
    print("\n✅ Очистка завершена!")
    print("📊 Статистика сброшена. Новые сделки будут считаться с чистого листа.")

if __name__ == "__main__":
    confirm = input("⚠️  Это удалит ВСЮ историю сделок! Продолжить? (yes/no): ")
    if confirm.lower() in ["yes", "y"]:
        clear_all_stats()
    else:
        print("❌ Отменено.")
