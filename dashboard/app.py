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


# Кэш для статистики
_stats_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 30  # секунды

def get_trading_stats(days=7):
    """Получение статистики торговли за N дней (оба бота) с кэшированием"""
    global _stats_cache
    
    # Проверяем кэш
    now = datetime.utcnow().timestamp()
    if _stats_cache["data"] and (now - _stats_cache["timestamp"]) < CACHE_TTL:
        return _stats_cache["data"]
    
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
    
    # Считываем из обоих ботов - только последние 50 сделок для скорости
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем all_trades как JSON (ReJSON-RL тип)
            all_trades_key = f"{prefix}:all_trades"
            try:
                # Получаем JSON массив целиком
                # Upstash Redis 1.0.0+: используем execute() с массивом
                result = redis.execute(["JSON.GET", all_trades_key, "$"])
                # execute возвращает строку JSON, парсим её
                trades_data = None
                if result:
                    try:
                        parsed = json.loads(result) if isinstance(result, str) else result
                        # JSON.GET с $ возвращает список с одним элементом
                        if isinstance(parsed, list) and len(parsed) > 0:
                            trades_data = parsed[0]
                        else:
                            trades_data = parsed
                    except:
                        pass
                if trades_data and isinstance(trades_data, list):
                    # Берем только последние 50 сделок
                    trades = trades_data[-50:] if len(trades_data) > 50 else trades_data
                    
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
            except Exception as e:
                print(f"Error reading all_trades for {bot_name}: {e}")
                    
            # Micro-step saves (JSON)
            try:
                saves_key = f"{prefix}:micro_step:saved_trades"
                result = redis.execute(["JSON.GET", saves_key, "$"])
                saves_data = None
                if result:
                    try:
                        parsed = json.loads(result) if isinstance(result, str) else result
                        if isinstance(parsed, list) and len(parsed) > 0:
                            saves_data = parsed[0]
                        else:
                            saves_data = parsed
                    except:
                        pass
                if saves_data and isinstance(saves_data, list):
                    stats["micro_step_saves"] += len(saves_data)
            except:
                pass
                
            # Active positions - из bot_state (не используем keys)
            try:
                result = redis.execute(["JSON.GET", f"{prefix}:bot_state", "$"])
                bot_state = None
                if result:
                    try:
                        parsed = json.loads(result) if isinstance(result, str) else result
                        if isinstance(parsed, list) and len(parsed) > 0:
                            bot_state = parsed[0]
                        else:
                            bot_state = parsed
                    except:
                        pass
                if bot_state and isinstance(bot_state, dict):
                    stats["active_positions"] += bot_state.get("active_signals", 0)
            except:
                pass
                
        except Exception as e:
            print(f"Redis {bot_name} error: {e}")
    
    # Win rate
    total = stats["win_count"] + stats["loss_count"]
    stats["win_rate"] = round(stats["win_count"] / total * 100, 1) if total > 0 else 0
    stats["total_pnl"] = round(stats["total_pnl"], 2)
    
    # Сохраняем в кэш
    _stats_cache["data"] = stats
    _stats_cache["timestamp"] = datetime.utcnow().timestamp()
    
    return stats


