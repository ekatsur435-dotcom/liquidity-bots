import os
"""
Market Context Filter v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Функции:
  1. BTC Correlation Guard   — блокирует LONG входы при BTC падении > -2% за 1ч
  2. Time Session Filter     — блокирует входы в 03:00-06:00 UTC (азиатская сессия)
  3. Daily P&L Stop          — прекращает торговлю при дневном убытке > X%
  4. Market Regime Detector  — определяет тренд/боковик/медвежий рынок
  5. Altcoin Decoupling Check — проверяет что альткоин движется независимо от BTC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass
from enum import Enum


class MarketRegime(Enum):
    BULL_TREND   = "bull_trend"      # Сильный восходящий тренд
    BEAR_TREND   = "bear_trend"      # Сильный нисходящий тренд
    SIDEWAYS     = "sideways"        # Боковик/консолидация
    HIGH_VOLATILITY = "high_volatility"  # Высокая волатильность
    UNKNOWN      = "unknown"


@dataclass
class MarketContextResult:
    allowed: bool           # Разрешён ли вход
    block_reason: str       # Причина блокировки (если есть)
    regime: MarketRegime    # Режим рынка
    btc_change_1h: float    # Изменение BTC за 1ч (%)
    btc_change_4h: float    # Изменение BTC за 4ч (%)
    is_asian_session: bool  # Азиатская сессия (03-06 UTC)
    daily_pnl_pct: float    # Дневной P&L (%)
    altcoin_decoupled: bool # Альткоин движется независимо от BTC
    warnings: List[str]     # Предупреждения (не блокировки)


class MarketContextFilter:
    """
    Глобальный фильтр рыночного контекста.
    Вызывается ПЕРЕД любым входом в позицию.
    """

    # ── Настройки блокировки (конфигурируются через .env) ───────────────────
    # BTC_CORRELATION_FILTER=false → полностью выключить (по умолчанию ВЫКЛ)
    # BTC_DROP_THRESHOLD=-3        → порог падения BTC для блокировки
    # DAILY_LOSS_STOP_PCT=-5       → дневной стоп (%)
    BTC_FILTER_ENABLED        = os.getenv("BTC_CORRELATION_FILTER", "false").lower() == "true"
    BTC_CRASH_THRESHOLD_LONG  = float(os.getenv("BTC_DROP_THRESHOLD", "-3.0"))   # По умолч. -3%
    BTC_CRASH_THRESHOLD_SHORT = float(os.getenv("BTC_RISE_THRESHOLD", "3.0"))    # ✅ v5.0: порог для SHORT (по умолч. +3%)
    BTC_PUMP_THRESHOLD_LONG   =  5.0   # BTC рост 1ч > 5% → осторожно лонг
    BTC_DUMP_THRESHOLD_SHORT  = -5.0   # ✅ v5.0: BTC падение > 5% → осторожно с шортами
    DAILY_LOSS_STOP_PCT       = float(os.getenv("DAILY_LOSS_STOP_PCT", "-5.0"))
    ASIAN_SESSION_BLOCK       = os.getenv("BLOCK_ASIAN_SESSION", "true").lower() == "true" 

    # UTC часы азиатской сессии (низкая ликвидность, много ложных пробоев)
    ASIAN_SESSION_START = 3   # 03:00 UTC
    ASIAN_SESSION_END   = 6   # 06:00 UTC

    def __init__(self, binance_client=None, redis_client=None):
        self._binance = binance_client
        self._redis   = redis_client
        self._btc_cache: Dict = {}      # Кеш BTC данных
        self._cache_ttl = 60            # Кеш на 60 секунд
        self._last_fetch = 0.0

    # ── BTC Данные ────────────────────────────────────────────────────────────

    async def _get_btc_changes(self) -> Tuple[float, float]:
        """
        Получает изменение BTC за 1ч и 4ч.
        Возвращает (change_1h_pct, change_4h_pct).
        Кешируется на 60 секунд.
        """
        import time
        now = time.time()
        if now - self._last_fetch < self._cache_ttl and self._btc_cache:
            return (
                self._btc_cache.get("change_1h", 0.0),
                self._btc_cache.get("change_4h", 0.0)
            )

        if not self._binance:
            return 0.0, 0.0

        try:
            # 1ч свечи BTC (последние 5 для надёжности)
            klines_1h = await self._binance.get_klines("BTCUSDT", "1h", 5)
            # 4ч свечи BTC (последние 3)
            klines_4h = await self._binance.get_klines("BTCUSDT", "4h", 3)

            change_1h = 0.0
            change_4h = 0.0

            if klines_1h and len(klines_1h) >= 2:
                open_1h  = float(klines_1h[-2].open  if hasattr(klines_1h[-2], 'open')  else klines_1h[-2][1])
                close_1h = float(klines_1h[-1].close if hasattr(klines_1h[-1], 'close') else klines_1h[-1][4])
                if open_1h > 0:
                    change_1h = (close_1h - open_1h) / open_1h * 100

            if klines_4h and len(klines_4h) >= 2:
                open_4h  = float(klines_4h[-2].open  if hasattr(klines_4h[-2], 'open')  else klines_4h[-2][1])
                close_4h = float(klines_4h[-1].close if hasattr(klines_4h[-1], 'close') else klines_4h[-1][4])
                if open_4h > 0:
                    change_4h = (close_4h - open_4h) / open_4h * 100

            self._btc_cache = {"change_1h": change_1h, "change_4h": change_4h}
            self._last_fetch = now
            return change_1h, change_4h

        except Exception as e:
            print(f"⚠️ [MarketContext] BTC fetch error: {e}")
            return 0.0, 0.0

    # ── Сессионный фильтр ─────────────────────────────────────────────────────

    @staticmethod
    def _is_asian_session() -> bool:
        """Проверяет азиатскую сессию 03:00-06:00 UTC"""
        utc_hour = datetime.now(timezone.utc).hour
        return MarketContextFilter.ASIAN_SESSION_START <= utc_hour < MarketContextFilter.ASIAN_SESSION_END

    # ── Дневной P&L ───────────────────────────────────────────────────────────

    def _get_daily_pnl(self) -> float:
        """Получает дневной P&L из Redis"""
        if not self._redis:
            return 0.0
        try:
            key = f"daily_pnl:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            val = self._redis.get(key)
            return float(val) if val else 0.0
        except Exception:
            return 0.0

    def update_daily_pnl(self, pnl_delta: float, direction: str = "long"):
        """Обновляет дневной P&L после закрытия сделки"""
        if not self._redis:
            return
        try:
            key = f"daily_pnl:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            current = self._get_daily_pnl()
            new_val = current + pnl_delta
            self._redis.set(key, str(new_val), ex=86400)  # TTL 24h
            print(f"📊 [MarketContext] Daily PnL updated: {current:.2f}% → {new_val:.2f}%")
        except Exception as e:
            print(f"⚠️ [MarketContext] Daily PnL update error: {e}")

    # ── Market Regime ─────────────────────────────────────────────────────────

    async def detect_market_regime(self, symbol: str = "BTCUSDT") -> MarketRegime:
        """
        Определяет режим рынка по BTC:
        - EMA тренд (EMA20 vs EMA50)
        - Волатильность (ATR)
        - Направление 4ч
        """
        if not self._binance:
            return MarketRegime.UNKNOWN

        try:
            klines = await self._binance.get_klines(symbol, "4h", 60)
            if not klines or len(klines) < 20:
                return MarketRegime.UNKNOWN

            def get_close(k):
                return float(k.close if hasattr(k, 'close') else k[4])

            closes = [get_close(k) for k in klines]
            
            # EMA расчёт
            def ema(data: List[float], period: int) -> float:
                k = 2 / (period + 1)
                e = data[0]
                for price in data[1:]:
                    e = price * k + e * (1 - k)
                return e

            ema20 = ema(closes[-20:], 20)
            ema50 = ema(closes[-50:], 50) if len(closes) >= 50 else closes[-1]
            current = closes[-1]

            # Изменение за 4 свечи (16ч)
            change_16h = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0

            # ATR упрощённый
            highs  = [float(k.high  if hasattr(k, 'high')  else k[2]) for k in klines[-14:]]
            lows   = [float(k.low   if hasattr(k, 'low')   else k[3]) for k in klines[-14:]]
            ranges = [highs[i] - lows[i] for i in range(len(highs))]
            atr = sum(ranges) / len(ranges) if ranges else 0
            atr_pct = atr / current * 100 if current > 0 else 0

            if atr_pct > 5.0:
                return MarketRegime.HIGH_VOLATILITY
            if current > ema20 > ema50 and change_16h > 1.0:
                return MarketRegime.BULL_TREND
            if current < ema20 < ema50 and change_16h < -1.0:
                return MarketRegime.BEAR_TREND
            return MarketRegime.SIDEWAYS

        except Exception as e:
            print(f"⚠️ [MarketContext] Regime detection error: {e}")
            return MarketRegime.UNKNOWN

    # ── Altcoin Decoupling ────────────────────────────────────────────────────

    async def check_altcoin_decoupling(self, symbol: str,
                                        btc_change_1h: float) -> bool:
        """
        Проверяет движется ли альткоин независимо от BTC.
        Если BTC падает, но альт растёт — высокая вероятность независимого движения.
        Возвращает True = альткоин декоррелирован.
        """
        if not self._binance or abs(btc_change_1h) < 0.5:
            return False  # BTC почти не двигается — нет смысла проверять

        try:
            klines = await self._binance.get_klines(symbol, "1h", 3)
            if not klines or len(klines) < 2:
                return False

            def get_close(k):
                return float(k.close if hasattr(k, 'close') else k[4])
            def get_open(k):
                return float(k.open if hasattr(k, 'open') else k[1])

            alt_change = (get_close(klines[-1]) - get_open(klines[-2])) / get_open(klines[-2]) * 100
            
            # Разнонаправленное движение = декорреляция
            if btc_change_1h < -1.0 and alt_change > 0.5:
                return True   # BTC падает, альт растёт
            if btc_change_1h > 1.0 and alt_change < -0.5:
                return True   # BTC растёт, альт падает

            return False
        except Exception:
            return False

    # ── Основной метод проверки ───────────────────────────────────────────────

    async def check(self,
                    direction: str = "long",
                    symbol: str = "UNKNOWN",
                    block_asian_session: bool = True,
                    allow_decoupled_alts: bool = True) -> MarketContextResult:
        """
        Главный метод. Вызывать перед каждым входом.
        
        Параметры:
            direction: "long" или "short"
            symbol: символ монеты (для проверки декорреляции)
            block_asian_session: блокировать азиатскую сессию
            allow_decoupled_alts: разрешить декоррелированные альты
        
        Возвращает MarketContextResult.
        """
        warnings = []
        block_reason = ""

        # 1. Дневной P&L стоп
        daily_pnl = self._get_daily_pnl()
        if daily_pnl <= self.DAILY_LOSS_STOP_PCT:
            return MarketContextResult(
                allowed=False,
                block_reason=f"🛑 Дневной стоп: P&L={daily_pnl:.2f}% (лимит {self.DAILY_LOSS_STOP_PCT}%)",
                regime=MarketRegime.UNKNOWN,
                btc_change_1h=0.0,
                btc_change_4h=0.0,
                is_asian_session=self._is_asian_session(),
                daily_pnl_pct=daily_pnl,
                altcoin_decoupled=False,
                warnings=warnings
            )

        # 2. Азиатская сессия
        asian = self._is_asian_session()
        if asian and block_asian_session:
            utc_now = datetime.now(timezone.utc)
            return MarketContextResult(
                allowed=False,
                block_reason=f"🌙 Азиатская сессия {utc_now.strftime('%H:%M')} UTC — низкая ликвидность",
                regime=MarketRegime.UNKNOWN,
                btc_change_1h=0.0,
                btc_change_4h=0.0,
                is_asian_session=True,
                daily_pnl_pct=daily_pnl,
                altcoin_decoupled=False,
                warnings=warnings
            )

        # 3. BTC изменение
        btc_1h, btc_4h = await self._get_btc_changes()

        # 4. Проверка декорреляции альта
        decoupled = False
        if symbol != "BTCUSDT" and symbol != "ETHUSDT":
            decoupled = await self.check_altcoin_decoupling(symbol, btc_1h)

        # 5. BTC фильтр для LONG (ОПЦИОНАЛЬНЫЙ — по умолч. ВЫКЛ, см. BTC_CORRELATION_FILTER=true)
        if direction == "long" and self.BTC_FILTER_ENABLED:
            if btc_1h <= self.BTC_CRASH_THRESHOLD_LONG:
                # Разрешаем если альт явно декоррелирован (движется независимо)
                if allow_decoupled_alts and decoupled:
                    warnings.append(
                        f"💡 BTC падает {btc_1h:.1f}%/1ч но {symbol} движется независимо — торгуем!"
                    )
                else:
                    return MarketContextResult(
                        allowed=False,
                        block_reason=f"🐻 BTC падает {btc_1h:.1f}%/1ч (порог {self.BTC_CRASH_THRESHOLD_LONG}%) — блок LONG",
                        regime=MarketRegime.BEAR_TREND,
                        btc_change_1h=btc_1h,
                        btc_change_4h=btc_4h,
                        is_asian_session=asian,
                        daily_pnl_pct=daily_pnl,
                        altcoin_decoupled=decoupled,
                        warnings=warnings
                    )
            if btc_1h >= self.BTC_PUMP_THRESHOLD_LONG:
                warnings.append(f"⚠️ BTC перегрет {btc_1h:.1f}%/1ч — осторожно с лонгами")
        elif direction == "long" and not self.BTC_FILTER_ENABLED:
            # Фильтр выключен — каждая монета торгуется по своей структуре
            if btc_1h <= -4.0:
                warnings.append(f"📊 BTC {btc_1h:.1f}%/1ч — проверь структуру {symbol} индивидуально")

        # ✅ v5.0: BTC фильтр для SHORT (симметричный LONG)
        if direction == "short" and self.BTC_FILTER_ENABLED:
            if btc_1h >= self.BTC_CRASH_THRESHOLD_SHORT:
                # Разрешаем если альт явно декоррелирован (растёт независимо от BTC)
                if allow_decoupled_alts and decoupled:
                    warnings.append(
                        f"💡 BTC растёт {btc_1h:.1f}%/1ч но {symbol} падает — торгуем SHORT!"
                    )
                else:
                    return MarketContextResult(
                        allowed=False,
                        block_reason=f"🚀 BTC растёт {btc_1h:.1f}%/1ч (порог +{self.BTC_CRASH_THRESHOLD_SHORT}%) — блок SHORT",
                        regime=MarketRegime.BULL_TREND,
                        btc_change_1h=btc_1h,
                        btc_change_4h=btc_4h,
                        is_asian_session=asian,
                        daily_pnl_pct=daily_pnl,
                        altcoin_decoupled=decoupled,
                        warnings=warnings
                    )
            if btc_1h <= self.BTC_DUMP_THRESHOLD_SHORT:
                warnings.append(f"⚠️ BTC падает {btc_1h:.1f}%/1ч — осторожно с шортами (может быть отскок)")

        # 6. Определяем режим рынка
        regime = await self.detect_market_regime()

        # Предупреждение для боковика
        if regime == MarketRegime.SIDEWAYS and direction == "long":
            warnings.append("📊 Рынок в боковике — предпочтительны контртрендовые входы")
        if regime == MarketRegime.HIGH_VOLATILITY:
            warnings.append("⚡ Высокая волатильность — уменьшите размер позиции")
        if btc_4h <= -4.0:
            warnings.append(f"🔴 BTC -4%+ за 4ч ({btc_4h:.1f}%) — высокий риск для альткоинов")

        return MarketContextResult(
            allowed=True,
            block_reason="",
            regime=regime,
            btc_change_1h=btc_1h,
            btc_change_4h=btc_4h,
            is_asian_session=asian,
            daily_pnl_pct=daily_pnl,
            altcoin_decoupled=decoupled,
            warnings=warnings
        )


# ── Глобальный инстанс (инициализируется в main.py) ──────────────────────────
_market_context: Optional[MarketContextFilter] = None


def get_market_context(binance_client=None, redis_client=None) -> MarketContextFilter:
    """Получить или создать глобальный MarketContextFilter"""
    global _market_context
    if _market_context is None:
        _market_context = MarketContextFilter(binance_client, redis_client)
    elif binance_client and not _market_context._binance:
        _market_context._binance = binance_client
    elif redis_client and not _market_context._redis:
        _market_context._redis = redis_client
    return _market_context
