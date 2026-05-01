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

                    total = len(trades)
                    wins = sum(1 for t in trades if (t.get('pnl_pct') or t.get('pnl') or 0) > 0)
                    losses = sum(1 for t in trades if (t.get('pnl_pct') or t.get('pnl') or 0) <= 0)
                    pnl = sum((t.get('pnl_pct') or t.get('pnl') or 0) for t in trades)
                    
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
    # ✅ FIX: Дедупликация по (symbol, direction), а не только symbol
    # Это позволяет иметь одновременно LONG и SHORT по одному символу
    seen_positions = set()
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

                            # 🔧 FIX: Нормализуем символ (убираем '-') для отображения
                            symbol_normalized = symbol.replace('-', '').upper()
                            # Дедуплицируем по (символ, направление) — можно держать обе стороны
                            dedup_key = f"{symbol_normalized}:{prefix}"
                            if dedup_key in seen_positions:
                                debug_info["skipped_dup"] += 1
                                continue  # Пропускаем настоящий дубликат
                            seen_positions.add(dedup_key)
                            
                            # ✅ Дополнительные поля для отображения
                            status = pos.get('status', 'active')
                            if status not in ['active', 'filled', 'open']:
                                debug_info["skipped_status"] += 1
                                continue  # Пропускаем неактивные позиции

                            # ✅ FIX: Считаем unrealized PnL из entry_price + current_price
                            # Боты не обновляют unrealized_pnl в Redis (только при закрытии)
                            entry_price   = pos.get("entry_price", 0) or 0
                            current_price = pos.get("current_price", pos.get("mark_price", 0)) or 0
                            stored_pnl    = pos.get("unrealized_pnl", pos.get("pnl", 0)) or 0
                            leverage      = pos.get("leverage", 1) or 1

                            if entry_price > 0 and current_price > 0:
                                price_change_pct = (current_price - entry_price) / entry_price * 100
                                if prefix == "short":
                                    price_change_pct = -price_change_pct  # Short: растёт → убыток
                                live_pnl = round(price_change_pct * leverage, 2)
                            else:
                                live_pnl = round(stored_pnl, 2)  # fallback на сохранённое

                            # Время в позиции
                            opened_at = pos.get("opened_at", pos.get("created_at", ""))
                            duration_min = 0
                            if opened_at:
                                try:
                                    from datetime import timezone
                                    opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                                    duration_min = int((datetime.now(timezone.utc) - opened_dt).total_seconds() / 60)
                                except Exception:
                                    duration_min = pos.get("duration_min", 0)

                            positions.append({
                                "symbol": symbol_normalized,  # Возвращаем нормализованный символ
                                "direction": prefix,
                                "entry": entry_price,
                                "current_price": current_price,
                                "current_pnl": live_pnl,
                                "tp": pos.get("take_profit", pos.get("tp", 0)),
                                "sl": pos.get("stop_loss", pos.get("sl", 0)),
                                "leverage": leverage,
                                "duration_min": duration_min,
                                "taken_tps": pos.get("partial_exits", pos.get("taken_tps", 0)),
                                "score": pos.get("score", 0),
                                "opened_at": opened_at,
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
                            pnl = (t.get("pnl_pct") or t.get("pnl") or 0)
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


@app.route("/api/signal_log")
def api_signal_log():
    """
    📊 Лог ВСЕХ сигналов (исполненные на бирже + TG-only / пропущенные).
    Параметры:
      ?limit=100   — кол-во последних сигналов (макс 500)
      ?bot=short|long|both  — какой бот (по умолчанию both)
    """
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        bot_filter = request.args.get("bot", "both").lower()

        result = {
            "short": [],
            "long": [],
            "stats": {
                "short": {"total": 0, "executed": 0, "tg_only": 0, "winrate_signal": 0},
                "long":  {"total": 0, "executed": 0, "tg_only": 0, "winrate_signal": 0},
            }
        }

        pairs = []
        if bot_filter in ("short", "both"):
            pairs.append(("short", get_redis_short))
        if bot_filter in ("long", "both"):
            pairs.append(("long", get_redis_long))

        for prefix, redis_getter in pairs:
            try:
                redis = redis_getter()
                items = redis.execute(["LRANGE", f"{prefix}:signal_log", "0", str(limit - 1)])
                signals = []
                if items:
                    for raw in items:
                        try:
                            s = json.loads(raw)
                            # Добавляем удобные поля для UI
                            s["bot"] = prefix
                            s["executed_label"] = "✅ Биржа" if s.get("executed") else "📡 TG-only"
                            skip = s.get("skip_reason") or ""
                            skip_labels = {
                                "exchange_full":        "🔴 Биржа заполнена",
                                "paused":               "⏸️ Пауза",
                                "auto_trading_disabled":"🔕 Авто откл.",
                                "btc_rising":           "📈 BTC памп",
                                "btc_falling":          "📉 BTC дамп",
                                "volume_too_low":       "📊 Низкий объём",
                                "bingx_rejected":       "❌ BingX отклонил",
                                "error":                "⚠️ Ошибка",
                            }
                            s["skip_label"] = skip_labels.get(skip, skip)
                            signals.append(s)
                        except Exception:
                            pass

                result[prefix] = signals

                total = len(signals)
                executed = sum(1 for s in signals if s.get("executed"))
                tg_only = total - executed
                # Простой winrate: executed + hit_tp vs hit_sl (если есть поле outcome)
                hits = sum(1 for s in signals if s.get("outcome") == "tp")
                losses = sum(1 for s in signals if s.get("outcome") == "sl")
                winrate = round(hits / (hits + losses) * 100, 1) if (hits + losses) > 0 else None

                result["stats"][prefix] = {
                    "total": total,
                    "executed": executed,
                    "tg_only": tg_only,
                    "winrate_signal": winrate,
                }
            except Exception as e:
                print(f"[signal_log] {prefix} error: {e}")

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/virtual_trades")
def api_virtual_trades():
    """
    📊 Закрытые виртуальные сделки (TG-only сигналы с исходом TP/SL/expired).
    Параметры:
      ?limit=100  — кол-во сделок
      ?bot=short|long|both
    """
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        bot_filter = request.args.get("bot", "both").lower()

        result = {
            "short": [],
            "long": [],
            "stats": {
                "short": {"total": 0, "tp": 0, "sl": 0, "expired": 0, "winrate": None, "avg_pnl": None},
                "long":  {"total": 0, "tp": 0, "sl": 0, "expired": 0, "winrate": None, "avg_pnl": None},
            }
        }

        pairs = []
        if bot_filter in ("short", "both"):
            pairs.append(("short", get_redis_short))
        if bot_filter in ("long", "both"):
            pairs.append(("long", get_redis_long))

        for prefix, redis_getter in pairs:
            try:
                redis = redis_getter()
                items = redis.execute(["LRANGE", f"{prefix}:virtual_trades", "0", str(limit - 1)])
                trades = []
                if items:
                    for raw in items:
                        try:
                            t = json.loads(raw)
                            t["bot"] = prefix
                            outcome = t.get("outcome", "unknown")
                            t["outcome_label"] = {
                                "tp": "✅ TP",
                                "sl": "❌ SL",
                                "expired": "⏰ Expired",
                            }.get(outcome, outcome)
                            trades.append(t)
                        except Exception:
                            pass

                result[prefix] = trades

                total = len(trades)
                tp_c  = sum(1 for t in trades if t.get("outcome") == "tp")
                sl_c  = sum(1 for t in trades if t.get("outcome") == "sl")
                exp_c = sum(1 for t in trades if t.get("outcome") == "expired")
                closed = tp_c + sl_c  # expired не считаем в winrate
                winrate = round(tp_c / closed * 100, 1) if closed > 0 else None
                pnls = [t.get("pnl_pct", 0) for t in trades if t.get("pnl_pct") is not None]
                avg_pnl = round(sum(pnls) / len(pnls), 2) if pnls else None

                result["stats"][prefix] = {
                    "total": total, "tp": tp_c, "sl": sl_c, "expired": exp_c,
                    "winrate": winrate, "avg_pnl": avg_pnl,
                }

                # Также читаем АКТИВНЫЕ виртуальные позиции
                try:
                    active_raw = redis.execute(["HGETALL", f"{prefix}:virtual_positions"])
                    if active_raw and isinstance(active_raw, list):
                        # HGETALL возвращает [field, value, field, value, ...]
                        active_list = []
                        for i in range(0, len(active_raw), 2):
                            try:
                                pos = json.loads(active_raw[i + 1])
                                pos["bot"] = prefix
                                pos["outcome"] = "open"
                                pos["outcome_label"] = "🔄 Active"
                                active_list.append(pos)
                            except Exception:
                                pass
                        result[f"{prefix}_active"] = active_list
                except Exception as e:
                    print(f"[virtual_trades] active {prefix} error: {e}")

            except Exception as e:
                print(f"[virtual_trades] {prefix} error: {e}")

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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



# Health check для Render
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
