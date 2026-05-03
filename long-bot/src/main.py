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
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
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

logger = logging.getLogger("long_bot")
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
from api.okx_client import get_okx_client  # 🆕 NEW: OKX fallback client
from core.market_data_integrator import get_market_data_integrator  # 🆕 NEW: Market Data Integrator
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
    BOT_TYPE      = "long"
    
    # ============================================================================
    # 🔧 ENVIRONMENT VARIABLES (настраиваются на Render Dashboard)
    # ============================================================================
    # MIN_SCORE_LONG       - Минимальный score для входа (default: 65) ⭐ КЛЮЧЕВОЙ
    # MAX_LONG_POSITIONS   - Макс. кол-во позиций (default: 10) ⭐ КЛЮЧЕВОЙ
    # LONG_SL_BUFFER       - SL буфер в процентах (default: 2.0) ⭐ КЛЮЧЕВОЙ
    # SCAN_INTERVAL        - Интервал сканирования в сек (default: 180)
    # LONG_LEVERAGE        - Плечо (default: "5-50")
    # LONG_TRAIL_ACTIVATION - Активация trailing SL (default: 0.025)
    # BTC_BLOCK_LONG_THRESHOLD - Блокировка при дампе BTC (default: 4.0)
    # SL_COOLDOWN_HOURS    - Кулдаун после SL в часах (default: 2.0)
    # MAX_DAILY_RISK       - Дневной лимит потерь % (default: 5.0) ⭐ КЛЮЧЕВОЙ
    # ============================================================================
    
    # ✅ FIX: Переименовано MIN_LONG_SCORE → MIN_SCORE_LONG для соответствия Render
    # ✅ REDUCED: default 75 → 65 (больше сигналов на медвежьем рынке)
    MIN_SCORE     = int(os.getenv("MIN_SCORE_LONG", "65"))  # ⭐ Снижен для активности
    
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "180"))  # ✅ 3 минуты для снижения нагрузки API
    
    # ✅ FIX: Уменьшено default с 20 до 10 (меньше позиций = меньше риск)
    MAX_POSITIONS = int(os.getenv("MAX_LONG_POSITIONS", "10"))  # ⭐ ИЗМЕНИТЬ на Render!
    
    LEVERAGE      = os.getenv("LONG_LEVERAGE", "5-50")

    # LONG: SL НИЖЕ входа, TP ВЫШЕ входа
    # ✅ FIX: Увеличен default с 1.5% до 2.0% (меньше ложных стопов)
    SL_BUFFER     = float(os.getenv("LONG_SL_BUFFER", "2.0"))  # ⭐ ИЗМЕНИТЬ на Render!
    SL_COOLDOWN_HOURS  = float(os.getenv("SL_COOLDOWN_HOURS", "2.0"))
    # ✅ FIX: MAX_DAILY_RISK определён ниже в блоке "Drawdown Control" (default 15.0)
    # Удалено дублирующее определение с default 5.0 (оно перезаписывалось)

    # 🆕 NEW: Advanced modules configuration
    # Smart DCA
    ENABLE_SMART_DCA = os.getenv("ENABLE_SMART_DCA", "true").lower() == "true"
    DCA_MAX_ADDITIONS = int(os.getenv("DCA_MAX_ADDITIONS", "3"))
    DCA_MAX_PORTFOLIO_RISK = float(os.getenv("DCA_MAX_PORTFOLIO_RISK", "3.0"))
    # ✅ FIX: добавлены отсутствующие параметры SmartDCAEngine (были NameError при инициализации)
    DCA_ATR_MULT = float(os.getenv("DCA_ATR_MULT", "1.5"))        # ATR множитель для DCA шага
    DCA_SIZE_MULT = float(os.getenv("DCA_SIZE_MULT", "1.5"))       # Anti-martingale множитель
    DCA_MAX_EXPOSURE_PCT = float(os.getenv("DCA_MAX_EXPOSURE_PCT", "0.40"))  # Макс 40% депо
    
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
    
    # 🆕 NEW: Momentum Detector (Trend Following)
    ENABLE_MOMENTUM_LONG = os.getenv("ENABLE_MOMENTUM_LONG", "true").lower() == "true"
    MOMENTUM_LONG_MIN_1MIN_CHANGE = float(os.getenv("MOMENTUM_LONG_MIN_1MIN_CHANGE", "0.8"))
    MOMENTUM_LONG_MIN_5MIN_CHANGE = float(os.getenv("MOMENTUM_LONG_MIN_5MIN_CHANGE", "2.0"))
    MOMENTUM_LONG_VOLUME_SPIKE = float(os.getenv("MOMENTUM_LONG_VOLUME_SPIKE", "2.5"))
    MOMENTUM_LONG_RSI_MIN = float(os.getenv("MOMENTUM_LONG_RSI_MIN", "40"))
    MOMENTUM_LONG_RSI_MAX = float(os.getenv("MOMENTUM_LONG_RSI_MAX", "75"))
    MOMENTUM_LONG_SCORE_MIN = float(os.getenv("MOMENTUM_LONG_SCORE_MIN", "50"))
    MOMENTUM_LONG_MAX_POSITIONS = int(os.getenv("MOMENTUM_LONG_MAX_POSITIONS", "5"))
    MOMENTUM_LONG_RISK_PER_TRADE = float(os.getenv("MOMENTUM_LONG_RISK_PER_TRADE", "0.0003"))
    MOMENTUM_LONG_SL_BUFFER = float(os.getenv("MOMENTUM_LONG_SL_BUFFER", "1.0"))
    MOMENTUM_LONG_TRAIL_START = float(os.getenv("MOMENTUM_LONG_TRAIL_START", "0.5"))
    
    # 🆕 NEW: Candle History Manager
    ENABLE_CANDLE_HISTORY = os.getenv("ENABLE_CANDLE_HISTORY", "true").lower() == "true"
    CANDLE_4H_COUNT = int(os.getenv("CANDLE_4H_COUNT", "30"))
    
    # ============================================================================
    # 🏛️ INSTITUTIONAL RISK MANAGEMENT (Aegis Integration)
    # ============================================================================
    # ✅ FIX: убрана первая дублирующая запись RISK_PER_TRADE=0.05 (она перезаписывалась ниже на 0.0005)
    # Итоговый RISK_PER_TRADE задаётся ниже через env RISK_PER_TRADE
    
    # Kelly Criterion Sizing
    KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))  # 25% Kelly
    
    # Position & Exposure Limits
    MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.25"))  # 25% на позицию
    MAX_EXPOSURE_PCT = float(os.getenv("MAX_EXPOSURE_PCT", "0.80"))  # 80% макс экспозиция
    
    # Drawdown Control (ВАШЕ требование: -15%)
    MAX_DAILY_RISK = float(os.getenv("MAX_DAILY_RISK", "15.0"))  # -15% (было 5.0)
    MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
    
    # ============================================================================
    # 🎯 6 TP LEVELS CONFIG (Phase 4 - best-ls-bot optimization)
    # ============================================================================
    TP_LEVELS = [3.0, 5.0, 8.0, 12.0, 18.0, 25.0]  # Long: дальние цели
    TP_WEIGHTS = [25, 25, 20, 15, 10, 5]  # Акцент на TP1-3
    
    # ============================================================================
    # 🚩 FEATURE FLAGS (Phase 3 - Institutional Analysis)
    # ============================================================================
    ENABLE_WYCKOFF_DETECTOR = os.getenv("ENABLE_WYCKOFF", "true").lower() == "true"
    ENABLE_BSL_SCANNER = os.getenv("ENABLE_BSL", "true").lower() == "true"
    ENABLE_OI_ANALYZER = os.getenv("ENABLE_OI", "true").lower() == "true"
    ENABLE_LIQ_MAPPER = os.getenv("ENABLE_LIQ", "true").lower() == "true"
    ENABLE_DELTA = os.getenv("ENABLE_DELTA", "true").lower() == "true"
    
    # BTC Correlation & Decoupling
    ENABLE_BTC_CORRELATION = os.getenv("ENABLE_BTC", "true").lower() == "true"
    ENABLE_DECOUPLING_BONUS = os.getenv("ENABLE_DECOUPLING", "true").lower() == "true"
    BTC_BLOCK_THRESHOLD = float(os.getenv("BTC_BLOCK_THRESHOLD", "-3.0"))  # Блок при -3% BTC
    
    # ============================================================================
    # 🏭 SECTOR DIVERSIFICATION (Phase 5 - Sector Limits)
    # ============================================================================
    ENABLE_SECTOR_LIMITS = os.getenv("ENABLE_SECTOR_LIMITS", "true").lower() == "true"
    MAX_PER_SECTOR_MEME = int(os.getenv("MAX_PER_SECTOR_MEME", "2"))
    MAX_PER_SECTOR_DEFI = int(os.getenv("MAX_PER_SECTOR_DEFI", "3"))
    MAX_PER_SECTOR_GAMEFI = int(os.getenv("MAX_PER_SECTOR_GAMEFI", "3"))
    MAX_PER_SECTOR_L1 = int(os.getenv("MAX_PER_SECTOR_L1", "3"))
    MAX_PER_SECTOR_L2 = int(os.getenv("MAX_PER_SECTOR_L2", "3"))
    MAX_PER_SECTOR_AI = int(os.getenv("MAX_PER_SECTOR_AI", "3"))
    MAX_PER_SECTOR_RWA = int(os.getenv("MAX_PER_SECTOR_RWA", "2"))
    MAX_PER_SECTOR_ORACLE = int(os.getenv("MAX_PER_SECTOR_ORACLE", "2"))
    MAX_PER_SECTOR_INFRA = int(os.getenv("MAX_PER_SECTOR_INFRA", "2"))
    CANDLE_2H_COUNT = int(os.getenv("CANDLE_2H_COUNT", "50"))
    CANDLE_1H_COUNT = int(os.getenv("CANDLE_1H_COUNT", "80"))
    CANDLE_30M_COUNT = int(os.getenv("CANDLE_30M_COUNT", "120"))
    CANDLE_15M_COUNT = int(os.getenv("CANDLE_15M_COUNT", "150"))
    CANDLE_5M_COUNT = int(os.getenv("CANDLE_5M_COUNT", "300"))
    CANDLE_MIN_REQUIRED = int(os.getenv("CANDLE_MIN_REQUIRED", "20"))

    # Trailing — LONG активирует при +2.5% (после TP1)
    TRAIL_ACTIVATION = float(os.getenv("LONG_TRAIL_ACTIVATION", "0.025"))
    LONG_TRAIL_ACTIVATION = TRAIL_ACTIVATION  # Alias для position_tracker.py

    SIGNAL_TTL_HOURS = 24

    AUTO_TRADING         = os.getenv("AUTO_TRADING_ENABLED", "true").lower() == "true"
    BINGX_DEMO           = os.getenv("BINGX_DEMO_MODE", "true").lower() == "true"
    RISK_PER_TRADE       = float(os.getenv("RISK_PER_TRADE", "0.0005"))
    # ✅ FIX: по умолчанию ВЫКЛЮЧЕН — паттерны REJECTION/SWEEP уже контртрендовые,
    # EMA50<200 блокирует все лонги в медвежьем рынке (баг: ни одна позиция не открывалась)
    TREND_FILTER_ENABLED = os.getenv("TREND_FILTER_ENABLED", "false").lower() == "true"

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
    """Уровень DCA с параметрами."""
    level: int
    price_drop_pct: float
    size_multiplier: float
    max_position_pct: float