def get_micro_trail_stats():
    """Статистика Micro-Step Trailing"""
    total_active = 0
    for redis_getter in [get_redis_short, get_redis_long]:
        try:
            redis = redis_getter()
            # Подсчитываем trailing из bot_state (не используем keys)
            for pfx in ["short", "long"]:
                try:
                    result = redis.execute(["JSON.GET", f"{pfx}:bot_state", "$"])
                    bot_state = None
                    if result:
                        try:
                            parsed = json.loads(result) if isinstance(result, str) else result
                            if isinstance(parsed, list) and len(parsed) > 0:
                                bot_state = parsed[0]
                            else:
                                bot_state = parsed
                        except:
                            pass
                    if bot_state and isinstance(bot_state, dict):
                        total_active += bot_state.get("active_signals", 0)
                except:
                    pass
        except:
            pass
    
    return {
        "active_positions": total_active,
        "trailing_enabled": True
    }


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
    
    dates = []
    pnl_data = []
    win_rate_data = []
    trades_data = []
    
    for i in range(days-1, -1, -1):
        date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        
        # Агрегируем данные из обоих ботов
        day_pnl = 0
        day_wins = 0
        day_losses = 0
        day_trades = 0
        
        for redis_getter in [get_redis_short, get_redis_long]:
            try:
                redis = redis_getter()
                # Пробуем новый формат stats:daily с префиксом
                for prefix in ["short", "long"]:
                    key = f"{prefix}:stats:daily:{date}"
                    try:
                        data = redis.get(key)
                        if data:
                            day_stats = json.loads(data)
                            day_pnl += day_stats.get("pnl", 0)
                            day_wins += day_stats.get("wins", 0)
                            day_losses += day_stats.get("losses", 0)
                            day_trades += day_stats.get("trades", 0)
                    except:
                        pass
            except:
                pass
        
        dates.append(date[5:])  # MM-DD
        pnl_data.append(round(day_pnl, 2))
        win_rate = (day_wins / day_trades * 100) if day_trades > 0 else 0
        win_rate_data.append(round(win_rate, 1))
        trades_data.append(day_trades)
    
    return jsonify({
        "dates": dates,
        "pnl": pnl_data,
        "win_rate": win_rate_data,
        "trades": trades_data
    })


@app.route("/api/trades")
def api_trades():
    """API: Последние 5 сделок SHORT и LONG с деталями"""
    trades = {"short": [], "long": []}
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем all_trades
            result = redis.execute(["JSON.GET", f"{prefix}:all_trades", "$"])
            if result:
                try:
                    parsed = json.loads(result) if isinstance(result, str) else result
                    if isinstance(parsed, list) and len(parsed) > 0:
                        all_trades = parsed[0]
                    else:
                        all_trades = parsed
                    
                    if isinstance(all_trades, list):
                        # Берём последние 5, сортируем по времени (если есть timestamp)
                        recent = sorted(all_trades, key=lambda x: x.get('timestamp', ''), reverse=True)[:5]
                        trades[prefix] = recent
                except:
                    pass
        except Exception as e:
            print(f"Error reading trades for {bot_name}: {e}")
    
    return jsonify(trades)


@app.route("/api/active_positions")
def api_active_positions():
    """API: Активные позиции с текущим P&L"""
    positions = []
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем active_signals из bot_state
            result = redis.execute(["JSON.GET", f"{prefix}:bot_state", "$"])
            if result:
                try:
                    parsed = json.loads(result) if isinstance(result, str) else result
                    if isinstance(parsed, list) and len(parsed) > 0:
                        bot_state = parsed[0]
                    else:
                        bot_state = parsed
                    
                    if isinstance(bot_state, dict):
                        active = bot_state.get("active_signals", [])
                        for pos in active:
                            positions.append({
                                "symbol": pos.get("symbol", "UNKNOWN"),
                                "direction": prefix,
                                "entry": pos.get("entry_price", 0),
                                "current_pnl": pos.get("current_pnl", 0),
                                "tp": pos.get("take_profits", [[]])[0] if pos.get("take_profits") else 0,
                                "sl": pos.get("stop_loss", 0),
                                "duration_min": pos.get("duration_min", 0),
                                "taken_tps": len(pos.get("taken_tps", []))
                            })
                except:
                    pass
        except Exception as e:
            print(f"Error reading positions for {bot_name}: {e}")
    
    return jsonify({"positions": positions, "count": len(positions)})


