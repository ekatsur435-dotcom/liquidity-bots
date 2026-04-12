"""
🔴 SHORT BOT - FastAPI Application
Поиск перекупленных монет для SHORT позиций
"""

import os
import asyncio
from datetime import datetime
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

# Добавляем путь к shared модулям
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))

from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client, MarketData
from core.scorer import get_short_scorer, ShortScorer, Direction
from core.pattern_detector import ShortPatternDetector
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Конфигурация бота"""
    BOT_TYPE = "short"
    MIN_SCORE = int(os.getenv("MIN_SHORT_SCORE", "65"))
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))  # seconds
    MAX_POSITIONS = int(os.getenv("MAX_SHORT_POSITIONS", "5"))
    LEVERAGE = os.getenv("SHORT_LEVERAGE", "5-10")
    
    # TP/SL levels
    TP_LEVELS = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]
    SL_BUFFER = float(os.getenv("SHORT_SL_BUFFER", "0.5"))


# ============================================================================
# GLOBAL STATE
# ============================================================================

class BotState:
    """Состояние бота"""
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
# LIFESPAN MANAGEMENT
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    # Startup
    print("🚀 Starting SHORT Bot...")
    
    # Initialize clients
    state.redis = get_redis_client()
    state.binance = get_binance_client()
    state.scorer = get_short_scorer(Config.MIN_SCORE)
    state.pattern_detector = ShortPatternDetector()
    state.telegram = TelegramBot(
        bot_token=os.getenv("SHORT_TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("SHORT_TELEGRAM_CHAT_ID"),
        topic_id=os.getenv("SHORT_TELEGRAM_TOPIC_ID")
    )
    
    # Test connections
    print("🔌 Testing connections...")
    redis_ok = state.redis.health_check()
    telegram_ok = await state.telegram.send_test_message()
    
    if redis_ok:
        print("✅ Redis connected")
    else:
        print("⚠️ Redis connection failed")
    
    if telegram_ok:
        print("✅ Telegram connected")
    else:
        print("⚠️ Telegram connection failed")
    
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
    symbols = await state.binance.get_all_symbols(min_volume_usdt=1_000_000)
    state.watchlist = symbols[:100]  # Топ-100 по объёму
    print(f"📊 Watchlist loaded: {len(state.watchlist)} symbols")
    
    # Update state in Redis
    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "watchlist_count": len(state.watchlist),
        "started_at": datetime.utcnow().isoformat()
    })
    
    state.is_running = True
    state.last_scan = datetime.utcnow()
 
    # ✅ ГЛАВНЫЙ ФИК: запускаем фоновый сканер!
    scanner_task = asyncio.create_task(background_scanner())
 
    print("✅ SHORT Bot started successfully!")  # или LONG Bot
 
    yield  # ← тут бот работает
 
    # Shutdown:
    print("🛑 Shutting down...")
    state.is_running = False
    scanner_task.cancel()  # останавливаем сканер
    try:
        await scanner_task
    except asyncio.CancelledError:
        pass
 
    if state.binance:
        await state.binance.close()
    if state.telegram:
        await state.telegram.close()
 
    print("👋 Bot stopped")

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="Liquidity Short Bot",
    description="🔴 SHORT Bot - Finds overbought perps for shorting",
    version="1.0.0",
    lifespan=lifespan
)


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint для Render и мониторинга"""
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
    """Полный статус бота"""
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot is not running")
    
    # Get from Redis
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
        "watchlist": state.watchlist[:10],  # Первые 10
        "watchlist_count": len(state.watchlist),
        "last_scan": state.last_scan.isoformat() if state.last_scan else None,
        "redis_connected": state.redis.health_check() if state.redis else False,
        "bot_state": bot_state
    }


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Запустить ручное сканирование"""
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot is not running")
    
    background_tasks.add_task(scan_market)
    
    return {
        "message": "Scan triggered",
        "timestamp": datetime.utcnow().isoformat(),
        "watchlist_count": len(state.watchlist)
    }


@app.get("/api/signals")
async def get_active_signals():
    """Получить активные сигналы"""
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
    """Получить сигналы для конкретной пары"""
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
    """Получить статистику торговли"""
    if not state.redis:
        raise HTTPException(status_code=503, detail="Redis not connected")
    
    stats = state.redis.get_stats_range(Config.BOT_TYPE, days)
    
    # Агрегируем
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
# CORE LOGIC - SCANNING
# ============================================================================

async def scan_symbol(symbol: str) -> Optional[Dict]:
    """
    Сканировать одну пару
    
    Args:
        symbol: Торговая пара (BTCUSDT)
    
    Returns:
        Данные сигнала или None
    """
    try:
        # Получаем рыночные данные
        market_data = await state.binance.get_complete_market_data(symbol)
        
        if not market_data:
            return None
        
        # Получаем 15m свечи для паттернов
        ohlcv_15m = await state.binance.get_klines(symbol, "15m", 100)
        
        if not ohlcv_15m or len(ohlcv_15m) < 20:
            return None
        
        # Расчёт дельты (приближённо через объёмы)
        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        
        # Определяем тренд цены
        price_trend = state.pattern_detector._get_price_trend(ohlcv_15m)
        
        # Детектируем паттерны
        patterns = state.pattern_detector.detect_all(
            ohlcv_15m, 
            hourly_deltas,
            None
        )
        
        # Расчёт Short Score
        score_result = state.scorer.calculate_score(
            rsi_1h=market_data.rsi_1h or 50,
            funding_current=market_data.funding_rate / 100,  # Проценты
            funding_accumulated=market_data.funding_accumulated / 100,
            long_ratio=market_data.long_short_ratio,
            oi_change_4d=market_data.oi_change_4d,
            price_change_4d=market_data.price_change_24h * 4,  # Приблизительно
            hourly_deltas=hourly_deltas,
            price_trend=price_trend,
            patterns=patterns
        )
        
        # Проверяем минимальный скор
        if not score_result.is_valid:
            return None
        
        # Формируем сигнал
        signal = {
            "symbol": symbol,
            "direction": "short",
            "score": score_result.total_score,
            "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            "price": market_data.price,
            "patterns": [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "entry_price": market_data.price,
            "stop_loss": market_data.price * (1 + Config.SL_BUFFER / 100),
            "take_profits": [
                (market_data.price * (1 - tp / 100), 25 if i < 2 else 20 if i < 3 else 15 if i < 4 else 10 if i < 5 else 5)
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
            "status": "active",
            "valid_until": (datetime.utcnow().replace(minute=datetime.utcnow().minute + 30)).isoformat()
        }
        
        return signal
    
    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def scan_market():
    """Полное сканирование рынка"""
    print(f"\n🔍 Starting market scan at {datetime.utcnow().isoformat()}")
    print(f"📊 Scanning {len(state.watchlist)} symbols...")
    
    new_signals = 0
    
    for symbol in state.watchlist:
        try:
            # Проверяем нет ли уже активного сигнала
            existing = state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)
            if existing and existing[0].get("status") == "active":
                continue
            
            # Сканируем
            signal = await scan_symbol(symbol)
            
            if signal:
                # Сохраняем в Redis
                state.redis.save_signal(Config.BOT_TYPE, symbol, signal)
                
                # Отправляем в Telegram
                await state.telegram.send_signal(
                    direction="short",
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
                
                print(f"✅ Signal generated: {symbol} - Score: {signal['score']}%")
                new_signals += 1
            
            # Небольшая задержка чтобы не перегружать API
            await asyncio.sleep(0.5)
        
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue
    
    # Обновляем статистику
    state.daily_signals += new_signals
    state.last_scan = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))
    
    # Обновляем состояние в Redis
    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "last_scan": state.last_scan.isoformat(),
        "daily_signals": state.daily_signals,
        "active_signals": state.active_signals
    })
    
    print(f"✅ Scan complete. New signals: {new_signals}")
    print(f"📊 Active signals: {state.active_signals}")


async def background_scanner():
    """Фоновое сканирование каждые N секунд"""
    while state.is_running:
        try:
            await scan_market()
        except Exception as e:
            print(f"Error in background scanner: {e}")
        
        # Ждём до следующего скана
        await asyncio.sleep(Config.SCAN_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    # Для локального запуска
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True
    )
