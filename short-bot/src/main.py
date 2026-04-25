"""
🔴 SHORT BOT v2.9 — FastAPI Application

ИСПРАВЛЕНИЯ v2.9:
  🎢 Micro-Step Trailing Stop — плавное движение SL микро-шагами
     TP1: +0.3%, TP2: +0.8%, TP3: +1.5% — не выбивает сделки!
  
ИСПРАВЛЕНИЯ v2.8:
  ✅ Symbol Profiler — индивидуальный анализ каждой монеты
  ✅ Order Block Detector — институциональные зоны входа
  ✅ Adaptive Timeframes — авто-выбор ТФ под волатильность
  ✅ Limit Entry System — лимитные ордера с TTL + fallback
  
ИСПРАВЛЕНИЯ v2.7:
  ✅ Liquidity Sweep Detection (ловля сборов стопов)
  ✅ TBS — Test Before Strike (ретест Order Block)
  ✅ Entry Confirmation System (мульти-ТФ + объём + ATR + уровни)
  ✅ Увеличены TP: 4%, 8%, 12%, 20%+ (R:R 2.7:1)
  ✅ Уменьшен SL: 1.5% (было 2.0%)
  ✅ MIN_VOLUME_USDT = 500000
  
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
from core.liquidity_detector import detect_smart_money_entry  # ✅ v2.7
from core.entry_confirmation import EntryConfirmation  # ✅ v2.7
from core.tbs_detector import detect_tbs_entry  # ✅ v2.7 TBS
from core.symbol_profiler import SymbolProfile, get_symbol_profiler, get_profile  # ✅ v2.8
from core.order_block_detector import detect_order_blocks, format_ob_for_signal  # ✅ v2.8
from core.liquidity_pool_scanner import scan_liquidity_pools, LiquidityPoolScanner  # ✅ Phase 3
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "short"
    # ✅ FIX: MIN_SHORT_SCORE default = 65
    # ✅ v2.5 BACKTEST: Score 67+ → WR 55.4%, PF 2.07x
    MIN_SCORE     = int(os.getenv("MIN_SHORT_SCORE", "65"))
    # ✅ FIX: SCAN_INTERVAL default = 200
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))  # BACKTEST: 120с
    # ✅ FIX: MAX_WATCHLIST default = 300
    MAX_POSITIONS = int(os.getenv("MAX_SHORT_POSITIONS", "20"))
    LEVERAGE      = os.getenv("SHORT_LEVERAGE", "5-50")

    # SHORT: SL ВЫШЕ входа, TP НИЖЕ входа
    # ✅ v2.5: Уменьшен SL с 2.0% до 1.5% для лучшего R:R
    SL_BUFFER     = float(os.getenv("SHORT_SL_BUFFER", "1.5"))  # was 2.0

    # TP динамические — short_filter.get_short_tp_config выбирает профиль
    # ✅ v2.5: Увеличены TP для лучшего R:R ≥ 2:1
    TP_LEVELS  = [4.0, 8.0, 12.0, 20.0, 30.0, 40.0]  # SHORT: SL=1.5% TP1=4% → R:R=2.7:1
    # ✅ BACKTEST: TP1 достигается 65% сделок → акцент на TP1-2
    TP_WEIGHTS = [25,  20,  20,  20,  10,   5]   # TP1=25%, TP2-4=20%, TP5=10%, TP6=5%

    # Trailing — SHORT активирует при +3% (после TP1)
    TRAIL_ACTIVATION = float(os.getenv("SHORT_TRAIL_ACTIVATION", "0.030"))
    SHORT_TRAIL_ACTIVATION = TRAIL_ACTIVATION  # Alias для position_tracker.py

    SIGNAL_TTL_HOURS = 24

    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "true").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.0005"))

    USE_SMC        = os.getenv("USE_SMC", "true").lower() == "true"
    USE_COINGLASS  = bool(os.getenv("COINGLASS_API_KEY", ""))

    # ✅ FIX: default MAX_WATCHLIST = 300
    # ✅ ADJUSTED: 300K → 150K для SHORT (мемы имеют меньший объём, но дают большие движения)
    MIN_VOLUME_USDT = int(os.getenv("MIN_VOLUME_USDT", "300000"))  # Было: 300000
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


# ============================================================================
# 🆕 RSI WATCHLIST TRACKER — мониторинг монет с растущим RSI
# ============================================================================

class RSIWatchlistTracker:
    """
    Отслеживает монеты где RSI начал расти (пересёк 35 снизу вверх).
    Эти монеты — кандидаты на LONG разворот или SHORT после отката.
    Хранит в памяти (сбрасывается при рестарте) + Redis кеш.
    """
    def __init__(self):
        self._rising: Dict[str, Dict] = {}   # symbol -> {rsi, since, prev_rsi}
        self._fallen: Dict[str, float] = {}  # symbol -> timestamp когда упал обратно
        
    def update(self, symbol: str, rsi: float, prev_rsi: float = 0):
        """Обновить RSI для символа"""
        now = datetime.utcnow().timestamp()
        
        # RSI пересёк 35 снизу — начал расти
        if rsi >= 35 and (prev_rsi < 35 or symbol not in self._rising):
            if symbol not in self._rising:
                self._rising[symbol] = {
                    "rsi": rsi, "since": now,
                    "prev_rsi": prev_rsi, "peak_rsi": rsi
                }
            else:
                self._rising[symbol]["rsi"] = rsi
                self._rising[symbol]["peak_rsi"] = max(
                    self._rising[symbol]["peak_rsi"], rsi
                )
        # RSI упал ниже 30 — сброс
        elif rsi < 30 and symbol in self._rising:
            del self._rising[symbol]
        
    def is_rsi_rising(self, symbol: str) -> bool:
        return symbol in self._rising
    
    def get_rising_symbols(self) -> List[str]:
        return list(self._rising.keys())
    
    def get_info(self, symbol: str) -> Dict:
        return self._rising.get(symbol, {})
    
    def cleanup_old(self, max_age_hours: int = 48):
        """Удаляет монеты которые давно в списке"""
        now = datetime.utcnow().timestamp()
        to_del = [s for s, d in self._rising.items()
                  if now - d["since"] > max_age_hours * 3600]
        for s in to_del:
            del self._rising[s]

_rsi_tracker = RSIWatchlistTracker()

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

    # ── Bybit (основной источник) ─────────────────────────────────────────
    # ✅ v2.4: При 403 (Render IP заблокирован Bybit) — автоматически
    #          переключаемся на Binance фьючерсы как источник watchlist.
    total_bybit_checked = 0
    total_bybit_usdt    = 0
    bybit_ok = False
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
                if vol >= min_vol:
                    bybit_syms.add(sym)
            bybit_ok = len(bybit_syms) > 0
        print(f"✅ Bybit symbols: {len(bybit_syms)} (checked: {total_bybit_checked}, USDT: {total_bybit_usdt})")
        print(f"   Min volume threshold: ${min_vol:,.0f}")
    except Exception as e:
        print(f"⚠️ Bybit watchlist error: {e}")

    # ── Binance (если есть прокси OR Bybit вернул 403/пусто) ─────────────
    # ✅ v2.4: force_binance_fallback если Bybit заблокирован
    force_binance = not bybit_ok
    try:
        if binance_client._use_binance or force_binance:
            if force_binance:
                print("⚡ Bybit 403/empty → AUTO-FALLBACK to Binance futures watchlist")
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
    print("🚀 Starting SHORT Bot v2.7...")
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

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if render_url:
        wh_url = f"{render_url}/webhook"
        ok = await state.telegram.setup_webhook(wh_url)
        print(f"{'✅' if ok else '⚠️'} Webhook: {wh_url}")
        if not ok:
            await asyncio.sleep(3)
            ok2 = await state.telegram.setup_webhook(wh_url)
            print(f"{'✅' if ok2 else '❌'} Webhook retry: {ok2}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set — Telegram commands won't work!")
        print("   Set RENDER_EXTERNAL_URL=https://YOUR-SERVICE.onrender.com in Render env vars")

    # ── BingX AutoTrader ───────────────────────────────────────────────────────
    print(f"🔧 AUTO_TRADING={Config.AUTO_TRADING} | DEMO={Config.BINGX_DEMO}")
    if Config.AUTO_TRADING:
        try:
            from api.bingx_client import BingXClient
            from execution.auto_trader import AutoTrader, TradeConfig

            # 🆕 DEBUG: Check API keys
            api_key = os.getenv("BINGX_API_KEY")
            api_secret = os.getenv("BINGX_API_SECRET")
            print(f"🔑 API Key present: {'✅' if api_key else '❌'} (len={len(api_key) if api_key else 0})")
            print(f"🔑 API Secret present: {'✅' if api_secret else '❌'} (len={len(api_secret) if api_secret else 0})")

            if not api_key or not api_secret:
                print("❌ BINGX_API_KEY or BINGX_API_SECRET not set!")
            else:
                bingx = BingXClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    demo=Config.BINGX_DEMO,
                )
                print("🔄 Testing BingX connection...")
                ok = await bingx.test_connection()
                print(f"🔄 BingX test_connection result: {ok}")
                if ok:
                    trade_cfg = TradeConfig(
                        enabled=True,
                        demo_mode=Config.BINGX_DEMO,
                        max_positions=Config.MAX_POSITIONS,
                        risk_per_trade=Config.RISK_PER_TRADE,
                        min_score_for_trade=Config.MIN_SCORE,
                        bot_type=Config.BOT_TYPE,
                    )
                    state.auto_trader = AutoTrader(
                        bingx_client=bingx, config=trade_cfg, telegram=state.telegram,
                        bot_type=Config.BOT_TYPE
                    )
                    mode = "DEMO" if Config.BINGX_DEMO else "REAL"
                    print(f"✅ BingX AutoTrader ready ({mode})")
                else:
                    print(f"❌ BingX connection failed — AutoTrader disabled (last_error: {bingx.last_error})")
        except Exception as e:
            print(f"❌ AutoTrader init exception: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("⚠️ AUTO_TRADING is disabled — AutoTrader not initialized")

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
        f"🤖 <b>SHORT Bot v2.9 запущен</b>\n\n"
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


app = FastAPI(lifespan=lifespan, title="SHORT Bot v2.9")


# ============================================================================
# ROUTES
# ============================================================================

# ✅ HEAD + GET для UptimeRobot (405 → 200)
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"status": "ok", "bot": "short", "version": "2.9",
                         "watchlist": len(state.watchlist),
                         "active": state.active_signals})

# ✅ HEAD + GET для Render health checks (405 → 200)
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({"bot": "SHORT Bot v2.9", "status": "running" if state.is_running else "stopped"})

@app.get("/status")
async def status():
    return {
        "bot_type": Config.BOT_TYPE, "version": "2.9",
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
@app.get("/webhook/reset")
async def setup_webhook():
    """GET /webhook/setup OR /webhook/reset → принудительно регистрирует вебхук."""
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        return {"error": "RENDER_EXTERNAL_URL not set in env vars"}
    if not state.telegram:
        return {"error": "Telegram not initialized"}
    wh_url = f"{render_url}/webhook"
    await state.telegram.delete_webhook()
    await asyncio.sleep(1)
    ok = await state.telegram.setup_webhook(wh_url)
    info = await state.telegram.get_webhook_info()
    return {"ok": ok, "url": wh_url, "webhook_info": info}


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
    """
    ✅ v2.4 FIX: Считаем ТОЛЬКО SHORT позиции этого бота.
    БЫЛО: len(get_positions()) — считало ВСЕ позиции BingX включая
          Результат: SHORT бот всегда видел 19-20 и был заблокирован навсегда!
    СТАЛО: фильтр side == "SHORT" → считаем только наши шорты.
    """
    if state.auto_trader:
        try:
            pos = await state.auto_trader.bingx.get_positions()
            # ✅ КРИТИЧЕСКИЙ ФИК: только SHORT позиции!
            short_pos = [p for p in pos if getattr(p, "side", "").upper() == "SELL"]
            if short_pos:
                msg = f"""📉 <b>SHORT Позиции {'[DEMO] ' if Config.DEMO_MODE else ''}({len(short_pos)}):</b>\n\n"""
                msg += "\n".join(f"  • {getattr(p,'symbol','?')} {getattr(p,'size',0):.2f} @ {getattr(p,'entry_price',0):.4f} (UPNL: {getattr(p,'unrealized_pnl',0):.2f})" for p in short_pos)
                print(msg)
            return len(short_pos)
        except Exception as e:
            print(f"[SHORT] _count_real_positions BingX error: {e}")
    # Fallback: Redis active signals
    cutoff = datetime.utcnow() - timedelta(hours=Config.SIGNAL_TTL_HOURS)
    try:
        all_active = state.redis.get_active_signals(Config.BOT_TYPE)
        return sum(1 for s in all_active
                   if datetime.fromisoformat(s.get("timestamp","2000-01-01")) > cutoff)
    except Exception:
        return 0


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """
    SHORT scan_symbol v2.7 (NO BTC CORR):
      - SL ВЫШЕ входа (short: stop loss = цена * (1 + SL_BUFFER%))
      - TP НИЖЕ входа (short: take profit = цена * (1 - TP%))
      - OI Proxy: bear_confirm / accumulation / weakness
      - volume_spike_ratio + atr_14_pct → scorer
      - Multi-TF priority: 2h/4h для исполнения, 15m/30m/1h → watch only
    """
    try:
        md = await state.binance.get_complete_market_data(symbol)
        if not md:
            return None

        # ✅ FIX: Определяем price сразу, чтобы избежать UnboundLocalError
        price = md.price

        # 🆕 RSI Watchlist tracking — обновляем трекер
        rsi_current = md.rsi_1h or 0
        _rsi_tracker.update(symbol, rsi_current)

        # ✅ Multi-TF загрузка: 15m + 30m + 1h + 2h + 4h (фокус на 2h/4h по бэктесту)
        ohlcv_15m_task = state.binance.get_klines(symbol, "15m", 100)
        ohlcv_30m_task = state.binance.get_klines(symbol, "30m", 50)
        ohlcv_1h_task = state.binance.get_klines(symbol, "1h", 30)
        ohlcv_2h_task = state.binance.get_klines(symbol, "2h", 20)
        ohlcv_4h_task = state.binance.get_klines(symbol, "4h", 14)
        ohlcv_15m, ohlcv_30m, ohlcv_1h, ohlcv_2h, ohlcv_4h = await asyncio.gather(
            ohlcv_15m_task, ohlcv_30m_task, ohlcv_1h_task, ohlcv_2h_task, ohlcv_4h_task
        )

        if not ohlcv_15m or len(ohlcv_15m) < 20:
            return None

        # Определяем лучший ТФ для сигнала (приоритет: 4h > 2h > 1h > 30m > 15m)
        primary_tf = "15m"  # default
        best_score_tf = 0
        tf_priority = {"4h": 5, "2h": 4, "1h": 3, "30m": 2, "15m": 1}

        # Проверяем паттерны на каждом ТФ и выбираем лучший
        for tf_name, tf_ohlcv in [("4h", ohlcv_4h), ("2h", ohlcv_2h), ("1h", ohlcv_1h), ("30m", ohlcv_30m), ("15m", ohlcv_15m)]:
            if tf_ohlcv and len(tf_ohlcv) >= 20:
                tf_patterns = state.pattern_detector.detect_all(tf_ohlcv, [], md)
                if tf_patterns:
                    tf_score = tf_priority.get(tf_name, 0) * 10 + len(tf_patterns) * 2
                    if tf_score > best_score_tf:
                        best_score_tf = tf_score
                        primary_tf = tf_name

        # Используем свечи лучшего ТФ для основного анализа
        tf_map = {"15m": ohlcv_15m, "30m": ohlcv_30m, "1h": ohlcv_1h, "2h": ohlcv_2h, "4h": ohlcv_4h}
        ohlcv_primary = tf_map.get(primary_tf, ohlcv_15m)
        
        # =========================================================================
        # ✅ v2.8: SYMBOL PROFILER — индивидуальный профиль монеты
        # =========================================================================
        symbol_profile = None
        try:
            symbol_profile = await get_profile(symbol, state.binance)
            if symbol_profile:
                # Адаптируем ТФ под профиль (если монета волатильная)
                if symbol_profile.ideal_tf != primary_tf and symbol_profile.ideal_tf in ["5m", "15m", "1h"]:
                    # Перезагружаем данные на оптимальном ТФ
                    new_ohlcv = await state.binance.get_klines(symbol, symbol_profile.ideal_tf, 100)
                    if new_ohlcv and len(new_ohlcv) >= 20:
                        ohlcv_primary = new_ohlcv
                        primary_tf = symbol_profile.ideal_tf
                        print(f"📊 [v2.9] {symbol}: Switched to {primary_tf} (volatility: {symbol_profile.volatility_class})")
        except Exception as e:
            print(f"⚠️ [v2.9] {symbol}: Profile error: {e}")
        
        # =========================================================================
        # ✅ v2.9: ORDER BLOCK DETECTOR — институциональные зоны
        # =========================================================================
        ob_data = None
        ob_result = None
        try:
            current_price = md.price
            ob_result = detect_order_blocks(
                ohlcv_primary, 
                direction="short",  # Для SHORT бота ищем bearish OB
                current_price=current_price
            )
            
            if ob_result and ob_result.bearish_ob:
                ob = ob_result.bearish_ob
                if ob.quality >= 60 and ob.freshness.value in ["fresh", "medium"]:
                    ob_data = format_ob_for_signal(ob)
                    print(f"🎯 [v2.9] {symbol}: OB detected @ ${ob.price_optimal:.6f} (Q:{ob.quality}, {ob.freshness.value})")
        except Exception as e:
            print(f"⚠️ [v2.9] {symbol}: OB detection error: {e}")

        # =========================================================================
        # ✅ v2.9: ENTRY CONFIRMATION SYSTEM (мульти-ТФ + объём + ATR + уровни)
        # =========================================================================
        try:
            # 1. Проверяем Liquidity Sweep (сбор стопов = сильнейший сигнал!)
            sweep = detect_smart_money_entry(ohlcv_primary, direction="short")
            if sweep and sweep["found"]:
                # 2. Подтверждение фильтрами
                tf_data_v26 = {}
                if ohlcv_4h: tf_data_v26["4h"] = ohlcv_4h
                if ohlcv_2h: tf_data_v26["2h"] = ohlcv_2h
                if ohlcv_1h: tf_data_v26["1h"] = ohlcv_1h
                
                confirmation = EntryConfirmation.comprehensive_check(
                    ohlcv_primary,
                    tf_data=tf_data_v26 if len(tf_data_v26) >= 2 else None,
                    direction="short"
                )
                
                if confirmation["passed"] and confirmation["score"] >= 75:
                    # 🎯 ВСЁ ПОДТВЕРЖДЕНО — супер-сигнал!
                    base_score = 85 + (confirmation["score"] - 75) // 5  # 85-95
                    reasons = sweep["reasons"] + confirmation["reasons"]
                    
                    # Генерируем сигнал с оптимальными уровнями
                    entry = md.price
                    sl = entry * (1 + Config.SL_BUFFER / 100)
                    tp1 = entry * (1 - 0.04)  # 4%
                    tp2 = entry * (1 - 0.08)  # 8%
                    tp3 = entry * (1 - 0.12)  # 12%
                    
                    print(f"🎯 [v2.9] LIQUIDITY SWEEP {symbol}: score={base_score}, conf={confirmation['score']}")
                    
                    return {
                        "symbol": symbol,
                        "direction": "short",
                        "score": base_score,
                        "price": entry,  # Alias для совместимости с telegram
                        "entry_price": entry,
                        "stop_loss": sl,
                        "take_profits": [tp1, tp2, tp3],
                        "reasons": reasons[:5],  # Топ-5 причин
                        "timeframe": primary_tf,
                        "pattern": "LIQUIDITY_SWEEP",
                        "best_pattern": "LIQUIDITY_SWEEP",  # Для telegram
                        "indicators": {"SMC": "Sweep+TBS", "Confirmation": f"Score:{confirmation['score']}"},
                        "zones": sweep.get("zones", {}) if isinstance(sweep, dict) else {}
                    }
                else:
                    # Sweep есть но не подтверждён — логируем но пропускаем
                    print(f"⚠️ [v2.9] {symbol}: Sweep найден но не подтверждён (score={confirmation.get('score', 0)})")
            
            # 3. Нет sweep — проверяем обычные фильтры (v2.9: бонусы, не блок)
            tf_data_v26 = {}
            if ohlcv_4h: tf_data_v26["4h"] = ohlcv_4h
            if ohlcv_2h: tf_data_v26["2h"] = ohlcv_2h
            if ohlcv_1h: tf_data_v26["1h"] = ohlcv_1h
            
            confirmation = EntryConfirmation.comprehensive_check(
                ohlcv_primary,
                tf_data=tf_data_v26 if len(tf_data_v26) >= 2 else None,
                direction="short"
            )
            
            # v2.9: Не блокируем, используем как бонус к скору
            if confirmation["score"] >= 70:
                base_score_bonus = (confirmation["score"] - 50) // 3  # +6..+16 бонус
                print(f"✅ [v2.9] {symbol}: Confirmation score={confirmation['score']}, бонус +{base_score_bonus}")
            elif confirmation["score"] >= 50:
                base_score_bonus = (confirmation["score"] - 50) // 5  # +0..+4 бонус
                print(f"⚠️ [v2.9] {symbol}: Confirmation score={confirmation['score']} (слабый сигнал)")
            else:
                base_score_bonus = 0
                print(f"ℹ️ [v2.9] {symbol}: Confirmation score={confirmation['score']} (нейтрально)")
            
        except Exception as e:
            print(f"⚠️ [v2.9] {symbol}: Ошибка EntryConfirmation: {e}")
            base_score_bonus = 0  # При ошибке продолжаем без бонуса
        
        # ✅ v2.9: TBS (Test Before Strike) — ретест Order Block
        try:
            tbs = detect_tbs_entry(ohlcv_primary, direction="short")
            if tbs and tbs["found"]:
                print(f"🎯 [v2.9] {symbol}: TBS DETECTED! Ретест зоны ${tbs['zone']:.4f}")
                base_score_bonus += 10  # +10 за TBS
        except Exception as e:
            print(f"⚠️ [v2.9] {symbol}: TBS error: {e}")

        # ✅ RSI 30m — информационный контекст (НЕ блокер!)
        # В даунтренде RSI 30m < 25 — это ПОДТВЕРЖДЕНИЕ падения, а не повод блокировать
        rsi_30m = 50.0  # дефолт
        rsi_30m_score_adj = 0
        try:
            if ohlcv_30m and len(ohlcv_30m) >= 14:
                closes_30m = [c.close for c in ohlcv_30m[-14:]]
                gains_30m = [max(0, closes_30m[i]-closes_30m[i-1]) for i in range(1,14)]
                losses_30m = [max(0, closes_30m[i-1]-closes_30m[i]) for i in range(1,14)]
                ag_30m = sum(gains_30m)/13; al_30m = sum(losses_30m)/13
                rsi_30m = 100 - 100/(1 + ag_30m/al_30m) if al_30m > 0 else 50
                # RSI 30m < 30 при падении = подтверждение медвежьего моментума
                if rsi_30m < 20:
                    rsi_30m_score_adj = +3   # очень перепродан — моментум сильный
                elif rsi_30m < 30:
                    rsi_30m_score_adj = +5   # перепродан — даунтренд подтверждён
                elif rsi_30m > 70:
                    rsi_30m_score_adj = -5   # перекуплен — откат вероятен, против шорта
        except Exception:
            pass

        # ✅ Multi-TF RSI 4h — контекст высшего порядка (НЕ блокер!)
        # RSI 4h < 30 = глубокий даунтренд = ЛУЧШИЙ SHORT (не блокируем!)
        rsi_4h = 50.0  # дефолт
        rsi_4h_score_adj = 0
        try:
            # ohlcv_4h уже загружен выше
            if ohlcv_4h and len(ohlcv_4h) >= 14:
                closes_4h = [c.close for c in ohlcv_4h[-14:]]
                gains = [max(0, closes_4h[i]-closes_4h[i-1]) for i in range(1,14)]
                losses = [max(0, closes_4h[i-1]-closes_4h[i]) for i in range(1,14)]
                ag = sum(gains)/13; al = sum(losses)/13
                rsi_4h = 100 - 100/(1 + ag/al) if al > 0 else 50
                # RSI 4h < 30 = сильный медвежий тренд = +10 к шорту
                if rsi_4h < 20:
                    rsi_4h_score_adj = +12
                elif rsi_4h < 30:
                    rsi_4h_score_adj = +8   # глубокий даунтренд = хороший SHORT
                elif rsi_4h < 40:
                    rsi_4h_score_adj = +5   # даунтренд подтверждён
                elif rsi_4h > 70:
                    rsi_4h_score_adj = -8   # перекуплен на 4h — риск разворота против шорта
        except Exception:
            pass

        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend   = state.pattern_detector._get_price_trend(ohlcv_primary)
        patterns      = state.pattern_detector.detect_all(ohlcv_primary, hourly_deltas, md)
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
                vols = [c.quote_volume for c in ohlcv_primary[-5:]]

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
            print(f"🔴 [FILTER0-SCORE] {symbol}: score_result.is_valid=False — отфильтрован!")
            return None

        price       = md.price
        final_score = score_result.total_score + oi_score_adj + base_score_bonus
        # ✅ FIX: добавляем RSI multi-TF бонусы
        final_score += rsi_30m_score_adj + rsi_4h_score_adj
        if rsi_30m_score_adj != 0:
            print(f"[MTF] {symbol}: RSI30m={rsi_30m:.0f} adj={rsi_30m_score_adj:+d}")
        if rsi_4h_score_adj != 0:
            print(f"[MTF] {symbol}: RSI4h={rsi_4h:.0f} adj={rsi_4h_score_adj:+d}")
        reasons     = list(score_result.reasons)

        # ── SHORT-специфичные фильтры ─────────────────────────────────────────
        sf   = get_short_filter()
        filt = sf.check(
            market_data=md, ohlcv_15m=ohlcv_15m,
            hourly_deltas=hourly_deltas,
        )
        if filt.blocked:
            print(f"🔴 [FILTER-BLOCKED] {symbol}: blocked=True, reasons={filt.reasons[:2]} — отфильтрован!")
            return None

        final_score += filt.score_delta
        reasons.extend(filt.reasons)

        # ── Realtime scorer ───────────────────────────────────────────────────
        rt = get_realtime_scorer()
        rt_result = await rt.score(
            direction="short", market_data=md,
            base_score=final_score, hourly_deltas=hourly_deltas,
        )
        if rt_result.early_only:
            # ✅ Ранние сигналы 63-66% — только в Telegram, без сделки
            await state.telegram.send_message(
                f"🛰️ <b>РАННИЙ SHORT WATCH</b>  Score: {rt_result.final_score:.0f}%\n\n"
                f"🔴 <b>#{symbol}</b>  ${md.price:,.6f}\n"
                + "\n".join(f"  • {r}" for r in rt_result.factors[:4])
                + "\n\n⏳ <i>Ждём подтверждения.</i>"
                )
            print(f"🔴 [FILTER-RT] {symbol}: should_enter=False — отфильтрован!")
            return None

        final_score = rt_result.final_score
        reasons.extend(rt_result.factors)
        print(f"📊 [RT] {symbol}: base={rt_result.base_score} bonus={rt_result.bonus:+d} final={rt_result.final_score}")

        # 🆕 Бонус если монета в RSI watchlist (RSI растёт от 35)
        if _rsi_tracker.is_rsi_rising(symbol):
            rsi_info = _rsi_tracker.get_info(symbol)
            peak = rsi_info.get("peak_rsi", 0)
            if peak >= 55:   # RSI дошёл до зоны SHORT
                final_score += 5
                reasons.append(f"RSI watchlist: вырос с 35 до {peak:.0f} → +5")
        
        if final_score < Config.MIN_SCORE:
            print(f"🔴 [FILTER1] {symbol}: score={final_score} < MIN={Config.MIN_SCORE} — отфильтрован!")
            return None

        # OI proxy — тихо (убраны verbose debug logs)

        # ── Динамические TP для SHORT ─────────────────────────────────────────
        best_pattern = patterns[0].name if patterns else None
        tp_levels, tp_weights = get_short_tp_config(
            funding_rate=md.funding_rate,
            pattern_name=best_pattern,
            btc_trend="neutral",
        )

        # ── SL ВЫШЕ входа, TP НИЖЕ входа (SHORT) ─────────────────────────────
        
        # ✅ v2.9: Пробуем использовать Liquidity Sweep Tail для точного стопа
        sweep_sl = None
        try:
            from core.liquidity_detector import LiquidityDetector
            ld = LiquidityDetector(_ohlcv(ohlcv_15m))
            sweep_result = ld.detect_sweep(direction="short")
            if sweep_result and sweep_result.found_sweep and sweep_result.sweep_high > 0:
                # Стоп за хвост свечи sweep + 0.3% buffer (выше для SHORT)
                sweep_sl = sweep_result.sweep_high * 1.003
                print(f"🎯 [v2.9] {symbol}: Sweep Tail SL = ${sweep_sl:.6f} (sweep_high=${sweep_result.sweep_high:.6f})")
        except Exception as e:
            pass  # Fallback на стандартный расчёт
        
        # Используем sweep-based стоп если он лучше (выше цены но не слишком далеко)
        default_sl = price * (1 + Config.SL_BUFFER / 100)
        if sweep_sl and sweep_sl > price and sweep_sl < price * 1.03:  # Не более 3% от цены
            stop_loss = sweep_sl
            reasons.append(f"🎯 v2.9 Sweep Tail SL: ${stop_loss:.6f}")
        else:
            stop_loss = default_sl
            
        entry_price = price
        smc_data    = {}

        if Config.USE_SMC:
            try:
                from core.smc_ict_detector import get_smc_result   # ✅ FIX: core not utils
                smc = get_smc_result(_ohlcv(ohlcv_primary), "short",
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
            print(f"🔴 [FILTER2-SMC] {symbol}: score={final_score} < MIN={Config.MIN_SCORE} — отфильтрован!")
            return None

        # ✅ SL для SHORT: минимум = SL_BUFFER%, не захардкоженный 1%
        min_sl_dist = Config.SL_BUFFER / 100
        if (stop_loss - price) / price < min_sl_dist:
            stop_loss = price * (1 + Config.SL_BUFFER / 100)

        # TP НИЖЕ входа для SHORT
        take_profits = [
            (round(price * (1 - tp / 100), 8), tp_weights[i] if i < len(tp_weights) else 15)
            for i, tp in enumerate(tp_levels)
        ]

        sl_pct = round((stop_loss - price) / price * 100, 2)
        print(f"🟢 [SIGNAL] {symbol}: score={final_score} — сигнал создан!")
        return {
            "symbol": symbol, "direction": "short",
            "score": final_score, "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            "price": price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 8), "sl_pct": sl_pct,
            "take_profits": take_profits,
            "patterns": [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "primary_tf": primary_tf,  # ✅ ТФ сигнала (2h/4h для исполнения)
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
            # 🆕 Сырые рыночные данные для /alltradestat
            "rsi_1h":           round(md.rsi_1h or 0, 1),
            "funding_rate":     round(md.funding_rate, 4),
            "oi_change":        round(md.oi_change_4d, 2),
            "long_short_ratio": round(md.long_short_ratio, 1),
            "volume_spike_ratio": round(getattr(md, "volume_spike_ratio", 1.0), 2),
            "atr_14_pct":       round(getattr(md, "atr_14_pct", 0.5), 3),
            "pattern":          patterns[0].name if patterns else "",
            "smc_data":         smc_data,
            # ✅ v2.8: Order Block данные для лимитных входов
            "ob_data":          ob_data if isinstance(ob_data, dict) else None,
            "entry_type":       ob_data.get("entry_type", "MARKET") if isinstance(ob_data, dict) else "MARKET",
            "limit_price":      ob_data.get("limit_price") if isinstance(ob_data, dict) else None,
            "limit_ttl":        symbol_profile.calculate_limit_ttl(ob_data.get("ob_freshness", "medium")) if symbol_profile and isinstance(ob_data, dict) else 900,
            "profile":          {
                "volatility_class": symbol_profile.volatility_class if symbol_profile else "medium",
                "ideal_tf": symbol_profile.ideal_tf if symbol_profile else "30m",
                "atr_pct": symbol_profile.atr_14_pct if symbol_profile else 1.0,
            } if symbol_profile else None,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "active", "taken_tps": [],
        }
    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None



async def scan_market():
    """
    ✅ v2.7 АРХИТЕКТУРА (SHORT, NO BTC CORR):
    - Telegram сигналы: ВСЕГДА при score >= MIN_SCORE (даже при 20/20 SHORT на бирже)
    - Биржевое исполнение: только если active_short < MAX и не /pause
    - Единственный блокер: команда /pause
    """
    if state.is_paused:
        return

    print(f"\n🔍 SHORT scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    # Считаем только SHORT позиции этого бота
    active_count  = await _count_real_positions()
    exchange_full = active_count >= Config.MAX_POSITIONS
    if exchange_full:
        print(f"📊 Exchange SHORT slots: {active_count}/{Config.MAX_POSITIONS} — "
              f"сигналы в TG продолжаются, биржа ждёт освобождения")

    new_signals   = 0
    tg_only_count = 0

    for symbol in state.watchlist:
        try:
            if _is_fresh(state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)):
                continue

            signal = await scan_symbol(symbol)
            if not signal:
                continue

            # ✅ ВСЕГДА: Telegram сигнал
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

            # ✅ TF фильтр ОТКЛЮЧЕН: все timeframe на биржу (v2.7)
            primary_tf = signal.get("timeframe", "15m")
            tf_for_execution = True  # Разрешаем всем ТФ

            # Биржевое исполнение: только если есть SHORT слоты, не на паузе
            if (not exchange_full and Config.AUTO_TRADING and not state.is_paused):
                if state.auto_trader:
                    try:
                        await state.auto_trader.execute_signal(signal)
                        active_count += 1
                        exchange_full = active_count >= Config.MAX_POSITIONS
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")
                new_signals += 1
                print(f"✅ SHORT executed: {symbol} [{primary_tf}] Score={signal['score']:.0f}% SL={signal['sl_pct']}%")
            else:
                tg_only_count += 1
                if exchange_full:
                    reason = "max SHORT positions"
                else:
                    reason = "paused or AT disabled"
                print(f"📡 SHORT TG-only: {symbol} [{primary_tf}] Score={signal['score']:.0f}% [{reason}]")

            await asyncio.sleep(0.4)
        except Exception as e:
            print(f"Error {symbol}: {e}")

    state.daily_signals += new_signals + tg_only_count
    state.last_scan      = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))
    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status":         "paused" if state.is_paused else "running",
        "last_scan":      state.last_scan.isoformat(),
        "daily_signals":  state.daily_signals,
        "active_signals": state.active_signals,
    })
    print(f"✅ Scan done. Executed: {new_signals} | TG-only: {tg_only_count} | "
          f"Exchange SHORT: {active_count}/{Config.MAX_POSITIONS}")


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
