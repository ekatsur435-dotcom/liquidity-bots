"""
🟢 LONG BOT v2.1 — FastAPI Application
FIXES:
  - SL/TP направление: теперь ПРАВИЛЬНОЕ для LONG (SL ниже, TP выше)
  - SL_BUFFER увеличен до 2.5% (был 0.5% — слишком мал)
  - price_change_4d: исправлен CandleData.close вместо [3]
  - SMC интегрирован в scan_symbol (уточняет SL через Order Block)
  - Команды работают из лички (reply_chat_id)
  - BingX AutoTrader подключён (если AUTO_TRADING_ENABLED=true)
  - background_scanner запускается в lifespan
  - PositionTracker мониторит TP/SL в реальном времени
"""

import os
import asyncio
from datetime import datetime, timedelta
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

    # ✅ FIX: SL увеличен с 0.5% до 2.5% — был слишком мал, выбивало мгновенно
    # Правило: SL должен быть ≥ 2x ATR или ≥ 2% для альткоинов
    SL_BUFFER     = float(os.getenv("LONG_SL_BUFFER", "2.5"))

    # TP уровни — % движения ВВЕРХ (для LONG)
    TP_LEVELS  = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]
    TP_WEIGHTS = [20,  20,  20,  15,  15,  10]   # % позиции (сумма=100)

    SIGNAL_TTL_HOURS = 24

    # BingX авто-трейдинг
    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))  # 1%

    # SMC
    USE_SMC       = os.getenv("USE_SMC", "true").lower() == "true"

    # CoinGlass
    USE_COINGLASS = bool(os.getenv("COINGLASS_API_KEY", ""))


# ============================================================================
# GLOBAL STATE
# ============================================================================

class BotState:
    def __init__(self):
        self.is_running   = False
        self.is_paused    = False
        self.last_scan    = None
        self.active_signals = 0
        self.daily_signals  = 0
        self.watchlist: List[str] = []
        self.redis        = None
        self.binance      = None
        self.scorer       = None
        self.pattern_detector = None
        self.telegram     = None
        self.cmd_handler  = None
        self.auto_trader  = None
        self.tracker: Optional[PositionTracker] = None
        self.coinglass    = None
        self._min_score   = Config.MIN_SCORE  # для /setscore

