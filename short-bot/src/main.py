"""
🔴 SHORT BOT v2.2 — FastAPI Application

ИСПРАВЛЕНО:
  - price_change_4d: .close атрибут (не [3])
  - SL ВЫШЕ входа (SHORT ✅), TP НИЖЕ входа (SHORT ✅)
  - SL_BUFFER: 2.5% (был 0.5% — выбивало мгновенно)
  - SMC: уточняет SL через Bearish Order Block
  - background_scanner запускается в lifespan
  - PositionTracker мониторит TP/SL/экспирацию
  - BingX AutoTrader: open_position при сигнале
  - Webhook: регистрируется один раз, отвечает в личку и группу
"""

import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
import uvicorn

import sys

# ─── надёжный поиск shared/ на Render и локально ─────────────────────────────
def _find_shared() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "shared"),
        os.path.join(here, "..", "shared"),
        os.path.join(here, "..", "..", "shared"),
        os.path.join(here, "..", "..", "..", "shared"),
        "/opt/render/project/src/shared",
    ]
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.isdir(c):
            return c
    return os.path.join(here, "..", "..", "shared")

_SHARED = _find_shared()
for _p in [_SHARED, os.path.dirname(_SHARED)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
print(f"📁 shared path: {_SHARED}")
# ─────────────────────────────────────────────────────────────────────────────

from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client
from core.scorer import get_short_scorer
from core.pattern_detector import ShortPatternDetector
from core.position_tracker import PositionTracker
from core.short_filter import get_short_filter, get_short_tp_config, ShortFilterResult
from core.realtime_scorer import get_realtime_scorer
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "short"
    MIN_SCORE     = int(os.getenv("MIN_SHORT_SCORE", "65"))
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "180"))
    MAX_POSITIONS = int(os.getenv("MAX_SHORT_POSITIONS", "20"))
    LEVERAGE      = os.getenv("SHORT_LEVERAGE", "5-50")

    # SL ВЫШЕ входа для SHORT (цена должна упасть)
    # 2.5% — разумный минимум для альткоинов (был 0.5% — выбивало мгновенно)
    SL_BUFFER     = float(os.getenv("SHORT_SL_BUFFER", "2.5"))

    # TP НИЖЕ входа для SHORT (% падения)
    # SHORT TPs — динамические (short_filter.get_short_tp_config выбирает профиль)
    TP_LEVELS  = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]   # дефолт (будет пересчитан)
    TP_WEIGHTS = [25,  25,  20,  15,  10,   5]       # SHORT: больше веса на TP1-2

    # Trailing stop — SHORT активирует РАНЬШЕ (+1%, не +1.5%)
    TRAIL_ACTIVATION = float(os.getenv("SHORT_TRAIL_ACTIVATION", "0.010"))  # 1%

    SIGNAL_TTL_HOURS = 24

    # BingX
    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.001"))

    # SMC и CoinGlass
    USE_SMC        = os.getenv("USE_SMC", "true").lower() == "true"
    USE_COINGLASS  = bool(os.getenv("COINGLASS_API_KEY", ""))

    # Watchlist — управляется через Render Environment Variables
    MIN_VOLUME_USDT = int(os.getenv("MIN_VOLUME_USDT", "1000000"))
    MAX_WATCHLIST   = int(os.getenv("MAX_WATCHLIST", "200"))


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
        self._min_score   = Config.MIN_SCORE

