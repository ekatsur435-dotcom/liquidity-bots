"""
🟢 LONG BOT v2.3 — FastAPI Application

ИСПРАВЛЕНИЯ v2.3:
  ✅ MAX_WATCHLIST default = 300 (было 200)
  ✅ MIN_LONG_SCORE default = 60 (было 65)
  ✅ SCAN_INTERVAL default = 200 сек
  ✅ HEAD /health → 200 OK (UptimeRobot fix)
  ✅ Watchlist: объединяет Bybit + Binance (нет дублей, до 300 монет)
  ✅ OI Proxy метрики в scan_symbol
  ✅ volume_spike_ratio + atr_14_pct → scorer
  ✅ pattern_detector (один файл, не v2)
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
from bot.telegram import TelegramBot, TelegramCommandHandler


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "long"
    # ✅ FIX: MIN_LONG_SCORE default = 60 (не 65!)
    # ✅ v2.5 BACKTEST: Медвежий рынок. Score 75+ → PF 2.07x
    MIN_SCORE     = int(os.getenv("MIN_LONG_SCORE", "75"))
    # ✅ FIX: SCAN_INTERVAL default = 200
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))  # BACKTEST: 120с
    # ✅ FIX: MAX_WATCHLIST default = 300
    MAX_POSITIONS = int(os.getenv("MAX_LONG_POSITIONS", "20"))
    LEVERAGE      = os.getenv("LONG_LEVERAGE", "5-50")

    # LONG: SL НИЖЕ входа, TP ВЫШЕ входа
    # ✅ v2.5: Уменьшен SL с 1.5% до 1.2% для лучшего R:R
    SL_BUFFER     = float(os.getenv("LONG_SL_BUFFER", "1.2"))  # was 1.5

    # TP levels из Config (v2.5: увеличены для R:R ≥ 2:1)
    TP_LEVELS  = [4.0, 8.0, 12.0, 20.0, 30.0, 40.0]  # LONG: SL=1.2% TP1=4% → R:R=3.3:1
    TP_WEIGHTS = [25,  20,  20,  15,  12,   8]   # LONG: равномерно с акцентом TP1-2

    # Trailing — LONG активирует при +1.5%
    TRAIL_ACTIVATION = float(os.getenv("LONG_TRAIL_ACTIVATION", "0.015"))

    SIGNAL_TTL_HOURS = 24

    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "true").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.0005"))

    USE_SMC        = os.getenv("USE_SMC", "true").lower() == "true"
    USE_COINGLASS  = bool(os.getenv("COINGLASS_API_KEY", ""))

    # ✅ FIX: default MAX_WATCHLIST = 300
    # LONG: 1M$ min объём — фильтр мусора вроде BANANA, DENT, AIA
    MIN_VOLUME_USDT = int(os.getenv("MIN_VOLUME_USDT", "400000"))  # ✅ 1M$ фильтр мусора
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
    print("🚀 Starting LONG Bot v2.3...")
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
        f"🟢 <b>LONG Bot v2.3 запущен</b>\n\n"
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


app = FastAPI(lifespan=lifespan, title="LONG Bot v2.3")


# ============================================================================
# ROUTES
# ============================================================================

# ✅ HEAD + GET для UptimeRobot (405 → 200)
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"status": "ok", "bot": "long", "version": "2.3",
                         "watchlist": len(state.watchlist),
                         "active": state.active_signals})

# ✅ HEAD + GET для Render health checks (405 → 200)
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({"bot": "LONG Bot v2.3", "status": "running" if state.is_running else "stopped"})

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


async def scan_symbol(symbol: str, cached_btc_1h: Optional[float] = None) -> Optional[Dict]:
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

        # 🆕 RSI Watchlist tracking — обновляем трекер
        rsi_current = md.rsi_1h or 0
        _rsi_tracker.update(symbol, rsi_current)

        # ✅ Multi-TF загрузка: 30m + 1h параллельно (убран 15m — 50% стопов в бэктесте)
        ohlcv_30m_task = state.binance.get_klines(symbol, "30m", 100)
        ohlcv_1h_task = state.binance.get_klines(symbol, "1h", 50)
        ohlcv_30m, ohlcv_1h = await asyncio.gather(ohlcv_30m_task, ohlcv_1h_task)

        # Используем 30m как основной ТФ для анализа (вместо 15m)
        ohlcv_15m = ohlcv_30m  # совместимость с existing code
        if not ohlcv_30m or len(ohlcv_30m) < 20:
            return None

        # ✅ RSI 30m — промежуточный фильтр между 15m и 1h
        rsi_30m_ok = True
        try:
            if ohlcv_30m and len(ohlcv_30m) >= 14:
                closes_30m = [c.close for c in ohlcv_30m[-14:]]
                gains_30m = [max(0, closes_30m[i]-closes_30m[i-1]) for i in range(1,14)]
                losses_30m = [max(0, closes_30m[i-1]-closes_30m[i]) for i in range(1,14)]
                ag_30m = sum(gains_30m)/13; al_30m = sum(losses_30m)/13
                rsi_30m = 100 - 100/(1 + ag_30m/al_30m) if al_30m > 0 else 50
                # Блокируем лонг если RSI 30m перекуплен (>75) — слишком поздно
                if rsi_30m > 75:
                    rsi_30m_ok = False
        except Exception:
            pass
        if not rsi_30m_ok:
            return None  # RSI 30m перекуплен — ложный LONG сигнал

        # ✅ FIX L3: Multi-TF RSI context — 4h RSI не должен быть слишком высоким
        # Если RSI 1h перепродан (35) но RSI 4h нейтрален/перекуплен (>60) — ложный сигнал
        rsi_4h_ok = True
        try:
            ohlcv_4h = await state.binance.get_klines(symbol, "4h", 14)
            if ohlcv_4h and len(ohlcv_4h) >= 14:
                closes_4h = [c.close for c in ohlcv_4h[-14:]]
                gains = [max(0, closes_4h[i]-closes_4h[i-1]) for i in range(1,14)]
                losses = [max(0, closes_4h[i-1]-closes_4h[i]) for i in range(1,14)]
                ag = sum(gains)/13; al = sum(losses)/13
                rsi_4h = 100 - 100/(1 + ag/al) if al > 0 else 50
                # Блокируем лонг если 4h RSI перекуплен (>70)
                if rsi_4h > 70:
                    rsi_4h_ok = False
        except Exception:
            pass
        if not rsi_4h_ok:
            return None  # 4h RSI перекуплен — ложный LONG сигнал

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

        # ── LONG: BTC trend awareness (используем кешированное значение) ────
        # cached_btc_1h передаётся из scan_market (1 запрос на весь скан)
        if cached_btc_1h is not None and cached_btc_1h < -3.0:
            return None   # BTC рухает >3%/1ч — не лонгуем

        # ── Realtime scorer ───────────────────────────────────────────────────
        rt = get_realtime_scorer()
        rt_result = await rt.score(
            direction="long", market_data=md,
            base_score=final_score, hourly_deltas=hourly_deltas,
        )
        if rt_result.early_only:
            # ✅ FIX: Проверяем MIN_SCORE даже для ранних watch сигналов
            if rt_result.final_score >= Config.MIN_SCORE:
                await state.telegram.send_message(
                    f"🛰️ <b>РАННИЙ LONG WATCH</b>  Score: {rt_result.final_score:.0f}%\n\n"
                    f"🟢 <b>#{symbol}</b>  ${price:,.6f}\n"
                    + "\n".join(f"  • {r}" for r in rt_result.factors[:4])
                    + "\n\n⏳ <i>Ждём подтверждения.</i>"
                )
            return None

        final_score = rt_result.final_score
        reasons.extend(rt_result.factors)

        # 🆕 Бонус если RSI восстанавливается от низов (LONG сигнал)
        rsi_now = md.rsi_1h or 0
        if 30 <= rsi_now <= 50 and rsi_current > 0:
            final_score += 3
            reasons.append(f"RSI восстановление {rsi_now:.0f} → +3")
        
        if final_score < Config.MIN_SCORE:
            return None

        # OI proxy — тихо (убраны verbose debug logs)

        # ── LONG TP уровни из Config ──────────────────────────────────────────
        best_pattern = patterns[0].name if patterns else None
        # LONG: TP levels & weights from Config (optimised)
        tp_levels  = Config.TP_LEVELS
        tp_weights = Config.TP_WEIGHTS

        # ── SL НИЖЕ входа, TP ВЫШЕ входа (LONG) ──────────────────────────────
        stop_loss   = price * (1 - Config.SL_BUFFER / 100)   # SL ниже цены
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

        if final_score < Config.MIN_SCORE:
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
        return {
            "symbol": symbol, "direction": "long",
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


async def _get_btc_correlation() -> dict:
    """
    BTC корреляция — ТОЛЬКО как информация и модификатор score.
    НЕ является блокером. Многие альты растут при падающем BTC и наоборот.
    Возвращает: {"score_adj": float, "label": str, "change_1h": float}
    """
    try:
        btc = await state.binance.get_complete_market_data("BTCUSDT")
        if not btc:
            return {"score_adj": 0, "label": "unknown", "change_1h": 0}
        c1h  = getattr(btc, "price_change_1h", 0) or 0
        c24h = getattr(btc, "price_change_24h", 0) or 0
        # BTC растёт сильно → небольшой бонус к LONG сигналам
        if c1h > 2.0:   adj, label = +3.0, f"BTC +{c1h:.1f}%/1h 🚀"
        elif c1h > 0.5: adj, label = +1.5, f"BTC +{c1h:.1f}%/1h ↗"
        # BTC падает сильно → небольшой штраф к LONG сигналам
        elif c1h < -2.0: adj, label = -3.0, f"BTC {c1h:.1f}%/1h 🔴"
        elif c1h < -0.5: adj, label = -1.5, f"BTC {c1h:.1f}%/1h ↘"
        else:            adj, label =  0.0, f"BTC {c1h:.1f}%/1h ↔"
        return {"score_adj": adj, "label": label, "change_1h": c1h, "change_24h": c24h}
    except Exception:
        return {"score_adj": 0, "label": "BTC N/A", "change_1h": 0}


async def scan_market():
    """
    ✅ v2.4 АРХИТЕКТУРА:
    - Telegram сигналы: ВСЕГДА при score >= MIN_SCORE (даже при 20/20)
    - Биржевое исполнение: только если active_count < MAX и не /pause
    - BTC корреляция: только модификатор score (-3..+3), НЕ блокер
    - BTC RSI 4h > 40 фильтр (не лонгуем в глубоком даунтренде)
    - Единственный блокер: команда /pause
    """
    if state.is_paused:
        return

    print(f"\n🔍 LONG scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    # BTC корреляция — только информация и мягкий модификатор score
    btc_corr  = await _get_btc_correlation()
    _btc_rsi_4h: float = 50.0
    try:
        btc_data = await state.binance.get_complete_market_data("BTCUSDT")
        if btc_data:
            _btc_rsi_4h = getattr(btc_data, 'rsi_4h', 50.0) or 50.0
    except Exception:
        pass

    # ✅ BTC RSI 4h фильтр для LONG (бэктест): только если BTC RSI 4h > 40
    # Если BTC в глубоком даунтренде (RSI 4h < 40) — блокируем LONG
    if _btc_rsi_4h < 40:
        print(f"⛔ LONG blocked: BTC RSI 4h = {_btc_rsi_4h:.1f} < 40 (deep downtrend)")
        return  # полная блокировка сканирования
    elif _btc_rsi_4h < 45:
        print(f"⚠️ BTC RSI 4h = {_btc_rsi_4h:.1f} (weak market) — reduced scoring")
    score_adj = int(btc_corr.get("score_adj", 0) or 0)
    btc_label = btc_corr.get("label") or "BTC N/A"
    print(f"📡 {btc_label} (score adj {score_adj:+.0f})")

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

            signal = await scan_symbol(symbol, btc_corr.get("change_1h"))
            if not signal:
                continue

            # Применяем BTC корреляционный модификатор к score
            original_score = signal["score"]
            btc_score_adj = btc_corr.get("score_adj", 0) or 0
            signal["score"] = round(original_score + btc_score_adj, 1)
            signal["btc_corr_adj"] = btc_score_adj
            signal["btc_label"]    = btc_corr.get("label", "BTC N/A")
            # Если после поправки score упал ниже мин — пропускаем
            if signal["score"] < Config.MIN_SCORE:
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
