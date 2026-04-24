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

from flask import Flask, render_template, jsonify
from upstash.redis_client import get_redis_client

app = Flask(__name__)


def get_trading_stats(days=7):
    """Получение статистики торговли за N дней"""
    try:
        redis = get_redis_client()
    except Exception as e:
        print(f"Redis error: {e}")
        return {}
    
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
