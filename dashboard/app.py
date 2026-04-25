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
            
            all_trades_key = f"{prefix}:all_trades"
            try:
                # Читаем all_trades как LIST (не JSON!)
                # Боты сохраняют через LPUSH в redis_client.py
                try:
                    trades_json = redis.execute(["LRANGE", all_trades_key, "0", "49"])
                    trades_data = [json.loads(t) for t in trades_json] if trades_json else []
                except Exception as e:
                    print(f"Error reading list for {bot_name}: {e}")
                    trades_data = []
                
                if trades_data:
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
                    
            # Micro-step saves (LIST)
            try:
                saves_key = f"{prefix}:micro_step:saved_trades"
                saves_json = redis.execute(["LRANGE", saves_key, "0", "-1"])
                if saves_json:
                    stats["micro_step_saves"] += len(saves_json)
            except:
                pass
                
            # Active positions - из state (STRING)
            try:
                state_data = redis.execute(["GET", f"{prefix}:state"])
                if state_data:
                    bot_state = json.loads(state_data)
                    stats["active_positions"] += len(bot_state.get("active_positions", []))
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
            # Подсчитываем trailing из state (STRING)
            for pfx in ["short", "long"]:
                try:
                    state_data = redis.execute(["GET", f"{pfx}:state"])
                    if state_data:
                        bot_state = json.loads(state_data)
                        total_active += len(bot_state.get("active_positions", []))
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
            
            # Читаем all_trades как LIST
            try:
                trades_json = redis.execute(["LRANGE", f"{prefix}:all_trades", "0", "4"])
                if trades_json:
                    all_trades = [json.loads(t) for t in trades_json]
                    trades[prefix] = all_trades[:5]
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
            
            # Читаем активные позиции из positions:*
            try:
                # Получаем все ключи позиций
                position_keys = redis.execute(["KEYS", f"{prefix}:positions:*"])
                if position_keys:
                    for key in position_keys[:10]:  # Максимум 10
                        pos_data = redis.execute(["GET", key])
                        if pos_data:
                            pos = json.loads(pos_data)
                            symbol = key.split(":")[-1]
                            positions.append({
                                "symbol": symbol,
                                "direction": prefix,
                                "entry": pos.get("entry_price", 0),
                                "current_pnl": pos.get("pnl", 0),
                                "tp": pos.get("take_profit", 0),
                                "sl": pos.get("stop_loss", 0),
                                "duration_min": pos.get("duration_min", 0),
                                "taken_tps": pos.get("partial_exits", 0)
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
            
            # Читаем историю (LIST) для событий TP/SL
            try:
                history_key = f"{prefix}:history"
                history_json = redis.execute(["LRANGE", history_key, "0", "9"])
                if history_json:
                    for t_json in history_json:
                        t = json.loads(t_json)
                        if t.get("status") == "closed":
                            events.append({
                                "type": (t.get("tp_level") or "sl").lower(),
                                "symbol": t.get("symbol", ""),
                                "direction": prefix,
                                "message": f"Closed @ {t.get('close_price', 0)}",
                                "timestamp": t.get("closed_at", ""),
                                "price": t.get("close_price", 0),
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
            
            # Читаем all_trades как LIST
            try:
                trades_json = redis.execute(["LRANGE", f"{prefix}:all_trades", "0", "-1"])
                if trades_json:
                    for t_json in trades_json:
                        try:
                            t = json.loads(t_json)
                            trade_date = t.get("closed_at", "")[:10] if t.get("closed_at") else ""
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
                            continue
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
