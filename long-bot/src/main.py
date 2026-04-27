"""
🤖 LONG BOT v4.0 — FastAPI Application

ИСПРАВЛЕНИЯ v4.0 (критические):
  ✅ BTC фильтр ОПЦИОНАЛЬНЫЙ — по умолч. ВЫКЛ (BTC_CORRELATION_FILTER=false)
     Альткоины торгуются по СВОЕЙ структуре независимо от BTC!
  ✅ Бонус за decoupling: альт растёт пока BTC падает → +5-12 к скору
  ✅ Дневной P&L стоп -5% (DAILY_LOSS_STOP_PCT)
  ✅ Азиатская сессия 03-06 UTC блокировка (BLOCK_ASIAN_SESSION)
  ✅ Zombie cleanup — удаление мёртвых Redis позиций
  ✅ SHORT BOT: исправлен "return Nonee" — краш на каждом символе
  ✅ TP уровни в sweep-пути используют Config.TP_LEVELS (было хардкод 4%)
  ✅ market_context.py — новый модуль контекста рынка
"""

import os
import time
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
from core.scorer import get_long_scorer
from core.pattern_detector import LongPatternDetector   # ← единый файл
from core.position_tracker import PositionTracker
from core.realtime_scorer import get_realtime_scorer
from core.liquidity_detector import detect_smart_money_entry  # ✅ v2.7
from core.entry_confirmation import EntryConfirmation  # ✅ v2.7
from core.tbs_detector import detect_tbs_entry  # ✅ v2.7 TBS
from core.symbol_profiler import SymbolProfile, get_symbol_profiler, get_profile
from core.order_block_detector import detect_order_blocks, format_ob_for_signal
from core.liquidity_pool_scanner import scan_liquidity_pools, LiquidityPoolScanner  # ✅ v2.8
from bot.telegram import TelegramBot, TelegramCommandHandler
from core.market_context import get_market_context, MarketContextFilter  # ✅ v4.0: Market Context Filter


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "long"
    # ✅ FIX: MIN_LONG_SCORE default = 70
    # ✅ v2.5 BACKTEST: Медвежий рынок. Score 75+ → PF 2.07x
    # 🔥 FIX v3.0.3: Снижаем MIN_SCORE с 70 до 60 (слишком много фильтрации!)
    MIN_SCORE     = int(os.getenv("MIN_LONG_SCORE", "65"))  # ✅ FIX v5: 60→65 лучший баланс качество/частота
    # ✅ FIX: SCAN_INTERVAL default = 200
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))  # BACKTEST: 120с
    # ✅ FIX: MAX_WATCHLIST default = 300
    MAX_POSITIONS = int(os.getenv("MAX_LONG_POSITIONS", "20"))
    LEVERAGE      = os.getenv("LONG_LEVERAGE", "5-50")

    # LONG: SL НИЖЕ входа, TP ВЫШЕ входа
    # ✅ v2.5: Уменьшен SL с 1.5% до 1.2% для лучшего R:R
    SL_BUFFER     = float(os.getenv("LONG_SL_BUFFER", "1.5"))  # ✅ FIX v5: 1.5% — даёт дышать, меньше ложных стопов

    # TP levels из Config (v2.5: увеличены для R:R ≥ 2:1)
    TP_LEVELS  = [2.5, 5.0, 8.0, 12.0, 20.0, 35.0]  # ✅ FIX v5: TP1=2.5% → R:R=1.67:1 (математически прибыльно)
    TP_WEIGHTS = [30,  25,  20,  15,  7,    3]   # TP1=30% — основной сбор прибыли

    # Trailing — LONG активирует при +2.5% (после TP1)
    TRAIL_ACTIVATION = float(os.getenv("LONG_TRAIL_ACTIVATION", "0.025"))
    LONG_TRAIL_ACTIVATION = TRAIL_ACTIVATION  # Alias для position_tracker.py

    SIGNAL_TTL_HOURS = 24

    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "true").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.0005"))

    USE_SMC        = os.getenv("USE_SMC", "true").lower() == "true"
    USE_COINGLASS  = bool(os.getenv("COINGLASS_API_KEY", ""))

    # ✅ FIX: default MAX_WATCHLIST = 300
    # LONG: 1M$ min объём — фильтр мусора вроде BANANA, DENT, AIA
    MIN_VOLUME_USDT = int(os.getenv("MIN_VOLUME_USDT", "300000"))  # ✅ 1M$ фильтр мусора
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
        self.market_ctx       = None  # ✅ v4.0 Market Context Filter
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
    print("🚀 Starting LONG Bot v6.0...")
    state.start_time = datetime.utcnow()

    state.redis            = get_redis_client()
    state.binance          = get_binance_client()
    state.scorer           = get_long_scorer(Config.MIN_SCORE)
    state.pattern_detector = LongPatternDetector()
    # ✅ FIX v2.4: LONG бот использовал SHORT_TELEGRAM_BOT_TOKEN → crash!
    state.telegram = TelegramBot(
        bot_token=os.getenv("LONG_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("LONG_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
        topic_id=os.getenv("LONG_TELEGRAM_TOPIC_ID") or os.getenv("TELEGRAM_TOPIC_ID"),
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
            # Retry once
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


    # ✅ FIX v5: Инициализируем Market Context Filter (было None — весь v4.0 функционал не работал!)
    state.market_ctx = MarketContextFilter(
        binance_client=state.binance,
        redis_client=state.redis
    )
    print("✅ MarketContextFilter initialized (BTC filter, session block, daily PnL, decoupling)")

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
        f"🟢 <b>LONG Bot v5.0 запущен</b>\n\n"
        f"📊 Watchlist: {len(state.watchlist)} монет\n"
        f"🛑 SL: {Config.SL_BUFFER}%  |  Score≥{Config.MIN_SCORE}%\n"
        f"🤖 AutoTrader: {at_str}\n"
        f"⚙️ Risk: {Config.RISK_PER_TRADE*100:.3f}% | Scan: {Config.SCAN_INTERVAL}s\n"
        f"🔍 LongScorer: ✅ | RealtimeScorer: ✅"
    )
    print(f"✅ LONG Bot started! AutoTrader: {at_str}")

    state.tracker = PositionTracker(
        bot_type=Config.BOT_TYPE, telegram=state.telegram,
        redis_client=state.redis, binance_client=state.binance,
        config=Config, auto_trader=state.auto_trader,
    )

    asyncio.create_task(background_scanner())
    asyncio.create_task(state.tracker.run())

    yield

    state.is_running = False
    print("🛑 Shutting down LONG Bot...")
    if state.binance:
        await state.binance.close()
    if state.auto_trader:
        await state.auto_trader.bingx.close()
    print("👋 LONG Bot stopped")


app = FastAPI(lifespan=lifespan, title="LONG Bot v5.0")


# ============================================================================
# ROUTES
# ============================================================================

# ✅ HEAD + GET для UptimeRobot (405 → 200)
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"status": "ok", "bot": "long", "version": "2.9",
                         "watchlist": len(state.watchlist),
                         "active": state.active_signals})

