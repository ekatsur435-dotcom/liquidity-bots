"""
📊 Phase 3 Dashboard — Web UI для анализа торговли

Запуск: python dashboard/app.py
URL: http://localhost:5000
"""

import os
import sys
import json
import traceback
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
        "be_count": 0,
        "total_pnl": 0.0,
        "total_pnl_leveraged": 0.0,
        "micro_step_saves": 0,
        "active_positions": 0,
        "trades": [],
        "short_trades": 0,
        "short_wins": 0,
        "short_losses": 0,
        "short_be": 0,
        "short_winrate": 0,
        "short_pnl": 0.0,
        "short_pnl_leveraged": 0.0,
        "long_trades": 0,
        "long_wins": 0,
        "long_losses": 0,
        "long_be": 0,
        "long_winrate": 0,
        "long_pnl": 0.0,
        "long_pnl_leveraged": 0.0,
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
                    # ✅ FIX v7: Считаем только ЗАКРЫТЫЕ сделки (status=closed_*)
                    # Активные позиции имеют pnl=0 (unrealized) и искажают статистику
                    all_t = trades_data[-100:] if len(trades_data) > 100 else trades_data
                    closed_statuses = {'closed_tp', 'closed_sl', 'closed_manual', 'closed'}
                    trades = [t for t in all_t
                              if t.get('status', 'active') in closed_statuses
                              or t.get('close_price') is not None]
                    # Fallback: если нет закрытых — берём все (старые данные без status)
                    if not trades:
                        trades = all_t[-50:]

                    def _num(v, default=0.0):
                        try:
                            return float(v) if v not in (None, '', '?') else default
                        except (ValueError, TypeError):
                            return default

                    def _lev(t):
                        try:
                            return int(float(t.get('leverage', 20) or 20))
                        except (ValueError, TypeError):
                            return 20

                    total = len(trades)
                    BE_FLOOR = -0.1  # BE/Flat: от -0.1% до 0%
                    wins   = sum(1 for t in trades if _num(t.get('pnl_pct') or t.get('pnl')) > 0)
                    be     = sum(1 for t in trades if BE_FLOOR <= _num(t.get('pnl_pct') or t.get('pnl')) <= 0)
                    losses = sum(1 for t in trades if _num(t.get('pnl_pct') or t.get('pnl')) < BE_FLOOR)
                    pnl = sum(_num(t.get('pnl_pct') or t.get('pnl')) for t in trades)
                    # Leveraged P&L = price_move% × leverage (real account impact)
                    pnl_lev = sum(
                        _num(t.get('pnl_pct') or t.get('pnl')) * _lev(t)
                        for t in trades
                    )

                    stats["total_trades"] += total
                    stats["win_count"] += wins
                    stats["loss_count"] += losses
                    stats["be_count"] += be
                    stats["total_pnl"] += pnl
                    stats["total_pnl_leveraged"] += pnl_lev

                    if bot_name == "SHORT":
                        stats["short_trades"] = total
                        stats["short_wins"] = wins
                        stats["short_losses"] = losses
                        stats["short_be"] = be
                        stats["short_winrate"] = round(wins / total * 100, 1) if total > 0 else 0
                        stats["short_pnl"] = round(pnl, 2)
                        stats["short_pnl_leveraged"] = round(pnl_lev, 2)
                    else:
                        stats["long_trades"] = total
                        stats["long_wins"] = wins
                        stats["long_losses"] = losses
                        stats["long_be"] = be
                        stats["long_winrate"] = round(wins / total * 100, 1) if total > 0 else 0
                        stats["long_pnl"] = round(pnl, 2)
                        stats["long_pnl_leveraged"] = round(pnl_lev, 2)
            except Exception as e:
                print(f"Error reading all_trades for {bot_name}: {e}")
                traceback.print_exc()
                    
            # Micro-step saves (LIST)
            try:
                saves_key = f"{prefix}:micro_step:saved_trades"
                saves_json = redis.execute(["LRANGE", saves_key, "0", "-1"])
                if saves_json:
                    stats["micro_step_saves"] += len(saves_json)
            except:
                pass
                
            # Active positions - считаем из positions:* ключей
            try:
                # Используем KEYS для Upstash (SCAN может работать нестабильно)
                result = redis.execute(["KEYS", f"{prefix}:positions:*"])
                if result and isinstance(result, list):
                    pos_count = len(result)
                    stats["active_positions"] += pos_count
                    print(f"[Dashboard] {bot_name} positions: {pos_count} keys found")
                else:
                    print(f"[Dashboard] {bot_name} positions: no keys found (result={result})")
            except Exception as e:
                print(f"[Dashboard] Error counting positions for {bot_name}: {e}")
                
        except Exception as e:
            print(f"Redis {bot_name} error: {e}")
    
    # Win rate (BE не считается ни win ни loss для %WR)
    total = stats["win_count"] + stats["loss_count"] + stats["be_count"]
    decisive = stats["win_count"] + stats["loss_count"]
    stats["win_rate"] = round(stats["win_count"] / decisive * 100, 1) if decisive > 0 else 0
    stats["total_trades"] = total
    stats["total_pnl"] = round(stats["total_pnl"], 2)
    stats["total_pnl_leveraged"] = round(stats["total_pnl_leveraged"], 2)
    
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
    days_param = request.args.get('days', '7')
    
    # Handle 'all' period - get all available data
    if days_param == 'all':
        days = 90  # Default to 90 days for 'all' (3 months)
    else:
        days = int(days_param)
    
    dates = []
    pnl_data = []
    win_rate_data = []
    trades_data = []
    short_pnl_data = []
    long_pnl_data = []
    
    for i in range(days-1, -1, -1):
        date = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        
        # Агрегируем данные из обоих ботов
        day_pnl = 0
        day_wins = 0
        day_losses = 0
        day_trades = 0
        day_short_pnl = 0
        day_long_pnl = 0
        
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
                            pnl = day_stats.get("pnl", 0)
                            day_pnl += pnl
                            day_wins += day_stats.get("wins", 0)
                            day_losses += day_stats.get("losses", 0)
                            day_trades += day_stats.get("trades", 0)
                            if prefix == "short":
                                day_short_pnl += pnl
                            else:
                                day_long_pnl += pnl
                    except:
                        pass
            except:
                pass
        
        dates.append(date[5:])  # MM-DD
        pnl_data.append(round(day_pnl, 2))
        short_pnl_data.append(round(day_short_pnl, 2))
        long_pnl_data.append(round(day_long_pnl, 2))
        win_rate = (day_wins / day_trades * 100) if day_trades > 0 else 0
        win_rate_data.append(round(win_rate, 1))
        trades_data.append(day_trades)
    
    return jsonify({
        "dates": dates,
        "pnl": pnl_data,
        "short_pnl": short_pnl_data,
        "long_pnl": long_pnl_data,
        "win_rate": win_rate_data,
        "trades": trades_data
    })


