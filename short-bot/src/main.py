"""
🤖 SHORT BOT v4.0 — FastAPI Application

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
import asyncio
import logging
import time  # 🆕 NEW: For BTC block caching
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

import sys

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler()  # Вывод в консоль (Render берёт отсюда)
    ]
)

# Уменьшаем шум от сторонних библиотек
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger("short_bot")
logger.info(f"📝 Logging initialized (level={LOG_LEVEL})")

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
from utils.data_aggregator import DataAggregator, get_data_aggregator
from api.okx_client import get_okx_client  # 🆕 NEW: OKX fallback client
from core.market_data_integrator import get_market_data_integrator  # 🆕 NEW: Market Data Integrator
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
from core.market_context import get_market_context, MarketContextFilter  # ✅ FIX v6  # ✅ v4.0
# 🆕 Aegis components
from core.pump_detector import detect_pump, PumpDetectionResult  # ✅ NEW: Z-Score detector
from core.kelly_risk_manager import get_kelly_risk_manager, SignalQuality  # ✅ NEW: Kelly sizing
from core.delta_analyzer import analyze_delta  # ✅ NEW: CVD/Order Flow analyzer

# 🆕 NEW: Advanced trading modules
from core.smart_dca import get_smart_dca, SmartDCA, DCAStrength  # ✅ NEW: Smart DCA
from core.grid_dca import get_grid_dca, GridDCA  # ✅ NEW: Grid DCA
from core.amd_detector import get_amd_detector, AMDDetector, AMDPhase  # ✅ NEW: AMD Detector
from core.xvp import get_xvp_analyzer, XVPAnalyzer  # ✅ NEW: Extended Volume Profile
from core.grid_entry import get_grid_entry, GridEntry  # ✅ NEW: Grid Entry System
from core.smart_sl_detector import get_smart_sl_detector, SmartSLDetector  # ✅ NEW: Smart Multi-TF SL
from core.dump_detector import get_dump_detector, DumpDetector, DumpType  # ✅ NEW: Dump Detector
from core.momentum_detector import get_momentum_detector, MomentumDetector  # ✅ NEW: Momentum Detector
from core.candle_history_manager import (
    get_candle_manager, CandleHistoryManager, TFConfig,
    fetch_and_store_candles, prefetch_all_tfs, check_data_ready
)  # ✅ NEW: Candle History for accurate analysis


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    BOT_TYPE      = "short"
    
    # ============================================================================
    # 🔧 ENVIRONMENT VARIABLES (настраиваются на Render Dashboard)
    # ============================================================================
    # MIN_SCORE_SHORT      - Минимальный score для входа (default: 70) ⭐ КЛЮЧЕВОЙ
    # MAX_SHORT_POSITIONS  - Макс. кол-во позиций (default: 10) ⭐ КЛЮЧЕВОЙ
    # SHORT_SL_BUFFER      - SL буфер в процентах (default: 2.0) ⭐ КЛЮЧЕВОЙ
    # SCAN_INTERVAL        - Интервал сканирования в сек (default: 120)
    # SHORT_LEVERAGE       - Плечо (default: "5-50")
    # SHORT_TRAIL_ACTIVATION - Активация trailing SL (default: 0.030)
    # BTC_BLOCK_SHORT_THRESHOLD - Блокировка при пампе BTC (default: 4.0)
    # SL_COOLDOWN_HOURS    - Кулдаун после SL в часах (default: 2.0)
    # MAX_DAILY_RISK       - Дневной лимит потерь % (default: 5.0) ⭐ КЛЮЧЕВОЙ
    # ============================================================================
    
    # ✅ FIX: Переименовано MIN_SHORT_SCORE → MIN_SCORE_SHORT для соответствия Render
    # ✅ v8: Увеличен default с 63 до 70 (строгая фильтрация)
    MIN_SCORE     = int(os.getenv("MIN_SCORE_SHORT", "70"))  # ⭐ ИЗМЕНИТЬ на Render!
    
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "180"))  # 3 минуты (было 2 мин)
    
    # ✅ FIX: Уменьшено default с 20 до 10 (меньше позиций = меньше риск)
    MAX_POSITIONS = int(os.getenv("MAX_SHORT_POSITIONS", "10"))  # ⭐ ИЗМЕНИТЬ на Render!
    
    LEVERAGE      = os.getenv("SHORT_LEVERAGE", "5-50")

    # SHORT: SL ВЫШЕ входа, TP НИЖЕ входа
    # ✅ FIX: Увеличен default с 1.5% до 2.0% (меньше ложных стопов)
    SL_BUFFER     = float(os.getenv("SHORT_SL_BUFFER", "2.0"))  # ⭐ ИЗМЕНИТЬ на Render!
    SL_COOLDOWN_HOURS  = float(os.getenv("SL_COOLDOWN_HOURS", "2.0"))
    MAX_DAILY_RISK = float(os.getenv("MAX_DAILY_RISK", "5.0"))  # ⭐ Дневной лимит потерь
    
    # Trailing — SHORT активирует при +1% (раньше чем раньше)
    TRAIL_ACTIVATION = float(os.getenv("SHORT_TRAIL_ACTIVATION", "0.030"))
    # ✅ FIX v7: BTC correlation — управляется через ENV
    BTC_BLOCK_THRESHOLD = float(os.getenv("BTC_BLOCK_SHORT_THRESHOLD", "4.0"))
    # При BTC_BLOCK_SHORT_THRESHOLD=99 → SHORT не блокируется даже при сильном памп BTC
    SL_COOLDOWN_HOURS  = float(os.getenv("SL_COOLDOWN_HOURS", "2.0"))

    # 🆕 NEW: Advanced modules configuration
    # Smart DCA
    ENABLE_SMART_DCA = os.getenv("ENABLE_SMART_DCA", "true").lower() == "true"
    DCA_MAX_ADDITIONS = int(os.getenv("DCA_MAX_ADDITIONS", "3"))
    DCA_MAX_PORTFOLIO_RISK = float(os.getenv("DCA_MAX_PORTFOLIO_RISK", "3.0"))
    
    # Grid DCA
    ENABLE_GRID_DCA = os.getenv("ENABLE_GRID_DCA", "true").lower() == "true"
    GRID_DCA_MAX_LEVELS = int(os.getenv("GRID_DCA_MAX_LEVELS", "5"))
    
    # AMD Detector
    ENABLE_AMD = os.getenv("ENABLE_AMD_DETECTOR", "true").lower() == "true"
    
    # XVP
    ENABLE_XVP = os.getenv("ENABLE_XVP", "true").lower() == "true"
    
    # Grid Entry
    ENABLE_GRID_ENTRY = os.getenv("ENABLE_GRID_ENTRY", "true").lower() == "true"
    
    # Smart SL
    ENABLE_SMART_SL = os.getenv("ENABLE_SMART_SL", "true").lower() == "true"
    SMART_SL_MIN_PCT = float(os.getenv("SMART_SL_MIN_PCT", "0.8"))
    SMART_SL_MAX_PCT = float(os.getenv("SMART_SL_MAX_PCT", "4.0"))
    
    # Dump Detector
    ENABLE_DUMP_DETECTOR = os.getenv("ENABLE_DUMP_DETECTOR", "true").lower() == "true"
    SHORT_TRAIL_ACTIVATION = TRAIL_ACTIVATION  # Alias для position_tracker.py
    
    # ============================================================================
    # 🏛️ INSTITUTIONAL RISK MANAGEMENT (Aegis Integration)
    # ============================================================================
    # ⚠️ ВАЖНО: Увеличиваем риск для видимости сделок
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.05"))  # 5% для видимости
    
    # Kelly Criterion Sizing
    KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))  # 25% Kelly
    
    # Position & Exposure Limits
    MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.25"))  # 25% на позицию
    MAX_EXPOSURE_PCT = float(os.getenv("MAX_EXPOSURE_PCT", "0.80"))  # 80% макс экспозиция
    
    # Drawdown Control (ВАШЕ требование: -15%)
    MAX_DAILY_RISK = float(os.getenv("MAX_DAILY_RISK", "15.0"))  # -15% (было 5.0)
    MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
    
    # ============================================================================
    # 🎯 6 TP LEVELS CONFIG (Phase 4 - Short оптимизация)
    # ============================================================================
    TP_LEVELS = [2.5, 4.0, 6.5, 9.0, 12.0, 17.0]  # Short: короче цели
    TP_WEIGHTS = [30, 25, 20, 13, 8, 4]  # Больше ранних TP
    
    # ============================================================================
    # 🚩 FEATURE FLAGS (Phase 3 - Institutional Analysis)
    # ============================================================================
    ENABLE_WYCKOFF_DETECTOR = os.getenv("ENABLE_WYCKOFF", "true").lower() == "true"
    ENABLE_BSL_SCANNER = os.getenv("ENABLE_BSL", "true").lower() == "true"
    ENABLE_OI_ANALYZER = os.getenv("ENABLE_OI", "true").lower() == "true"
    ENABLE_LIQ_MAPPER = os.getenv("ENABLE_LIQ", "true").lower() == "true"
    ENABLE_DELTA = os.getenv("ENABLE_DELTA", "true").lower() == "true"
    
    # BTC Correlation (КРИТИЧНО ДЛЯ SHORT!)
    ENABLE_BTC_CORRELATION = os.getenv("ENABLE_BTC", "true").lower() == "true"
    # Decoupling НЕ используется для short
    ENABLE_DECOUPLING_BONUS = False
    
    # ============================================================================
    # 🏭 SECTOR DIVERSIFICATION (Phase 5 - Sector Limits)
    # ============================================================================
    ENABLE_SECTOR_LIMITS = os.getenv("ENABLE_SECTOR_LIMITS", "true").lower() == "true"
    MAX_PER_SECTOR_MEME = int(os.getenv("MAX_PER_SECTOR_MEME", "3"))  # Мемы лучше для шортов
    MAX_PER_SECTOR_DEFI = int(os.getenv("MAX_PER_SECTOR_DEFI", "3"))
    MAX_PER_SECTOR_GAMEFI = int(os.getenv("MAX_PER_SECTOR_GAMEFI", "3"))
    MAX_PER_SECTOR_L1 = int(os.getenv("MAX_PER_SECTOR_L1", "3"))
    MAX_PER_SECTOR_L2 = int(os.getenv("MAX_PER_SECTOR_L2", "3"))
    MAX_PER_SECTOR_AI = int(os.getenv("MAX_PER_SECTOR_AI", "3"))
    MAX_PER_SECTOR_RWA = int(os.getenv("MAX_PER_SECTOR_RWA", "2"))
    MAX_PER_SECTOR_ORACLE = int(os.getenv("MAX_PER_SECTOR_ORACLE", "2"))
    MAX_PER_SECTOR_INFRA = int(os.getenv("MAX_PER_SECTOR_INFRA", "2"))
    
    # 🆕 NEW: Momentum Detector (Dump Catching for Short)
    ENABLE_MOMENTUM_SHORT = os.getenv("ENABLE_MOMENTUM_SHORT", "true").lower() == "true"
    MOMENTUM_SHORT_MIN_1MIN_CHANGE = float(os.getenv("MOMENTUM_SHORT_MIN_1MIN_CHANGE", "0.8"))  # -0.8% за мин
    MOMENTUM_SHORT_MIN_5MIN_CHANGE = float(os.getenv("MOMENTUM_SHORT_MIN_5MIN_CHANGE", "2.0"))   # -2% за 5 мин
    MOMENTUM_SHORT_VOLUME_SPIKE = float(os.getenv("MOMENTUM_SHORT_VOLUME_SPIKE", "2.5"))
    MOMENTUM_SHORT_RSI_MIN = float(os.getenv("MOMENTUM_SHORT_RSI_MIN", "60"))   # RSI > 60 (перекуплено)
    MOMENTUM_SHORT_RSI_MAX = float(os.getenv("MOMENTUM_SHORT_RSI_MAX", "85"))  # RSI < 85 (не перепродано)
    MOMENTUM_SHORT_SCORE_MIN = float(os.getenv("MOMENTUM_SHORT_SCORE_MIN", "50"))
    MOMENTUM_SHORT_MAX_POSITIONS = int(os.getenv("MOMENTUM_SHORT_MAX_POSITIONS", "5"))
    MOMENTUM_SHORT_RISK_PER_TRADE = float(os.getenv("MOMENTUM_SHORT_RISK_PER_TRADE", "0.0003"))
    MOMENTUM_SHORT_SL_BUFFER = float(os.getenv("MOMENTUM_SHORT_SL_BUFFER", "1.0"))
    MOMENTUM_SHORT_TRAIL_START = float(os.getenv("MOMENTUM_SHORT_TRAIL_START", "0.5"))
    
    # 🆕 NEW: Candle History Manager
    ENABLE_CANDLE_HISTORY = os.getenv("ENABLE_CANDLE_HISTORY", "true").lower() == "true"
    CANDLE_4H_COUNT = int(os.getenv("CANDLE_4H_COUNT", "30"))
    CANDLE_2H_COUNT = int(os.getenv("CANDLE_2H_COUNT", "50"))
    CANDLE_1H_COUNT = int(os.getenv("CANDLE_1H_COUNT", "80"))
    CANDLE_30M_COUNT = int(os.getenv("CANDLE_30M_COUNT", "120"))
    CANDLE_15M_COUNT = int(os.getenv("CANDLE_15M_COUNT", "150"))
    CANDLE_5M_COUNT = int(os.getenv("CANDLE_5M_COUNT", "300"))
    CANDLE_MIN_REQUIRED = int(os.getenv("CANDLE_MIN_REQUIRED", "20"))

    SIGNAL_TTL_HOURS = 24

    AUTO_TRADING   = os.getenv("AUTO_TRADING_ENABLED", "true").lower() == "true"
    BINGX_DEMO     = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.0005"))

    USE_SMC        = os.getenv("USE_SMC", "true").lower() == "true"
    USE_COINGLASS  = bool(os.getenv("COINGLASS_API_KEY", ""))
    USE_COINMARKETCAP = bool(os.getenv("COINMARKETCAP_API_KEY", ""))
    USE_COINGECKO  = os.getenv("USE_COINGECKO", "true").lower() == "true"  # Работает и без API ключа

    # ✅ REDUCED: 300 → 80 монет (меньше запросов к API)
    # ✅ INCREASED: 300K → 1M (только ликвидные монеты, меньше ошибок)
    MIN_VOLUME_USDT = int(os.getenv("MIN_VOLUME_USDT", "10000000"))  # 🔧 $10M минимум (was $1M)
    MAX_WATCHLIST   = int(os.getenv("MAX_WATCHLIST", "80"))  # 80 монет max
    # 🆕 Aegis: Минимальная капитализация $900k (фильтр неликвидных мелких монет)
    MIN_MARKET_CAP  = int(os.getenv("MIN_MARKET_CAP", "900000"))  # $900k минимум
    # 🆕 STRICT: Минимальный объем для входа в сделку (отдельно от watchlist)
    MIN_ENTRY_VOLUME_USDT = int(os.getenv("MIN_ENTRY_VOLUME_USDT", "10000000"))  # $10M


# ============================================================================
# 🆕 SMART DCA v2 ENGINE (Aegis Integration - Phase 2)
# ============================================================================

from dataclasses import dataclass

@dataclass
class DCALevel:
    level: int
    price_drop_pct: float
    size_multiplier: float
    max_position_pct: float


class SmartDCAEngine:
    """Smart DCA v2 из Aegis-bots для Short."""
    
    def __init__(self, config: Config):
        self.config = config
        self.dca_levels = self._init_dca_levels()
        self.total_exposure_pct = 0.0
        self.circuit_breaker_triggered = False
        
    def _init_dca_levels(self) -> List:
        levels = []
        for i in range(1, 5):  # 4 уровня
            level = DCALevel(
                level=i,
                price_drop_pct=i * self.config.DCA_ATR_MULT,
                size_multiplier=self.config.DCA_SIZE_MULT ** (i - 1),
                max_position_pct=min(0.10 * i, 0.40)
            )
            levels.append(level)
        return levels
    
    def calculate_dca_orders(self, entry_price: float, position_size: float,
                            atr: float, portfolio_value: float) -> List[Dict]:
        if not self.config.ENABLE_SMART_DCA:
            return []
        if self.total_exposure_pct >= self.config.DCA_MAX_EXPOSURE_PCT:
            print(f"⚠️ [DCA] Circuit breaker: exposure {self.total_exposure_pct:.1%}")
            self.circuit_breaker_triggered = True
            return []
        orders = []
        current_size = position_size
        for level in self.dca_levels:
            kelly_fraction = self.config.KELLY_FRACTION
            level_size = current_size * level.size_multiplier * kelly_fraction
            level_exposure = (level_size * entry_price) / portfolio_value
            if self.total_exposure_pct + level_exposure > self.config.DCA_MAX_EXPOSURE_PCT:
                print(f"⚠️ [DCA] Level {level.level} skipped: exposure limit")
                break
            # Для short: цена растет (против нас)
            dca_price = entry_price * (1 + level.price_drop_pct / 100)
            order = {
                "level": level.level,
                "price": dca_price,
                "size": level_size,
                "exposure_pct": level_exposure,
                "atr_mult": level.price_drop_pct / atr if atr > 0 else 0
            }
            orders.append(order)
            self.total_exposure_pct += level_exposure
            current_size = level_size
        return orders
    
    def reset_exposure(self):
        self.total_exposure_pct = 0.0
        self.circuit_breaker_triggered = False


# ============================================================================
# 🆕 SECTOR POSITION MANAGER (Phase 5)
# ============================================================================

class SectorPositionManager:
    def __init__(self, config: Config, sector_mapper):
        self.config = config
        self.sector_mapper = sector_mapper
        self.positions_by_sector: Dict[str, List[str]] = {}
        
    def can_open_position(self, symbol: str) -> Tuple[bool, str]:
        if not self.config.ENABLE_SECTOR_LIMITS:
            return True, "Sector limits disabled"
        sector = self.sector_mapper.get_sector(symbol)
        if not sector:
            return True, "No sector assigned"
        current_count = len(self.positions_by_sector.get(sector, []))
        max_allowed = self._get_max_for_sector(sector)
        if current_count >= max_allowed:
            return False, f"Sector {sector} limit: {current_count}/{max_allowed}"
        return True, f"Sector {sector}: {current_count}/{max_allowed}"
    
    def _get_max_for_sector(self, sector: str) -> int:
        mapping = {
            "Meme": self.config.MAX_PER_SECTOR_MEME,
            "DeFi": self.config.MAX_PER_SECTOR_DEFI,
            "GameFi": self.config.MAX_PER_SECTOR_GAMEFI,
            "L1": self.config.MAX_PER_SECTOR_L1,
            "L2": self.config.MAX_PER_SECTOR_L2,
            "AI": self.config.MAX_PER_SECTOR_AI,
            "RWA": self.config.MAX_PER_SECTOR_RWA,
            "Oracle": self.config.MAX_PER_SECTOR_ORACLE,
            "Infra": self.config.MAX_PER_SECTOR_INFRA
        }
        return mapping.get(sector, 3)
    
    def add_position(self, symbol: str):
        sector = self.sector_mapper.get_sector(symbol)
        if sector:
            if sector not in self.positions_by_sector:
                self.positions_by_sector[sector] = []
            self.positions_by_sector[sector].append(symbol)
            
    def remove_position(self, symbol: str):
        sector = self.sector_mapper.get_sector(symbol)
        if sector and sector in self.positions_by_sector:
            if symbol in self.positions_by_sector[sector]:
                self.positions_by_sector[sector].remove(symbol)
                
    def get_sector_stats(self) -> Dict:
        stats = {}
        for sector, positions in self.positions_by_sector.items():
            max_allowed = self._get_max_for_sector(sector)
            stats[sector] = {
                "current": len(positions),
                "max": max_allowed,
                "symbols": positions
            }
        return stats


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
        
        # 🆕 NEW: Smart DCA v2 Engine
        self.dca_engine: Optional[SmartDCAEngine] = None
        
        # 🆕 NEW: Sector Position Manager
        self.sector_manager: Optional[SectorPositionManager] = None
        self.sector_mapper = None
        
        # 🆕 NEW: Institutional Detectors (Phase 3)
        self.wyckoff_detector = None
        self.bsl_scanner = None
        self.oi_analyzer = None
        self.liq_mapper = None
        self.delta_analyzer = None
        
        # Daily stats for risk tracking
        self.daily_stats = {
            "signals_generated": 0,
            "positions_opened": 0,
            "positions_closed": 0,
            "win_count": 0,
            "loss_count": 0,
            "consecutive_losses": 0,
            "daily_pnl": 0.0,
            "max_daily_dd": 0.0
        }


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
    print("🚀 Starting SHORT Bot v6.0...")
    state.start_time = datetime.utcnow()

    state.redis            = get_redis_client()
    state.binance          = get_binance_client()
    state.okx              = get_okx_client()  # 🆕 NEW: OKX fallback for OI, funding, liquidations
    state.market_integrator = get_market_data_integrator(state.okx, state.binance)  # 🆕 NEW: Full market context
    state.scorer           = get_short_scorer(Config.MIN_SCORE)
    state.pattern_detector = ShortPatternDetector()
    
    # 🆕 NEW: Initialize advanced trading modules
    print("🔄 Initializing advanced modules...")
    
    # Smart DCA
    if Config.ENABLE_SMART_DCA:
        state.smart_dca = get_smart_dca()
        print(f"✅ Smart DCA: max {Config.DCA_MAX_ADDITIONS} additions, {Config.DCA_MAX_PORTFOLIO_RISK}% risk")
    
    # Grid DCA
    if Config.ENABLE_GRID_DCA:
        state.grid_dca = get_grid_dca()
        print(f"✅ Grid DCA: {Config.GRID_DCA_MAX_LEVELS} levels")
    
    # AMD Detector
    if Config.ENABLE_AMD:
        state.amd_detector = get_amd_detector()
        print("✅ AMD Detector: Accumulation/Manipulation/Distribution phases")
    
    # XVP
    if Config.ENABLE_XVP:
        state.xvp = get_xvp_analyzer()
        print("✅ XVP: Extended Volume Profile (POC, VAH/VAL, Single Prints)")
    
    # Grid Entry
    if Config.ENABLE_GRID_ENTRY:
        state.grid_entry = get_grid_entry()
        print("✅ Grid Entry: Multi-level entry system")
    
    # Smart SL Detector
    if Config.ENABLE_SMART_SL:
        from core.smart_sl_detector import SLConfig
        sl_config = SLConfig(
            min_sl_pct=Config.SMART_SL_MIN_PCT,
            max_sl_pct=Config.SMART_SL_MAX_PCT
        )
        state.smart_sl = get_smart_sl_detector(sl_config)
        print(f"✅ Smart SL: {Config.SMART_SL_MIN_PCT}%-{Config.SMART_SL_MAX_PCT}% range, multi-TF analysis")
    
    # Dump Detector
    if Config.ENABLE_DUMP_DETECTOR:
        state.dump_detector = get_dump_detector()
        print("✅ Dump Detector: Flash crash & panic sell detection")
    
    # 🆕 NEW: Momentum Detector (Dump Catching for Short)
    print(f"\n{'='*60}")
    print(f"🔥 MOMENTUM DETECTOR INITIALIZATION 🔥")
    print(f"{'='*60}")
    print(f"ENABLE_MOMENTUM_SHORT = {Config.ENABLE_MOMENTUM_SHORT}")
    print(f"{'='*60}\n")
    
    if Config.ENABLE_MOMENTUM_SHORT:
        try:
            print("[MOMENTUM] Creating detector instance...")
            state.momentum_detector = get_momentum_detector(direction="short")
            print(f"[MOMENTUM] ✅ Detector created: {state.momentum_detector is not None}")
            print(f"[MOMENTUM] ✅ Direction: {state.momentum_detector.direction if state.momentum_detector else 'N/A'}")
            print("[MOMENTUM] ✅ Dump catching mode ACTIVE (velocity + panic volume)")
        except Exception as e:
            print(f"[MOMENTUM] ❌ FAILED to create detector: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[MOMENTUM] ⚠️ DISABLED in config")
    
    # 🆕 NEW: Candle History Manager
    if Config.ENABLE_CANDLE_HISTORY:
        from core.candle_history_manager import CandleHistoryManager, TFConfig
        tf_configs = {
            "4h": TFConfig("4h", Config.CANDLE_4H_COUNT, 240, 1),
            "2h": TFConfig("2h", Config.CANDLE_2H_COUNT, 120, 2),
            "1h": TFConfig("1h", Config.CANDLE_1H_COUNT, 60, 3),
            "30m": TFConfig("30m", Config.CANDLE_30M_COUNT, 30, 4),
            "15m": TFConfig("15m", Config.CANDLE_15M_COUNT, 15, 5),
            "5m": TFConfig("5m", Config.CANDLE_5M_COUNT, 5, 6),
        }
        state.candle_manager = CandleHistoryManager(tf_configs)
        print(f"✅ Candle History: {Config.CANDLE_4H_COUNT}/{Config.CANDLE_1H_COUNT}/{Config.CANDLE_30M_COUNT}/"
              f"{Config.CANDLE_15M_COUNT}/{Config.CANDLE_5M_COUNT} candles per TF")
    
    # Pump Detector (CVD уже в delta_analyzer)
    state.pump_detector_enabled = True
    print("✅ Pump Detector: Z-Score based detection")
    print("✅ CVD Analyzer: Cumulative Volume Delta")
    
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
                        max_daily_risk=Config.MAX_DAILY_RISK,
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

    # =============================================================================
    # EXTERNAL API CLIENTS INITIALIZATION
    # =============================================================================
    logger.info("🔌 Initializing external API clients...")
    
    # CoinGlass
    if Config.USE_COINGLASS:
        try:
            from api.coinglass_client import CoinglassClient
            cg_key = os.getenv("COINGLASS_API_KEY", "")
            logger.info(f"🔑 CoinGlass API key: {'✅ Set' if cg_key else '❌ Not set'}")
            state.coinglass = CoinglassClient(api_key=cg_key)
            logger.info("✅ CoinGlass: Client initialized successfully")
            test_data = await state.coinglass.get_liquidation_data("BTC", "1h", 1)
            if test_data:
                logger.info(f"✅ CoinGlass: Connection test passed")
            else:
                logger.warning("⚠️ CoinGlass: Connection test returned empty data")
        except Exception as e:
            logger.error(f"❌ CoinGlass: Initialization failed - {type(e).__name__}: {str(e)[:100]}")
            Config.USE_COINGLASS = False
            state.coinglass = None
    else:
        logger.info("⚠️ CoinGlass: Disabled (COINGLASS_API_KEY not set)")
    
    # CoinMarketCap
    if Config.USE_COINMARKETCAP:
        try:
            from api.coinmarketcap_client import get_coinmarketcap_client
            cmc_key = os.getenv("COINMARKETCAP_API_KEY", "")
            logger.info(f"🔑 CoinMarketCap API key: {'✅ Set' if cmc_key else '❌ Not set'}")
            state.cmc = get_coinmarketcap_client()
            logger.info("✅ CoinMarketCap: Client initialized successfully")
            test_data = await state.cmc.get_quotes_latest(["BTC"])
            if test_data:
                logger.info(f"✅ CoinMarketCap: Connection test passed")
            else:
                logger.warning("⚠️ CoinMarketCap: Connection test returned empty data")
        except Exception as e:
            logger.error(f"❌ CoinMarketCap: Initialization failed - {type(e).__name__}: {str(e)[:100]}")
            Config.USE_COINMARKETCAP = False
            state.cmc = None
    else:
        logger.info("⚠️ CoinMarketCap: Disabled (COINMARKETCAP_API_KEY not set)")
    
    # CoinGecko
    if Config.USE_COINGECKO:
        try:
            from api.coingecko_client import get_coingecko_client
            cg_key = os.getenv("COINGECKO_API_KEY", "")
            logger.info(f"🔑 CoinGecko API key: {'✅ Set (PRO tier)' if cg_key else '⚠️ Not set (free tier)'}")
            state.coingecko = get_coingecko_client()
            logger.info("✅ CoinGecko: Client initialized successfully")
            test_data = await state.coingecko.get_coin_data("BTC")
            if test_data:
                logger.info(f"✅ CoinGecko: Connection test passed (price: ${test_data.price:,.2f})")
            else:
                logger.warning("⚠️ CoinGecko: Connection test returned empty data")
        except Exception as e:
            logger.error(f"❌ CoinGecko: Initialization failed - {type(e).__name__}: {str(e)[:100]}")
            Config.USE_COINGECKO = False
            state.coingecko = None
    else:
        logger.info("⚠️ CoinGecko: Disabled by configuration")
    
    # Summary
    available_sources = []
    if Config.USE_COINGLASS and state.coinglass:
        available_sources.append("CoinGlass")
    if Config.USE_COINMARKETCAP and state.cmc:
        available_sources.append("CoinMarketCap")
    if Config.USE_COINGECKO and state.coingecko:
        available_sources.append("CoinGecko")
    available_sources.extend(["Bybit", "Binance"])
    
    logger.info(f"📊 Available data sources: {', '.join(available_sources)} ({len(available_sources)}/5)")


    # ✅ FIX v6: Инициализируем Market Context Filter
    state.market_ctx = MarketContextFilter(
        binance_client=state.binance,
        redis_client=state.redis
    )
    print("✅ MarketContextFilter initialized")

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
        f"🤖 <b>SHORT Bot v6.0 запущен</b>\n\n"
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

    # 🧹 При старте очищаем zombie позиции (есть в Redis, но не на бирже)
    print("🧹 [STARTUP] Проверка zombie позиций...")
    try:
        cleaned = await state.tracker.cleanup_zombies()
        if cleaned > 0:
            print(f"🧹 [STARTUP] Очищено {cleaned} ghost-позиций при старте")
    except Exception as e:
        print(f"⚠️ [STARTUP] Ошибка cleanup_zombies: {e}")

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


app = FastAPI(lifespan=lifespan, title="SHORT Bot v6.0")


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
    return JSONResponse({"bot": "SHORT Bot v6.0", "status": "running" if state.is_running else "stopped"})

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


async def scan_symbol(symbol: str, btc_1h: float | None = None) -> Optional[Dict]:
    """
    SHORT scan_symbol v2.7 (NO BTC CORR):
      - SL ВЫШЕ входа (short: stop loss = цена * (1 + SL_BUFFER%))
      - TP НИЖЕ входа (short: take profit = цена * (1 - TP%))
      - OI Proxy: bear_confirm / accumulation / weakness
      - volume_spike_ratio + atr_14_pct → scorer
      - Multi-TF priority: 2h/4h для исполнения, 15m/30m/1h → watch only
    """
    try:
        print(f"🔬 [SCAN-SHORT-ENTRY] {symbol}: ENTERED scan_symbol!")  # DEBUG ENTRY
        print(f"🔬 [SCAN-SHORT-ENTRY] {symbol}: calling get_complete_market_data...")  # DEBUG
        md = await state.binance.get_complete_market_data(symbol)
        print(f"🔬 [SCAN-SHORT-ENTRY] {symbol}: get_complete_market_data returned: {type(md)}")  # DEBUG
        if not md:
            print(f"🔬 [SCAN-SHORT-ENTRY] {symbol}: NO market data")
            return None
        print(f"🔬 [SCAN-SHORT-ENTRY] {symbol}: got market data, price={md.price}")  # DEBUG

        # ✅ FIX: Определяем price сразу, чтобы избежать UnboundLocalError
        price = md.price

        # 🆕 Aegis: Фильтр минимальной капитализации ($900k)
        market_cap = getattr(md, 'market_cap', 0) or 0
        if market_cap and market_cap < Config.MIN_MARKET_CAP:
            print(f"🔴 [MARKET-CAP-SHORT] {symbol}: cap=${market_cap:,.0f} < ${Config.MIN_MARKET_CAP:,.0f} — skip")
            return None
        elif market_cap:
            print(f"💰 [MARKET-CAP-SHORT] {symbol}: cap=${market_cap:,.0f} ✅")

        # ✅ v4.0: MARKET CONTEXT FILTER — для SHORT блокируем азиатскую сессию и дневной стоп
        if hasattr(state, 'market_ctx') and state.market_ctx:
            ctx = await state.market_ctx.check(
                direction="short",
                symbol=symbol,
                block_asian_session=False,  # ✅ FIX: Asian session OFF
                allow_decoupled_alts=True
            )
            if not ctx.allowed:
                print(f"⛔ [CTX-SHORT] {symbol}: {ctx.block_reason}")
                return None
            for w in ctx.warnings:
                print(f"⚠️ [CTX-SHORT] {symbol}: {w}")

        # 🆕 RSI Watchlist tracking — обновляем трекер
        rsi_current = md.rsi_1h or 0
        _rsi_tracker.update(symbol, rsi_current)

        # ✅ Multi-TF загрузка: 15m + 30m + 1h + 2h + 4h (фокус на 2h/4h по бэктесту)
        ohlcv_15m_task = state.binance.get_klines(symbol, "15m", 200)  # Увеличили до 200
        ohlcv_30m_task = state.binance.get_klines(symbol, "30m", 50)
        ohlcv_1h_task = state.binance.get_klines(symbol, "1h", 30)
        ohlcv_2h_task = state.binance.get_klines(symbol, "2h", 20)
        ohlcv_4h_task = state.binance.get_klines(symbol, "4h", 14)
        ohlcv_15m, ohlcv_30m, ohlcv_1h, ohlcv_2h, ohlcv_4h = await asyncio.gather(
            ohlcv_15m_task, ohlcv_30m_task, ohlcv_1h_task, ohlcv_2h_task, ohlcv_4h_task
        )

        # 🆕 NEW: Сохраняем свечи в Candle History Manager для точного анализа
        if hasattr(state, 'candle_manager') and state.candle_manager:
            try:
                tf_data = {
                    "15m": ohlcv_15m,
                    "30m": ohlcv_30m,
                    "1h": ohlcv_1h,
                    "2h": ohlcv_2h,
                    "4h": ohlcv_4h
                }
                for tf, data in tf_data.items():
                    if data:
                        state.candle_manager.update_candles(symbol, tf, data)
                
                # Проверяем качество данных
                quality = state.candle_manager.get_all_data_quality(symbol)
                insufficient = [tf for tf, q in quality.items() if q['status'] == 'insufficient']
                if insufficient:
                    print(f"⚠️ {symbol}: Insufficient data for {insufficient}")
                    
                    # 🆕 TF FALLBACK: если 1h/30m нет данных, используем 15m
                    if '30m' in insufficient or '1h' in insufficient:
                        try:
                            ohlcv_15m_fallback = await state.binance.get_klines(symbol, "15m", 200)
                            if ohlcv_15m_fallback and len(ohlcv_15m_fallback) >= 50:
                                state.candle_manager.update_candles(symbol, "15m", ohlcv_15m_fallback)
                                print(f"   📊 [TF-FALLBACK] {symbol}: Using 15m data instead of {insufficient}")
                                # Обновляем insufficient - 15m теперь есть
                                quality = state.candle_manager.get_all_data_quality(symbol)
                                insufficient = [tf for tf, q in quality.items() if q['status'] == 'insufficient']
                        except Exception as e:
                            print(f"   ⚠️ [TF-FALLBACK] {symbol}: Failed to get 15m: {e}")
            except Exception as e:
                print(f"[CandleHistory] {symbol} error: {e}")

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
                    new_ohlcv = await state.binance.get_klines(symbol, symbol_profile.ideal_tf, 200)  # Увеличили до 200
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
        # ✅ v2.9: TBS (Test Before Strike) — ретест зоны сопротивления/поддержки
        # =========================================================================
        tbs_found = False
        tbs_zone = None
        try:
            from core.tbs_detector import detect_tbs_entry
            tbs = detect_tbs_entry(ohlcv_primary, direction="short")
            if tbs and tbs["found"]:
                tbs_found = True
                tbs_zone = tbs['zone']
                print(f"🎯 [v2.9] {symbol}: TBS DETECTED! Ретест зоны ${tbs_zone:.4f}")
        except Exception as e:
            pass  # TBS не критичен

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
                base_score_bonus += 15  # +15 за TBS (усилено для слабых рынков)
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
                # RSI 4h < 30 = сильный медвежий тренд = бонусы для SHORT
                if rsi_4h < 20:
                    rsi_4h_score_adj = +15  # усилено для слабых рынков
                elif rsi_4h < 30:
                    rsi_4h_score_adj = +10  # глубокий даунтренд
                elif rsi_4h < 40:
                    rsi_4h_score_adj = +7   # даунтренд подтверждён
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
        
        # ── Override tracking (для честного отображения скора) ──────────────────
        override_used = False
        override_type = None
        base_score_before_override = 0

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
        # 💡 SMART SCORING: TBS + качественный OB переопределяют строгий скоринг
        ob_quality    = (ob_result.bearish_ob.quality if ob_result and ob_result.bearish_ob else 0)
        ob_quality_ok = ob_quality >= 50   # ✅ FIX: Понижен с 60 → 50 для слабых рынков
        ob_q_high     = ob_quality >= 65   # Высокое качество (снижено с 70)
        
        if confirmation["passed"] and confirmation["score"] >= 75:
            # ВСЁ ПОДТВЕРЖДЕНО — супер-сигнал!
            base_score = 85 + (confirmation["score"] - 75) // 5  # 85-95
            reasons = sweep["reasons"] + confirmation["reasons"]
        final_score += rsi_30m_score_adj + rsi_4h_score_adj
        if rsi_30m_score_adj != 0:
            print(f"[MTF] {symbol}: RSI30m={rsi_30m:.0f} adj={rsi_30m_score_adj:+d}")
        if rsi_4h_score_adj != 0:
            print(f"[MTF] {symbol}: RSI4h={rsi_4h:.0f} adj={rsi_4h_score_adj:+d}")
        reasons     = list(score_result.reasons)
        
        # 🆕 NEW: Market Data Integrator — полный рыночный контекст
        market_context_adjustment = 0
        if hasattr(state, 'market_integrator') and state.market_integrator:
            try:
                # Получаем полный контекст
                ctx = await state.market_integrator.get_full_context(symbol)
                
                # Логируем контекст
                await state.market_integrator.log_context(symbol, ctx, "short")
                
                # Рассчитываем корректировку
                adjustment = state.market_integrator.calculate_score_adjustment(ctx, "short")
                market_context_adjustment = adjustment["score_delta"]
                
                # Добавляем причины
                reasons.extend(adjustment["reasons"])
                
                # Блокировка если нужно
                if adjustment["should_block"]:
                    print(f"🔴 [MARKET-INTEGRATOR] {symbol}: BLOCKED — {adjustment['reasons'][:2]}")
                    return None
                
                # Применяем корректировку
                final_score += market_context_adjustment
                
                print(f"📊 [MARKET-INTEGRATOR] {symbol}: score adjusted by {market_context_adjustment:+d} "
                      f"(quality: {ctx.data_quality_score}%, confidence: {adjustment['confidence']})")
                
            except Exception as e:
                print(f"⚠️ [MARKET-INTEGRATOR] {symbol}: Error — {e}")

        # ── SHORT-специфичные фильтры ─────────────────────────────────────────
        sf   = get_short_filter()
        filt = sf.check(
            market_data=md, ohlcv_15m=ohlcv_15m,
            hourly_deltas=hourly_deltas,
            btc_price_1h_change=btc_1h,   # ✅ FIX v7: теперь ShortFilter получает реальный BTC 1h
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
        
        # 🌊 ELLIOTT WAVE v3.0: Детекция волн для точных входов
        elliott_min_score = Config.MIN_SCORE  # По умолчанию
        try:
            from core.elliott_detector import detect_elliott_wave, WavePosition
            
            # Получаем OHLCV для анализа волн (используем оригинальные данные)
            wave_ohlcv = ohlcv_primary
            wave_result = detect_elliott_wave(wave_ohlcv, direction="short")
            
            # 📝 ЛОГИРОВАНИЕ ВОЛН (для анализа)
            print(f"🌊 [ELLIOTT-SHORT] {symbol}: Волна={wave_result.wave} | "
                  f"Тип={wave_result.wave_type.value} | "
                  f"Позиция={wave_result.position.value} | "
                  f"Уверенность={wave_result.confidence:.0%} | "
                  f"Качество={wave_result.structure_quality}")
            # Безопасный доступ к details
            details_reason = wave_result.details.get('reason', 'N/A') if isinstance(wave_result.details, dict) else 'N/A'
            print(f"🌊 [ELLIOTT-SHORT] {symbol}: Детали: {details_reason}")
            
            # 🚫 БЛОКИРОВКА ЛОВУШЕК (Волна 2 и B)
            # ✅ FIX: НЕ блокируем неизвестные волны "?" — только реальные ловушки с высокой уверенностью
            if wave_result.is_trap and wave_result.wave not in ["?", "unknown"] and wave_result.confidence > 0.70:
                print(f"🚫 [ELLIOTT-BLOCK-SHORT] {symbol}: Волна {wave_result.wave} — ЛОВУШКА! "
                      f"Блокируем вход. Следующая цель: {wave_result.next_target}")
                # Пишем в Redis для анализа
                try:
                    reason_text = wave_result.details.get('reason', 'Wave 2 or B trap') if isinstance(wave_result.details, dict) else 'Wave 2 or B trap'
                    state.redis.save_signal(Config.BOT_TYPE, symbol, {
                        "timestamp": datetime.utcnow().isoformat(),
                        "symbol": symbol,
                        "direction": "short",
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
                print(f"🎯 [ELLIOTT-BOOST-SHORT] {symbol}: Идеальная волна {wave_result.wave}! "
                      f"Бонус +{wave_boost}, мин скор={elliott_min_score}")
            
            # 📈 ТРЕНД (Волна 3) — небольшой бонус
            elif wave_result.position == WavePosition.TREND:
                final_score += 3
                reasons.append(f"🌊 Elliott Wave 3 (trend) +3")
                print(f"📈 [ELLIOTT-TREND-SHORT] {symbol}: Волна 3 тренда")
            
            # ⚠️ ФИНАЛ (Волна 5) — осторожно, но можно
            elif wave_result.position == WavePosition.FINAL:
                reasons.append(f"⚠️ Elliott Wave 5 (final) — осторожно!")
                print(f"⚠️ [ELLIOTT-FINAL-SHORT] {symbol}: Волна 5 — финал импульса")
            
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
            print(f"🌊 [ELLIOTT-ERROR-SHORT] {symbol}: {e}")
            elliott_data = {"error": str(e)}
        
        # Используем скорректированный минимум скора
        min_score_for_entry = elliott_min_score if 'elliott_min_score' in locals() else Config.MIN_SCORE
        
        if final_score < min_score_for_entry:
            print(f"🔴 [FILTER1] {symbol}: score={final_score} < MIN={min_score_for_entry} — отфильтрован!")
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

        # 🆕 Aegis: Z-Score Pump Detector (критично для SHORT!)
        try:
            pump_result = detect_pump(_ohlcv(ohlcv_15m), direction="short")
            if pump_result.detected and pump_result.signal_type == "pump":
                # Для SHORT: памп (Z > 2) = хороший вход
                z_bonus = min(20, int(pump_result.z_score * 5))  # Z=3 → +15
                final_score = min(100, final_score + z_bonus)
                reasons.append(f"📈 Z-Score PUMP: {pump_result.z_score:.1f}σ (RSI {pump_result.rsi:.0f}) +{z_bonus}")
                print(f"🎯 [AEGIS-Z] {symbol}: PUMP detected Z={pump_result.z_score:.2f}, +{z_bonus} to score")
            elif pump_result.z_score < -2.0:
                # Для SHORT: дамп (Z < -2) = плохой вход, штраф
                z_penalty = min(15, int(abs(pump_result.z_score) * 3))
                final_score = max(0, final_score - z_penalty)
                reasons.append(f"⚠️ Z-Score Dump: {pump_result.z_score:.1f}σ — штраф -{z_penalty}")
        except Exception as e:
            print(f"[AEGIS-Z] {symbol} error: {e}")

        # 🆕 Aegis: Delta Analyzer (CVD + Order Flow)
        try:
            delta_result = analyze_delta(_ohlcv(ohlcv_15m), direction="short")
            if delta_result.score >= 30:
                # Для SHORT: bearish divergence или sell imbalance
                delta_bonus = min(15, int(delta_result.score * 0.25))  # max +15
                final_score = min(100, final_score + delta_bonus)
                reasons.append(f"🌊 Delta/CVD: {delta_result.reasons[0][:50]}... +{delta_bonus}")
                print(f"🎯 [AEGIS-Δ] {symbol}: CVD score={delta_result.score:.0f}, +{delta_bonus}")
        except Exception as e:
            print(f"[AEGIS-Δ] {symbol} error: {e}")

        # 🆕 NEW: Dump Detector (Flash Crash Detection)
        try:
            if hasattr(state, 'dump_detector') and state.dump_detector:
                dump_result = state.dump_detector.analyze(
                    _ohlcv(ohlcv_15m),
                    cvd_value=delta_result.cvd_pressure if 'delta_result' in locals() else None
                )
                if dump_result.detected:
                    if dump_result.is_bottoming:
                        # Для SHORT: дамп + дно = будет отскок, штраф
                        bottom_penalty = min(15, int(dump_result.confidence / 5))
                        final_score = max(0, final_score - bottom_penalty)
                        reasons.append(f"⚠️ Dump Bottoming: {dump_result.dump_type.value} -{bottom_penalty}")
                        print(f"⚠️ [DUMP-SHORT] {symbol}: BOTTOM detected after {dump_result.dump_type.value}, -{bottom_penalty}")
                    else:
                        # Для SHORT: дамп без дна = продолжение падения, бонус
                        dump_bonus = min(15, int(dump_result.confidence / 6))
                        final_score = min(100, final_score + dump_bonus)
                        reasons.append(f"📈 Active Dump: {dump_result.dump_type.value} +{dump_bonus}")
                        print(f"🎯 [DUMP-SHORT] {symbol}: Active dump {dump_result.dump_type.value}, +{dump_bonus}")
        except Exception as e:
            print(f"[DUMP-SHORT] {symbol} error: {e}")

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
        
        # ✅ FIX: Проверка SMC паттерна — сигнал только при наличии структуры
        # 🆕 Aegis: Z-Score и Delta теперь тоже считаются валидными паттернами!
        has_smc_pattern = (
            patterns or  # Есть паттерн от pattern_detector
            (ob_quality >= 60) or  # Есть качественный Order Block (bearish)
            tbs_found or  # Есть TBS (Test Before Strike)
            (smc_data.get("has_ob", False) or smc_data.get("has_fvg", False)) or  # SMC детектор
            # 🆕 Aegis: Сильный Z-Score сигнал тоже паттерн!
            (pump_result.detected if 'pump_result' in locals() else False) or
            # 🆕 Aegis: Сильная дивергенция CVD тоже паттерн!
            (delta_result.score >= 40 if 'delta_result' in locals() else False)
        )
        
        if not has_smc_pattern:
            print(f"🔴 [FILTER-SMC-SHORT] {symbol}: Нет SMC паттерна — сигнал отменён!")
            return None
        
        # Определяем лучший паттерн для отображения
        if not best_pattern:
            if ob_quality >= 60:
                best_pattern = f"OB_Q{ob_quality}"
            elif tbs_found:
                best_pattern = "TBS"
            elif smc_data.get("has_ob", False):
                best_pattern = "SMC_OB"
            elif smc_data.get("has_fvg", False):
                best_pattern = "SMC_FVG"
            # 🆕 Aegis: Z-Score паттерны
            elif 'pump_result' in locals() and pump_result.detected:
                best_pattern = f"Z-SCORE_{pump_result.signal_type.upper()}"
            # 🆕 Aegis: Delta/CVD паттерны
            elif 'delta_result' in locals() and delta_result.score >= 40:
                best_pattern = f"CVD_{delta_result.divergence.upper()}"
            else:
                best_pattern = "SMC_STRUCTURE"
        
        print(f"🟢 [SIGNAL-SHORT] {symbol}: score={final_score} pattern={best_pattern} — сигнал создан!")
        return {
            "symbol": symbol, "direction": "short",
            "score": final_score, "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            # ✅ Честное отображение скора
            "base_score": base_score_before_override,
            "override_used": override_used,
            "override_type": override_type,
            "price": price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 8), "sl_pct": sl_pct,
            "elliott_wave": elliott_data if 'elliott_data' in locals() else None,
            "take_profits": take_profits,
            "patterns": [p.name for p in patterns],
            "best_pattern": best_pattern,  # ✅ Используем определённый выше паттерн
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
        import traceback
        print(f"🔴 [SCAN-SHORT-ERROR] {symbol}: {e}")
        print(f"🔴 [SCAN-SHORT-ERROR] {symbol}: {traceback.format_exc()[:500]}")
        return None



async def scan_market():
    """
    ✅ v2.7 АРХИТЕКТУРА (SHORT, NO BTC CORR):
    - Telegram сигналы: ВСЕГДА при score >= MIN_SCORE (даже при 20/20 SHORT на бирже)
    - Биржевое исполнение: только если active_short < MAX и не /pause
    - Единственный блокер: команда /pause
    """
    print(f"🔬 [SCAN-MARKET-ENTRY] is_paused={state.is_paused}, is_running={state.is_running}")  # DEBUG
    if state.is_paused:
        print(f"🔬 [SCAN-MARKET-ENTRY] SKIPPING: bot is paused!")  # DEBUG
        return

    print(f"\n🔍 SHORT scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | SL={Config.SL_BUFFER}% | Score≥{Config.MIN_SCORE}")

    # ✅ FIX v7: Получаем BTC 1h изменение для ShortFilter (было None всегда!)
    _btc_1h_change: float | None = None
    try:
        _btc_md = await state.binance.get_complete_market_data("BTCUSDT")
        if _btc_md:
            _btc_1h_change = getattr(_btc_md, "price_change_1h", None)
            btc_status = f"+{_btc_1h_change:.2f}%" if _btc_1h_change and _btc_1h_change > 0 else f"{_btc_1h_change:.2f}%" if _btc_1h_change else "N/A"
            # Автоблокировка SHORT при BTC > BTC_BLOCK_THRESHOLD%
            if _btc_1h_change and _btc_1h_change >= Config.BTC_BLOCK_THRESHOLD:
                block_msg = f"🚫 <b>BTC БЛОКИРОВКА SHORT</b>\n\n📈 BTC растёт на <b>{btc_status}</b> (порог: {Config.BTC_BLOCK_THRESHOLD}%)\n⏸ Шорт-входы заблокированы на этом скане\n\n<i>Ждём когда BTC остынет...</i>"
                print(f"🚫 [BTC-BLOCK] BTC {btc_status} ≥ {Config.BTC_BLOCK_THRESHOLD}% — SHORT заблокирован на этом скане!")
                # 🆕 NEW: Telegram уведомление о блокировке (1 раз в 10 минут)
                if hasattr(state, 'telegram') and state.telegram:
                    cache_key = f"btc_block_alert:{int(time.time() / 600)}"  # 10 минутная бUCKET
                    try:
                        if not state.redis.get(cache_key):
                            await state.telegram.send_message(block_msg)
                            state.redis.set(cache_key, "1", 600)  # Cache на 10 мин
                            print(f"   📨 BTC block alert sent to Telegram")
                    except Exception as e:
                        print(f"   ⚠️ Failed to send BTC block alert: {e}")
                _btc_1h_change = _btc_1h_change  # Позволяем ShortFilter принять решение
            print(f"📡 BTC 1h: {btc_status}")
    except Exception as e:
        print(f"⚠️ BTC price fetch error: {e}")

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

            # ✅ FIX v7: SL Cooldown — пропускаем символ если недавно был стоп
            sl_cd_key = f"sl_cooldown:{Config.BOT_TYPE}:{symbol}"
            try:
                cd_val = state.redis.get(sl_cd_key)
                if cd_val:
                    print(f"⏸ [COOLDOWN] {symbol}: недавний SL, пропускаем (cooldown активен)")
                    continue
            except Exception:
                pass

            print(f"🔍 [DEBUG-SHORT] {symbol}: calling scan_symbol...")  # DEBUG
            signal = await scan_symbol(symbol, btc_1h=_btc_1h_change)  # ✅ FIX v7
            if not signal:
                print(f"🔍 [DEBUG-SHORT] {symbol}: scan_symbol returned None")  # DEBUG
                continue
            print(f"🔍 [DEBUG-SHORT] {symbol}: scan_symbol returned signal! score={signal.get('score', 0)}")  # DEBUG

            # ✅ ВСЕГДА: Telegram сигнал (с честным отображением скора)
            tg_msg_id = await state.telegram.send_signal(
                direction="short", symbol=signal["symbol"],
                score=signal["score"], price=signal["price"],
                pattern=signal["best_pattern"] or "N/A",
                indicators=signal["indicators"],
                entry=signal["entry_price"],
                stop_loss=signal["stop_loss"],
                take_profits=signal["take_profits"],
                leverage=Config.LEVERAGE, risk="≤1% deposit",
                base_score=signal.get("base_score"),
                override_used=signal.get("override_used", False),
                override_type=signal.get("override_type"),
            )
            signal["tg_msg_id"] = tg_msg_id
            state.redis.save_signal(Config.BOT_TYPE, symbol, signal)

            # ✅ TF фильтр ОТКЛЮЧЕН: все timeframe на биржу (v2.7)
            primary_tf = signal.get("timeframe", "15m")
            tf_for_execution = True  # Разрешаем всем ТФ

            # 🆕 STRICT: Проверка объема перед входом
            quote_volume = md.quote_volume_24h if hasattr(md, 'quote_volume_24h') else 0
            if quote_volume < Config.MIN_ENTRY_VOLUME_USDT:
                print(f"📊 [VOLUME-FILTER-SHORT] {symbol}: ${quote_volume/1e6:.1f}M < ${Config.MIN_ENTRY_VOLUME_USDT/1e6:.0f}M — skip")
                continue
            
            # 🆕 BTC FILTER: Не шортить если BTC растет (не шортим против тренда)
            btc_trend_ok = True
            if hasattr(state, 'btc_context') and state.btc_context:
                btc_data = state.btc_context.get('trend', {})
                if btc_data.get('direction') == 'UP' and btc_data.get('strength', 0) > 0.6:
                    btc_trend_ok = False
                    print(f"📊 [BTC-FILTER] {symbol}: BTC rising hard — skip SHORT")
            
            # Биржевое исполнение: только если есть SHORT слоты, не на паузе, BTC ок
            if (not exchange_full and Config.AUTO_TRADING and not state.is_paused and btc_trend_ok):
                if state.auto_trader:
                    try:
                        await state.auto_trader.execute_signal(signal)
                        active_count += 1
                        exchange_full = active_count >= Config.MAX_POSITIONS
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")
                new_signals += 1
                print(f"✅ SHORT executed: {symbol} [{primary_tf}] Score={signal['score']:.0f}% SL={signal['sl_pct']}%")
            elif not btc_trend_ok:
                tg_only_count += 1
                print(f"📡 SHORT TG-only: {symbol} [{primary_tf}] [BTC rising]")
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

    # 🆕 NEW: Momentum Scanning (Dump Catching)
    if Config.ENABLE_MOMENTUM_SHORT and hasattr(state, 'momentum_detector'):
        try:
            print(f"\n🚀 [MOMENTUM-SHORT] Scanning {len(state.watchlist)} symbols for dumps...")
            
            async def get_1m_candles(symbol, tf, limit):
                return await state.binance.get_recent_candles(symbol, timeframe="1m", limit=30)
            
            momentum_signals = await state.momentum_detector.scan_watchlist(
                state.watchlist,
                get_1m_candles,
                min_volume_usdt=100000,  # Ниже порог для мемов
                debug=True  # 🔍 DEBUG: показывать почему отсеиваются монеты
            )
            
            if momentum_signals:
                print(f"🎯 [MOMENTUM-SHORT] Found {len(momentum_signals)} dump signals")
                
                for mom_sig in momentum_signals:
                    try:
                        symbol = mom_sig.symbol
                        
                        # Дедупликация
                        if _is_fresh(state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)):
                            continue
                        
                        # Преобразуем momentum signal
                        signal = {
                            "symbol": symbol,
                            "direction": "short",
                            "score": mom_sig.score,
                            "price": mom_sig.entry_price,
                            "entry_price": mom_sig.entry_price,
                            "stop_loss": round(mom_sig.entry_price * (1 + Config.MOMENTUM_SHORT_SL_BUFFER/100), 6),
                            "take_profits": [
                                round(mom_sig.entry_price * 0.985, 6),  # TP1 1.5%
                                round(mom_sig.entry_price * 0.97, 6),   # TP2 3%
                            ],
                            "sl_pct": Config.MOMENTUM_SHORT_SL_BUFFER,
                            "best_pattern": "momentum_dump",
                            "indicators": {
                                "rsi": round(mom_sig.rsi, 1),
                                "volume_spike": round(mom_sig.volume_spike, 1),
                                "velocity_1m": round(abs(mom_sig.change_1m), 2),
                                "factors": mom_sig.factors
                            },
                            "signal_type": "momentum",
                            "risk_per_trade": Config.MOMENTUM_SHORT_RISK_PER_TRADE,
                        }
                        
                        # Telegram сигнал
                        tg_msg_id = await state.telegram.send_signal(
                            direction="short", symbol=symbol,
                            score=signal["score"], price=signal["price"],
                            pattern="💥 MOMENTUM DUMP",
                            indicators=signal["indicators"],
                            entry=signal["entry_price"],
                            stop_loss=signal["stop_loss"],
                            take_profits=signal["take_profits"],
                            leverage=Config.LEVERAGE, risk="≤0.5% deposit",
                        )
                        signal["tg_msg_id"] = tg_msg_id
                        state.redis.save_signal(Config.BOT_TYPE, symbol, signal)
                        
                        # Биржевое исполнение
                        if not exchange_full and Config.AUTO_TRADING and not state.is_paused:
                            if state.auto_trader:
                                try:
                                    await state.auto_trader.execute_momentum_signal(signal)
                                    active_count += 1
                                    exchange_full = active_count >= Config.MAX_POSITIONS
                                    new_signals += 1
                                    print(f"✅ [MOMENTUM-SHORT] Executed: {symbol} Score={signal['score']:.0f}%")
                                except Exception as e:
                                    print(f"❌ [MOMENTUM-SHORT] Error {symbol}: {e}")
                        else:
                            tg_only_count += 1
                            print(f"📡 [MOMENTUM-SHORT] TG-only: {symbol}")
                            
                    except Exception as e:
                        print(f"❌ [MOMENTUM-SHORT] Signal error: {e}")
                        
        except Exception as e:
            print(f"❌ [MOMENTUM-SHORT] Scanner error: {e}")
    
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