# ✅ HEAD + GET для Render health checks (405 → 200)
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({"bot": "LONG Bot v6.0", "status": "running" if state.is_running else "stopped"})

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
    """GET /webhook/setup OR /webhook/reset → принудительно регистрирует вебхук Telegram."""
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        return {"error": "RENDER_EXTERNAL_URL not set in env vars"}
    if not state.telegram:
        return {"error": "Telegram not initialized"}
    wh_url = f"{render_url}/webhook"
    # Сначала удаляем старый вебхук
    await state.telegram.delete_webhook()
    await asyncio.sleep(1)
    # Регистрируем новый
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

# _count_real_positions: see full implementation below (filters LONG only)


async def scan_symbol(symbol: str) -> Optional[Dict]:
    """
    LONG scan_symbol v2.3:
      - SL НИЖЕ входа (long: stop loss = цена * (1 - SL_BUFFER%))
      - TP ВЫШЕ входа (long: фиксируем прибыль при росте)
      - OI Proxy: bull_confirm / accumulation / weakness_long
      - volume_spike_ratio + atr_14_pct → scorer
    """
    try:
        md = await state.binance.get_complete_market_data(symbol)
        if not md:
            return None
        
        # ✅ FIX: Определяем price сразу, чтобы избежать UnboundLocalError
        price = md.price

        # ✅ v4.0: MARKET CONTEXT FILTER — BTC корреляция, сессия, дневной стоп
        if hasattr(state, 'market_ctx') and state.market_ctx:
            ctx = await state.market_ctx.check(
                direction="long",
                symbol=symbol,
                block_asian_session=True,
                allow_decoupled_alts=True
            )
            if not ctx.allowed:
                # Логируем блокировку только 1 раз в минуту чтобы не спамить
                print(f"⛔ [CTX-LONG] {symbol}: {ctx.block_reason}")
                return None
            for w in ctx.warnings:
                print(f"⚠️ [CTX-LONG] {symbol}: {w}")

        # 🆕 RSI Watchlist tracking — обновляем трекер
        rsi_current = md.rsi_1h or 0
        _rsi_tracker.update(symbol, rsi_current)

        # ✅ Multi-TF загрузка: 30m + 1h параллельно (убран 15m — 50% стопов в бэктесте)
        ohlcv_30m_task = state.binance.get_klines(symbol, "30m", 100)
        ohlcv_1h_task = state.binance.get_klines(symbol, "1h", 50)
        ohlcv_30m, ohlcv_1h = await asyncio.gather(ohlcv_30m_task, ohlcv_1h_task)

        # Используем 30m как основной ТФ для анализа (вместо 15m)
        ohlcv_15m = ohlcv_30m  # совместимость с existing code
        primary_tf = "30m"
        ohlcv_primary = ohlcv_30m
        
        # =========================================================================
        # ✅ v2.8: SYMBOL PROFILER — индивидуальный профиль монеты
        # =========================================================================
        symbol_profile = None
        try:
            symbol_profile = await get_profile(symbol, state.binance)
            if symbol_profile:
                # Адаптируем ТФ под профиль (если монета волатильная)
                if symbol_profile.ideal_tf != "30m" and symbol_profile.ideal_tf in ["5m", "15m", "1h"]:
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
                direction="long",  # Для LONG бота ищем bullish OB
                current_price=current_price
            )
            
            if ob_result and ob_result.bullish_ob:
                ob = ob_result.bullish_ob
                if ob.quality >= 60 and ob.freshness.value in ["fresh", "medium"]:
                    ob_data = format_ob_for_signal(ob)
                    print(f"🎯 [v2.9] {symbol}: OB detected @ ${ob.price_optimal:.6f} (Q:{ob.quality}, {ob.freshness.value})")
        except Exception as e:
            print(f"⚠️ [v2.9] {symbol}: OB detection error: {e}")

        # =========================================================================
        # ✅ v2.9: ENTRY CONFIRMATION SYSTEM (мульти-ТФ + объём + ATR + уровни)
        # =========================================================================
        try:
            # 1. Проверяем Liquidity Sweep (сбор стопов лонгистов = шорт ликвидность)
            sweep = detect_smart_money_entry(ohlcv_primary, direction="long")
            if sweep and sweep["found"]:
                # 2. Подтверждение фильтрами
                tf_data_v26 = {}
                if ohlcv_1h: tf_data_v26["1h"] = ohlcv_1h
                
                confirmation = EntryConfirmation.comprehensive_check(
                    ohlcv_primary,
                    tf_data=tf_data_v26 if len(tf_data_v26) >= 1 else None,
                    direction="long"
                )
                
                if confirmation["passed"] and confirmation["score"] >= 75:
                    # 🎯 ВСЁ ПОДТВЕРЖДЕНО — супер-сигнал!
                    base_score = 85 + (confirmation["score"] - 75) // 5  # 85-95
                    reasons = sweep["reasons"] + confirmation["reasons"]
                    
                    entry = md.price
                    sl = entry * (1 - Config.SL_BUFFER / 100)
                    # ✅ v4.0 FIX: Используем Config.TP_LEVELS вместо хардкода 4%/8%/12%
                    tp1 = entry * (1 + Config.TP_LEVELS[0] / 100)  # 1.5%
                    tp2 = entry * (1 + Config.TP_LEVELS[1] / 100)  # 3.0%
                    tp3 = entry * (1 + Config.TP_LEVELS[2] / 100)  # 5.0%
                    
                    print(f"🎯 [v2.9] LIQUIDITY SWEEP {symbol}: score={base_score}, conf={confirmation['score']}")
                    
                    # ✅ FIX v5: единый формат TP [(price, weight)] для position_tracker
                    _tp_w = Config.TP_WEIGHTS
                    return {
                        "symbol": symbol,
                        "direction": "long",
                        "score": min(100, base_score),
                        "price": entry,
                        "entry_price": entry,
                        "stop_loss": sl,
                        "take_profits": [
                            (round(entry * (1 + Config.TP_LEVELS[i] / 100), 8), _tp_w[i])
                            for i in range(min(3, len(Config.TP_LEVELS)))
                        ],
                        "reasons": reasons[:5],
                        "timeframe": primary_tf,
                        "pattern": "LIQUIDITY_SWEEP",
                        "best_pattern": "LIQUIDITY_SWEEP",  # Для telegram
                        "indicators": {"SMC": "Sweep+TBS", "Confirmation": f"Score:{confirmation['score']}"},
                        "zones": sweep.get("zones", {}) if isinstance(sweep, dict) else {}
                    }
                else:
                    print(f"⚠️ [v2.9] {symbol}: Sweep найден но не подтверждён")
            
            # 3. Нет sweep — проверяем обычные фильтры (v2.9: бонусы, не блок)
            tf_data_v26 = {}
            if ohlcv_1h: tf_data_v26["1h"] = ohlcv_1h
            
            confirmation = EntryConfirmation.comprehensive_check(
                ohlcv_primary,
                tf_data=tf_data_v26 if len(tf_data_v26) >= 1 else None,
                direction="long"
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
            print(f"⚠️ [v2.9] {symbol}: Ошибка: {e}")
            base_score_bonus = 0
        
        # ✅ v2.9: TBS (Test Before Strike) — ретест поддержки
        tbs_found = False
        tbs_zone = None
        try:
            tbs = detect_tbs_entry(ohlcv_primary, direction="long")
            if tbs and tbs["found"]:
                tbs_found = True
                tbs_zone = tbs['zone']
                print(f"🎯 [v2.9] {symbol}: TBS DETECTED! Ретест ${tbs_zone:.4f}")
                base_score_bonus += 10  # +10 за TBS
        except Exception as e:
            print(f"⚠️ [v2.9] {symbol}: TBS error: {e}")

        # ✅ RSI 30m — бонус/штраф к скору (v2.9: не блокер)
        rsi_30m_adj = 0
        try:
            if ohlcv_30m and len(ohlcv_30m) >= 14:
                closes_30m = [c.close for c in ohlcv_30m[-14:]]
                gains_30m = [max(0, closes_30m[i]-closes_30m[i-1]) for i in range(1,14)]
                losses_30m = [max(0, closes_30m[i-1]-closes_30m[i]) for i in range(1,14)]
                ag_30m = sum(gains_30m)/13; al_30m = sum(losses_30m)/13
                rsi_30m = 100 - 100/(1 + ag_30m/al_30m) if al_30m > 0 else 50
                # v2.7: Не блокируем, корректируем скор
                if rsi_30m < 30:
                    rsi_30m_adj = +5  # Перепродан — хорошо для LONG
                elif rsi_30m > 75:
                    rsi_30m_adj = -5  # Перекуплен — плохо для LONG
                elif rsi_30m > 65:
                    rsi_30m_adj = -2  # Начало перекупленности
        except Exception:
            pass
        
        # ✅ Multi-TF RSI 4h — бонус/штраф (v2.7: не блокер)
        rsi_4h_adj = 0
        try:
            ohlcv_4h = await state.binance.get_klines(symbol, "4h", 14)
            if ohlcv_4h and len(ohlcv_4h) >= 14:
                closes_4h = [c.close for c in ohlcv_4h[-14:]]
                gains = [max(0, closes_4h[i]-closes_4h[i-1]) for i in range(1,14)]
                losses = [max(0, closes_4h[i-1]-closes_4h[i]) for i in range(1,14)]
                ag = sum(gains)/13; al = sum(losses)/13
                rsi_4h = 100 - 100/(1 + ag/al) if al > 0 else 50
                # v2.7: Не блокируем, корректируем скор
                if rsi_4h < 35:
                    rsi_4h_adj = +8   # Перепродан на 4h — отлично для LONG
                elif rsi_4h > 70:
                    rsi_4h_adj = -8   # Перекуплен на 4h — плохо для LONG
                elif rsi_4h > 60:
                    rsi_4h_adj = -3   # Начало перекупленности
        except Exception:
            pass
        
        # Применяем RSI корректировки к базовому бонусу
        base_score_bonus = base_score_bonus + rsi_30m_adj + rsi_4h_adj

        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend   = state.pattern_detector._get_price_trend(ohlcv_30m)
        patterns      = state.pattern_detector.detect_all(ohlcv_30m, hourly_deltas, md)
        p4d           = await _get_price_change_4d(symbol, md.price_change_24h * 4)

        # ── OI Proxy (LONG специфика) ─────────────────────────────────────────
        oi_bull_confirm  = False
        oi_accumulation  = False
        oi_weakness_long = False
        oi_score_adj     = 0.0

        try:
            oi_history = await state.binance.get_open_interest_history(symbol, "15m", 5)
            # ✅ FIX L2: проверяем что OI данные свежие (не старше 30 мин)
            # Если Bybit геоблокирован и fallback Binance — OI может быть stale
            if oi_history:
                latest_ts = oi_history[-1].get("timestamp", 0) if isinstance(oi_history[-1], dict) else 0
                if latest_ts and (time.time() * 1000 - latest_ts) > 1_800_000:  # >30 мин
                    oi_history = []  # данные устарели — не используем
            if oi_history and len(oi_history) >= 3:
                ois  = [float(h.get("sumOpenInterest", 0)) for h in oi_history]
                vols = [c.quote_volume for c in ohlcv_30m[-5:]]

                oi_growing  = ois[-1] > ois[0] if ois[0] else False
                vol_growing = len(vols) >= 3 and vols[-1] > vols[-3]
                price_up    = getattr(md, "price_change_1h", 0) > 0.5
                price_down  = getattr(md, "price_change_1h", 0) < -0.5

                # Bull confirm: цена растёт + OI растёт + объём растёт
                oi_bull_confirm = oi_growing and vol_growing and price_up
                if oi_bull_confirm:
                    oi_score_adj += 1.5

                # OI стабильно растёт = реальные деньги входят в лонг
                oi_accumulation = (all(ois[i] <= ois[i+1] for i in range(len(ois)-1))
                                   if len(ois) >= 3 else False)
                if oi_accumulation:
                    oi_score_adj += 2.5

                # Слабость: цена растёт но OI/объём падают = нет поддержки
                oi_falling      = ois[-1] < ois[0] if ois[0] else False
                vol_falling     = len(vols) >= 3 and vols[-1] < vols[-3]
                oi_weakness_long = price_up and (oi_falling or vol_falling)
                if oi_weakness_long:
                    oi_score_adj -= 2.0
        except Exception as e:
            print(f"OI Proxy error {symbol}: {e}")

        # ── Base score ────────────────────────────────────────────────────────
        # ✅ v4.0: Рассчитываем изменение альта за 1ч для детектора независимости
        symbol_change_1h = 0.0
        btc_change_1h_score = 0.0
        try:
            if ohlcv_1h and len(ohlcv_1h) >= 2:
                c1 = ohlcv_1h[-1]
                c0 = ohlcv_1h[-2]
                close1 = float(c1.close if hasattr(c1, 'close') else c1[4])
                open0  = float(c0.open  if hasattr(c0, 'open')  else c0[1])
                if open0 > 0:
                    symbol_change_1h = (close1 - open0) / open0 * 100
            # BTC change от market_ctx если доступен
            if hasattr(state, 'market_ctx') and state.market_ctx and state.market_ctx._btc_cache:
                btc_change_1h_score = state.market_ctx._btc_cache.get('change_1h', 0.0)
        except Exception:
            pass

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
            symbol_change_1h=symbol_change_1h,        # ✅ v4.0: для decoupling bonus
            btc_change_1h=btc_change_1h_score,        # ✅ v4.0: для decoupling bonus
        )
        
        # ✅ FIX v3.1: SMART SCORING — многоуровневый оверрайд для LONG
        ob_quality    = (ob_result.bullish_ob.quality if ob_result and ob_result.bullish_ob else 0)
        ob_quality_ok = ob_quality >= 60   # ✅ Снижен порог с 70 → 60
        ob_q_high     = ob_quality >= 70   # Высокое качество
        
        if not score_result.is_valid:
            override_reason = None
            boost = 0
            
            # Уровень 1: TBS + OB >= 70 — сильный оверрайд (было единственным условием)
            if tbs_found and ob_q_high:
                override_reason = f"TBS+OB_Q{ob_quality}"
                boost = 15
            # Уровень 2: TBS + OB >= 60 — умеренный оверрайд  
            elif tbs_found and ob_quality_ok:
                override_reason = f"TBS+OB_Q{ob_quality}"
                boost = 10
            # Уровень 3: только TBS без OB (риск выше)
            elif tbs_found and base_score_bonus >= 5:
                override_reason = f"TBS+confirmation"
                boost = 8
            # Уровень 4: OB >= 70 без TBS (институциональная зона)
            elif ob_q_high and base_score_bonus >= 3:
                override_reason = f"OB_Q{ob_quality}+confirmation"
                boost = 5
            
            if override_reason:
                print(f"💡 [SMART-SCORE-LONG] {symbol}: is_valid=False, но {override_reason} — ОВЕРРАЙД! Скор +{boost}")
                from core.scorer import ScoreResult, Confidence
                score_result = ScoreResult(
                    total_score=max(70, score_result.total_score + boost),
                    max_possible=score_result.max_possible,
                    direction=score_result.direction,
                    is_valid=True,
                    confidence=Confidence.MEDIUM if boost < 12 else Confidence.HIGH,
                    grade="B" if boost < 12 else "A",
                    components=score_result.components,
                    reasons=score_result.reasons + [f"🎯 {override_reason} — умный вход"],
                )
            else:
                print(f"🔴 [FILTER0-LONG] {symbol}: score_result.is_valid=False — отфильтрован! (нет TBS/OB70)")
                return None
        
        reasons     = list(score_result.reasons)
        final_score = min(100, score_result.total_score + max(0, base_score_bonus))  # ← БАЗОВЫЙ + БОНУСЫ от confirmation/TBS

        # ── Realtime scorer ───────────────────────────────────────────────────
        rt = get_realtime_scorer()
        rt_result = await rt.score(
            direction="long", market_data=md,
            base_score=final_score, hourly_deltas=hourly_deltas,
        )
        if rt_result.early_only:
            # ✅ Ранние сигналы 63-66% — только в Telegram, без сделки
            await state.telegram.send_message(
                f"🛰️ <b>РАННИЙ LONG WATCH</b>  Score: {rt_result.final_score:.0f}%\n\n"
                f"🟢 <b>#{symbol}</b>  ${md.price:,.6f}\n"
                + "\n".join(f"  • {r}" for r in rt_result.factors[:4])
                + "\n\n⏳ <i>Ждём подтверждения.</i>"
                )
            return None

        final_score = rt_result.final_score
        reasons.extend(rt_result.factors)
        print(f"📊 [RT-LONG] {symbol}: base={rt_result.base_score} bonus={rt_result.bonus:+d} final={rt_result.final_score}")

        # 🆕 Бонус если RSI восстанавливается от низов (LONG сигнал)
        rsi_now = md.rsi_1h or 0
        if 30 <= rsi_now <= 50 and rsi_current > 0:
            final_score += 3
            reasons.append(f"RSI восстановление {rsi_now:.0f} → +3")
        
        # 🌊 ELLIOTT WAVE v3.0: Детекция волн для точных входов
        elliott_min_score = Config.MIN_SCORE  # По умолчанию
        try:
            from core.elliott_detector import detect_elliott_wave, WavePosition
            
            # Получаем OHLCV для анализа волн (используем оригинальные данные, не _ohlcv)
            wave_ohlcv = ohlcv_primary if primary_tf == "1h" else ohlcv_15m
            wave_result = detect_elliott_wave(wave_ohlcv, direction="long")
            
            # 📝 ЛОГИРОВАНИЕ ВОЛН (для анализа)
            print(f"🌊 [ELLIOTT-LONG] {symbol}: Волна={wave_result.wave} | "
                  f"Тип={wave_result.wave_type.value} | "
                  f"Позиция={wave_result.position.value} | "
                  f"Уверенность={wave_result.confidence:.0%} | "
                  f"Качество={wave_result.structure_quality}")
            # Безопасный доступ к details
            details_reason = wave_result.details.get('reason', 'N/A') if isinstance(wave_result.details, dict) else 'N/A'
            print(f"🌊 [ELLIOTT-LONG] {symbol}: Детали: {details_reason}")
            
            # 🚫 БЛОКИРОВКА ЛОВУШЕК (Волна 2 и B)
            # ✅ FIX: НЕ блокируем неизвестные волны "?" — только реальные ловушки с высокой уверенностью
            if wave_result.is_trap and wave_result.wave not in ["?", "unknown"] and wave_result.confidence > 0.70:  # ✅ FIX v5: 0.5→0.7 меньше ложных блоков
                print(f"🚫 [ELLIOTT-BLOCK-LONG] {symbol}: Волна {wave_result.wave} — ЛОВУШКА! "
                      f"Блокируем вход. Следующая цель: {wave_result.next_target}")
                # Пишем в Redis для анализа
                try:
                    reason_text = wave_result.details.get('reason', 'Wave 2 or B trap') if isinstance(wave_result.details, dict) else 'Wave 2 or B trap'
                    state.redis.save_signal(Config.BOT_TYPE, symbol, {
                        "timestamp": datetime.utcnow().isoformat(),
                        "symbol": symbol,
                        "direction": "long",
                        "wave": wave_result.wave,
                        "position": wave_result.position.value,
                        "action": "BLOCKED_TRAP",
                        "reason": reason_text,
                        "score": final_score,
                        "price": md.price
                    })
                except:
                    pass
                return None  # 🚫 БЛОКИРУЕМ ВХОД
            
            # 🎯 ИДЕАЛЬНЫЕ ВХОДЫ (Волна 4 и C) — бонус и снижение минимума
            if wave_result.ideal_entry:
                wave_boost = 10 if wave_result.confidence > 0.75 else 5
                final_score += wave_boost
                elliott_min_score = max(50, Config.MIN_SCORE - 15)  # Снижаем минимум
                reasons.append(f"🌊 Elliott Wave {wave_result.wave} (ideal) +{wave_boost}")
                print(f"🎯 [ELLIOTT-BOOST-LONG] {symbol}: Идеальная волна {wave_result.wave}! "
                      f"Бонус +{wave_boost}, мин скор={elliott_min_score}")
            
            # 📈 ТРЕНД (Волна 3) — небольшой бонус
            elif wave_result.position == WavePosition.TREND:
                final_score += 3
                reasons.append(f"🌊 Elliott Wave 3 (trend) +3")
                print(f"📈 [ELLIOTT-TREND-LONG] {symbol}: Волна 3 тренда")
            
            # ⚠️ ФИНАЛ (Волна 5) — осторожно, но можно
            elif wave_result.position == WavePosition.FINAL:
                reasons.append(f"⚠️ Elliott Wave 5 (final) — осторожно!")
                print(f"⚠️ [ELLIOTT-FINAL-LONG] {symbol}: Волна 5 — финал импульса")
                # Можно добавить ужесточение SL здесь если нужно
            
            # 📝 Сохраняем инфо о волне в данные сигнала
            elliott_data = {
                "wave": wave_result.wave,
                "wave_type": wave_result.wave_type.value,
                "position": wave_result.position.value,
                "confidence": wave_result.confidence,
                "ideal_entry": wave_result.ideal_entry,
                "is_trap": wave_result.is_trap,
                "fib_ratio": wave_result.fib_ratio,
                "next_target": wave_result.next_target,
                "structure_quality": wave_result.structure_quality
            }
            
        except Exception as e:
            print(f"🌊 [ELLIOTT-ERROR-LONG] {symbol}: {e}")
            elliott_data = {"error": str(e)}
            # 🔥 FIX: При ошибке Elliott Wave — снижаем минимум для TBS+OB сигналов
            if tbs_found and ob_quality >= 70:
                elliott_min_score = 55  # Очень низкий порог при ошибке
                print(f"💡 [ELLIOTT-FALLBACK-LONG] {symbol}: Ошибка волн, но TBS+OB_Q{ob_quality} — снижаем мин до 55")
        
        # 🔥 FIX: Fallback для TBS+OB при любой ошибке Elliott
        if 'elliott_min_score' not in locals():
            elliott_min_score = Config.MIN_SCORE
            # Если нет данных Elliott но есть сильный TBS+OB — снижаем порог
            if tbs_found and ob_quality >= 70:
                elliott_min_score = max(55, Config.MIN_SCORE - 10)
                print(f"💡 [LONG-FALLBACK] {symbol}: Нет данных Elliott, TBS+OB_Q{ob_quality} — мин={elliott_min_score}")
        
        min_score_for_entry = elliott_min_score
        
        if final_score < min_score_for_entry:
            print(f"🔴 [FILTER1-LONG] {symbol}: score={final_score} < MIN={min_score_for_entry} — отфильтрован!")
            return None

        # OI proxy — тихо (убраны verbose debug logs)

        # ── LONG TP уровни из Config ──────────────────────────────────────────
        best_pattern = patterns[0].name if patterns else None
        # LONG: TP levels & weights from Config (optimised)
        tp_levels  = Config.TP_LEVELS
        tp_weights = Config.TP_WEIGHTS

        # ── SL НИЖЕ входа, TP ВЫШЕ входа (LONG) ──────────────────────────────
        price       = md.price
        
        # ✅ v2.9: Пробуем использовать Liquidity Sweep Tail для точного стопа
        sweep_sl = None
        try:
            from core.liquidity_detector import LiquidityDetector
            ld = LiquidityDetector(_ohlcv(ohlcv_15m))
            sweep_result = ld.detect_sweep(direction="long")
            if sweep_result and sweep_result.found_sweep and sweep_result.sweep_low > 0:
                # Стоп за хвост свечи sweep + 0.3% buffer
                sweep_sl = sweep_result.sweep_low * 0.997
                print(f"🎯 [v2.9] {symbol}: Sweep Tail SL = ${sweep_sl:.6f} (sweep_low=${sweep_result.sweep_low:.6f})")
        except Exception as e:
            pass  # Fallback на стандартный расчёт
        
        # Используем sweep-based стоп если он лучше (ниже цены но не слишком далеко)
        default_sl = price * (1 - Config.SL_BUFFER / 100)
        if sweep_sl and sweep_sl < price and sweep_sl > price * 0.97:  # Не более 3% от цены
            stop_loss = sweep_sl
            reasons.append(f"🎯 v2.7 Sweep Tail SL: ${stop_loss:.6f}")
        else:
            stop_loss = default_sl
            
        entry_price = price
        smc_data    = {}

        if Config.USE_SMC:
            try:
                from core.smc_ict_detector import get_smc_result   # ✅ FIX: core not utils
                smc = get_smc_result(_ohlcv(ohlcv_15m), "long",    # ✅ FIX: "long" not "short"
                                     base_sl_pct=Config.SL_BUFFER, base_entry=price)
                if smc.score_bonus > 0:
                    final_score += smc.score_bonus
                    reasons.extend(smc.reasons)
                if smc.refined_sl and smc.refined_sl < price:      # ✅ FIX: SL must be below
                    stop_loss = smc.refined_sl
                if smc.ob_entry:
                    entry_price = smc.ob_entry
                smc_data = {"has_ob": smc.has_ob, "has_fvg": smc.has_fvg,
                            "score_bonus": smc.score_bonus}
            except Exception as e:
                print(f"SMC error {symbol}: {e}")

        # 🌊 Phase 3: EQH/EQL Scanner — детекция пулов ликвидности
        pool_data = {}
        try:
            pool_scan = scan_liquidity_pools(_ohlcv(ohlcv_15m), symbol, primary_tf)
            if pool_scan.active_sweeps:
                # Бонус за активный sweep зоны ликвидности
                final_score = min(100, final_score + 10)
                reasons.append(f"🌊 Liquidity sweep detected (+10)")
                pool_data = {
                    "eqh_levels": len(pool_scan.eqh_levels),
                    "eql_levels": len(pool_scan.eql_levels),
                    "active_sweeps": len(pool_scan.active_sweeps)
                }
        except Exception as e:
            print(f"🌊 [v2.9] Pool scan error {symbol}: {e}")

        if final_score < Config.MIN_SCORE:
            print(f"🔴 [FILTER2-SMC-LONG] {symbol}: score={final_score} < MIN={Config.MIN_SCORE} — отфильтрован!")
            return None

        # ✅ FIX: Проверка SL для LONG — должен быть НИЖЕ цены
        if (price - stop_loss) / price < 0.005:       # минимум 0.5% SL
            stop_loss = price * (1 - Config.SL_BUFFER / 100)

        # ✅ FIX: TP ВЫШЕ входа для LONG
        take_profits = [
            (round(price * (1 + tp / 100), 8), tp_weights[i] if i < len(tp_weights) else 15)
            for i, tp in enumerate(tp_levels)
        ]

        sl_pct = round((price - stop_loss) / price * 100, 2)  # ✅ FIX: правильный расчёт %
        print(f"🟢 [SIGNAL-LONG] {symbol}: score={final_score} — сигнал создан!")
        return {
            "symbol": symbol, "direction": "long",
            "score": final_score, "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            "price": price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 8), "sl_pct": sl_pct,
            "take_profits": take_profits,
            "patterns": [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "elliott_wave": elliott_data if 'elliott_data' in locals() else None,
            "indicators": {
                "RSI": f"{md.rsi_1h:.1f}" if md.rsi_1h else "N/A",
                "Funding": f"{md.funding_rate:+.3f}%",
                "L/S Ratio": f"{md.long_short_ratio:.0f}% longs",
                "OI Change": f"{md.oi_change_4d:+.1f}% (4d)",
                "Price 4d": f"{p4d:+.1f}%",
            },
            "oi_proxy": {
                "bull_confirm": oi_bull_confirm,
                "accumulation": oi_accumulation,
                "weakness":     oi_weakness_long,
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


async def _count_real_positions() -> int:
    """
    ✅ v2.4: Считаем ТОЛЬКО LONG позиции этого бота.
    Оба бота на одном BingX аккаунте — фильтр по side=LONG обязателен.
    """
    if state.auto_trader:
        try:
            pos      = await state.auto_trader.bingx.get_positions()
            long_pos = [p for p in pos if getattr(p, "side", "").upper() == "LONG"]
            if long_pos:
                print(f"[LONG] Open positions: {len(long_pos)} "
                      f"({', '.join(getattr(p,'symbol','?') for p in long_pos[:5])})")
            return len(long_pos)
        except Exception as e:
            print(f"[LONG] _count_real_positions error: {e}")
    cutoff = datetime.utcnow() - timedelta(hours=Config.SIGNAL_TTL_HOURS)
    try:
        all_active = state.redis.get_active_signals(Config.BOT_TYPE)
        return sum(1 for s in all_active
                   if datetime.fromisoformat(s.get("timestamp", "2000-01-01")) > cutoff)
    except Exception:
        return 0


async def scan_market():
    """
    ✅ v2.7 АРХИТЕКТУРА (NO BTC CORR):
    - Telegram сигналы: ВСЕГДА при score >= MIN_SCORE (даже при 20/20)
    - Биржевое исполнение: только если active_count < MAX и не /pause
    - Единственный блокер: команда /pause
    """
    if state.is_paused:
        return

    print(f"\n🔍 LONG scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    # Считаем активные LONG позиции на бирже
    active_count  = await _count_real_positions()
    exchange_full = active_count >= Config.MAX_POSITIONS
    if exchange_full:
        print(f"📊 Exchange LONG slots: {active_count}/{Config.MAX_POSITIONS} — "
              f"сигналы в TG продолжаются, биржа ждёт освобождения")

    new_signals   = 0
    tg_only_count = 0  # сигналы отправленные только в TG (биржа полна)

    for symbol in state.watchlist:
        try:
            # Дедупликация: не повторяем недавний сигнал по этому символу
            if _is_fresh(state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)):
                continue
            # ✅ FIX v5: SL Cooldown — пауза 2ч после стопа по символу
            try:
                sl_cd = state.redis.get(f"sl_cooldown:long:{symbol}")
                if sl_cd:
                    continue
            except Exception:
                pass

            signal = await scan_symbol(symbol)
            if not signal:
                continue

            # ✅ ВСЕГДА: Telegram сигнал (независимо от состояния биржи)
            tg_msg_id = await state.telegram.send_signal(
                direction="long", symbol=signal["symbol"],
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

            # ✅ Биржевое исполнение: только если есть слоты И не на паузе
            if not exchange_full and Config.AUTO_TRADING and not state.is_paused:
                if state.auto_trader:
                    try:
                        await state.auto_trader.execute_signal(signal)
                        active_count += 1
                        exchange_full = active_count >= Config.MAX_POSITIONS
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")
                new_signals += 1
                print(f"✅ LONG executed: {symbol} Score={signal['score']:.0f}% SL={signal['sl_pct']}%")
            else:
                tg_only_count += 1
                reason = "max positions" if exchange_full else "paused"
                print(f"📡 LONG TG-only: {symbol} Score={signal['score']:.0f}% [{reason}]")

            await asyncio.sleep(0.4)
        except Exception as e:
            print(f"Error {symbol}: {e}")

    state.daily_signals += new_signals + tg_only_count
    state.last_scan      = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))
    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status":        "paused" if state.is_paused else "running",
        "last_scan":     state.last_scan.isoformat(),
        "daily_signals": state.daily_signals,
        "active_signals": state.active_signals,
    })
    print(f"✅ Scan done. Executed: {new_signals} | TG-only: {tg_only_count} | "
          f"Exchange: {active_count}/{Config.MAX_POSITIONS}")


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