class SmartDCAEngine:
    """
    Smart DCA v2 из Aegis-bots.
    ATR-based spacing, Kelly sizing, Circuit breaker.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.dca_levels = self._init_dca_levels()
        self.total_exposure_pct = 0.0
        self.circuit_breaker_triggered = False
        
    def _init_dca_levels(self) -> List:
        """Инициализация уровней DCA."""
        levels = []
        base_mult = self.config.DCA_ATR_MULT
        
        for i in range(1, 5):  # 4 уровня
            level = DCALevel(
                level=i,
                price_drop_pct=i * self.config.DCA_ATR_MULT,  # ATR-based
                size_multiplier=self.config.DCA_SIZE_MULT ** (i - 1),  # Anti-martingale
                max_position_pct=min(0.10 * i, 0.40)  # Max 40%
            )
            levels.append(level)
            
        return levels
    
    def calculate_dca_orders(self, entry_price: float, position_size: float,
                            atr: float, portfolio_value: float) -> List[Dict]:
        """
        Расчет DCA ордеров с Kelly Criterion sizing.
        """
        if not self.config.ENABLE_SMART_DCA:
            return []
            
        # Circuit breaker check
        if self.total_exposure_pct >= self.config.DCA_MAX_EXPOSURE_PCT:
            print(f"⚠️ [DCA] Circuit breaker: exposure {self.total_exposure_pct:.1%}")
            self.circuit_breaker_triggered = True
            return []
            
        orders = []
        current_size = position_size
        
        for level in self.dca_levels:
            # Kelly Criterion sizing
            kelly_fraction = self.config.KELLY_FRACTION
            level_size = current_size * level.size_multiplier * kelly_fraction
            
            # Проверка лимита экспозиции
            level_exposure = (level_size * entry_price) / portfolio_value
            if self.total_exposure_pct + level_exposure > self.config.DCA_MAX_EXPOSURE_PCT:
                print(f"⚠️ [DCA] Level {level.level} skipped: exposure limit")
                break
                
            # Для long: цена падает (против нас)
            dca_price = entry_price * (1 - level.price_drop_pct / 100)
            
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
        """Сброс счетчика экспозиции."""
        self.total_exposure_pct = 0.0
        self.circuit_breaker_triggered = False


# ============================================================================
# 🆕 SECTOR POSITION MANAGER (Phase 5)
# ============================================================================

class SectorPositionManager:
    """
    Управление позициями по секторам.
    Диверсификация и лимиты на секторы.
    """
    
    def __init__(self, config: Config, sector_mapper):
        self.config = config
        self.sector_mapper = sector_mapper
        self.positions_by_sector: Dict[str, List[str]] = {}
        
    def can_open_position(self, symbol: str) -> Tuple[bool, str]:
        """Проверка возможности открытия позиции в секторе."""
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
        """Получить лимит для сектора."""
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
        """Добавление позиции в сектор."""
        sector = self.sector_mapper.get_sector(symbol)
        if sector:
            if sector not in self.positions_by_sector:
                self.positions_by_sector[sector] = []
            self.positions_by_sector[sector].append(symbol)
            
    def remove_position(self, symbol: str):
        """Удаление позиции из сектора."""
        sector = self.sector_mapper.get_sector(symbol)
        if sector and sector in self.positions_by_sector:
            if symbol in self.positions_by_sector[sector]:
                self.positions_by_sector[sector].remove(symbol)
                
    def get_sector_stats(self) -> Dict:
        """Статистика по секторам."""
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
            "max_daily_dd": 0.0  # Track max drawdown
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
    state.okx              = get_okx_client()  # 🆕 NEW: OKX fallback for OI, funding, liquidations
    state.market_integrator = get_market_data_integrator(state.okx, state.binance)  # 🆕 NEW: Full market context
    state.scorer           = get_long_scorer(Config.MIN_SCORE)
    state.pattern_detector = LongPatternDetector()
    
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
    
    # 🆕 NEW: Momentum Detector (Trend Following)
    print(f"\n{'='*60}")
    print(f"🔥 MOMENTUM DETECTOR INITIALIZATION 🔥")
    print(f"{'='*60}")
    print(f"ENABLE_MOMENTUM_LONG = {Config.ENABLE_MOMENTUM_LONG}")
    print(f"{'='*60}\n")
    
    if Config.ENABLE_MOMENTUM_LONG:
        try:
            print("[MOMENTUM] Creating detector instance...")
            state.momentum_detector = get_momentum_detector(direction="long")
            print(f"[MOMENTUM] ✅ Detector created: {state.momentum_detector is not None}")
            print(f"[MOMENTUM] ✅ Direction: {state.momentum_detector.direction if state.momentum_detector else 'N/A'}")
            print("[MOMENTUM] ✅ Trend following mode ACTIVE (velocity + volume spike)")
        except Exception as e:
            print(f"[MOMENTUM] ❌ FAILED to create detector: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[MOMENTUM] ⚠️ DISABLED in config")
    
    # 🆕 NEW: Candle History Manager
    # ✅ FIX: только TF которые реально загружаются (30m + 1h)
    # 4h/2h/15m/5m не загружаются → убраны чтобы не спамить "Insufficient data"
    if Config.ENABLE_CANDLE_HISTORY:
        from core.candle_history_manager import CandleHistoryManager, TFConfig
        tf_configs = {
            "1h":  TFConfig("1h",  Config.CANDLE_1H_COUNT,  60, 1),
            "30m": TFConfig("30m", Config.CANDLE_30M_COUNT, 30, 2),
        }
        state.candle_manager = CandleHistoryManager(tf_configs)
        print(f"✅ Candle History: 1h={Config.CANDLE_1H_COUNT} / 30m={Config.CANDLE_30M_COUNT} candles")
    
    # Pump Detector (CVD уже в delta_analyzer)
    state.pump_detector_enabled = True
    print("✅ Pump Detector: Z-Score based detection")
    print("✅ CVD Analyzer: Cumulative Volume Delta")
    
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
                        max_daily_risk=Config.MAX_DAILY_RISK,
                        trend_filter_enabled=Config.TREND_FILTER_ENABLED,
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
            # Test connection
            test_data = await state.coinglass.get_liquidation_data("BTC", "1h", 1)
            if test_data:
                logger.info(f"✅ CoinGlass: Connection test passed (liquidations API working)")
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
            # Test connection
            test_data = await state.cmc.get_quotes_latest(["BTC"])
            if test_data:
                logger.info(f"✅ CoinMarketCap: Connection test passed (quotes API working)")
            else:
                logger.warning("⚠️ CoinMarketCap: Connection test returned empty data")
        except Exception as e:
            logger.error(f"❌ CoinMarketCap: Initialization failed - {type(e).__name__}: {str(e)[:100]}")
            Config.USE_COINMARKETCAP = False
            state.cmc = None
    else:
        logger.info("⚠️ CoinMarketCap: Disabled (COINMARKETCAP_API_KEY not set)")
    
    # CoinGecko (работает без ключа!)
    if Config.USE_COINGECKO:
        try:
            from api.coingecko_client import get_coingecko_client
            cg_key = os.getenv("COINGECKO_API_KEY", "")
            logger.info(f"🔑 CoinGecko API key: {'✅ Set (PRO tier)' if cg_key else '⚠️ Not set (free tier - 30 req/min)'}")
            state.coingecko = get_coingecko_client()
            logger.info("✅ CoinGecko: Client initialized successfully")
            # Test connection
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
    
    # Summary of available data sources
    available_sources = []
    if Config.USE_COINGLASS and state.coinglass:
        available_sources.append("CoinGlass")
    if Config.USE_COINMARKETCAP and state.cmc:
        available_sources.append("CoinMarketCap")
    if Config.USE_COINGECKO and state.coingecko:
        available_sources.append("CoinGecko")
    available_sources.extend(["Bybit", "Binance"])  # Always available
    
    logger.info(f"📊 Available data sources: {', '.join(available_sources)} ({len(available_sources)}/5)")


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
    asyncio.create_task(virtual_position_monitor())  # 🔍 Виртуальный TP/SL монитор

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
        print(f"🔬 [SCAN-LONG-ENTRY] {symbol}: ENTERED scan_symbol!")  # DEBUG ENTRY
        print(f"🔬 [SCAN-LONG-ENTRY] {symbol}: calling get_complete_market_data...")  # DEBUG
        md = await state.binance.get_complete_market_data(symbol)
        print(f"🔬 [SCAN-LONG-ENTRY] {symbol}: get_complete_market_data returned: {type(md)}")  # DEBUG
        if not md:
            print(f"🔬 [SCAN-LONG-ENTRY] {symbol}: NO market data")
            return None
        print(f"🔬 [SCAN-LONG-ENTRY] {symbol}: got market data, price={md.price}")  # DEBUG
        
        # ✅ FIX: Определяем price сразу, чтобы избежать UnboundLocalError
        price = md.price

        # 🆕 Aegis: Фильтр минимальной капитализации ($900k)
        market_cap = getattr(md, 'market_cap', 0) or 0
        if market_cap and market_cap < Config.MIN_MARKET_CAP:
            print(f"🔴 [MARKET-CAP-LONG] {symbol}: cap=${market_cap:,.0f} < ${Config.MIN_MARKET_CAP:,.0f} — skip")
            return None
        elif market_cap:
            print(f"💰 [MARKET-CAP-LONG] {symbol}: cap=${market_cap:,.0f} ✅")

        # ✅ v4.0: MARKET CONTEXT FILTER — BTC корреляция, сессия, дневной стоп
        if hasattr(state, 'market_ctx') and state.market_ctx:
            ctx = await state.market_ctx.check(
                direction="long",
                symbol=symbol,
                block_asian_session=False,  # ✅ FIX: Asian session OFF
                allow_decoupled_alts=True
            )
            if not ctx.allowed:
                # Логируем блокировку только 1 раз в минуту чтобы не спамить
                print(f"⛔ [CTX-LONG] {symbol}: {ctx.block_reason}")
                # 🆕 NEW: Telegram уведомление о блокировке (1 раз в час для BTC блока)
                if hasattr(state, 'telegram') and state.telegram and 'BTC' in ctx.block_reason:
                    try:
                        cache_key = f"btc_block_long:{symbol}:{int(time.time() / 3600)}"  # 1 час
                        if not state.redis.get(cache_key):
                            block_alert = f"🔴 <b>BTC БЛОКИРОВКА LONG</b>\n\n{ctx.block_reason}\n\n📍 Символ: <b>#{symbol}</b>\n\n<i>Ждём стабилизации BTC (1h timeframe)...</i>"
                            await state.telegram.send_message(block_alert)
                            state.redis.set(cache_key, "1", 3600)  # 1 час TTL
                            print(f"   📨 BTC block alert for LONG sent to Telegram (1h)")
                    except Exception as e:
                        print(f"   ⚠️ Failed to send BTC block alert: {e}")
                return None
            for w in ctx.warnings:
                print(f"⚠️ [CTX-LONG] {symbol}: {w}")

        # 🆕 RSI Watchlist tracking — обновляем трекер
        rsi_current = md.rsi_1h or 0
        _rsi_tracker.update(symbol, rsi_current)

        # ✅ Multi-TF загрузка: 30m + 1h параллельно (убран 15m — 50% стопов в бэктесте)
        # ✅ FIX: увеличены лимиты чтобы CandleHistoryManager не возвращал "insufficient"
        # Required: 30m≥120, 1h≥80
        ohlcv_30m_task = state.binance.get_klines(symbol, "30m", 200)  # 200 ≥ 120 ✅
        ohlcv_1h_task = state.binance.get_klines(symbol, "1h", 90)    # 90 ≥ 80 ✅
        ohlcv_30m, ohlcv_1h = await asyncio.gather(ohlcv_30m_task, ohlcv_1h_task)

        # 🆕 NEW: Сохраняем свечи в Candle History Manager для точного анализа
        if hasattr(state, 'candle_manager') and state.candle_manager:
            try:
                tf_data = {
                    "30m": ohlcv_30m,
                    "1h": ohlcv_1h
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
                    _p4d_sweep = md.price_change_24h * 4 if md.price_change_24h else 0
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
                        "rsi_1h": md.rsi_1h,
                        "funding_rate": md.funding_rate,
                        "oi_change": md.oi_change_4d,
                        "long_short_ratio": md.long_short_ratio,
                        "timestamp": datetime.utcnow().isoformat(),
                        "status": "new",
                        "taken_tps": 0,
                        "indicators": {
                            "SMC": "Sweep+TBS",
                            "Confirmation": f"Score:{confirmation['score']}",
                            "RSI": f"{md.rsi_1h:.1f}" if md.rsi_1h else "N/A",
                            "Funding": f"{md.funding_rate:+.3f}%",
                            "L/S Ratio": f"{md.long_short_ratio:.0f}% longs",
                            "OI Change": f"{md.oi_change_4d:+.1f}% (4d)",
                            "Price 4d": f"{_p4d_sweep:+.1f}%",
                        },
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

        # =========================================================================
        # ✅ MULTI-TIMEFRAME RSI CONFLUENCE — 30m + 1h + 4h (LONG)
        # Точный анализ: RSI на каждом ТФ разный → ищем СОВПАДЕНИЕ перепроданности
        # =========================================================================
        rsi_30m = 50.0
        rsi_4h  = 50.0
        rsi_1h_val = md.rsi_1h or 50.0

        # --- RSI 30m ---
        try:
            if ohlcv_30m and len(ohlcv_30m) >= 15:
                closes_30m = [c.close for c in ohlcv_30m[-15:]]
                gains_30m  = [max(0, closes_30m[i]-closes_30m[i-1]) for i in range(1,15)]
                losses_30m = [max(0, closes_30m[i-1]-closes_30m[i]) for i in range(1,15)]
                ag_30m = sum(gains_30m)/14; al_30m = sum(losses_30m)/14
                rsi_30m = 100 - 100/(1 + ag_30m/al_30m) if al_30m > 0 else 50
        except Exception:
            pass

        # --- RSI 4h ---
        ohlcv_4h = []
        try:
            ohlcv_4h = await state.binance.get_klines(symbol, "4h", 35)
            if ohlcv_4h and len(ohlcv_4h) >= 15:
                closes_4h = [c.close for c in ohlcv_4h[-15:]]
                gains_4h  = [max(0, closes_4h[i]-closes_4h[i-1]) for i in range(1,15)]
                losses_4h = [max(0, closes_4h[i-1]-closes_4h[i]) for i in range(1,15)]
                ag_4h = sum(gains_4h)/14; al_4h = sum(losses_4h)/14
                rsi_4h = 100 - 100/(1 + ag_4h/al_4h) if al_4h > 0 else 50
        except Exception:
            pass

        print(f"📊 [MTF-RSI-LONG] {symbol}: 4H={rsi_4h:.1f} 1H={rsi_1h_val:.1f} 30m={rsi_30m:.1f}")

        # --- MTF Confluence для LONG: перепроданность сразу на нескольких TF ---
        mtf_rsi_bonus  = 0
        mtf_rsi_reason = ""

        if rsi_4h < 30 and rsi_1h_val < 35 and rsi_30m < 35:
            mtf_rsi_bonus  = +18
            mtf_rsi_reason = f"🎯 MTF LONG: 4H={rsi_4h:.0f} 1H={rsi_1h_val:.0f} 30m={rsi_30m:.0f} — перепродан везде! +18"
        elif rsi_4h < 35 and rsi_1h_val < 40:
            mtf_rsi_bonus  = +12
            mtf_rsi_reason = f"🎯 MTF LONG: 4H={rsi_4h:.0f} 1H={rsi_1h_val:.0f} — конфлюенс перепроданности +12"
        elif rsi_4h < 40 and rsi_1h_val < 38:
            mtf_rsi_bonus  = +7
            mtf_rsi_reason = f"📊 MTF LONG: 4H={rsi_4h:.0f} 1H={rsi_1h_val:.0f} — частичный конфлюенс +7"
        elif rsi_4h < 40 and rsi_30m < 30:
            mtf_rsi_bonus  = +5
            mtf_rsi_reason = f"📊 MTF LONG: 4H={rsi_4h:.0f} 30m={rsi_30m:.0f} — перепродан 4H+30m +5"
        elif rsi_4h > 72 and rsi_1h_val > 70:
            mtf_rsi_bonus  = -10
            mtf_rsi_reason = f"⚠️ MTF LONG: 4H={rsi_4h:.0f} 1H={rsi_1h_val:.0f} — перекуплен 4H+1H, риск для LONG -10"
        elif rsi_4h > 65 and rsi_1h_val > 72:
            mtf_rsi_bonus  = -5
            mtf_rsi_reason = f"⚠️ MTF LONG: 4H={rsi_4h:.0f} 1H={rsi_1h_val:.0f} — начало перекупленности -5"

        if mtf_rsi_reason:
            print(f"   {mtf_rsi_reason}")

        # 🛑 HARD BLOCK: жёсткий запрет на LONG в экстремальной перекупленности
        # Логика: при RSI 4H>80 движение уже на излёте, LONG = покупка на пике
        if rsi_4h > 80:
            print(f"🛑 [RSI-CEIL-LONG] {symbol}: RSI 4H={rsi_4h:.0f} > 80 — пик памп, LONG заблокирован")
            return None
        if rsi_4h > 74 and rsi_1h_val > 72:
            print(f"🛑 [RSI-CEIL-LONG] {symbol}: RSI 4H={rsi_4h:.0f} 1H={rsi_1h_val:.0f} — оба перекуплены, LONG заблокирован")
            return None

        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend   = state.pattern_detector._get_price_trend(ohlcv_30m)
        patterns      = state.pattern_detector.detect_all(ohlcv_30m, hourly_deltas, md)
        p4d           = await _get_price_change_4d(symbol, md.price_change_24h * 4)

        # ── OI Proxy (LONG специфика) ─────────────────────────────────────────
        oi_bull_confirm  = False
        oi_accumulation  = False
        oi_weakness_long = False
        oi_score_adj     = 0.0
        
        # ── Override tracking (для честного отображения скора) ──────────────────
        override_used = False
        override_type = None
        base_score_before_override = 0

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

        # ✅ Ликвидационный анализ для скора (из market_data_integrator)
        _liq_analysis = getattr(md, 'liquidation_analysis', None)

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
            symbol_change_1h=symbol_change_1h,
            btc_change_1h=btc_change_1h_score,
            liq_analysis=_liq_analysis,          # ✅ Ликвидации в скор
            mtf_rsi_bonus=mtf_rsi_bonus,         # ✅ MTF RSI конфлюенс в скор
            mtf_rsi_reason=mtf_rsi_reason,
        )
        
        # ✅ FIX v3.1: SMART SCORING — многоуровневый оверрайд для LONG
        ob_quality    = (ob_result.bullish_ob.quality if ob_result and ob_result.bullish_ob else 0)
        ob_quality_ok = ob_quality >= 60   # ✅ Снижен порог с 70 → 60
        ob_q_high     = ob_quality >= 70   # Высокое качество
        
        # ✅ FIX v7: Детальные логи score breakdown
        print(f"📊 [SCORE] {symbol}: total={score_result.total_score:.1f}% valid={score_result.is_valid} "
              f"rsi={getattr(md,'rsi_1h',0):.0f} fund={getattr(md,'funding_rate',0):.3f}% "
              f"oi4d={getattr(md,'oi_change_4d',0):.1f}% ob_q={ob_quality}")

        # ✅ FIX: Учитываем base_score_bonus (TBS/confirmation) при проверке порога
        effective_min = 30 if (symbol_profile and getattr(symbol_profile, "volatility_class", "") == "extreme") else 40
        adjusted_score = score_result.total_score + max(0, base_score_bonus)
        score_is_valid = score_result.is_valid or adjusted_score >= effective_min

        # 🛑 STRICT MODE: Нет оверрайдов! Только чистые валидные сигналы
        if not score_is_valid:
            print(f"🚫 [FILTER-LONG] {symbol}: score invalid ({score_result.total_score:.1f}%+{base_score_bonus}={adjusted_score:.0f}% < {effective_min}), skipping (STRICT MODE)")
            return None

        reasons     = list(score_result.reasons)
        base_score_before_override = score_result.total_score  # Сохраняем базовый скор
        final_score = min(100, score_result.total_score + max(0, base_score_bonus))  # ← БАЗОВЫЙ + БОНУСЫ от confirmation/TBS
        
        # 🆕 NEW: Market Data Integrator — полный рыночный контекст
        market_context_adjustment = 0
        if hasattr(state, 'market_integrator') and state.market_integrator:
            try:
                # Получаем полный контекст
                ctx = await state.market_integrator.get_full_context(symbol)
                
                # Логируем контекст
                await state.market_integrator.log_context(symbol, ctx, "long")
                
                # Рассчитываем корректировку
                adjustment = state.market_integrator.calculate_score_adjustment(ctx, "long")
                market_context_adjustment = adjustment["score_delta"]
                
                # Добавляем причины
                reasons.extend(adjustment["reasons"])
                
                # Блокировка если нужно
                if adjustment["should_block"]:
                    print(f"🔴 [MARKET-INTEGRATOR-LONG] {symbol}: BLOCKED — {adjustment['reasons'][:2]}")
                    return None
                
                # Применяем корректировку
                final_score += market_context_adjustment
                final_score = min(100, final_score)  # Cap at 100
                
                print(f"📊 [MARKET-INTEGRATOR-LONG] {symbol}: score adjusted by {market_context_adjustment:+d} "
                      f"(quality: {ctx.data_quality_score}%, confidence: {adjustment['confidence']})")
                
            except Exception as e:
                print(f"⚠️ [MARKET-INTEGRATOR-LONG] {symbol}: Error — {e}")

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

        # ✅ FIX: HTF (4H) Bias Filter — не лонговать против 4H тренда
        # Портировано из liquidity-bots-fixed 2 (v3.2)
        try:
            ohlcv_4h_bias = await state.binance.get_klines(symbol, "4h", 25)
            if ohlcv_4h_bias and len(ohlcv_4h_bias) >= 21:
                closes_4h_bias = [float(c.close if hasattr(c, 'close') else c[4]) for c in ohlcv_4h_bias]
                ema9_4h  = sum(closes_4h_bias[-9:]) / 9
                ema21_4h = sum(closes_4h_bias[-21:]) / 21
                if ema9_4h < ema21_4h * 0.995:  # 4H EMA9 < EMA21×0.995 = подтверждённый даунтренд
                    if final_score < 85:          # ✅ FIX v4: поднято с 75 → 85 (почти никогда не пропускать против тренда)
                        print(f"🚫 [HTF-4H-LONG] {symbol}: EMA9({ema9_4h:.4f}) < EMA21({ema21_4h:.4f}) — 4H даунтренд, score={final_score}<85 → skip")
                        return None
                    else:
                        print(f"⚠️ [HTF-4H-LONG] {symbol}: 4H даунтренд, но score={final_score}≥85 — редкий сигнал, разрешаем")
        except Exception as e:
            print(f"⚠️ [HTF-4H-LONG] {symbol}: ошибка проверки 4H тренда: {e}")

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
        price = md.price

        # ═══════════════════════════════════════════════════════════════════
        # ✅ v5: ATR-АДАПТИВНЫЙ SL — разные активы требуют разных стопов
        # BTC (ATR~1.5%) ≠ мем-коин (ATR~5%). Фиксированный 2% — ошибка.
        # Источник: symbol_profile.atr_14_pct (считается в symbol_profiler.py)
        # ═══════════════════════════════════════════════════════════════════
        atr_pct = 1.5  # дефолт если профиль недоступен
        vol_class = "medium"
        if symbol_profile and symbol_profile.atr_14_pct > 0:
            atr_pct   = symbol_profile.atr_14_pct
            vol_class = symbol_profile.volatility_class
        # Мультипликатор по волатильности: тихий рынок — меньше буфер, экстрем — больше
        atr_multiplier = {"low": 1.5, "medium": 2.0, "high": 2.5, "extreme": 3.0}.get(vol_class, 2.0)
        adaptive_sl_pct = round(min(max(atr_pct * atr_multiplier, 1.2), 8.0), 2)  # зажато: 1.2%–8%
        atr_price = price * atr_pct / 100          # ATR в единицах цены
        default_sl = price * (1 - adaptive_sl_pct / 100)
        print(f"📐 [SL-ATR] {symbol}: ATR={atr_pct:.2f}% vol={vol_class} mult={atr_multiplier}× → SL={adaptive_sl_pct:.2f}%")

        # ═══════════════════════════════════════════════════════════════════
        # ✅ v5: SWING LOW SL — структурный стоп за последний значимый минимум
        # Данные 4H уже получены выше (ohlcv_4h_bias). Цена ломает свинг = сценарий инвалидирован.
        # ═══════════════════════════════════════════════════════════════════
        swing_sl = None
        try:
            if ohlcv_4h_bias and len(ohlcv_4h_bias) >= 8:
                lows_4h = [float(c.low if hasattr(c, 'low') else c[2]) for c in ohlcv_4h_bias]
                # Свинг-лоу: локальный минимум с lookback=2 (ниже 2 соседей с каждой стороны)
                swing_lows = []
                for idx in range(2, len(lows_4h) - 2):
                    if lows_4h[idx] == min(lows_4h[idx - 2: idx + 3]):
                        swing_lows.append(lows_4h[idx])
                # Берём наивысший свинг-лоу НИЖЕ текущей цены (ближайшая структурная поддержка)
                valid = [sl for sl in swing_lows if sl < price * 0.997]
                if valid:
                    nearest_swing = max(valid)                           # ближайший снизу
                    swing_sl = nearest_swing - atr_price * 0.5          # -0.5×ATR буфер
                    print(f"📐 [SWING-SL] {symbol}: SwingLow={nearest_swing:.6f} → SL={swing_sl:.6f} (-0.5×ATR)")
        except Exception as e:
            pass

        # ═══════════════════════════════════════════════════════════════════
        # ✅ v5: SWEEP TAIL SL — за хвост ликвидностного свипа
        # ═══════════════════════════════════════════════════════════════════
        sweep_sl = None
        try:
            from core.liquidity_detector import LiquidityDetector
            ld = LiquidityDetector(_ohlcv(ohlcv_15m))
            sweep_result = ld.detect_sweep(direction="long")
            if sweep_result and sweep_result.found_sweep and sweep_result.sweep_low > 0:
                sweep_sl = sweep_result.sweep_low * 0.997
                print(f"🎯 [SWEEP-SL] {symbol}: sweep_low={sweep_result.sweep_low:.6f} → SL={sweep_sl:.6f}")
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════════════
        # ✅ Phase 8: WYCKOFF SPRING SL — SL за лоу ложного пробоя накопления
        # ═══════════════════════════════════════════════════════════════════
        wyckoff_sl = None
        try:
            wyckoff_pattern = next((p for p in patterns if p.name == "WYCKOFF_SPRING"), None)
            if wyckoff_pattern and wyckoff_pattern.suggested_sl_pct > 0:
                wyckoff_sl = price * (1 - wyckoff_pattern.suggested_sl_pct / 100)
                print(f"🌀 [WYCKOFF-SL] {symbol}: Spring → SL={wyckoff_sl:.6f} ({wyckoff_pattern.suggested_sl_pct:.2f}%)")
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════════════
        # ✅ Phase 7: XVP VOL PROFILE SL — SL под VAL (Value Area Low)
        # VAL = нижняя граница зоны стоимости (70% объёма) → сильная поддержка
        # ═══════════════════════════════════════════════════════════════════
        xvp_sl = None
        try:
            xvp_candles = []
            for c in (ohlcv_4h_bias or []):
                if hasattr(c, 'open'):
                    xvp_candles.append({"open": float(c.open), "high": float(c.high),
                                        "low": float(c.low), "close": float(c.close),
                                        "volume": float(getattr(c, 'volume', 0) or 0)})
                elif isinstance(c, (list, tuple)) and len(c) >= 6:
                    xvp_candles.append({"open": float(c[1]), "high": float(c[2]),
                                        "low": float(c[3]), "close": float(c[4]),
                                        "volume": float(c[5])})
            if len(xvp_candles) >= 10:
                xvp_result = get_xvp_analyzer().analyze(xvp_candles)
                if xvp_result.val > 0 and xvp_result.val < price * 0.99:
                    xvp_sl = xvp_result.val * 0.997   # чуть ниже VAL с буфером
                    print(f"📊 [XVP-SL] {symbol}: VAL={xvp_result.val:.6f} POC={xvp_result.poc:.6f} → SL={xvp_sl:.6f}")
        except Exception as e:
            print(f"[XVP-SL] {symbol} error: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # ✅ v5: ИЕРАРХИЯ ВЫБОРА SL (Институциональный стандарт):
        # 1. Swing Low (структура 4H)      — структурная инвалидация
        # 2. Sweep Tail (ликвидность 15m)  — за хвост ликвидности
        # 3. Wyckoff Spring Low            — за ложный пробой
        # 4. XVP VAL (Volume Profile)      — за зону стоимости
        # 5. ATR-adaptive default          — адаптивный фолбэк
        # Потом SMC может уточнить через OB.low или FVG.lower
        # Финальная проверка: SL в диапазоне [0.8×ATR — 4×ATR]
        # ═══════════════════════════════════════════════════════════════════
        min_sl_dist = atr_price * 0.8   # минимум: 0.8×ATR от цены
        max_sl_dist = atr_price * 4.0   # максимум: 4×ATR от цены

        if swing_sl and min_sl_dist <= (price - swing_sl) <= max_sl_dist:
            stop_loss = swing_sl
            reasons.append(f"📐 Swing Low SL: ${stop_loss:.6f} ({adaptive_sl_pct:.1f}%)")
        elif sweep_sl and min_sl_dist <= (price - sweep_sl) <= max_sl_dist:
            stop_loss = sweep_sl
            reasons.append(f"🎯 Sweep Tail SL: ${stop_loss:.6f}")
        elif wyckoff_sl and min_sl_dist <= (price - wyckoff_sl) <= max_sl_dist:
            stop_loss = wyckoff_sl
            reasons.append(f"🌀 Wyckoff Spring SL: ${stop_loss:.6f}")
        elif xvp_sl and min_sl_dist <= (price - xvp_sl) <= max_sl_dist:
            stop_loss = xvp_sl
            reasons.append(f"📊 XVP VAL SL: ${stop_loss:.6f}")
        else:
            stop_loss = default_sl
            reasons.append(f"📊 ATR SL: ${stop_loss:.6f} ({adaptive_sl_pct:.1f}%)")
            
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
                # ✅ FIX v4: для LONG ob_entry должен быть НИЖЕ текущей цены
                # Если OB выше цены — это пропущенная зона (цена уже ушла), не гоняться!
                if smc.ob_entry and smc.ob_entry < price * 1.001:  # не более +0.1% от цены
                    entry_price = smc.ob_entry
                elif smc.ob_entry and smc.ob_entry >= price * 1.001:
                    print(f"⚠️ [SMC-OB-SKIP] {symbol}: ob_entry={smc.ob_entry:.6f} > price={price:.6f} — OB выше цены, entry=price")
                smc_data = {"has_ob": smc.has_ob, "has_fvg": smc.has_fvg,
                            "score_bonus": smc.score_bonus}
            except Exception as e:
                print(f"SMC error {symbol}: {e}")

        # 🌊 Phase 3 + Phase 5: EQH/EQL Scanner — детекция пулов ликвидности + буфер SL
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
            # ✅ Phase 5: EQL Buffer — если SL сидит НА уровне retail-ловушки,
            # смещаем его ЗА EQL зону, чтобы manipulation spike не вынес нас
            if pool_scan.nearest_eql and pool_scan.nearest_eql.level < price * 0.995:
                eql_level = pool_scan.nearest_eql.level
                # Если наш SL выше EQL + 0.3% (т.е. между ценой и EQL — розничная ловушка)
                if stop_loss > eql_level * 1.003:
                    new_sl = eql_level * 0.997   # за EQL с буфером 0.3%
                    sl_new_dist = price - new_sl
                    if min_sl_dist <= sl_new_dist <= max_sl_dist:
                        print(f"🌊 [EQL-BUF] {symbol}: SL {stop_loss:.6f} в ловушке EQL {eql_level:.6f} → смещён до {new_sl:.6f}")
                        stop_loss = new_sl
                        reasons.append(f"🌊 EQL Buffer SL: ${new_sl:.6f} (за ликвидную зону)")
        except Exception as e:
            print(f"🌊 [v2.9] Pool scan error {symbol}: {e}")

        # 🆕 Aegis: Z-Score Pump/Dump Detection
        try:
            pump_result = detect_pump(_ohlcv(ohlcv_15m), direction="long")
            if pump_result.detected and pump_result.signal_type == "dump":
                # Для LONG: дамп (Z < -2) = хороший вход
                z_bonus = min(15, int(abs(pump_result.z_score) * 3))  # Z=-3 → +9
                final_score = min(100, final_score + z_bonus)
                reasons.append(f"📉 Z-Score Dump: {pump_result.z_score:.1f}σ (RSI {pump_result.rsi:.0f}) +{z_bonus}")
                print(f"🎯 [AEGIS-Z] {symbol}: DUMP detected Z={pump_result.z_score:.2f}, +{z_bonus} to score")
            elif pump_result.z_score > 2.0:
                # Для LONG: памп (Z > 2) = плохой вход, штраф
                z_penalty = min(10, int(pump_result.z_score * 2))
                final_score = max(0, final_score - z_penalty)
                reasons.append(f"⚠️ Z-Score Pump: +{pump_result.z_score:.1f}σ — штраф -{z_penalty}")
        except Exception as e:
            print(f"[AEGIS-Z] {symbol} error: {e}")

        # 🆕 Aegis: Delta Analyzer (CVD + Order Flow)
        try:
            delta_result = analyze_delta(_ohlcv(ohlcv_15m), direction="long")
            if delta_result.score >= 30:
                # Для LONG: bullish divergence или buy imbalance
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
                        # Для LONG: дамп + дно = отличный вход
                        bottom_bonus = min(20, int(dump_result.confidence / 5))
                        final_score = min(100, final_score + bottom_bonus)
                        reasons.append(f"📈 Dump Bottoming: {dump_result.dump_type.value} +{bottom_bonus}")
                        print(f"🎯 [DUMP-LONG] {symbol}: BOTTOM detected after {dump_result.dump_type.value}, +{bottom_bonus}")
                    else:
                        # Для LONG: дамп без дна = опасно, штраф
                        dump_penalty = min(15, int(dump_result.confidence / 6))
                        final_score = max(0, final_score - dump_penalty)
                        reasons.append(f"⚠️ Active Dump: {dump_result.dump_type.value} -{dump_penalty}")
                        print(f"⚠️ [DUMP-LONG] {symbol}: Active dump {dump_result.dump_type.value}, -{dump_penalty}")
        except Exception as e:
            print(f"[DUMP-LONG] {symbol} error: {e}")

        if final_score < Config.MIN_SCORE:
            print(f"🔴 [FILTER2-SMC-LONG] {symbol}: score={final_score} < MIN={Config.MIN_SCORE} — отфильтрован!")
            return None

        # ✅ v5: SMC может уточнить SL через OB.low / FVG.lower
        # Принимаем только если укладывается в ATR-диапазон
        if stop_loss != default_sl and Config.USE_SMC:
            pass  # SMC уже обработан выше
        # Дополнительно: если SMC дал refined_sl — проверяем его качество
        sl_dist = price - stop_loss
        if sl_dist < min_sl_dist:
            print(f"⚠️ [SL-CLAMP-MIN] {symbol}: SL слишком близко ({sl_dist/price*100:.2f}% < {min_sl_dist/price*100:.2f}%) → ATR default")
            stop_loss = default_sl
        elif sl_dist > max_sl_dist:
            print(f"⚠️ [SL-CLAMP-MAX] {symbol}: SL слишком далеко ({sl_dist/price*100:.2f}% > {max_sl_dist/price*100:.2f}%) → 4×ATR cap")
            stop_loss = price - max_sl_dist

        # ✅ FIX: TP ВЫШЕ входа для LONG
        take_profits = [
            (round(price * (1 + tp / 100), 8), tp_weights[i] if i < len(tp_weights) else 15)
            for i, tp in enumerate(tp_levels)
        ]

        sl_pct = round((price - stop_loss) / price * 100, 2)

        # ═══════════════════════════════════════════════════════════════════
        # ✅ v5: RR GATE — не брать сделку если Risk:Reward < 1.5
        # Золотое правило: TP1 должен быть минимум в 1.5× дальше чем SL
        # ═══════════════════════════════════════════════════════════════════
        if take_profits and sl_pct > 0:
            tp1_price = take_profits[0][0]
            tp1_pct = (tp1_price - price) / price * 100
            rr_ratio = tp1_pct / sl_pct if sl_pct > 0 else 0
            if rr_ratio < 1.5:
                print(f"🚫 [RR-GATE] {symbol}: RR={rr_ratio:.2f} (TP1={tp1_pct:.2f}% / SL={sl_pct:.2f}%) < 1.5 → сделку не брать")
                return None
            print(f"✅ [RR-GATE] {symbol}: RR={rr_ratio:.2f} ≥ 1.5 — OK")
        
        # ✅ FIX: Проверка SMC паттерна — сигнал только при наличии структуры
        # 🆕 Aegis: Z-Score и Delta теперь тоже считаются валидными паттернами!
        has_smc_pattern = (
            patterns or  # Есть паттерн от pattern_detector
            (ob_quality >= 60) or  # Есть качественный Order Block
            tbs_found or  # Есть TBS (Test Before Strike)
            (pool_data.get("active_sweeps", 0) > 0) or  # Есть активный sweep ликвидности
            # 🆕 Aegis: Сильный Z-Score дамп тоже паттерн!
            (pump_result.detected if 'pump_result' in locals() else False) or
            # 🆕 Aegis: Сильная дивергенция CVD тоже паттерн!
            (delta_result.score >= 40 if 'delta_result' in locals() else False)
        )
        
        if not has_smc_pattern:
            print(f"🔴 [FILTER-SMC-LONG] {symbol}: Нет SMC паттерна — сигнал отменён!")
            return None
        
        # Определяем лучший паттерн для отображения
        if not best_pattern:
            if ob_quality >= 60:
                best_pattern = f"OB_Q{ob_quality}"
            elif tbs_found:
                best_pattern = "TBS"
            elif pool_data.get("active_sweeps", 0) > 0:
                best_pattern = "LIQUIDITY_SWEEP"
            # 🆕 Aegis: Z-Score паттерны
            elif 'pump_result' in locals() and pump_result.detected:
                best_pattern = f"Z-SCORE_{pump_result.signal_type.upper()}"
            # 🆕 Aegis: Delta/CVD паттерны
            elif 'delta_result' in locals() and delta_result.score >= 40:
                best_pattern = f"CVD_{delta_result.divergence.upper()}"
            else:
                best_pattern = "SMC_STRUCTURE"
        
        print(f"🟢 [SIGNAL-LONG] {symbol}: score={final_score} pattern={best_pattern} — сигнал создан!")
        return {
            "symbol": symbol, "direction": "long",
            "score": final_score, "grade": score_result.grade,
            "confidence": score_result.confidence.value,
            # ✅ Честное отображение скора
            "base_score": base_score_before_override,
            "override_used": override_used,
            "override_type": override_type,
            "price": price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 8), "sl_pct": sl_pct,
            "take_profits": take_profits,
            "patterns": [p.name for p in patterns],
            "best_pattern": best_pattern,
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
            # ✅ FIX: quote_volume нужен в scan_market() для volume-фильтра (поле = volume_24h)
            "quote_volume_24h": getattr(md, 'volume_24h', 0),
        }
    except Exception as e:
        import traceback
        print(f"🔴 [SCAN-LONG-ERROR] {symbol}: {e}")
        print(f"🔴 [SCAN-LONG-ERROR] {symbol}: {traceback.format_exc()[:500]}")
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


_scan_lock = None  # asyncio.Lock — инициализируется лениво в event loop

async def scan_market():
    """
    ✅ v2.7 АРХИТЕКТУРА (NO BTC CORR):
    - Telegram сигналы: ВСЕГДА при score >= MIN_SCORE (даже при 20/20)
    - Биржевое исполнение: только если active_count < MAX и не /pause
    - Единственный блокер: команда /pause
    """
    # ✅ FIX v4.1: Предотвращаем параллельный запуск scan_market (петля WIFUSDT/BRETTUSDT)
    global _scan_lock
    if _scan_lock is None:
        _scan_lock = asyncio.Lock()
    if _scan_lock.locked():
        print(f"⚠️ [SCAN] Previous scan still running — skipping concurrent invocation")
        return

    async with _scan_lock:
        await _scan_market_impl()


async def _scan_market_impl():
    """Внутренняя реализация scan_market — вызывается только через scan_market()."""
    print(f"🔬 [SCAN-MARKET-ENTRY] is_paused={state.is_paused}, is_running={state.is_running}")  # DEBUG
    if state.is_paused:
        print(f"🔬 [SCAN-MARKET-ENTRY] SKIPPING: bot is paused!")  # DEBUG
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

            print(f"🔍 [DEBUG-LONG] {symbol}: calling scan_symbol...")  # DEBUG
            signal = await scan_symbol(symbol)
            if not signal:
                print(f"🔍 [DEBUG-LONG] {symbol}: scan_symbol returned None")  # DEBUG
                continue
            print(f"🔍 [DEBUG-LONG] {symbol}: scan_symbol returned signal! score={signal.get('score', 0)}")  # DEBUG

            # ✅ ВСЕГДА: Telegram сигнал (с честным отображением скора)
            tg_msg_id = await state.telegram.send_signal(
                direction="long", symbol=signal["symbol"],
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

            # 📊 SIGNAL LOG: Запись всех сигналов (исполненных + пропущенных)
            _signal_log_entry = {**signal, "executed": False, "skip_reason": None}

            # 🆕 STRICT: Проверка объема перед входом
            quote_volume = signal.get("quote_volume_24h", 0)  # ✅ FIX: md не доступен в scan_market()
            if quote_volume < Config.MIN_ENTRY_VOLUME_USDT:
                print(f"📊 [VOLUME-FILTER] {symbol}: ${quote_volume/1e6:.1f}M < ${Config.MIN_ENTRY_VOLUME_USDT/1e6:.0f}M — skip")
                _signal_log_entry["skip_reason"] = "volume_too_low"
                try: state.redis.save_signal_log(Config.BOT_TYPE, _signal_log_entry)
                except Exception: pass
                continue

            # 🆕 BTC FILTER: Не лонгить если BTC падает (ловим ножи)
            btc_trend_ok = True
            if hasattr(state, 'btc_context') and state.btc_context:
                btc_data = state.btc_context.get('trend', {})
                if btc_data.get('direction') == 'DOWN' and btc_data.get('strength', 0) > 0.6:
                    btc_trend_ok = False
                    print(f"📊 [BTC-FILTER] {symbol}: BTC falling hard — skip LONG")

            # ✅ Биржевое исполнение: только если есть слоты И не на паузе И BTC ок
            if not exchange_full and Config.AUTO_TRADING and not state.is_paused and btc_trend_ok:
                if state.auto_trader:
                    try:
                        _trade_result = await state.auto_trader.execute_signal(signal)
                        # ✅ FIX: считаем слот ТОЛЬКО если сделка реально открылась
                        if _trade_result is not None:
                            active_count += 1
                            exchange_full = active_count >= Config.MAX_POSITIONS
                            new_signals += 1
                            _signal_log_entry["executed"] = True
                            _signal_log_entry["executed_at"] = datetime.utcnow().isoformat()
                            print(f"✅ LONG executed: {symbol} Score={signal['score']:.0f}% SL={signal['sl_pct']}%")
                        else:
                            _signal_log_entry["skip_reason"] = "trader_rejected"
                            print(f"⚠️ LONG skipped (AutoTrader rejected — trend/cooldown/balance/duplicate): {symbol}")
                    except Exception as e:
                        _signal_log_entry["skip_reason"] = "error"
                        print(f"AutoTrader error {symbol}: {e}")
            elif not btc_trend_ok:
                tg_only_count += 1
                _signal_log_entry["skip_reason"] = "btc_falling"
                print(f"📡 LONG TG-only: {symbol} [BTC falling]")
            else:
                tg_only_count += 1
                if exchange_full:
                    _signal_log_entry["skip_reason"] = "exchange_full"
                    reason = "max positions"
                elif not Config.AUTO_TRADING:
                    _signal_log_entry["skip_reason"] = "auto_trading_disabled"
                    reason = "auto trading disabled"
                else:
                    _signal_log_entry["skip_reason"] = "paused"
                    reason = "paused"
                print(f"📡 LONG TG-only: {symbol} Score={signal['score']:.0f}% [{reason}]")

            # 📊 Сохраняем в постоянный лог сигналов
            try:
                state.redis.save_signal_log(Config.BOT_TYPE, _signal_log_entry)
            except Exception:
                pass

            # 🔍 Если не исполнено на бирже — сохраняем как виртуальную позицию для мониторинга TP/SL
            if not _signal_log_entry.get("executed"):
                try:
                    state.redis.save_virtual_position(Config.BOT_TYPE, symbol, _signal_log_entry)
                except Exception:
                    pass

            await asyncio.sleep(1.0)  # ✅ FIX: 0.4→1.0s — снижаем Bybit rate limit
        except Exception as e:
            print(f"Error {symbol}: {e}")

    # 🆕 NEW: Momentum Scanning (Trend Following)
    if Config.ENABLE_MOMENTUM_LONG and hasattr(state, 'momentum_detector'):
        try:
            print(f"\n🚀 [MOMENTUM-LONG] Scanning {len(state.watchlist)} symbols for velocity...")
            
            async def get_1m_candles(symbol, tf, limit):
                return await state.binance.get_recent_candles(symbol, timeframe="1m", limit=30)
            
            momentum_signals = await state.momentum_detector.scan_watchlist(
                state.watchlist,
                get_1m_candles,
                min_volume_usdt=100000,  # Ниже порог для мемов
                debug=True  # 🔍 DEBUG: показывать почему отсеиваются монеты
            )
            
            if momentum_signals:
                print(f"🎯 [MOMENTUM-LONG] Found {len(momentum_signals)} momentum signals")
                
                for mom_sig in momentum_signals:
                    try:
                        symbol = mom_sig.symbol
                        
                        # Дедупликация: не повторяем недавний сигнал
                        if _is_fresh(state.redis.get_signals(Config.BOT_TYPE, symbol, limit=1)):
                            continue
                        
                        # Преобразуем momentum signal в формат обычного сигнала
                        signal = {
                            "symbol": symbol,
                            "direction": "long",
                            "score": mom_sig.score,
                            "price": mom_sig.entry_price,
                            "entry_price": mom_sig.entry_price,
                            "stop_loss": round(mom_sig.entry_price * (1 - Config.MOMENTUM_LONG_SL_BUFFER/100), 6),
                            "take_profits": [
                                round(mom_sig.entry_price * 1.015, 6),  # TP1 1.5%
                                round(mom_sig.entry_price * 1.03, 6),   # TP2 3%
                            ],
                            "sl_pct": Config.MOMENTUM_LONG_SL_BUFFER,
                            "best_pattern": "momentum_velocity",
                            "indicators": {
                                "rsi": round(mom_sig.rsi, 1),
                                "volume_spike": round(mom_sig.volume_spike, 1),
                                "velocity_1m": round(mom_sig.change_1m, 2),
                                "factors": mom_sig.factors
                            },
                            "signal_type": "momentum",
                            "risk_per_trade": Config.MOMENTUM_LONG_RISK_PER_TRADE,
                        }
                        
                        # Telegram сигнал
                        tg_msg_id = await state.telegram.send_signal(
                            direction="long", symbol=symbol,
                            score=signal["score"], price=signal["price"],
                            pattern="🚀 MOMENTUM",
                            indicators=signal["indicators"],
                            entry=signal["entry_price"],
                            stop_loss=signal["stop_loss"],
                            take_profits=signal["take_profits"],
                            leverage=Config.LEVERAGE, risk="≤0.5% deposit",
                        )
                        signal["tg_msg_id"] = tg_msg_id
                        state.redis.save_signal(Config.BOT_TYPE, symbol, signal)

                        # 📊 SIGNAL LOG для momentum
                        _mom_log_entry = {**signal, "executed": False, "skip_reason": None, "signal_type": "momentum"}

                        # Биржевое исполнение momentum сигналов (только если есть слоты)
                        if not exchange_full and Config.AUTO_TRADING and not state.is_paused:
                            if state.auto_trader:
                                try:
                                    # Momentum использует отдельный риск!
                                    await state.auto_trader.execute_momentum_signal(signal)
                                    active_count += 1
                                    exchange_full = active_count >= Config.MAX_POSITIONS
                                    new_signals += 1
                                    _mom_log_entry["executed"] = True
                                    _mom_log_entry["executed_at"] = datetime.utcnow().isoformat()
                                    print(f"✅ [MOMENTUM-LONG] Executed: {symbol} Score={signal['score']:.0f}%")
                                except Exception as e:
                                    _mom_log_entry["skip_reason"] = "error"
                                    print(f"❌ [MOMENTUM-LONG] Error {symbol}: {e}")
                        else:
                            tg_only_count += 1
                            if exchange_full:
                                _mom_log_entry["skip_reason"] = "exchange_full"
                            elif not Config.AUTO_TRADING:
                                _mom_log_entry["skip_reason"] = "auto_trading_disabled"
                            else:
                                _mom_log_entry["skip_reason"] = "paused"
                            print(f"📡 [MOMENTUM-LONG] TG-only: {symbol}")

                        try:
                            state.redis.save_signal_log(Config.BOT_TYPE, _mom_log_entry)
                        except Exception:
                            pass

                        # Виртуальная позиция для не-исполненных momentum сигналов
                        if not _mom_log_entry.get("executed"):
                            try:
                                state.redis.save_virtual_position(Config.BOT_TYPE, symbol, _mom_log_entry)
                            except Exception:
                                pass

                    except Exception as e:
                        print(f"❌ [MOMENTUM-LONG] Signal error: {e}")
                        
        except Exception as e:
            print(f"❌ [MOMENTUM-LONG] Scanner error: {e}")
    
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


# =============================================================================
# 🔍 VIRTUAL TP/SL MONITOR — следит за виртуальными позициями (TG-only сигналы)
# =============================================================================

async def virtual_position_monitor():
    """
    Фоновая задача: каждую минуту проверяет цену по всем виртуальным позициям.
    Если цена достигла TP1 или SL — закрывает позицию с outcome tp/sl.
    Если позиция висит >24 часов — закрывает как expired.
    """
    await asyncio.sleep(30)  # небольшая пауза при старте
    while state.is_running:
        try:
            virtual_positions = state.redis.get_virtual_positions(Config.BOT_TYPE)
            if virtual_positions:
                print(f"🔍 [VIRTUAL-LONG] Monitoring {len(virtual_positions)} virtual positions")

                # ✅ FIX: Дедупликация символов — получаем цену каждого символа ОДИН РАЗ
                # Было: 38 позиций → 38 get_price вызовов без паузы → rate limit
                # Стало: ~20 уникальных символов с паузой 0.3s = ~6 сек
                unique_symbols = list({
                    pos.get("symbol") for pos in virtual_positions.values()
                    if pos.get("symbol")
                })
                prices_cache: Dict[str, float] = {}
                for sym in unique_symbols:
                    try:
                        p = await state.binance.get_price(sym)
                        if p and p > 0:
                            prices_cache[sym] = p
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)  # пауза между запросами цены

                for field, pos in list(virtual_positions.items()):
                    try:
                        symbol        = pos.get("symbol")
                        entry_price   = float(pos.get("entry_price") or 0)
                        stop_loss     = float(pos.get("stop_loss") or 0)
                        take_profits  = pos.get("take_profits") or []
                        direction     = pos.get("direction", "long")
                        opened_at_str = pos.get("virtual_opened_at", "")

                        if not symbol or entry_price <= 0:
                            continue

                        # Проверяем истечение 24 часов
                        outcome = None
                        if opened_at_str:
                            try:
                                opened_at = datetime.fromisoformat(opened_at_str)
                                if (datetime.utcnow() - opened_at).total_seconds() > 86400:
                                    outcome = "expired"
                            except Exception:
                                pass

                        if outcome is None:
                            # ✅ FIX: Берём из кэша цен (не вызываем API повторно!)
                            current_price = prices_cache.get(symbol, 0)
                            if not current_price or current_price <= 0:
                                continue

                            # ✅ FIX: take_profits[0] может быть float, list [price,weight] или dict
                            tp1 = None
                            if take_profits:
                                tp_item = take_profits[0]
                                if isinstance(tp_item, (list, tuple)):
                                    tp1 = float(tp_item[0])
                                elif isinstance(tp_item, dict):
                                    tp1 = float(tp_item.get("price", 0)) or None
                                else:
                                    tp1 = float(tp_item)

                            if direction == "long":
                                if stop_loss > 0 and current_price <= stop_loss:
                                    outcome = "sl"
                                elif tp1 and current_price >= tp1:
                                    outcome = "tp"
                            else:  # short
                                if stop_loss > 0 and current_price >= stop_loss:
                                    outcome = "sl"
                                elif tp1 and current_price <= tp1:
                                    outcome = "tp"
                        else:
                            current_price = entry_price  # для expired берём entry

                        if outcome:
                            # PnL расчёт
                            try:
                                lev_str = str(pos.get("leverage", "10")).split("-")[0]
                                leverage = float(lev_str) if lev_str.replace(".", "").isdigit() else 10.0
                            except Exception:
                                leverage = 10.0
                            change_pct = (current_price - entry_price) / entry_price * 100
                            if direction == "short":
                                change_pct = -change_pct
                            pnl_pct = round(change_pct * leverage, 2)

                            state.redis.close_virtual_position(
                                Config.BOT_TYPE, field, outcome, current_price, pnl_pct
                            )
                            emoji = "✅" if outcome == "tp" else ("⏰" if outcome == "expired" else "❌")
                            print(f"{emoji} [VIRTUAL-LONG] {symbol}: {outcome} @ {current_price:.6f} | PnL={pnl_pct:+.1f}%")

                    except Exception as e:
                        print(f"[VIRTUAL-LONG] Error checking {field}: {e}")

        except Exception as e:
            print(f"[VIRTUAL-LONG] Monitor error: {e}")

        await asyncio.sleep(60)  # проверяем каждую минуту


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 8000)), reload=False)
