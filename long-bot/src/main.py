"""
🟢 LONG BOT - FastAPI Application
Поиск перепроданных монет для LONG позиций
"""

import os
import asyncio
from datetime import datetime
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
import uvicorn

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))

from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client
from core.scorer import get_long_scorer
from core.pattern_detector import LongPatternDetector
from bot.telegram import TelegramBot, TelegramCommandHandler
from utils.coinglass_client import CoinglassClient


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE = "long"
    MIN_SCORE = int(os.getenv("MIN_LONG_SCORE", "65"))
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
    MAX_POSITIONS = int(os.getenv("MAX_LONG_POSITIONS", "5"))
    LEVERAGE = os.getenv("LONG_LEVERAGE", "3-5")

    TP_LEVELS = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]
    SL_BUFFER = float(os.getenv("LONG_SL_BUFFER", "0.8"))

    USE_COINGLASS = bool(os.getenv("COINGLASS_API_KEY", ""))


# ============================================================================
# GLOBAL STATE
# ============================================================================

class BotState:
    def __init__(self):
        self.is_running = False
        self.last_scan = None
        self.active_signals = 0
        self.daily_signals = 0
        self.watchlist: List[str] = []
        self.redis = None
        self.binance = None
        self.scorer = None
        self.pattern_detector = None
        self.telegram = None
        self.cmd_handler = None
        self.coinglass: Optional[CoinglassClient] = None

state = BotState()


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting LONG Bot...")

    state.redis = get_redis_client()
    state.binance = get_binance_client()
    state.scorer = get_long_scorer(Config.MIN_SCORE)
    state.pattern_detector = LongPatternDetector()
    state.telegram = TelegramBot(
        bot_token=os.getenv("LONG_TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("LONG_TELEGRAM_CHAT_ID"),
        topic_id=os.getenv("LONG_TELEGRAM_TOPIC_ID")
    )

    if Config.USE_COINGLASS:
        state.coinglass = CoinglassClient(api_key=os.getenv("COINGLASS_API_KEY"))
        print("✅ CoinGlass client initialized")
    else:
        print("⚠️ CoinGlass disabled (no COINGLASS_API_KEY)")

    redis_ok = state.redis.health_check()
    telegram_ok = await state.telegram.send_test_message()
    print(f"✅ Redis: {redis_ok}, Telegram: {telegram_ok}")

    # ✅ ИСПРАВЛЕНО: передаём scan_market и Config напрямую
    state.cmd_handler = TelegramCommandHandler(
        bot=state.telegram,
        redis_client=state.redis,
        bot_state=state,
        bot_type=Config.BOT_TYPE,
        scan_callback=scan_market,   # ← функция, а не строка модуля
        config=Config                # ← класс Config напрямую
    )

    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        webhook_url = f"{render_url}/webhook"
        await state.telegram.setup_webhook(webhook_url)
        print(f"✅ Webhook registered: {webhook_url}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set — webhook not registered")

    symbols = await state.binance.get_all_symbols(min_volume_usdt=1_000_000)
    state.watchlist = symbols[:100]
    print(f"📊 Watchlist: {len(state.watchlist)} symbols")

    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "watchlist_count": len(state.watchlist),
        "started_at": datetime.utcnow().isoformat()
    })

    state.is_running = True
    state.last_scan = datetime.utcnow()

    scanner_task = asyncio.create_task(background_scanner())

    print("✅ LONG Bot started successfully!")

    yield

    print("🛑 Shutting down...")
    state.is_running = False
    scanner_task.cancel()
    try:
        await scanner_task
    except asyncio.CancelledError:
        pass

    if state.binance:
        await state.binance.close()
    if state.telegram:
        await state.telegram.close()
    if state.coinglass:
        await state.coinglass.close()

    print("👋 Long Bot stopped")


# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="Liquidity Long Bot",
    description="🟢 LONG Bot - Finds oversold perps for buying",
    version="1.0.0",
    lifespan=lifespan
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
        "last_scan": state.last_scan.isoformat() if state.last_scan else None,
        "watchlist_count": len(state.watchlist),
        "active_signals": state.active_signals,
        "coinglass_enabled": Config.USE_COINGLASS,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/status")
