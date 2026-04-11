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

# Добавляем путь к shared модулям
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))

from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client
from core.scorer import get_long_scorer
from core.pattern_detector import LongPatternDetector
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Конфигурация бота"""
    BOT_TYPE = "long"
    MIN_SCORE = int(os.getenv("MIN_LONG_SCORE", "65"))
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
    MAX_POSITIONS = int(os.getenv("MAX_LONG_POSITIONS", "5"))
    LEVERAGE = os.getenv("LONG_LEVERAGE", "3-5")
    
    TP_LEVELS = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]
    SL_BUFFER = float(os.getenv("LONG_SL_BUFFER", "0.8"))


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
    
    # Test connections
    redis_ok = state.redis.health_check()
    telegram_ok = await state.telegram.send_test_message()
    
    print(f"✅ Redis: {redis_ok}, Telegram: {telegram_ok}")
    
    # Initialize command handler
    state.cmd_handler = TelegramCommandHandler(
        bot=state.telegram,
        redis_client=state.redis,
        bot_state=state,
        bot_type=Config.BOT_TYPE        # ← добавить это
    )
    
    # Register webhook in Telegram
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        webhook_url = f"{render_url}/webhook"
        await state.telegram.setup_webhook(webhook_url)
        print(f"✅ Webhook registered: {webhook_url}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set — webhook not registered")
    
    # Load watchlist
    symbols = await state.binance.get_all_symbols(min_volume_usdt=50_000_000)
    state.watchlist = symbols[:50]
    print(f"📊 Watchlist: {len(state.watchlist)} symbols")
    
    # Update state
    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "watchlist_count": len(state.watchlist),
        "started_at": datetime.utcnow().isoformat()
    })
    
    state.is_running = True
    state.last_scan = datetime.utcnow()
    
    print("✅ LONG Bot started!")
    
    yield
    
    print("🛑 Shutting down LONG Bot...")
    state.is_running = False
    
    if state.redis:
        state.redis.update_bot_state(Config.BOT_TYPE, {
            "status": "stopped",
            "stopped_at": datetime.utcnow().isoformat()
        })
    
    if state.binance:
        await state.binance.close()
    if state.telegram:
        await state.telegram.close()
    
    print("👋 LONG Bot stopped")


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
            "leverage": Config.LEVERAGE
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
    
    return {
        "message": "Scan triggered",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/api/signals")
async def get_active_signals():
    if not state.redis:
        raise HTTPException(status_code=503, detail="Redis not connected")
    
    signals = state.redis.get_active_signals(Config.BOT_TYPE)
    
    return {
        "bot_type": Config.BOT_TYPE,
        "count": len(signals),
        "signals": signals
    }


@app.get("/api/signals/{symbol}")
async def get_symbol_signals(symbol: str):
    if not state.redis:
        raise HTTPException(status_code=503, detail="Redis not connected")
    
    signals = state.redis.get_signals(Config.BOT_TYPE, symbol.upper())
    
    return {
        "symbol": symbol.upper(),
        "count": len(signals),
        "signals": signals
    }


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
    """
    Endpoint для приёма команд от Telegram.
    Telegram будет слать сюда все сообщения пользователей.
    """
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
    """Проверить статус webhook"""
    if state.telegram:
        info = await state.telegram.get_webhook_info()
        return {"webhook": info}
    return {"error": "Telegram not initialized"}


# ============================================================================
# CORE LOGIC
# ============================================================================

async def scan_symbol(symbol: str) -> Optional[Dict]:
    """Сканировать одну пару для LONG"""
    try:
        market_data = await state.binance.get_complete_market_data(symbol)
        
        if not market_data:
            return None
        
        ohlcv_15m = await state.binance.get_klines(symbol, "15m", 50)
        
        if not ohlcv_15m or len(ohlcv_15m) < 20:
            return None
        
        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend = state.pattern_detector._get_price_trend(ohlcv_15m)
        
        # Детектируем LONG паттерны
        patterns = state.pattern_detector.detect_all(ohlcv_15m, hourly_deltas)
        
        # Расчёт Long Score
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
        
        # Формируем сигнал
        signal = {
            "symbol": symbol,
            "direction": "long",
            "score": score_result.total_score,
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
            "indicators": {
                "RSI": f"{market_data.rsi_1h:.1f}" if market_data.rsi_1h else "N/A",
                "Funding": f"{market_data.funding_rate:+.3f}%",
                "L/S Ratio": f"{market_data.long_short_ratio:.0f}% longs",
                "OI Change": f"{market_data.oi_change_4d:+.1f}% (4d)"
            },
            "reasons": score_result.reasons,
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