@app.route("/api/trades")
def api_trades():
    """API: Последние 20 сделок SHORT и LONG с деталями"""
    trades = {"short": [], "long": []}
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем all_trades как LIST (последние 20)
            try:
                trades_json = redis.execute(["LRANGE", f"{prefix}:all_trades", "0", "19"])
                if trades_json:
                    all_trades = [json.loads(t) for t in trades_json]
                    trades[prefix] = all_trades[:20]
            except:
                pass
        except Exception as e:
            print(f"Error reading trades for {bot_name}: {e}")
    
    return jsonify(trades)


@app.route("/api/positions")
def api_positions():
    """API: Получить список активных позиций (все)"""
    positions = []
    seen_symbols = set()
    debug_info = {"short_keys": 0, "long_keys": 0, "skipped_status": 0, "skipped_dup": 0}
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            try:
                result = redis.execute(["KEYS", f"{prefix}:positions:*"])
                position_keys = result if result and isinstance(result, list) else []
                debug_info[f"{prefix}_keys"] = len(position_keys)
                
                for key in position_keys:  # ✅ ВСЕ позиции, без ограничения
                    pos_data = redis.execute(["GET", key])
                    if pos_data:
                        try:
                            pos = json.loads(pos_data)
                            symbol = key.split(":")[-1]
                            
                            # 🔧 FIX v2.5: включаем направление в ключ дедупликации
                            # Раньше: BTCUSDT short + BTCUSDT long = одно пропускалось как dup
                            symbol_normalized = symbol.replace('-', '').upper()
                            dedup_key = f"{prefix}:{symbol_normalized}"
                            if dedup_key in seen_symbols:
                                debug_info["skipped_dup"] += 1
                                continue
                            seen_symbols.add(dedup_key)
                            
                            # ✅ Дополнительные поля для отображения
                            status = pos.get('status', 'active')
                            if status not in ['active', 'filled', 'open']:
                                debug_info["skipped_status"] += 1
                                continue  # Пропускаем неактивные позиции

                            # ✅ FIX: определяем тип позиции — биржевая или виртуальная
                            has_order_id = bool(pos.get("order_id"))
                            pos_type = "exchange" if has_order_id else "virtual"

                            positions.append({
                                "symbol": symbol_normalized,  # Возвращаем нормализованный символ
                                "direction": prefix,
                                "entry": pos.get("entry_price", 0),
                                "current_pnl": pos.get("unrealized_pnl", pos.get("pnl", 0)),
                                "tp": pos.get("take_profit", pos.get("tp", 0)),
                                "sl": pos.get("stop_loss", pos.get("sl", 0)),
                                "duration_min": pos.get("duration_min", 0),
                                "taken_tps": pos.get("partial_exits", pos.get("taken_tps", 0)),
                                "order_id": pos.get("order_id"),
                                "pos_type": pos_type,  # "exchange" | "virtual"
                            })
                        except Exception as e:
                            print(f"[API] Error parsing position {key}: {e}")
                            continue
            except Exception as e:
                print(f"Error scanning positions for {bot_name}: {e}")
        except Exception as e:
            print(f"Error reading positions for {bot_name}: {e}")
    
    print(f"[API Positions] Found {len(positions)} active positions. Debug: {debug_info}")
    return jsonify({"positions": positions, "count": len(positions), "debug": debug_info})


