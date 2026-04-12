"""
🟢 LONG BOT - FastAPI Application
Исправления:
  1. TP веса: 20/20/20/15/15/10 вместо 25/25/20/15/10/5
  2. Дедупликация по времени: сигналы живут 24ч, потом пересканируются
  3. price_change_4d: реальные данные с 1d-klines вместо *4
  4. pause/resume: is_paused флаг, background_scanner не умирает
  5. MAX_POSITIONS: жёсткий лимит перед каждым скандированием
  + PositionTracker: уведомления TP/SL/экспирации, P&L статистика
"""

import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))

from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client, MarketData
from core.scorer import get_long_scorer
from core.pattern_detector import LongPatternDetector
from core.position_tracker import PositionTracker
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "long"
    MIN_SCORE     = int(os.getenv("MIN_LONG_SCORE", "65"))
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
    MAX_POSITIONS = int(os.getenv("MAX_LONG_POSITIONS", "5"))
    LEVERAGE      = os.getenv("LONG_LEVERAGE", "5-10")
    SL_BUFFER     = float(os.getenv("LONG_SL_BUFFER", "0.5"))

    # FIX 1: Пересмотренные веса TP — равномернее, TP6 получает 10% вместо 5%
    TP_LEVELS   = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]   # % движения
    TP_WEIGHTS  = [20,  20,  20,  15,  15,  10]        # % от позиции (сумма=100)

    # FIX 2: Сигнал считается "свежим" N часов. После — можно пересканировать.
    SIGNAL_TTL_HOURS = 24

    USE_COINGLASS = bool(os.getenv("COINGLASS_API_KEY", ""))


# ============================================================================
# GLOBAL STATE
# ============================================================================

class BotState:
    def __init__(self):
        self.is_running  = False
        self.is_paused   = False          # FIX 4: отдельный флаг паузы
        self.last_scan   = None
        self.active_signals = 0
        self.daily_signals  = 0
        self.watchlist: List[str] = []
        self.redis      = None
        self.binance    = None
        self.scorer     = None
        self.pattern_detector = None
        self.telegram   = None
        self.cmd_handler = None
        self.coinglass  = None
        self.tracker: Optional[PositionTracker] = None   # NEW

