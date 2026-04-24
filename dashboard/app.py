"""
📊 Phase 3 Dashboard — Web UI для анализа торговли

Запуск: python dashboard/app.py
URL: http://localhost:5000
"""

import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flask import Flask, render_template, jsonify, request
from flask_sock import Sock
from upstash_redis import Redis

app = Flask(__name__)
sock = Sock(app)


def get_redis_short():
    """Redis для SHORT бота"""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    from upstash_redis import Redis
    return Redis(url=url, token=token)


def get_redis_long():
    """Redis для LONG бота"""
    url = os.environ.get("UPSTASH_REDIS_LONG_URL", os.environ.get("UPSTASH_REDIS_REST_URL"))
    token = os.environ.get("UPSTASH_REDIS_LONG_TOKEN", os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""))
    from upstash_redis import Redis
    return Redis(url=url, token=token)


def get_trading_stats(days=7):
    """Получение статистики торговли за N дней (оба бота)"""
    stats = {
        "total_trades": 0,
        "win_count": 0,
        "loss_count": 0,
        "total_pnl": 0.0,
        "micro_step_saves": 0,
        "active_positions": 0,
        "trades": [],
        "short_trades": 0,
        "short_winrate": 0,
        "short_pnl": 0.0,
        "long_trades": 0,
        "long_winrate": 0,
        "long_pnl": 0.0
    }
    
    # Считываем из обоих ботов
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем all_trades для реальной статистики
            all_trades_key = f"{prefix}:all_trades"
            trades_data = redis.get(all_trades_key)
            if trades_data:
                trades = json.loads(trades_data)
                total = len(trades)
                wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
                losses = sum(1 for t in trades if t.get('pnl', 0) <= 0)
                pnl = sum(t.get('pnl', 0) for t in trades)
                
                stats["total_trades"] += total
                stats["win_count"] += wins
                stats["loss_count"] += losses
                stats["total_pnl"] += pnl
                
                if bot_name == "SHORT":
                    stats["short_trades"] = total
                    stats["short_winrate"] = round(wins / total * 100, 1) if total > 0 else 0
                    stats["short_pnl"] = round(pnl, 2)
                else:
                    stats["long_trades"] = total
                    stats["long_winrate"] = round(wins / total * 100, 1) if total > 0 else 0
                    stats["long_pnl"] = round(pnl, 2)
                    
            # Старый формат stats:daily как fallback
            for i in range(days):
                date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
                key = f"stats:daily:{date}"
                data = redis.get(key)
                if data:
                    day_stats = json.loads(data)
                    stats["total_trades"] += day_stats.get("trades", 0)
                    stats["win_count"] += day_stats.get("wins", 0)
                    stats["loss_count"] += day_stats.get("losses", 0)
                    stats["total_pnl"] += day_stats.get("pnl", 0)
                    
            # Micro-step saves
            saves_key = "micro_step:saved_trades"
            saves_data = redis.get(saves_key)
            if saves_data:
                saves = json.loads(saves_data)
                stats["micro_step_saves"] += len(saves)
                
            # Active positions
            active_key = "positions:active"
            active_data = redis.get(active_key)
            if active_data:
                active = json.loads(active_data)
                stats["active_positions"] += len(active)
                
        except Exception as e:
            print(f"Redis {bot_name} error: {e}")
    
    # Win rate
    total = stats["win_count"] + stats["loss_count"]
    stats["win_rate"] = round(stats["win_count"] / total * 100, 1) if total > 0 else 0
    stats["total_pnl"] = round(stats["total_pnl"], 2)
    
    return stats
    
    stats = {
        "total_trades": 0,
        "win_count": 0,
        "loss_count": 0,
        "total_pnl": 0.0,
        "micro_step_saves": 0,
        "avg_entry_improvement": 0.0,
        "trades": []
    }
    
    # Читаем сделки из Redis
    for i in range(days):
        date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        key = f"stats:daily:{date}"
        try:
            data = redis.get(key)
            if data:
                day_stats = json.loads(data)
                stats["total_trades"] += day_stats.get("trades", 0)
                stats["win_count"] += day_stats.get("wins", 0)
                stats["loss_count"] += day_stats.get("losses", 0)
                stats["total_pnl"] += day_stats.get("pnl", 0)
        except Exception as e:
            print(f"Error reading stats for {date}: {e}")
    
    return stats