@app.route("/api/feed")
def api_feed():
    """API: Live feed последних событий (TBS, входы, TP/SL)"""
    events = []
    
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            
            # Читаем all_trades для событий TP/SL (последние закрытия)
            try:
                trades_json = redis.execute(["LRANGE", f"{prefix}:all_trades", "0", "9"])
                if trades_json:
                    for t_json in trades_json:
                        try:
                            t = json.loads(t_json)
                            # Показываем только закрытые сделки с exit_reason
                            if t.get("exit_reason") or t.get("status") == "closed":
                                events.append({
                                    "type": t.get("exit_reason", "closed").lower(),
                                    "symbol": t.get("symbol", ""),
                                    "direction": prefix,
                                    "message": f"{t.get('exit_reason', 'Closed')} @ {t.get('exit_price', t.get('close_price', 0))}",
                                    "timestamp": t.get("exit_time", t.get("closed_at", "")),
                                    "price": t.get("exit_price", t.get("close_price", 0)),
                                    "pnl": (t.get("pnl_pct") or t.get("pnl") or 0)
                                })
                        except:
                            continue
            except:
                pass
                    
            # Также читаем активные сигналы для входов
            try:
                signal_keys = []
                cursor = 0
                while True:
                    result = redis.execute(["SCAN", str(cursor), "MATCH", f"{prefix}:signals:*", "COUNT", "100"])
                    if result and len(result) >= 2:
                        cursor = int(result[0])
                        keys = result[1] if isinstance(result[1], list) else []
                        signal_keys.extend(keys)
                        if cursor == 0:
                            break
                    else:
                        break
                
                for key in signal_keys[:5]:
                    sig_list = redis.execute(["LRANGE", key, "0", "0"])
                    if sig_list:
                        try:
                            sig = json.loads(sig_list[0])
                            if sig.get("status") == "active" or sig.get("type") == "entry":
                                symbol = key.split(":")[-1]
                                events.append({
                                    "type": "entry",
                                    "symbol": symbol,
                                    "direction": prefix,
                                    "message": f"Entry @ {sig.get('entry_price', 0)}",
                                    "timestamp": sig.get("timestamp", ""),
                                    "price": sig.get("entry_price", 0),
                                    "pnl": None
                                })
                        except:
                            continue
            except:
                pass
                    
        except Exception as e:
            print(f"Error reading feed for {bot_name}: {e}")
    
    # Сортируем по времени (новые сверху)
    events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify({"events": events[:15]})