@app.route("/api/feed")
def api_feed():
    """API: Live feed последних событий (TBS, входы, TP/SL)"""
    events = []
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем recent_events (если есть)
            result = redis.execute(["JSON.GET", f"{prefix}:recent_events", "$"])
            if result:
                try:
                    parsed = json.loads(result) if isinstance(result, str) else result
                    if isinstance(parsed, list) and len(parsed) > 0:
                        events_data = parsed[0]
                    else:
                        events_data = parsed
                    
                    if isinstance(events_data, list):
                        for ev in events_data[-10:]:  # Последние 10
                            events.append({
                                "type": ev.get("type", "info"),  # tbs, entry, tp, sl
                                "symbol": ev.get("symbol", ""),
                                "direction": prefix,
                                "message": ev.get("message", ""),
                                "timestamp": ev.get("timestamp", ""),
                                "price": ev.get("price", 0),
                                "pnl": ev.get("pnl", None)
                            })
                except:
                    pass
            
            # Также проверяем recent_trades для TP/SL событий
            result = redis.execute(["JSON.GET", f"{prefix}:all_trades", "$"])
            if result:
                try:
                    parsed = json.loads(result) if isinstance(result, str) else result
                    if isinstance(parsed, list) and len(parsed) > 0:
                        trades = parsed[0]
                    else:
                        trades = parsed
                    
                    if isinstance(trades, list):
                        for t in trades[-5:]:
                            if t.get("exit_reason") in ["TP1", "TP2", "TP3", "SL"]:
                                events.append({
                                    "type": t.get("exit_reason", "").lower(),
                                    "symbol": t.get("symbol", ""),
                                    "direction": prefix,
                                    "message": f"{t.get('exit_reason')} @ {t.get('exit_price', 0)}",
                                    "timestamp": t.get("exit_time", ""),
                                    "price": t.get("exit_price", 0),
                                    "pnl": t.get("pnl", 0)
                                })
                except:
                    pass
                    
        except Exception as e:
            print(f"Error reading feed for {bot_name}: {e}")
    
    # Сортируем по времени (новые сверху)
    events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify({"events": events[:15]})


@app.route("/api/summary")
def api_summary():
    """API: Сводка P&L за сегодня, вчера, неделю"""
    summary = {
        "today": {"pnl": 0, "trades": 0, "winrate": 0},
        "yesterday": {"pnl": 0, "trades": 0, "winrate": 0},
        "week": {"pnl": 0, "trades": 0, "winrate": 0}
    }
    
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            result = redis.execute(["JSON.GET", f"{prefix}:all_trades", "$"])
            if result:
                try:
                    parsed = json.loads(result) if isinstance(result, str) else result
                    if isinstance(parsed, list) and len(parsed) > 0:
                        trades = parsed[0]
                    else:
                        trades = parsed
                    
                    if isinstance(trades, list):
                        for t in trades:
                            trade_date = t.get("exit_time", "")[:10] if t.get("exit_time") else ""
                            pnl = t.get("pnl", 0)
                            is_win = pnl > 0
                            
                            # Week
                            summary["week"]["pnl"] += pnl
                            summary["week"]["trades"] += 1
                            if is_win:
                                summary["week"]["wins"] = summary["week"].get("wins", 0) + 1
                            
                            # Today
                            if trade_date == today:
                                summary["today"]["pnl"] += pnl
                                summary["today"]["trades"] += 1
                                if is_win:
                                    summary["today"]["wins"] = summary["today"].get("wins", 0) + 1
                            
                            # Yesterday
                            if trade_date == yesterday:
                                summary["yesterday"]["pnl"] += pnl
                                summary["yesterday"]["trades"] += 1
                                if is_win:
                                    summary["yesterday"]["wins"] = summary["yesterday"].get("wins", 0) + 1
                except:
                    pass
        except Exception as e:
            print(f"Error reading summary for {bot_name}: {e}")
    
    # Calculate winrates
    for period in ["today", "yesterday", "week"]:
        total = summary[period]["trades"]
        wins = summary[period].get("wins", 0)
        summary[period]["winrate"] = round(wins / total * 100, 1) if total > 0 else 0
    
    return jsonify(summary)


# Health check для Render
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
