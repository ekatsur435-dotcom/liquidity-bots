"""
📊 Анализ сделок из Redis
Вариант B: Понять паттерн плохих сделок
"""

import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Импортируем Redis клиент как в dashboard
from upstash_redis import Redis
import os

def get_redis_short():
    """Redis для SHORT бота"""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    return Redis(url=url, token=token)

def get_redis_long():
    """Redis для LONG бота"""
    url = os.environ.get("UPSTASH_REDIS_LONG_URL", os.environ.get("UPSTASH_REDIS_REST_URL"))
    token = os.environ.get("UPSTASH_REDIS_LONG_TOKEN", os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""))
    return Redis(url=url, token=token)

def analyze_trades(hours=24):
    """Анализ сделок за последние N часов"""
    print(f"📊 Анализ сделок за последние {hours} часов\n")
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем all_trades
            all_trades_key = f"{prefix}:all_trades"
            list_len = redis.llen(all_trades_key) or 0
            
            if list_len == 0:
                print(f"🔴 {bot_name}: Нет сделок")
                continue
            
            trades_json = redis.lrange(all_trades_key, -100, -1)  # Последние 100
            trades = []
            for t in trades_json:
                try:
                    trades.append(json.loads(t))
                except:
                    pass
            
            # Фильтруем по времени
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            recent_trades = [
                t for t in trades 
                if datetime.fromisoformat(t.get('close_time', '2000-01-01').replace('Z', '+00:00').replace('+00:00', '')) > cutoff
            ]
            
            if not recent_trades:
                print(f"🔴 {bot_name}: Нет сделок за последние {hours}ч")
                continue
            
            print(f"\n📈 {bot_name} БОТ ({len(recent_trades)} сделок за {hours}ч):")
            print("=" * 60)
            
            # Статистика
            wins = [t for t in recent_trades if t.get('pnl', 0) > 0]
            losses = [t for t in recent_trades if t.get('pnl', 0) <= 0]
            
            total_pnl = sum(t.get('pnl', 0) for t in recent_trades)
            win_rate = len(wins) / len(recent_trades) * 100 if recent_trades else 0
            
            print(f"  Всего: {len(recent_trades)} | Побед: {len(wins)} ({win_rate:.1f}%) | Проигрышей: {len(losses)}")
            print(f"  Общий P&L: {total_pnl:+.2f}%")
            
            # Средний выигрыш/проигрыш
            if wins:
                avg_win = sum(t.get('pnl', 0) for t in wins) / len(wins)
                print(f"  Средний выигрыш: +{avg_win:.2f}%")
            if losses:
                avg_loss = sum(t.get('pnl', 0) for t in losses) / len(losses)
                print(f"  Средний проигрыш: {avg_loss:.2f}%")
            
            # Статистика по типу закрытия
            close_types = defaultdict(list)
            for t in recent_trades:
                close_type = t.get('close_type', 'unknown')
                close_types[close_type].append(t)
            
            print(f"\n  По типу закрытия:")
            for close_type, trades_list in close_types.items():
                pnl = sum(t.get('pnl', 0) for t in trades_list)
                print(f"    {close_type}: {len(trades_list)} сделок, P&L: {pnl:+.2f}%")
            
            # Топ-5 худших сделок
            print(f"\n  🔴 ТОП-5 ХУДШИХ:")
            worst = sorted(recent_trades, key=lambda x: x.get('pnl', 0))[:5]
            for t in worst:
                symbol = t.get('symbol', '?')
                pnl = t.get('pnl', 0)
                close = t.get('close_type', '?')
                print(f"    {symbol}: {pnl:+.2f}% ({close})")
            
            # Топ-5 лучших сделок
            print(f"\n  🟢 ТОП-5 ЛУЧШИХ:")
            best = sorted(recent_trades, key=lambda x: x.get('pnl', 0), reverse=True)[:5]
            for t in best:
                symbol = t.get('symbol', '?')
                pnl = t.get('pnl', 0)
                close = t.get('close_type', '?')
                print(f"    {symbol}: {pnl:+.2f}% ({close})")
            
            # Проблемные монеты (2+ стопа подряд)
            print(f"\n  ⚠️  МОНЕТЫ С ПРОБЛЕМАМИ:")
            symbol_losses = defaultdict(int)
            for t in recent_trades:
                if t.get('pnl', 0) < 0:
                    symbol_losses[t.get('symbol', '?')] += 1
            
            for symbol, count in sorted(symbol_losses.items(), key=lambda x: -x[1]):
                if count >= 2:
                    print(f"    {symbol}: {count} лосса")
            
        except Exception as e:
            print(f"  ❌ {bot_name} ошибка: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("📊 Анализ завершён!")

if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    analyze_trades(hours)