state = BotState()


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting LONG Bot v2.1...")

    state.redis   = get_redis_client()
    state.binance = get_binance_client()
    state.scorer  = get_long_scorer(Config.MIN_SCORE)
    state.pattern_detector = LongPatternDetector()
    state.telegram = TelegramBot(
        bot_token=os.getenv("LONG_TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("LONG_TELEGRAM_CHAT_ID"),
        topic_id=os.getenv("LONG_TELEGRAM_TOPIC_ID"),
    )

    # Проверка соединений
    redis_ok    = state.redis.health_check()
    telegram_ok = await state.telegram.send_test_message()
    print(f"{'✅' if redis_ok else '❌'} Redis | {'✅' if telegram_ok else '❌'} Telegram")

    # Command handler
    state.cmd_handler = TelegramCommandHandler(
        bot=state.telegram,
        redis_client=state.redis,
        bot_state=state,
        bot_type=Config.BOT_TYPE,
        scan_callback=scan_market,
    )

    # Webhook (один раз, без дублирования)
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        ok = await state.telegram.setup_webhook(f"{render_url}/webhook")
        if not ok:
            print("⚠️ Webhook registration failed — commands may not work")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set")

    # BingX AutoTrader
    if Config.AUTO_TRADING:
        try:
            from api.bingx_client import BingXClient
            from execution.auto_trader import AutoTrader, TradeConfig
            bingx = BingXClient(
                api_key=os.getenv("BINGX_API_KEY"),
                api_secret=os.getenv("BINGX_API_SECRET"),
                demo=Config.BINGX_DEMO,
            )
            trade_cfg = TradeConfig(
                enabled=True,
                demo_mode=Config.BINGX_DEMO,
                risk_per_trade=Config.RISK_PER_TRADE,
                max_positions=Config.MAX_POSITIONS,
                min_score_for_trade=Config.MIN_SCORE,  # синхронизируем с MIN_SCORE бота
            )
            state.auto_trader = AutoTrader(
                bingx_client=bingx,
                config=trade_cfg,
                telegram=state.telegram,   # ← передаём для уведомлений
            )
            mode = "DEMO" if Config.BINGX_DEMO else "REAL"
            print(f"✅ BingX AutoTrader initialized ({mode})")
        except Exception as e:
            print(f"⚠️ AutoTrader init failed: {e}")
            state.auto_trader = None
    else:
        print("ℹ️ AutoTrader disabled (AUTO_TRADING_ENABLED=false)")

    # CoinGlass (опционально)
    if Config.USE_COINGLASS:
        try:
            from api.coinglass_client import CoinglassClient
            state.coinglass = CoinglassClient(api_key=os.getenv("COINGLASS_API_KEY"))
            print("✅ CoinGlass connected")
        except Exception as e:
            print(f"⚠️ CoinGlass: {e}")
            Config.USE_COINGLASS = False

    # Watchlist
    symbols = await state.binance.get_all_symbols(min_volume_usdt=1_000_000)
    state.watchlist = symbols[:100]
    print(f"📊 Watchlist: {len(state.watchlist)} symbols")

    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "running",
        "watchlist_count": len(state.watchlist),
        "started_at": datetime.utcnow().isoformat(),
        "sl_buffer_pct": Config.SL_BUFFER,
        "auto_trading": Config.AUTO_TRADING,
    })

    state.is_running = True
    state.last_scan  = datetime.utcnow()

    # Запуск фоновых задач
    scanner_task = asyncio.create_task(background_scanner())

    state.tracker = PositionTracker(
        bot_type=Config.BOT_TYPE,
        telegram=state.telegram,
        redis_client=state.redis,
        binance_client=state.binance,
        config=Config,
    )
    tracker_task = asyncio.create_task(state.tracker.run())

    print("✅ LONG Bot started!")
    await state.telegram.send_message(
        f"🟢 <b>LONG Bot запущен</b>\n"
        f"📊 Watchlist: {len(state.watchlist)} монет\n"
        f"🛑 SL: {Config.SL_BUFFER}%  |  Score min: {Config.MIN_SCORE}%\n"
        f"🤖 AutoTrader: {'✅ ' + ('DEMO' if Config.BINGX_DEMO else 'REAL') if Config.AUTO_TRADING else '❌ OFF'}"
    )

    yield  # ← бот работает

    # Shutdown
    print("🛑 Shutting down LONG Bot...")
    state.is_running = False

    if state.tracker:
        state.tracker.stop()

    for task in (scanner_task, tracker_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if state.auto_trader:
        try:
            await state.auto_trader.bingx.close()
        except Exception:
            pass
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
    description="🟢 LONG Bot v2.1",
    version="2.1.0",
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
        "sl_buffer_pct": Config.SL_BUFFER,
        "auto_trading": Config.AUTO_TRADING,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/status")
async def get_status():
    bot_state = state.redis.get_bot_state(Config.BOT_TYPE)
    return {
        "status": "paused" if state.is_paused else "running",
        "bot_type": Config.BOT_TYPE,
        "config": {
            "min_score": Config.MIN_SCORE,
            "sl_buffer_pct": Config.SL_BUFFER,
            "tp_levels": Config.TP_LEVELS,
            "tp_weights": Config.TP_WEIGHTS,
            "leverage": Config.LEVERAGE,
            "auto_trading": Config.AUTO_TRADING,
            "use_smc": Config.USE_SMC,
        },
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
    signals = state.redis.get_active_signals(Config.BOT_TYPE)
    return {"bot_type": Config.BOT_TYPE, "count": len(signals), "signals": signals}


@app.get("/api/signals/{symbol}")
async def get_symbol_signals(symbol: str):
    signals = state.redis.get_signals(Config.BOT_TYPE, symbol.upper())
    return {"symbol": symbol.upper(), "count": len(signals), "signals": signals}


@app.get("/api/stats")
async def get_stats(days: int = 7):
    bot_st = state.redis.get_bot_state(Config.BOT_TYPE) or {}
    daily_trades = bot_st.get("daily_trades", {})
    result = []
    for i in range(days):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({
            "date": day,
            **daily_trades.get(day, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        })
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
    """Получаем команды от Telegram."""
    try:
        update = await request.json()
        if state.cmd_handler:
            await state.cmd_handler.handle_update(update)
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False}


@app.get("/webhook/info")
async def webhook_info():
    if state.telegram:
        return {"webhook": await state.telegram.get_webhook_info()}
    return {"error": "Telegram not initialized"}


# ============================================================================
# CORE LOGIC
# ============================================================================

async def _get_real_price_change_4d(symbol: str, fallback: float) -> float:
    """
    ✅ FIX: Используем .close атрибут CandleData, не индекс [3].
    Было: float(klines[-1][3]) → AttributeError: 'CandleData' not subscriptable
    Стало: klines[-1].close
    """
    try:
        klines = await state.binance.get_klines(symbol, "1d", 6)
        if klines and len(klines) >= 5:
            close_now = klines[-1].close   # ✅ атрибут
            close_4d  = klines[-5].close   # ✅ атрибут
            if close_4d > 0:
                return round((close_now - close_4d) / close_4d * 100, 2)
    except Exception as e:
        pass  # тихо используем fallback
    return fallback


def _is_signal_fresh(existing_signals: List[Dict]) -> bool:
    if not existing_signals:
        return False
    sig = existing_signals[0]
    if sig.get("status") not in ("active",):
        return False
    created_at = sig.get("timestamp", "")
    if not created_at:
        return True
    try:
        age_h = (datetime.utcnow() - datetime.fromisoformat(created_at)).total_seconds() / 3600
        return age_h < Config.SIGNAL_TTL_HOURS
    except Exception:
        return True


def _get_ohlcv_list(candles) -> List[List[float]]:
    """Конвертируем List[CandleData] → List[List[float]] для SMC детектора."""
    return [[c.open, c.high, c.low, c.close, c.volume] for c in candles]


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """
    Сканировать одну пару для LONG.

    ✅ ИСПРАВЛЕНО:
    - SL теперь НИЖЕ входа (было выше — SHORT логика!)
    - TP теперь ВЫШЕ входа (были ниже — SHORT логика!)
    - SMC уточняет SL через Order Block
    - price_change_4d через .close атрибут
    """
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

        # ✅ FIX price_change_4d
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

        price = market_data.price
        reasons = list(score_result.reasons)
        final_score = score_result.total_score

        # ─── SMC: уточняем SL через Order Block ───────────────────────────
        # ✅ SL по умолчанию НИЖЕ входа (LONG!)
        stop_loss  = price * (1 - Config.SL_BUFFER / 100)
        entry_price = price
        smc_data = {}

        if Config.USE_SMC:
            try:
                from core.smc_ict_detector import get_smc_result
                ohlcv_list = _get_ohlcv_list(ohlcv_15m)
                smc = get_smc_result(
                    ohlcv_list, "long",
                    base_sl_pct=Config.SL_BUFFER,
                    base_entry=price
                )
                if smc.score_bonus > 0:
                    final_score += smc.score_bonus
                    reasons.extend(smc.reasons)
                if smc.refined_sl:
                    # SL от SMC должен быть НИЖЕ цены для LONG
                    if smc.refined_sl < price:
                        stop_loss = smc.refined_sl
                if smc.ob_entry:
                    entry_price = smc.ob_entry
                smc_data = {
                    "has_ob": smc.has_ob,
                    "has_fvg": smc.has_fvg,
                    "score_bonus": smc.score_bonus,
                }
            except Exception as e:
                print(f"SMC error {symbol}: {e}")

        if final_score < Config.MIN_SCORE:
            return None

        # ─── CoinGlass буст ───────────────────────────────────────────────
        cg_delta = 0
        if Config.USE_COINGLASS and state.coinglass:
            try:
                cg_sym = symbol.replace("USDT", "")
                liq = await state.coinglass.get_liquidation_signal(cg_sym)
                if liq and liq.get("signal") == "long":
                    cg_delta = min(8, liq.get("strength", 0) // 5)
                    reasons.append(f"CoinGlass liq: {liq.get('reason', '')} +{cg_delta}pts")
                    final_score += cg_delta
            except Exception:
                pass

        # ─── SL/TP ────────────────────────────────────────────────────────
        # ✅ TP ВЫШЕ входа (LONG!)
        take_profits = [
            (round(price * (1 + tp / 100), 8), Config.TP_WEIGHTS[i])
            for i, tp in enumerate(Config.TP_LEVELS)
        ]

        # Минимальный SL: хотя бы 1.5% от входа (защита от мгновенного выбивания)
        min_sl_distance = price * 0.015
        if price - stop_loss < min_sl_distance:
            stop_loss = price * (1 - max(Config.SL_BUFFER, 1.5) / 100)

        indicators = {
            "RSI":      f"{market_data.rsi_1h:.1f}" if market_data.rsi_1h else "N/A",
            "Funding":  f"{market_data.funding_rate:+.3f}%",
            "L/S Ratio": f"{market_data.long_short_ratio:.0f}% longs",
            "OI Change": f"{market_data.oi_change_4d:+.1f}% (4d)",
            "Price 4d":  f"{price_change_4d:+.1f}%",
        }
        if cg_delta:
            indicators["CoinGlass"] = f"+{cg_delta}pts"

        sl_pct = abs((stop_loss - price) / price * 100)
        signal = {
            "symbol":          symbol,
            "direction":       "long",
            "score":           final_score,
            "grade":           score_result.grade,
            "confidence":      score_result.confidence.value,
            "price":           price,
            "patterns":        [p.name for p in patterns],
            "best_pattern":    patterns[0].name if patterns else None,
            "entry_price":     entry_price,
            "stop_loss":       round(stop_loss, 8),
            "sl_pct":          round(sl_pct, 2),
            "take_profits":    take_profits,
            "indicators":      indicators,
            "reasons":         reasons,
            "smc":             smc_data,
            "coinglass_delta": cg_delta,
            "timestamp":       datetime.utcnow().isoformat(),
            "status":          "active",
            "taken_tps":       [],
        }

        return signal

    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def _count_real_positions() -> int:
    """
    Считает РЕАЛЬНЫЕ открытые позиции:
    1. Если AutoTrader активен — спрашиваем BingX напрямую (точно)
    2. Иначе — считаем сигналы из Redis созданные менее SIGNAL_TTL_HOURS назад

    Это исправляет баг "Max positions reached (23/5)" — старые сигналы
    в Redis не должны блокировать новые торги.
    """
    # Способ 1: считаем живые позиции на бирже
    if state.auto_trader and Config.AUTO_TRADING:
        try:
            positions = await state.auto_trader.bingx.get_positions()
            return len(positions)
        except Exception as e:
            print(f"[count_positions] BingX error: {e}, falling back to Redis")

    # Способ 2: только свежие сигналы (< SIGNAL_TTL_HOURS)
    try:
        all_active = state.redis.get_active_signals(Config.BOT_TYPE)
        cutoff = datetime.utcnow() - timedelta(hours=Config.SIGNAL_TTL_HOURS)
        fresh = []
        for s in all_active:
            try:
                ts = datetime.fromisoformat(s.get("timestamp", ""))
                if ts > cutoff:
                    fresh.append(s)
            except Exception:
                pass
        return len(fresh)
    except Exception:
        return 0


async def scan_market():
    """Полное сканирование рынка."""
    if state.is_paused:
        print("⏸ Scan skipped — paused")
        return

    print(f"\n🔍 LONG scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    # FIX: считаем реальные позиции, не накопившиеся Redis-сигналы
    active_count = await _count_real_positions()
    if active_count >= Config.MAX_POSITIONS:
        print(f"⏸️ Max positions reached ({active_count}/{Config.MAX_POSITIONS})")
        state.last_scan = datetime.utcnow()
        return

    new_signals = 0

    for symbol in state.watchlist:
        if new_signals + active_count >= Config.MAX_POSITIONS:
            break

        try:
            existing = state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)
            if _is_signal_fresh(existing):
                continue

            signal = await scan_symbol(symbol)

            if signal:
                state.redis.save_signal(Config.BOT_TYPE, symbol, signal)

                # Отправляем сигнал в Telegram
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

                # BingX AutoTrader
                if state.auto_trader and Config.AUTO_TRADING:
                    try:
                        await state.auto_trader.open_position(
                        symbol=signal["symbol"],
                        direction="long",
                        entry_price=signal["entry_price"],
                        stop_loss=signal["stop_loss"],
                        take_profits=signal["take_profits"],
                        signal_score=signal["score"],
                        smc_data=signal.get("smc"),
                    )
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")

                print(f"✅ LONG Signal: {symbol} Score={signal['score']}% SL={signal['sl_pct']}%")
                new_signals += 1

            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"Error {symbol}: {e}")
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

    print(f"✅ Scan complete. New: {new_signals} | Active: {state.active_signals}")


async def background_scanner():
    """Фоновый сканер. is_paused не убивает задачу."""
    while state.is_running:
        if not state.is_paused:
            try:
                await scan_market()
            except Exception as e:
                print(f"Scanner error: {e}")
        else:
            print("⏸ Scan skipped (paused)")
        await asyncio.sleep(Config.SCAN_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
