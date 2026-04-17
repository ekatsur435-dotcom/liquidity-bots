"""
🔴 SHORT BOT v2.3 — FastAPI Application

ИСПРАВЛЕНИЯ v2.3:
  ✅ MAX_WATCHLIST default = 300 (было 200)
  ✅ MIN_SHORT_SCORE default = 60 (было 65)
  ✅ SCAN_INTERVAL default = 200 сек
  ✅ HEAD /health → 200 OK (UptimeRobot fix)
  ✅ Watchlist: объединяет Bybit + Binance (нет дублей, до 300 монет)
  ✅ OI Proxy метрики в scan_symbol
  ✅ volume_spike_ratio + atr_14_pct → scorer
  ✅ pattern_detector (один файл, не v2)
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

from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client
from core.scorer import get_short_scorer
from core.pattern_detector import ShortPatternDetector   # ← единый файл
from core.position_tracker import PositionTracker
from core.short_filter import get_short_filter, get_short_tp_config
from core.realtime_scorer import get_realtime_scorer
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "short"
    # ✅ FIX: MIN_SHORT_SCORE default = 60 (не 65!)
    MIN_SCORE     = int(os.getenv("MIN_SHORT_SCORE", "60"))
    # ✅ FIX: SCAN_INTERVAL default = 200
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "200"))
    # ✅ FIX: MAX_WATCHLIST default = 300
    MAX_POSITIONS = int(os.getenv("MAX_SHORT_POSITIONS", "20"))
    LEVERAGE      = os.getenv("SHORT_LEVERAGE", "5-50")

    # SHORT: SL ВЫШЕ входа, TP НИЖЕ входа
    SL_BUFFER     = float(os.getenv("SHORT_SL_BUFFER", "2.5"))

    # TP динамические — short_filter.get_short_tp_config выбирает профиль
    TP_LEVELS  = [1.5, 3.0, 5.0, 6.3, 8.5, 12.2]
    TP_WEIGHTS = [25,  25,  20,  15,  10,   5]   # SHORT: больше на TP1-2

    # Trailing — SHORT активирует при +1% (лонг: +1.5%)
    TRAIL_ACTIVATION = float(os.getenv("SHORT_TRAIL_ACTIVATION", "0.010"))

    SIGNAL_TTL_HOURS = 24

    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.0005"))

    USE_SMC        = os.getenv("USE_SMC", "true").lower() == "true"
    USE_COINGLASS  = bool(os.getenv("COINGLASS_API_KEY", ""))

    # ✅ FIX: default MAX_WATCHLIST = 300
    # ✅ ADJUSTED: 300K → 150K для SHORT (мемы имеют меньший объём, но дают большие движения)
    MIN_VOLUME_USDT = int(os.getenv("MIN_VOLUME_USDT", "150000"))  # Было: 300000
    MAX_WATCHLIST   = int(os.getenv("MAX_WATCHLIST", "300"))


# ============================================================================
# GLOBAL STATE
# ============================================================================

class BotState:
    def __init__(self):
        self.is_running       = False
        self.is_paused        = False
        self.last_scan        = None
        self.active_signals   = 0
        self.daily_signals    = 0
        self.watchlist: List[str] = []
        self.redis            = None
        self.binance          = None
        self.scorer           = None
        self.pattern_detector = None
        self.telegram         = None
        self.cmd_handler      = None
        self.auto_trader      = None
        self.tracker: Optional[PositionTracker] = None
        self.coinglass        = None
        self._min_score       = Config.MIN_SCORE
        self.start_time       = None

state = BotState()


# ============================================================================
# COMBINED WATCHLIST (Bybit + Binance)
# ============================================================================

async def _build_combined_watchlist(binance_client, min_vol: float, max_count: int) -> List[str]:
    """
    Объединяет тикеры с Bybit и Binance.
    ✅ FIX: Добавлен fallback на FALLBACK_WATCHLIST если оба источника пустые.
    """
    from utils.binance_client import FALLBACK_WATCHLIST

    bybit_syms  = set()
    binance_syms = set()

    # ✅ FIX: Убедимся что источник инициализирован
    try:
        await binance_client._init_source()
    except Exception as e:
        print(f"⚠️ _init_source error: {e}")

    # Bybit (основной источник)
    total_bybit_checked = 0
    total_bybit_usdt = 0
    try:
        result = await binance_client._bybit("/v5/market/tickers", {"category": "linear"})
        if result and result.get("list"):
            EXCLUDE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
            all_tickers = result.get("list", [])
            print(f"📊 Bybit API returned: {len(all_tickers)} total tickers")
            
            for t in all_tickers:
                total_bybit_checked += 1
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                total_bybit_usdt += 1
                if any(sym.endswith(s) for s in EXCLUDE_SUFFIXES):
                    continue
                vol = float(t.get("turnover24h", 0))
                # ✅ DEBUG: Показываем топ волюмов
                if vol >= min_vol:
                    bybit_syms.add(sym)
                    
        print(f"✅ Bybit symbols: {len(bybit_syms)} (checked: {total_bybit_checked}, USDT: {total_bybit_usdt})")
        print(f"   Min volume threshold: ${min_vol:,.0f}")
    except Exception as e:
        print(f"⚠️ Bybit watchlist error: {e}")

    # Binance (если доступен через прокси)
    try:
        if binance_client._use_binance:
            tickers = await binance_client._binance("/fapi/v1/ticker/24hr")
            if tickers:
                EXCLUDE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
                for t in tickers:
                    sym = t.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    if any(sym.endswith(s) for s in EXCLUDE_SUFFIXES):
                        continue
                    vol = float(t.get("quoteVolume", 0))
                    if vol >= min_vol:
                        binance_syms.add(sym)
                print(f"✅ Binance symbols: {len(binance_syms)}")
    except Exception as e:
        print(f"⚠️ Binance watchlist error: {e}")

    # ✅ DEBUG: Статистика до объединения
    print(f"📈 Pre-merge: Bybit={len(bybit_syms)}, Binance={len(binance_syms)}, threshold=${min_vol:,.0f}")
    
    # ✅ FIX: Fallback если оба источника пустые
    total_found = len(bybit_syms) + len(binance_syms)
    if total_found == 0:
        print(f"⚠️ No symbols from APIs! Using FALLBACK_WATCHLIST ({len(FALLBACK_WATCHLIST)} coins)")
        return FALLBACK_WATCHLIST[:max_count]

    # Объединяем
    combined = list(bybit_syms | binance_syms)
    combined.sort()

    # Предпочитаем символы присутствующие на ОБОИХ биржах
    both = list(bybit_syms & binance_syms)
    only_one = [s for s in combined if s not in both]

    result_list = (both + only_one)[:max_count]
    print(f"📊 Combined watchlist: {len(result_list)} symbols "
          f"(both={len(both)}, bybit_only={len(bybit_syms-binance_syms)}, "
          f"binance_only={len(binance_syms-bybit_syms)})")

    # ✅ FIX: Дополнительная проверка
    if len(result_list) == 0:
        print(f"⚠️ Empty result! Using FALLBACK_WATCHLIST")
        return FALLBACK_WATCHLIST[:max_count]

    return result_list


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting SHORT Bot v2.3...")
    state.start_time = datetime.utcnow()

    state.redis            = get_redis_client()
    state.binance          = get_binance_client()
    state.scorer           = get_short_scorer(Config.MIN_SCORE)
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
        bot=state.telegram, redis_client=state.redis,
        bot_state=state, bot_type=Config.BOT_TYPE,
        scan_callback=scan_market, config=Config,
    )

    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        await state.telegram.setup_webhook(f"{render_url}/webhook")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set")

    # ── BingX AutoTrader ───────────────────────────────────────────────────────
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
            ok = await bingx.test_connection()
            if ok:
                trade_cfg = TradeConfig(
                    enabled=True,
                    demo_mode=Config.BINGX_DEMO,
                    max_positions=Config.MAX_POSITIONS,
                    risk_per_trade=Config.RISK_PER_TRADE,
                    min_score_for_trade=Config.MIN_SCORE,
                )
                state.auto_trader = AutoTrader(
                    bingx_client=bingx, config=trade_cfg, telegram=state.telegram
                )
                mode = "DEMO" if Config.BINGX_DEMO else "REAL"
                print(f"✅ BingX AutoTrader ready ({mode})")
            else:
                print("❌ BingX connection failed — AutoTrader disabled")
        except Exception as e:
            print(f"❌ AutoTrader init: {e}")
            import traceback; traceback.print_exc()

    # CoinGlass
    if Config.USE_COINGLASS:
        try:
            from utils.coinglass_client import CoinglassClient
            state.coinglass = CoinglassClient(api_key=os.getenv("COINGLASS_API_KEY"))
            print("✅ CoinGlass connected")
        except Exception as e:
            print(f"⚠️ CoinGlass: {e}")
            Config.USE_COINGLASS = False

    # ── Watchlist: Bybit + Binance ─────────────────────────────────────────────
    # Инициализируем источник данных
    await state.binance._init_source()

    try:
        state.watchlist = await _build_combined_watchlist(
            state.binance, Config.MIN_VOLUME_USDT, Config.MAX_WATCHLIST
        )
    except Exception as e:
        print(f"⚠️ Combined watchlist failed: {e} — using binance only")
        try:
            state.watchlist = await state.binance.get_all_symbols(
                min_volume_usdt=Config.MIN_VOLUME_USDT
            )
            state.watchlist = state.watchlist[:Config.MAX_WATCHLIST]
        except Exception as e2:
            print(f"⚠️ Binance watchlist failed too: {e2}")
            state.watchlist = []

    print(f"📊 Watchlist: {len(state.watchlist)} symbols")

    state.is_running = True
    state.last_scan  = datetime.utcnow()

    # Стартовое сообщение
    mode_str = "DEMO" if Config.BINGX_DEMO else "REAL"
    at_str   = f"✅ {mode_str}" if state.auto_trader else "❌ disabled"
    await state.telegram.send_message(
        f"🔴 <b>SHORT Bot v2.3 запущен</b>\n\n"
        f"📊 Watchlist: {len(state.watchlist)} монет\n"
        f"🛑 SL: {Config.SL_BUFFER}%  |  Score≥{Config.MIN_SCORE}%\n"
        f"🤖 AutoTrader: {at_str}\n"
        f"⚙️ Risk: {Config.RISK_PER_TRADE*100:.3f}% | Scan: {Config.SCAN_INTERVAL}s\n"
        f"🔍 ShortFilter: ✅ | RealtimeScorer: ✅"
    )
    print(f"✅ SHORT Bot started! AutoTrader: {at_str}")

    state.tracker = PositionTracker(
        bot_type=Config.BOT_TYPE, telegram=state.telegram,
        redis_client=state.redis, binance_client=state.binance,
        config=Config, auto_trader=state.auto_trader,
    )

    asyncio.create_task(background_scanner())
    asyncio.create_task(state.tracker.run())

    yield

    state.is_running = False
    print("🛑 Shutting down SHORT Bot...")
    if state.binance:
        await state.binance.close()
    if state.auto_trader:
        await state.auto_trader.bingx.close()
    print("👋 SHORT Bot stopped")


app = FastAPI(lifespan=lifespan, title="SHORT Bot v2.3")


# ============================================================================
# ROUTES
# ============================================================================

# ✅ HEAD + GET для UptimeRobot (405 → 200)
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"status": "ok", "bot": "short", "version": "2.3",
                         "watchlist": len(state.watchlist),
                         "active": state.active_signals})

@app.get("/")
async def root():
    return {"bot": "SHORT Bot v2.3", "status": "running" if state.is_running else "stopped"}

@app.get("/status")
async def status():
    return {
        "bot_type": Config.BOT_TYPE, "version": "2.3",
        "is_running": state.is_running, "is_paused": state.is_paused,
        "watchlist_count": len(state.watchlist), "active_signals": state.active_signals,
        "last_scan": state.last_scan.isoformat() if state.last_scan else None,
        "config": {
            "min_score": Config.MIN_SCORE, "sl_buffer": Config.SL_BUFFER,
            "scan_interval": Config.SCAN_INTERVAL,
            "auto_trading": Config.AUTO_TRADING,
            "risk_per_trade": Config.RISK_PER_TRADE,
            "max_watchlist": Config.MAX_WATCHLIST,
        },
        "auto_trader_ready": state.auto_trader is not None,
    }

@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if not state.is_running:
        raise HTTPException(status_code=503, detail="Bot not running")
    # ✅ FIX: Проверяем is_paused
    if state.is_paused:
        raise HTTPException(status_code=503, detail="Bot is paused — use /resume first")
    background_tasks.add_task(scan_market)
    return {"message": "Scan triggered", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/signals")
async def get_active_signals():
    signals = state.redis.get_active_signals(Config.BOT_TYPE)
    return {"bot_type": Config.BOT_TYPE, "count": len(signals), "signals": signals}

@app.get("/api/positions")
async def get_positions():
    if state.auto_trader:
        pos = await state.auto_trader.bingx.get_positions()
        return {"count": len(pos), "positions": [
            {"symbol": p.symbol, "side": p.side, "size": p.size,
             "entry": p.entry_price, "upnl": p.unrealized_pnl}
            for p in pos
        ]}
    return {"count": 0, "positions": []}

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

@app.get("/webhook/setup")
async def setup_webhook():
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url and state.telegram:
        ok = await state.telegram.setup_webhook(f"{render_url}/webhook")
        return {"ok": ok, "url": f"{render_url}/webhook"}
    return {"error": "No RENDER_EXTERNAL_URL"}


# ============================================================================
# CORE LOGIC
# ============================================================================

async def _get_price_change_4d(symbol: str, fallback: float) -> float:
    try:
        klines = await state.binance.get_klines(symbol, "1d", 6)
        if klines and len(klines) >= 5:
            now = klines[-1].close
            old = klines[-5].close
            if old > 0:
                return round((now - old) / old * 100, 2)
    except Exception:
        pass
    return fallback

def _is_fresh(existing: List[Dict]) -> bool:
    if not existing or existing[0].get("status") != "active":
        return False
    try:
        age_h = (datetime.utcnow() -
                 datetime.fromisoformat(existing[0].get("timestamp", ""))
                 ).total_seconds() / 3600
        return age_h < Config.SIGNAL_TTL_HOURS
    except Exception:
        return True

def _ohlcv(candles) -> List[List[float]]:
    return [[c.open, c.high, c.low, c.close, c.volume] for c in candles]

async def _count_real_positions() -> int:
    if state.auto_trader:
        try:
            pos = await state.auto_trader.bingx.get_positions()
            return len(pos)
        except Exception:
            pass
    cutoff = datetime.utcnow() - timedelta(hours=Config.SIGNAL_TTL_HOURS)
    try:
        all_active = state.redis.get_active_signals(Config.BOT_TYPE)
        return sum(1 for s in all_active
                   if datetime.fromisoformat(s.get("timestamp","2000-01-01")) > cutoff)
    except Exception:
        return 0


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """
    SHORT scan_symbol v2.3:
      - SL ВЫШЕ входа (short: stop loss = цена * (1 + SL_BUFFER%))
      - TP НИЖЕ входа (short: фиксируем прибыль при падении)
      - ShortFilter: фильтрация по BTC trend, RSI, фандинг, свеча
      - OI Proxy: bear_confirm / accumulation / weakness
      - volume_spike_ratio + atr_14_pct → scorer
    """
    try:
        md = await state.binance.get_complete_market_data(symbol)
        if not md:
            return None

        ohlcv_15m = await state.binance.get_klines(symbol, "15m", 100)
        if not ohlcv_15m or len(ohlcv_15m) < 20:
            return None

        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend   = state.pattern_detector._get_price_trend(ohlcv_15m)
        patterns      = state.pattern_detector.detect_all(ohlcv_15m, hourly_deltas, md)
        p4d           = await _get_price_change_4d(symbol, md.price_change_24h * 4)

        # ── OI Proxy (SHORT специфика) ────────────────────────────────────────
        oi_bear_confirm = False
        oi_accumulation = False
        oi_weakness_short = False
        oi_score_adj    = 0.0

        try:
            oi_history = await state.binance.get_open_interest_history(symbol, "15m", 5)
            if oi_history and len(oi_history) >= 3:
                ois  = [float(h.get("sumOpenInterest", 0)) for h in oi_history]
                vols = [c.quote_volume for c in ohlcv_15m[-5:]]

                # OI и объём падают вместе с ценой → медвежье подтверждение
                oi_growing   = ois[-1] > ois[0] if ois[0] else False
                vol_growing  = len(vols) >= 3 and vols[-1] > vols[-3]
                price_down   = getattr(md, "price_change_1h", 0) < -0.5

                # Bear confirm: цена падает + OI растёт (шорты открываются)
                oi_bear_confirm = price_down and oi_growing
                if oi_bear_confirm:
                    oi_score_adj += 1.5

                # OI стабильно растёт = реальные деньги идут в шорт
                oi_accumulation = (all(ois[i] <= ois[i+1] for i in range(len(ois)-1))
                                   if len(ois) >= 3 else False)
                if oi_accumulation:
                    oi_score_adj += 2.5

                # Слабость: цена падает но OI тоже падает = шорты закрываются
                oi_falling = ois[-1] < ois[0] if ois[0] else False
                oi_weakness_short = price_down and oi_falling
                if oi_weakness_short:
                    oi_score_adj -= 2.0
        except Exception as e:
            print(f"OI Proxy error {symbol}: {e}")

        # ── Base score ────────────────────────────────────────────────────────
        score_result = state.scorer.calculate_score(
            rsi_1h=md.rsi_1h or 50,
            funding_current=md.funding_rate / 100,
            funding_accumulated=md.funding_accumulated / 100,
            long_ratio=md.long_short_ratio,
            oi_change_4d=md.oi_change_4d,
            price_change_4d=p4d,
            hourly_deltas=hourly_deltas,
            price_trend=price_trend,
            patterns=patterns,
            volume_spike_ratio=getattr(md, "volume_spike_ratio", 1.0),
            atr_14_pct=getattr(md, "atr_14_pct", 0.5),
        )
        if not score_result.is_valid:
            return None

        price       = md.price
        final_score = score_result.total_score + oi_score_adj
        reasons     = list(score_result.reasons)

        # ── SHORT-специфичные фильтры ─────────────────────────────────────────
        btc_change_1h: Optional[float] = None
        try:
            btc_md = await state.binance.get_complete_market_data("BTCUSDT")
            if btc_md:
                btc_change_1h = btc_md.price_change_24h / 24
        except Exception:
            pass

        sf   = get_short_filter()
        filt = sf.check(
            market_data=md, ohlcv_15m=ohlcv_15m,
            hourly_deltas=hourly_deltas,
            btc_price_1h_change=btc_change_1h,
        )
        if filt.blocked:
            print(f"[ShortFilter] {symbol}: {filt.block_reason}")
            return None

        final_score += filt.score_delta
        reasons.extend(filt.reasons)

        # ── Realtime scorer ───────────────────────────────────────────────────
        rt = get_realtime_scorer()
        rt_result = rt.score(
            direction="short", market_data=md,
            base_score=final_score, hourly_deltas=hourly_deltas,
        )
        if rt_result.early_only:
            await state.telegram.send_message(
                f"🛰️ <b>РАННИЙ SHORT WATCH</b>  Score: {rt_result.final_score:.0f}%\n\n"
                f"🔴 <code>#{symbol}</code>  ${price:,.6f}\n"
                + "\n".join(f"  • {r}" for r in rt_result.factors[:4])
                + "\n\n⏳ <i>Ждём подтверждения.</i>"
            )
            return None

        final_score = rt_result.final_score
        reasons.extend(rt_result.factors)

        if final_score < Config.MIN_SCORE:
            return None

        # OI proxy лог
        if oi_bear_confirm: print(f"[OI] {symbol}: bear confirm +1.5")
        if oi_accumulation: print(f"[OI] {symbol}: accumulation +2.5")
        if oi_weakness_short: print(f"[OI] {symbol}: weakness -2.0")

        # ── Динамические TP для SHORT ─────────────────────────────────────────
        best_pattern = patterns[0].name if patterns else None
        btc_trend    = ("down" if (btc_change_1h or 0) < -0.5 else
                        "up"   if (btc_change_1h or 0) > 0.5 else "sideways")
        tp_levels, tp_weights = get_short_tp_config(
            funding_rate=md.funding_rate,
            pattern_name=best_pattern,
            btc_trend=btc_trend,
        )

        # ── SL ВЫШЕ входа, TP НИЖЕ входа (SHORT) ─────────────────────────────
        stop_loss   = price * (1 + Config.SL_BUFFER / 100)
        entry_price = price
        smc_data    = {}

        if Config.USE_SMC:
            try:
                from utils.smc_ict_detector import get_smc_result
                smc = get_smc_result(_ohlcv(ohlcv_15m), "short",
                                     base_sl_pct=Config.SL_BUFFER, base_entry=price)
                if smc.score_bonus > 0:
                    final_score += smc.score_bonus
                    reasons.extend(smc.reasons)
                if smc.refined_sl and smc.refined_sl > price:
                    stop_loss = smc.refined_sl
                if smc.ob_entry:
                    entry_price = smc.ob_entry
                smc_data = {"has_ob": smc.has_ob, "has_fvg": smc.has_fvg,
                            "score_bonus": smc.score_bonus}
            except Exception as e:
                print(f"SMC error {symbol}: {e}")

        if final_score < Config.MIN_SCORE:
            return None

        # Проверка SL корректности для SHORT
        if (stop_loss - price) / price < 0.01:
            stop_loss = price * 1.01

        # TP НИЖЕ входа для SHORT
        take_profits = [
            (round(price * (1 - tp / 100), 8), tp_weights[i] if i < len(tp_weights) else 15)
            for i, tp in enumerate(tp_levels)
        ]

        sl_pct = round((stop_loss - price) / price * 100, 2)
        return {
            "symbol": symbol, "direction": "short",
            "score": final_score, "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            "price": price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 8), "sl_pct": sl_pct,
            "take_profits": take_profits,
            "patterns": [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "indicators": {
                "RSI": f"{md.rsi_1h:.1f}" if md.rsi_1h else "N/A",
                "Funding": f"{md.funding_rate:+.3f}%",
                "L/S Ratio": f"{md.long_short_ratio:.0f}% longs",
                "OI Change": f"{md.oi_change_4d:+.1f}% (4d)",
                "Price 4d": f"{p4d:+.1f}%",
            },
            "oi_proxy": {
                "bear_confirm": oi_bear_confirm,
                "accumulation": oi_accumulation,
                "weakness":     oi_weakness_short,
                "score_adj":    round(oi_score_adj, 2),
            },
            "volume_spike": round(getattr(md, "volume_spike_ratio", 1.0), 2),
            "atr_pct":      round(getattr(md, "atr_14_pct", 0.5), 3),
            "reasons": reasons, "smc": smc_data,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "active", "taken_tps": [],
        }
    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def scan_market():
    if state.is_paused:
        return
    print(f"\n🔍 SHORT scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    active_count = await _count_real_positions()
    if active_count >= Config.MAX_POSITIONS:
        print(f"⏸ Max positions ({active_count}/{Config.MAX_POSITIONS})")
        state.last_scan = datetime.utcnow()
        return

    new_signals = 0
    for symbol in state.watchlist:
        if new_signals + active_count >= Config.MAX_POSITIONS:
            break
        try:
            if _is_fresh(state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)):
                continue
            signal = await scan_symbol(symbol)
            if signal:
                tg_msg_id = await state.telegram.send_signal(
                    direction="short", symbol=signal["symbol"],
                    score=signal["score"], price=signal["price"],
                    pattern=signal["best_pattern"] or "N/A",
                    indicators=signal["indicators"],
                    entry=signal["entry_price"],
                    stop_loss=signal["stop_loss"],
                    take_profits=signal["take_profits"],
                    leverage=Config.LEVERAGE, risk="≤1% deposit",
                )
                signal["tg_msg_id"] = tg_msg_id
                state.redis.save_signal(Config.BOT_TYPE, symbol, signal)

                # ✅ FIX: Двойная проверка is_paused перед открытием позиции
                if state.auto_trader and Config.AUTO_TRADING and not state.is_paused:
                    try:
                        await state.auto_trader.execute_signal(signal)
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")
                elif state.is_paused:
                    print(f"⏸ Skipping trade {symbol} — bot is paused")

                print(f"✅ SHORT: {symbol} Score={signal['score']:.0f}% SL={signal['sl_pct']}%")
                new_signals += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            print(f"Error {symbol}: {e}")

    state.daily_signals += new_signals
    state.last_scan      = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))
    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status": "paused" if state.is_paused else "running",
        "last_scan": state.last_scan.isoformat(),
        "daily_signals": state.daily_signals,
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
        await asyncio.sleep(Config.SCAN_INTERVAL)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 8000)), reload=False)