state = BotState()


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting SHORT Bot v2.2...")

    state.redis   = get_redis_client()
    state.binance = get_binance_client()
    state.scorer  = get_short_scorer(Config.MIN_SCORE)
    state.pattern_detector = ShortPatternDetector()
    state.telegram = TelegramBot(
        bot_token=os.getenv("SHORT_TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("SHORT_TELEGRAM_CHAT_ID"),
        topic_id=os.getenv("SHORT_TELEGRAM_TOPIC_ID"),
    )

    redis_ok    = state.redis.health_check()
    telegram_ok = await state.telegram.send_test_message()
    print(f"{'✅' if redis_ok else '❌'} Redis | {'✅' if telegram_ok else '❌'} Telegram")

    state.cmd_handler = TelegramCommandHandler(
        bot=state.telegram,
        redis_client=state.redis,
        bot_state=state,
        bot_type=Config.BOT_TYPE,
        scan_callback=scan_market,
        config=Config,
    )

    # Webhook — один раз
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        await state.telegram.setup_webhook(f"{render_url}/webhook")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set")

    # BingX AutoTrader
    print(f"🔧 AUTO_TRADING={Config.AUTO_TRADING} | DEMO={Config.BINGX_DEMO}")
    if Config.AUTO_TRADING:
        try:
            from api.bingx_client import BingXClient
            from execution.auto_trader import AutoTrader, TradeConfig
            bingx = BingXClient(
                api_key=os.getenv("BINGX_API_KEY"),
                api_secret=os.getenv("BINGX_API_SECRET"),
                demo=Config.BINGX_DEMO,
            )
            connected = await bingx.test_connection()
            if not connected:
                print("⚠️ BingX connection failed — AutoTrader disabled")
            else:
                trade_cfg = TradeConfig(
                    enabled=True,
                    demo_mode=Config.BINGX_DEMO,
                    risk_per_trade=Config.RISK_PER_TRADE,
                    max_positions=Config.MAX_POSITIONS,
                    min_score_for_trade=Config.MIN_SCORE,
                )
                state.auto_trader = AutoTrader(
                    bingx_client=bingx,
                    config=trade_cfg,
                    telegram=state.telegram,
                )
                mode = "DEMO" if Config.BINGX_DEMO else "REAL"
                print(f"✅ BingX AutoTrader ready ({mode})")
        except Exception as e:
            print(f"❌ AutoTrader init FAILED: {e}")
            import traceback
            traceback.print_exc()
            state.auto_trader = None
    else:
        print("ℹ️ AutoTrader disabled (AUTO_TRADING_ENABLED=false)")

    # CoinGlass
    if Config.USE_COINGLASS:
        try:
            from utils.coinglass_client import CoinglassClient
            state.coinglass = CoinglassClient(api_key=os.getenv("COINGLASS_API_KEY"))
            print("✅ CoinGlass connected")
        except Exception as e:
            print(f"⚠️ CoinGlass: {e}")
            Config.USE_COINGLASS = False

    # Watchlist
    symbols = await state.binance.get_all_symbols(min_volume_usdt=Config.MIN_VOLUME_USDT)
    state.watchlist = symbols[:Config.MAX_WATCHLIST]
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

    scanner_task = asyncio.create_task(background_scanner())
    state.tracker = PositionTracker(
        bot_type=Config.BOT_TYPE,
        telegram=state.telegram,
        redis_client=state.redis,
        binance_client=state.binance,
        config=Config,
    )
    tracker_task = asyncio.create_task(state.tracker.run())

    trader_status = f"✅ {'DEMO' if Config.BINGX_DEMO else 'REAL'}" if state.auto_trader else "❌ OFF"
    print(f"✅ SHORT Bot started! AutoTrader: {trader_status}")
    await state.telegram.send_message(
        f"🔴 <b>SHORT Bot v2.2 запущен</b>\n\n"
        f"📊 Watchlist: {len(state.watchlist)} монет\n"
        f"🛑 SL: {Config.SL_BUFFER}%  |  Score≥{Config.MIN_SCORE}%\n"
        f"🤖 AutoTrader: {trader_status}\n"
        f"🔍 ShortFilter: ✅ | RealtimeScorer: ✅"
    )

    yield

    print("🛑 Shutting down SHORT Bot...")
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
    print("👋 SHORT Bot stopped")


# ============================================================================
# APP
# ============================================================================

app = FastAPI(
    title="Liquidity Short Bot",
    description="🔴 SHORT Bot v2.2",
    version="2.2.0",
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
        raise HTTPException(status_code=503, detail="Bot not running")
    if state.is_paused:
        raise HTTPException(status_code=409, detail="Bot paused")
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
    """Реальное 4-дневное изменение — через .close атрибут CandleData."""
    try:
        klines = await state.binance.get_klines(symbol, "1d", 6)
        if klines and len(klines) >= 5:
            close_now = klines[-1].close
            close_4d  = klines[-5].close
            if close_4d > 0:
                return round((close_now - close_4d) / close_4d * 100, 2)
    except Exception:
        pass
    return fallback


def _is_signal_fresh(existing: List[Dict]) -> bool:
    if not existing:
        return False
    sig = existing[0]
    if sig.get("status") != "active":
        return False
    try:
        age_h = (datetime.utcnow() - datetime.fromisoformat(sig["timestamp"])).total_seconds() / 3600
        return age_h < Config.SIGNAL_TTL_HOURS
    except Exception:
        return True


def _ohlcv_list(candles) -> List[List[float]]:
    return [[c.open, c.high, c.low, c.close, c.volume] for c in candles]


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """
    Сканировать пару для SHORT.

    SL = ВЫШЕ входа (short: цена должна упасть)
    TP = НИЖЕ входа (short: фиксируем прибыль при падении)
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

        price       = market_data.price
        final_score = score_result.total_score
        reasons     = list(score_result.reasons)

        # ── SHORT-специфичные фильтры ─────────────────────────────────────
        # BTC 1h изменение для режима рынка
        btc_change_1h: Optional[float] = None
        try:
            btc_md = await state.binance.get_complete_market_data("BTCUSDT")
            if btc_md:
                btc_change_1h = btc_md.price_change_24h / 24  # грубо: 24ч / 24 = 1ч
        except Exception:
            pass

        sf = get_short_filter()
        filt = sf.check(
            market_data          = market_data,
            ohlcv_15m            = ohlcv_15m,
            hourly_deltas        = hourly_deltas,
            btc_price_1h_change  = btc_change_1h,
        )

        if filt.blocked:
            print(f"[SHORT filter] {symbol}: {filt.block_reason}")
            return None

        final_score += filt.score_delta
        reasons.extend(filt.reasons)

        # ── Realtime бонус (OI 15m, taker ratio, ликвидации) ──────────────
        rt = get_realtime_scorer()
        rt_result = rt.score(
            direction     = "short",
            market_data   = market_data,
            base_score    = final_score,
            hourly_deltas = hourly_deltas,
        )
        final_score   = rt_result.final_score
        reasons.extend(rt_result.factors)

        # EARLY SHORT сигнал — Telegram без сделки
        if rt_result.early_only:
            await state.telegram.send_message(
                f"🛰️ <b>РАННИЙ SHORT WATCH</b>  Score: {final_score}%\n\n"
                f"🔴 <code>{symbol}</code>  ${price:,.6f}\n"
                + "\n".join(f"  • {r}" for r in rt_result.factors[:4])
                + f"\n\n⏳ <i>Ждём подтверждения. Сделка не открыта.</i>"
            )
            return None

        if final_score < Config.MIN_SCORE:
            return None

        # ── Динамические TP для SHORT ──────────────────────────────────────
        best_pattern = patterns[0].name if patterns else None
        btc_trend = "down" if (btc_change_1h or 0) < -0.5 else (
                    "up" if (btc_change_1h or 0) > 0.5 else "sideways")
        tp_levels, tp_weights = get_short_tp_config(
            funding_rate = market_data.funding_rate,
            pattern_name = best_pattern,
            btc_trend    = btc_trend,
        )

        # ── SL: ВЫШЕ входа для SHORT ──────────────────────────────────────
        stop_loss   = price * (1 + Config.SL_BUFFER / 100)
        entry_price = price
        smc_data    = {}

        if Config.USE_SMC:
            try:
                from utils.smc_ict_detector import get_smc_result
                smc = get_smc_result(
                    _ohlcv_list(ohlcv_15m), "short",
                    base_sl_pct=Config.SL_BUFFER,
                    base_entry=price,
                )
                if smc.score_bonus > 0:
                    final_score += smc.score_bonus
                    reasons.extend(smc.reasons)
                # SL от SMC должен быть ВЫШЕ цены для SHORT
                if smc.refined_sl and smc.refined_sl > price:
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

        # CoinGlass
        cg_delta = 0
        if Config.USE_COINGLASS and state.coinglass:
            try:
                cg_sym = symbol.replace("USDT", "")
                liq = await state.coinglass.get_liquidation_signal(cg_sym)
                if liq and liq.get("signal") == "short":
                    cg_delta = min(8, liq.get("strength", 0) // 5)
                    reasons.append(f"CoinGlass short liq +{cg_delta}pts")
                    final_score += cg_delta
            except Exception:
                pass

        # ── TP: НИЖЕ входа для SHORT (динамические уровни) ───────────────
        take_profits = [
            (round(price * (1 - tp / 100), 8), tp_weights[i])
            for i, tp in enumerate(tp_levels)
        ]

        # Минимум 1.5% SL от входа
        if stop_loss - price < price * 0.015:
            stop_loss = price * (1 + max(Config.SL_BUFFER, 1.5) / 100)

        sl_pct = abs((stop_loss - price) / price * 100)

        indicators = {
            "RSI":       f"{market_data.rsi_1h:.1f}" if market_data.rsi_1h else "N/A",
            "Funding":   f"{market_data.funding_rate:+.3f}%",
            "L/S Ratio": f"{market_data.long_short_ratio:.0f}% longs",
            "OI Change": f"{market_data.oi_change_4d:+.1f}% (4d)",
            "Price 4d":  f"{price_change_4d:+.1f}%",
        }
        if cg_delta:
            indicators["CoinGlass"] = f"+{cg_delta}pts"

        return {
            "symbol":          symbol,
            "direction":       "short",
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

    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def scan_market():
    if state.is_paused:
        print("⏸ Scan skipped — paused")
        return

    print(f"\n🔍 SHORT scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    active_count = len(state.redis.get_active_signals(Config.BOT_TYPE))
    if active_count >= Config.MAX_POSITIONS:
        print(f"⚠️ Max positions ({Config.MAX_POSITIONS}) reached")
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
                    risk="≤1% deposit",
                )

                if state.auto_trader and Config.AUTO_TRADING:
                    try:
                        await state.auto_trader.execute_signal(signal)
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")

                print(f"✅ SHORT Signal: {symbol} Score={signal['score']}% SL={signal['sl_pct']}%")
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

    print(f"✅ Scan done. New: {new_signals} | Active: {state.active_signals}")


async def background_scanner():
    while state.is_running:
        if not state.is_paused:
            try:
                await scan_market()
            except Exception as e:
                print(f"Scanner error: {e}")
        else:
            print("⏸ Scan skipped (paused)")
        await asyncio.sleep(Config.SCAN_INTERVAL)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