async def get_status():
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot is not running")
    bot_state = state.redis.get_bot_state(Config.BOT_TYPE)
    return {
        "status": "running",
        "bot_type": Config.BOT_TYPE,
        "config": {
            "min_score": Config.MIN_SCORE,
            "scan_interval": Config.SCAN_INTERVAL,
            "max_positions": Config.MAX_POSITIONS,
            "leverage": Config.LEVERAGE,
            "coinglass_enabled": Config.USE_COINGLASS
        },
        "watchlist_count": len(state.watchlist),
        "last_scan": state.last_scan.isoformat() if state.last_scan else None,
        "bot_state": bot_state
    }


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot is not running")
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
    stats = state.redis.get_stats_range(Config.BOT_TYPE, days)
    total_signals = sum(s.get("signals", 0) for s in stats)
    total_trades = sum(s.get("trades", 0) for s in stats)
    total_pnl = sum(s.get("pnl", 0) for s in stats)
    return {
        "bot_type": Config.BOT_TYPE,
        "period_days": days,
        "total_signals": total_signals,
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "daily_stats": stats
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
        info = await state.telegram.get_webhook_info()
        return {"webhook": info}
    return {"error": "Telegram not initialized"}


# ============================================================================
# CORE LOGIC
# ============================================================================

async def _get_coinglass_boost(symbol: str, direction: str) -> tuple[int, str]:
    """Получить буст/штраф скора от CoinGlass"""
    if not state.coinglass or not Config.USE_COINGLASS:
        return 0, ""
    try:
        cg_symbol = symbol.replace("USDT", "").replace("PERP", "")
        liq_signal = await state.coinglass.get_liquidation_signal(cg_symbol)

        if not liq_signal or liq_signal["signal"] == "neutral":
            return 0, ""

        cg_direction = liq_signal["signal"]
        strength = liq_signal["strength"]
        reason = liq_signal["reason"]

        if direction == "long" and cg_direction == "long":
            # Ликвидации лонгов → выбили слабых рук → хороший момент для входа в лонг
            delta = min(10, strength // 5)
            return delta, f"CoinGlass: {reason}"
        elif direction == "long" and cg_direction == "short":
            # Ликвидации шортов → цена уже выросла → против нашего лонга
            delta = -min(8, strength // 5)
            return delta, f"CoinGlass contra: {reason}"

        return 0, ""
    except Exception as e:
        print(f"CoinGlass boost error for {symbol}: {e}")
        return 0, ""


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """Сканировать одну пару для LONG"""
    try:
        market_data = await state.binance.get_complete_market_data(symbol)
        if not market_data:
            return None

        ohlcv_15m = await state.binance.get_klines(symbol, "15m", 100)
        if not ohlcv_15m or len(ohlcv_15m) < 20:
            return None

        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend = state.pattern_detector._get_price_trend(ohlcv_15m)
        patterns = state.pattern_detector.detect_all(ohlcv_15m, hourly_deltas)

        score_result = state.scorer.calculate_score(
            rsi_1h=market_data.rsi_1h or 50,
            funding_current=market_data.funding_rate / 100,
            funding_accumulated=market_data.funding_accumulated / 100,
            long_ratio=market_data.long_short_ratio,
            oi_change_4d=market_data.oi_change_4d,
            price_change_4d=market_data.price_change_24h * 4,
            hourly_deltas=hourly_deltas,
            price_trend=price_trend,
            patterns=patterns
        )

        if not score_result.is_valid:
            return None

        # ✅ CoinGlass буст/штраф
        cg_delta, cg_reason = await _get_coinglass_boost(symbol, "long")
        final_score = score_result.total_score + cg_delta

        if final_score < Config.MIN_SCORE:
            return None

        reasons = list(score_result.reasons)
        if cg_reason:
            reasons.append(cg_reason)

        indicators = {
            "RSI": f"{market_data.rsi_1h:.1f}" if market_data.rsi_1h else "N/A",
            "Funding": f"{market_data.funding_rate:+.3f}%",
            "L/S Ratio": f"{market_data.long_short_ratio:.0f}% longs",
            "OI Change": f"{market_data.oi_change_4d:+.1f}% (4d)"
        }
        if cg_delta != 0:
            indicators["CoinGlass"] = f"{'+' if cg_delta > 0 else ''}{cg_delta} pts"

        signal = {
            "symbol": symbol,
            "direction": "long",
            "score": final_score,
            "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            "price": market_data.price,
            "patterns": [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "entry_price": market_data.price,
            "stop_loss": market_data.price * (1 - Config.SL_BUFFER / 100),
            "take_profits": [
                (market_data.price * (1 + tp / 100), 25 if i < 2 else 20 if i < 3 else 15 if i < 4 else 10 if i < 5 else 5)
                for i, tp in enumerate(Config.TP_LEVELS)
            ],
            "indicators": indicators,
            "reasons": reasons,
            "coinglass_delta": cg_delta,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "active"
        }

        return signal

    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def scan_market():
    """Полное сканирование рынка"""
    print(f"\n🔍 LONG Bot scan at {datetime.utcnow().isoformat()}")

    new_signals = 0

    for symbol in state.watchlist:
        try:
            existing = state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)
            if existing and existing[0].get("status") == "active":
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
                    risk="≤1% deposit"
                )

                print(f"✅ LONG Signal: {symbol} - Score: {signal['score']}%")
                new_signals += 1

            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    state.daily_signals += new_signals
    state.last_scan = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))

    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "last_scan": state.last_scan.isoformat(),
        "daily_signals": state.daily_signals,
        "active_signals": state.active_signals
    })

    print(f"✅ Scan complete. New signals: {new_signals}")


async def background_scanner():
    while state.is_running:
        try:
            await scan_market()
        except Exception as e:
            print(f"Error in background scanner: {e}")
        await asyncio.sleep(Config.SCAN_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True
    )