def get_micro_trail_stats():
    """Статистика Micro-Step Trailing"""
    try:
        redis = get_redis_client()
        keys = redis.keys("trailing:*")
        
        active_count = len(keys) if keys else 0
        
        return {
            "active_positions": active_count,
            "trailing_enabled": True
        }
    except:
        return {"active_positions": 0, "trailing_enabled": False}


@app.route("/")
def index():
    """Главная страница"""
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """API: Статистика торговли"""
    stats = get_trading_stats(days=7)
    trail_stats = get_micro_trail_stats()
    
    return jsonify({
        **stats,
        **trail_stats,
        "win_rate": stats["win_count"] / max(1, stats["total_trades"]) * 100,
        "avg_pnl": stats["total_pnl"] / max(1, stats["total_trades"])
    })


@app.route("/api/saved_trades")
def api_saved_trades():
    """API: Сделки, спасенные Micro-Step Trailing"""
    # TODO: Чтение из backtest_results.json или Redis
    try:
        with open("../shared/analysis/backtest_results.json") as f:
            data = json.load(f)
            return jsonify(data.get("saved_trades", []))
    except:
        return jsonify([])


@app.route("/api/slippage")
def api_slippage():
    """API: Статистика проскальзывания"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from execution.limit_executor import get_slippage_tracker
        
        tracker = get_slippage_tracker()
        stats = tracker.get_stats(days=7)
        
        return jsonify({
            "avg_slippage_pct": stats.get("avg_slippage", 0),
            "total_records": stats.get("count", 0),
            "by_source": stats.get("by_source", {}),
            "limit_avg": stats.get("limit_avg", 0),
            "market_avg": stats.get("market_avg", 0),
            "recommended_micro_step": tracker.get_recommended_micro_step()
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/chart_data")
def api_chart_data():
    """API: Данные для графиков (P&L, Win Rate по дням)"""
    days = int(request.args.get('days', 7))
    
    try:
        redis = get_redis_client()
    except:
        return jsonify({"dates": [], "pnl": [], "win_rate": [], "trades": []})
    
    dates = []
    pnl_data = []
    win_rate_data = []
    trades_data = []
    
    for i in range(days-1, -1, -1):
        date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        key = f"stats:daily:{date}"
        
        try:
            data = redis.get(key)
            if data:
                day_stats = json.loads(data)
                dates.append(date[5:])  # MM-DD
                pnl_data.append(day_stats.get("pnl", 0))
                
                wins = day_stats.get("wins", 0)
                total = day_stats.get("trades", 0)
                win_rate = (wins / total * 100) if total > 0 else 0
                win_rate_data.append(round(win_rate, 1))
                trades_data.append(total)
            else:
                dates.append(date[5:])
                pnl_data.append(0)
                win_rate_data.append(0)
                trades_data.append(0)
        except:
            dates.append(date[5:])
            pnl_data.append(0)
            win_rate_data.append(0)
            trades_data.append(0)
    
    return jsonify({
        "dates": dates,
        "pnl": pnl_data,
        "win_rate": win_rate_data,
        "trades": trades_data
    })


# Health check для Render
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ⚡ WebSocket для real-time обновлений
@sock.route("/ws")
def ws_handler(ws):
    """WebSocket endpoint для real-time обновлений"""
    import asyncio
    
    while True:
        try:
            # Получаем актуальные данные
            stats = get_trading_stats(days=1)
            
            # Отправляем клиенту
            ws.send(json.dumps({
                "type": "update",
                "timestamp": datetime.utcnow().isoformat(),
                "data": stats
            }))
            
            # Ждем 5 секунд перед следующим обновлением
            import time
            time.sleep(5)
            
        except Exception as e:
            print(f"WebSocket error: {e}")
            break


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