@app.route("/api/signal_log")
def api_signal_log():
    """API: Лог всех сигналов — исполненные на бирже + TG-only"""
    limit = min(int(request.args.get('limit', 400)), 1000)
    signals = []
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            items = redis.execute(["LRANGE", f"{prefix}:signal_log", "0", str(limit - 1)])
            if items:
                for item in items:
                    try:
                        s = json.loads(item)
                        s["bot_type"] = prefix
                        signals.append(s)
                    except:
                        pass
        except Exception as e:
            print(f"[signal_log] {bot_name}: {e}")
    signals.sort(key=lambda x: x.get("log_ts", ""), reverse=True)
    return jsonify({"signals": signals[:limit], "count": len(signals)})


@app.route("/api/virtual_monitor")
def api_virtual_monitor():
    """API: Виртуальный мониторинг — открытые и закрытые виртуальные позиции"""
    positions = []
    for bot_name, redis_getter in [("SHORT", get_redis_short), ("LONG", get_redis_long)]:
        try:
            redis = redis_getter()
            prefix = bot_name.lower()
            # Открытые (HASH → [field1, val1, field2, val2, ...])
            try:
                raw = redis.execute(["HGETALL", f"{prefix}:virtual_positions"])
                if raw and isinstance(raw, list):
                    for i in range(0, len(raw) - 1, 2):
                        try:
                            pos = json.loads(raw[i + 1])
                            pos["_status"] = "open"
                            pos["bot_type"] = prefix
                            positions.append(pos)
                        except:
                            pass
            except:
                pass
            # Закрытые (LIST)
            try:
                items = redis.execute(["LRANGE", f"{prefix}:virtual_trades", "0", "49"])
                if items:
                    for item in items:
                        try:
                            pos = json.loads(item)
                            pos["_status"] = "closed"
                            pos["bot_type"] = prefix
                            positions.append(pos)
                        except:
                            pass
            except:
                pass
        except Exception as e:
            print(f"[virtual_monitor] {bot_name}: {e}")
    positions.sort(key=lambda x: x.get("virtual_opened_at", x.get("log_ts", "")), reverse=True)
    return jsonify({"positions": positions, "count": len(positions)})


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
                    _BE_FLOOR = -0.1  # BE/Flat: от -0.1% до 0%
                    for t_json in trades_json:
                        try:
                            t = json.loads(t_json)
                            trade_date = t.get("closed_at", "")[:10] if t.get("closed_at") else ""
                            pnl = float(t.get("pnl_pct") or t.get("pnl") or 0)
                            is_win  = pnl > 0
                            is_be   = _BE_FLOOR <= pnl <= 0
                            is_loss = pnl < _BE_FLOOR

                            # Week
                            summary["week"]["pnl"] += pnl
                            summary["week"]["trades"] += 1
                            if is_win:
                                summary["week"]["wins"] = summary["week"].get("wins", 0) + 1
                            if is_be:
                                summary["week"]["be"] = summary["week"].get("be", 0) + 1
                            if is_loss:
                                summary["week"]["losses"] = summary["week"].get("losses", 0) + 1

                            # Today
                            if trade_date == today:
                                summary["today"]["pnl"] += pnl
                                summary["today"]["trades"] += 1
                                if is_win:
                                    summary["today"]["wins"] = summary["today"].get("wins", 0) + 1
                                if is_be:
                                    summary["today"]["be"] = summary["today"].get("be", 0) + 1
                                if is_loss:
                                    summary["today"]["losses"] = summary["today"].get("losses", 0) + 1

                            # Yesterday
                            if trade_date == yesterday:
                                summary["yesterday"]["pnl"] += pnl
                                summary["yesterday"]["trades"] += 1
                                if is_win:
                                    summary["yesterday"]["wins"] = summary["yesterday"].get("wins", 0) + 1
                                if is_be:
                                    summary["yesterday"]["be"] = summary["yesterday"].get("be", 0) + 1
                                if is_loss:
                                    summary["yesterday"]["losses"] = summary["yesterday"].get("losses", 0) + 1
                        except:
                            continue
            except:
                pass
        except Exception as e:
            print(f"Error reading summary for {bot_name}: {e}")
    
    # Calculate winrates (BE исключается из decisive для честного WR%)
    for period in ["today", "yesterday", "week"]:
        wins   = summary[period].get("wins", 0)
        losses = summary[period].get("losses", 0)
        be     = summary[period].get("be", 0)
        decisive = wins + losses
        summary[period]["winrate"] = round(wins / decisive * 100, 1) if decisive > 0 else 0
        summary[period]["be"] = be
    
    return jsonify(summary)