state = BotState()


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting LONG Bot...")

    state.redis    = get_redis_client()
    state.binance  = get_binance_client()
    state.scorer   = get_long_scorer(Config.MIN_SCORE)
    state.pattern_detector = LongPatternDetector()
    state.telegram = TelegramBot(
        bot_token=os.getenv("LONG_TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("LONG_TELEGRAM_CHAT_ID"),
        topic_id=os.getenv("LONG_TELEGRAM_TOPIC_ID"),
    )

    # CoinGlass — опционально
    if Config.USE_COINGLASS:
        try:
            from coinglass_client import CoinglassClient
            state.coinglass = CoinglassClient(api_key=os.getenv("COINGLASS_API_KEY"))
            print("✅ CoinGlass client initialized")
        except ImportError:
            print("⚠️ CoinGlass: coinglass_client.py not found")
            Config.USE_COINGLASS = False
    else:
        print("ℹ️ CoinGlass disabled (no COINGLASS_API_KEY)")

    print("🔌 Testing connections...")
    redis_ok    = state.redis.health_check()
    telegram_ok = await state.telegram.send_test_message()
    print(f"✅ Redis: {redis_ok}, Telegram: {telegram_ok}")

    state.cmd_handler = TelegramCommandHandler(
        bot=state.telegram,
        redis_client=state.redis,
        bot_state=state,
        bot_type=Config.BOT_TYPE,
        scan_callback=scan_market,
        config=Config,
    )

    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        await state.telegram.setup_webhook(f"{render_url}/webhook")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set — webhook skipped")

    symbols = await state.binance.get_all_symbols(min_volume_usdt=1_000_000)
    state.watchlist = symbols[:100]
    print(f"📊 Watchlist: {len(state.watchlist)} symbols")

    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "watchlist_count": len(state.watchlist),
        "started_at": datetime.utcnow().isoformat(),
    })

    state.is_running = True
    state.last_scan  = datetime.utcnow()

    # Запускаем фоновые задачи
    scanner_task = asyncio.create_task(background_scanner())

    # NEW: Position tracker
    state.tracker = PositionTracker(
        bot_type=Config.BOT_TYPE,
        telegram=state.telegram,
        redis_client=state.redis,
        binance_client=state.binance,
        config=Config,
    )
    tracker_task = asyncio.create_task(state.tracker.run())

    print("✅ LONG Bot started!")

    yield   # ← приложение работает

    # --- Shutdown ---
    print("🛑 Shutting down...")
    state.is_running = False

    if state.tracker:
        state.tracker.stop()

    for task in (scanner_task, tracker_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if state.binance:
        await state.binance.close()
    if state.telegram:
        await state.telegram.close()
    if state.coinglass:
        await state.coinglass.close()

    print("👋 LONG Bot stopped")


# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="Liquidity Long Bot",
    description="🟢 LONG Bot - Finds oversold perps for buying",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy" if state.is_running else "unhealthy",
        "bot_type": Config.BOT_TYPE,
        "is_running": state.is_running,
        "is_paused": state.is_paused,
        "last_scan": state.last_scan.isoformat() if state.last_scan else None,
        "watchlist_count": len(state.watchlist),
        "active_signals": state.active_signals,
        "coinglass_enabled": Config.USE_COINGLASS,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/status")
async def get_status():
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot is not running")
    bot_state = state.redis.get_bot_state(Config.BOT_TYPE)
    return {
        "status": "paused" if state.is_paused else "running",
        "bot_type": Config.BOT_TYPE,
        "config": {
            "min_score": Config.MIN_SCORE,
            "scan_interval": Config.SCAN_INTERVAL,
            "max_positions": Config.MAX_POSITIONS,
            "leverage": Config.LEVERAGE,
            "signal_ttl_hours": Config.SIGNAL_TTL_HOURS,
            "coinglass_enabled": Config.USE_COINGLASS,
        },
        "watchlist": state.watchlist[:10],
        "watchlist_count": len(state.watchlist),
        "last_scan": state.last_scan.isoformat() if state.last_scan else None,
        "redis_connected": state.redis.health_check() if state.redis else False,
        "bot_state": bot_state,
    }


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot is not running")
    if state.is_paused:
        raise HTTPException(status_code=409, detail="Bot is paused")
    background_tasks.add_task(scan_market)
    return {"message": "Scan triggered", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/signals")
async def get_active_signals():
    if not state.redis:
        raise HTTPException(status_code=503, detail="Redis not connected")
    signals = state.redis.get_active_signals(Config.BOT_TYPE)
    return {"bot_type": Config.BOT_TYPE, "count": len(signals), "signals": signals}


@app.get("/api/signals/{symbol}")
async def get_symbol_signals(symbol: str):
    if not state.redis:
        raise HTTPException(status_code=503, detail="Redis not connected")
    signals = state.redis.get_signals(Config.BOT_TYPE, symbol.upper())
    return {"symbol": symbol.upper(), "count": len(signals), "signals": signals}


@app.get("/api/stats")
async def get_stats(days: int = 7):
    if not state.redis:
        raise HTTPException(status_code=503, detail="Redis not connected")
    bot_st = state.redis.get_bot_state(Config.BOT_TYPE) or {}
    daily_trades = bot_st.get("daily_trades", {})

    # Формируем ответ за последние N дней
    result = []
    for i in range(days):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({"date": day, **daily_trades.get(day, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})})

    total_trades = sum(r.get("trades", 0) for r in result)
    total_wins   = sum(r.get("wins", 0) for r in result)
    total_pnl    = round(sum(r.get("pnl", 0) for r in result), 4)
    winrate      = round(total_wins / total_trades * 100, 1) if total_trades else 0

    return {
        "bot_type": Config.BOT_TYPE,
        "period_days": days,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "winrate_pct": winrate,
        "total_pnl_pct": total_pnl,
        "daily": result,
    }


@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        if state.cmd_handler:
            await state.cmd_handler.handle_update(update)
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/webhook/info")
async def webhook_info():
    if state.telegram:
        return {"webhook": await state.telegram.get_webhook_info()}
    return {"error": "Telegram not initialized"}


# ============================================================================
# CORE LOGIC
# ============================================================================

async def _get_coinglass_boost(symbol: str, direction: str):
    """Буст/штраф от CoinGlass. Возвращает (delta, reason)."""
    if not state.coinglass or not Config.USE_COINGLASS:
        return 0, ""
    try:
        cg_symbol  = symbol.replace("USDT", "").replace("PERP", "")
        liq_signal = await state.coinglass.get_liquidation_signal(cg_symbol)
        if not liq_signal or liq_signal["signal"] == "neutral":
            return 0, ""
        cg_dir   = liq_signal["signal"]
        strength = liq_signal["strength"]
        reason   = liq_signal["reason"]
        if direction == "long" and cg_dir == "long":
            delta = min(10, strength // 5)
            return delta, f"CoinGlass: {reason}"
        elif direction == "long" and cg_dir == "short":
            delta = -min(8, strength // 5)
            return delta, f"CoinGlass contra: {reason}"
    except Exception as e:
        print(f"CoinGlass boost error {symbol}: {e}")
    return 0, ""


async def _get_real_price_change_4d(symbol: str, fallback: float) -> float:
    """
    FIX 3: Реальное 4-дневное изменение цены с Binance 1d-klines.
    Fallback к price_change_24h * 4 если данных нет.
    """
    try:
        klines = await state.binance.get_klines(symbol, "1d", 6)
        if klines and len(klines) >= 5:
            # klines[-1] — текущий день, klines[-5] — 4 дня назад
            close_now  = float(klines[-1][3])    # close текущего дня
            close_4d   = float(klines[-5][3])    # close 4 дня назад
            if close_4d > 0:
                return (close_now - close_4d) / close_4d * 100
    except Exception as e:
        print(f"price_change_4d fallback for {symbol}: {e}")
    return fallback


def _is_signal_fresh(existing_signals: List[Dict]) -> bool:
    """
    FIX 2: Сигнал "свежий" если ему меньше SIGNAL_TTL_HOURS.
    Если старше — позволяем пересканировать монету.
    """
    if not existing_signals:
        return False
    sig = existing_signals[0]
    if sig.get("status") != "active":
        return False
    created_at = sig.get("timestamp", "")
    if not created_at:
        return True   # нет timestamp — считаем свежим
    try:
        created = datetime.fromisoformat(created_at)
        age_h   = (datetime.utcnow() - created).total_seconds() / 3600
        return age_h < Config.SIGNAL_TTL_HOURS
    except Exception:
        return True


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """Сканировать одну пару для LONG."""
    try:
        market_data = await state.binance.get_complete_market_data(symbol)
        if not market_data:
            return None

        ohlcv_15m = await state.binance.get_klines(symbol, "15m", 100)
        if not ohlcv_15m or len(ohlcv_15m) < 20:
            return None

        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend   = state.pattern_detector._get_price_trend(ohlcv_15m)
        patterns      = state.pattern_detector.detect_all(ohlcv_15m, hourly_deltas, None)

        # FIX 3: реальное 4d изменение
        price_change_4d = await _get_real_price_change_4d(
            symbol, market_data.price_change_24h * 4
        )

        score_result = state.scorer.calculate_score(
            rsi_1h=market_data.rsi_1h or 50,
            funding_current=market_data.funding_rate / 100,
            funding_accumulated=market_data.funding_accumulated / 100,
            long_ratio=market_data.long_short_ratio,
            oi_change_4d=market_data.oi_change_4d,
            price_change_4d=price_change_4d,
            hourly_deltas=hourly_deltas,
            price_trend=price_trend,
            patterns=patterns,
        )

        if not score_result.is_valid:
            return None

        # CoinGlass буст/штраф
        cg_delta, cg_reason = await _get_coinglass_boost(symbol, "long")
        final_score = score_result.total_score + cg_delta

        if final_score < Config.MIN_SCORE:
            return None

        reasons = list(score_result.reasons)
        if cg_reason:
            reasons.append(cg_reason)

        indicators = {
            "RSI":      f"{market_data.rsi_1h:.1f}" if market_data.rsi_1h else "N/A",
            "Funding":  f"{market_data.funding_rate:+.3f}%",
            "L/S Ratio": f"{market_data.long_short_ratio:.0f}% longs",
            "OI Change": f"{market_data.oi_change_4d:+.1f}% (4d)",
            "Price 4d":  f"{price_change_4d:+.1f}%",
        }
        if cg_delta != 0:
            indicators["CoinGlass"] = f"{'+' if cg_delta > 0 else ''}{cg_delta} pts"

        # FIX 1: правильные веса TP
        take_profits = [
            (
                market_data.price * (1 - tp / 100),
                Config.TP_WEIGHTS[i],
            )
            for i, tp in enumerate(Config.TP_LEVELS)
        ]

        signal = {
            "symbol":       symbol,
            "direction":    "long",
            "score":        final_score,
            "grade":        score_result.grade,
            "confidence":   score_result.confidence.value,
            "price":        market_data.price,
            "patterns":     [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "entry_price":  market_data.price,
            "stop_loss":    market_data.price * (1 + Config.SL_BUFFER / 100),
            "take_profits": take_profits,
            "indicators":   indicators,
            "reasons":      reasons,
            "coinglass_delta": cg_delta,
            "timestamp":    datetime.utcnow().isoformat(),
            "status":       "active",
            "taken_tps":    [],    # для PositionTracker
        }

        return signal

    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def scan_market():
    """Полное сканирование рынка."""
    if state.is_paused:
        print("⏸ Scan skipped — bot is paused")
        return

    print(f"\n🔍 Starting LONG scan at {datetime.utcnow().isoformat()}")
    print(f"📊 Scanning {len(state.watchlist)} symbols...")

    # FIX 5: жёсткий лимит позиций
    active_count = len(state.redis.get_active_signals(Config.BOT_TYPE))
    if active_count >= Config.MAX_POSITIONS:
        print(f"⚠️ Max positions reached ({active_count}/{Config.MAX_POSITIONS}), scan skipped")
        state.last_scan = datetime.utcnow()
        return

    new_signals = 0

    for symbol in state.watchlist:
        # FIX 5: повторная проверка лимита внутри цикла
        if new_signals + active_count >= Config.MAX_POSITIONS:
            print(f"⚠️ Position limit ({Config.MAX_POSITIONS}) reached mid-scan")
            break

        try:
            existing = state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)

            # FIX 2: проверяем свежесть по времени, не только по статусу
            if _is_signal_fresh(existing):
                continue

            signal = await scan_symbol(symbol)

            if signal:
                state.redis.save_signal(Config.BOT_TYPE, symbol, signal)

                await state.telegram.send_signal(
                    direction="long",
                    symbol=signal["symbol"],
                    score=signal["score"],
                    price=signal["price"],
                    pattern=signal["best_pattern"] or "N/A",
                    indicators=signal["indicators"],
                    entry=signal["entry_price"],
                    stop_loss=signal["stop_loss"],
                    take_profits=signal["take_profits"],
                    leverage=Config.LEVERAGE,
                    risk="≤1% deposit",
                )

                print(f"✅ Signal: {symbol} — Score: {signal['score']}%")
                new_signals += 1

            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    state.daily_signals += new_signals
    state.last_scan      = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))

    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status":         "paused" if state.is_paused else "running",
        "last_scan":      state.last_scan.isoformat(),
        "daily_signals":  state.daily_signals,
        "active_signals": state.active_signals,
    })

    print(f"✅ Scan complete. New: {new_signals}, Active: {state.active_signals}")


async def background_scanner():
    """
    FIX 4: is_paused не убивает задачу — просто пропускает скан.
    Задача живёт всё время жизни бота.
    """
    while state.is_running:
        if not state.is_paused:
            try:
                await scan_market()
            except Exception as e:
                print(f"Error in background scanner: {e}")
        else:
            print("⏸ Scan skipped (paused)")
        await asyncio.sleep(Config.SCAN_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=False,
    )