@app.route("/api/reset_stats", methods=["POST"])
def reset_stats():
    """
    ✅ FIX v7: Сброс статистики Redis (старые данные до фиксов).
    POST /api/reset_stats   — очищает all_trades, stats:daily ключи.
    """
    try:
        for redis_getter, prefix in [(get_redis_short, "short"), (get_redis_long, "long")]:
            try:
                r = redis_getter()
                r.execute(["DEL", f"{prefix}:all_trades"])
                r.execute(["DEL", f"{prefix}:stats:daily"])
                # Также удаляем кэшированные ключи daily по датам
                result = r.execute(["KEYS", f"{prefix}:stats:daily:*"])
                if result:
                    for key in result:
                        r.execute(["DEL", key])
                print(f"[Dashboard] Stats reset for {prefix}")
            except Exception as e:
                print(f"[Dashboard] Reset error {prefix}: {e}")
        return json.dumps({"status": "ok", "message": "Статистика сброшена. Данные накопятся заново."})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



@app.route("/api/cleanup_positions", methods=["POST"])
def cleanup_positions():
    """
    Удалить СТАРЫЕ positions:* ключи (открытые > 6 часов назад без обновления).
    Безопасно: активные позиции (last_updated < 6ч) не трогает.
    """
    deleted = 0
    skipped = 0
    cutoff = datetime.utcnow() - timedelta(hours=6)
    try:
        for redis_getter, prefix in [(get_redis_short, "short"), (get_redis_long, "long")]:
            try:
                r = redis_getter()
                keys = r.execute(["KEYS", f"{prefix}:positions:*"])
                if not keys:
                    continue
                for key in keys:
                    try:
                        raw = r.execute(["GET", key])
                        if not raw:
                            r.execute(["DEL", key])
                            deleted += 1
                            continue
                        pos = json.loads(raw)
                        # Берём самую свежую метку времени
                        ts_str = pos.get("last_updated") or pos.get("opened_at") or pos.get("timestamp", "")
                        if ts_str:
                            ts = datetime.fromisoformat(ts_str[:19])
                            if ts > cutoff:
                                skipped += 1
                                continue  # Позиция недавняя — не трогаем
                        r.execute(["DEL", key])
                        deleted += 1
                    except Exception:
                        r.execute(["DEL", key])
                        deleted += 1
                print(f"[Cleanup] {prefix}: deleted={deleted} skipped={skipped}")
            except Exception as e:
                print(f"[Cleanup] Error {prefix}: {e}")
        return json.dumps({"status": "ok", "deleted": deleted, "skipped": skipped,
                           "message": f"Удалено {deleted} старых позиций, пропущено {skipped} активных (< 6ч)."})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# Health check для Render
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
