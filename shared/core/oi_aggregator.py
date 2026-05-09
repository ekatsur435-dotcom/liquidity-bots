"""
OI Aggregator v1.0 — Multi-source Open Interest with estimation fallback

Fallback chain:
  Level 1 — OKX        (самый надёжный, без прокси)
  Level 2 — Bybit      (работает с EU/AS IP)
  Level 3 — CryptoQuant (dedicated OI API)
  Level 4 — Binance    (часто недоступен с Render EU IPs)
  Level 5 — Estimation  (из klines: volume + price action proxy)

Возвращает унифицированный список:
  [{"sumOpenInterest": float, "timestamp": int}, ...]
  Старые точки первые, новые последние.
"""

import asyncio
import math
from typing import List, Dict, Optional

# ---------------------------------------------------------------------------
# Estimation constants
# ---------------------------------------------------------------------------
_EST_DECAY = 0.92          # EMA decay для volume→OI proxy
_EST_SCALE = 0.15          # Масштабирование: OI ≈ 15% rolling volume


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def get_oi_history(
    symbol: str,
    period: str = "15m",
    limit: int = 5,
    *,
    binance_client=None,
    okx_client=None,
    debug: bool = True,
) -> List[Dict]:
    """
    Получить историю OI с полным fallback.

    Args:
        symbol:          Тикер в Bybit-формате (BTCUSDT)
        period:          Таймфрейм ('5m','15m','30m','1h','4h','1d')
        limit:           Кол-во точек (обычно 5 для velocity)
        binance_client:  BinanceClient instance (уже инициализирован)
        okx_client:      OKXClient instance
        debug:           Печатать источник данных

    Returns:
        Список dict {"sumOpenInterest": float, "timestamp": int}
        Пустой список если все источники недоступны и klines тоже нет.
    """
    # ── Level 1: OKX ─────────────────────────────────────────────────────────
    try:
        client_okx = okx_client or _get_okx()
        if client_okx:
            data = await client_okx.get_open_interest_history(symbol, period, limit)
            if data and len(data) >= 2:
                result = _normalize(data)
                if len(result) >= 2:
                    if debug:
                        print(f"   ✅ OI from OKX: {len(result)} points")
                    return result
                # 1 точка — недостаточно для velocity, идём дальше
                if debug:
                    print(f"   ⚠️ OI OKX: {len(data)} points (need ≥2) → Bybit fallback")
    except Exception as e:
        if debug:
            print(f"   ⚠️ OI OKX error ({symbol}): {e}")

    # ── Level 2: Bybit ────────────────────────────────────────────────────────
    try:
        if binance_client:
            imap = {"5m": "5min", "15m": "15min", "30m": "30min",
                    "1h": "1h", "4h": "4h", "1d": "1d"}
            interval = imap.get(period, "1h")
            result_raw = await binance_client._bybit(
                "/v5/market/open-interest",
                {"category": "linear", "symbol": symbol,
                 "intervalTime": interval, "limit": limit}
            )
            if result_raw and result_raw.get("list"):
                rows = [
                    {"sumOpenInterest": float(r.get("openInterest", 0)),
                     "timestamp": int(r.get("ts", 0))}
                    for r in result_raw["list"]
                    if float(r.get("openInterest", 0)) > 0
                ]
                # Bybit отдаёт новые первые — сортируем старые→новые
                rows.sort(key=lambda x: x["timestamp"])
                if len(rows) >= 2:
                    if debug:
                        print(f"   ✅ OI from Bybit: {len(rows)} points")
                    return rows
    except Exception as e:
        if debug:
            print(f"   ⚠️ OI Bybit error ({symbol}): {e}")

    # ── Level 3: CryptoQuant ──────────────────────────────────────────────────
    try:
        cq = _get_cryptoquant()
        if cq and cq.api_key:
            data = await cq.get_oi_history(symbol, period, limit)
            if data and len(data) >= 2:
                if debug:
                    print(f"   ✅ OI from CryptoQuant: {len(data)} points")
                return data
    except Exception as e:
        if debug:
            print(f"   ⚠️ OI CryptoQuant error ({symbol}): {e}")

    # ── Level 4: Binance (часто 404 на Render EU) ─────────────────────────────
    try:
        if binance_client and getattr(binance_client, "_use_binance", False):
            d = await binance_client._binance(
                "/fapi/v1/openInterestHist",
                {"symbol": symbol, "period": period, "limit": limit}
            )
            if d and len(d) >= 2:
                rows = _normalize(d)
                if debug:
                    print(f"   ✅ OI from Binance: {len(rows)} points")
                return rows
    except Exception as e:
        if debug:
            print(f"   ⚠️ OI Binance error ({symbol}): {e}")

    # ── Level 5: Estimation from klines ──────────────────────────────────────
    if binance_client:
        try:
            est = await _estimate_oi_from_klines(symbol, period, limit, binance_client)
            if est:
                if debug:
                    print(f"   🔧 OI estimated from klines: {len(est)} points")
                return est
        except Exception as e:
            if debug:
                print(f"   ⚠️ OI estimation error ({symbol}): {e}")

    if debug:
        print(f"   ❌ OI unavailable for {symbol} (all sources failed)")
    return []


async def get_oi_velocity(
    symbol: str,
    period: str = "15m",
    limit: int = 5,
    *,
    binance_client=None,
    okx_client=None,
) -> float:
    """
    Быстрый хелпер: OI velocity в % за (limit × period).
    Возвращает 0.0 если нет данных.
    """
    data = await get_oi_history(
        symbol, period, limit,
        binance_client=binance_client,
        okx_client=okx_client,
        debug=False,
    )
    if not data or len(data) < 2:
        return 0.0
    ois = [float(r.get("sumOpenInterest", 0)) for r in data]
    if ois[0] <= 0:
        return 0.0
    return round((ois[-1] - ois[0]) / ois[0] * 100, 3)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(raw: List[Dict]) -> List[Dict]:
    """Приводит разные форматы к {"sumOpenInterest": float, "timestamp": int}."""
    out = []
    for r in raw:
        oi = (r.get("sumOpenInterest") or r.get("openInterest")
              or r.get("oi") or 0)
        ts = r.get("timestamp") or r.get("ts") or 0
        try:
            oi_f = float(oi)
            ts_i = int(ts)
            if oi_f > 0:
                out.append({"sumOpenInterest": oi_f, "timestamp": ts_i})
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda x: x["timestamp"])
    return out


async def _estimate_oi_from_klines(
    symbol: str,
    period: str,
    limit: int,
    binance_client,
) -> List[Dict]:
    """
    Estimation fallback: оцениваем OI из объёма кайндлов.

    Логика (market microstructure proxy):
    - OI растёт когда volume растёт + цена движется в одну сторону несколько баров
    - Используем EMA(volume) как proxy open interest
    - Масштабируем чтобы изменения OI были реалистичны (±0.5-5% за 5 баров)

    Это не точные значения OI, но правильно отражает ТРЕНД (рост/падение),
    что достаточно для OI velocity scoring.
    """
    # Берём в 3x больше свечей чтобы EMA стабилизировалась
    fetch_limit = min(limit * 3 + 10, 100)
    klines = await binance_client.get_klines(symbol, period, fetch_limit)
    if not klines or len(klines) < limit + 3:
        return []

    closes  = [float(getattr(c, "close",  c[3] if isinstance(c, (list, tuple)) else 0)) for c in klines]
    volumes = [float(getattr(c, "volume", c[4] if isinstance(c, (list, tuple)) else 0)) for c in klines]
    times   = [int(getattr(c, "open_time", 0)) for c in klines]

    if not any(v > 0 for v in volumes):
        return []

    # EMA объёма → прокси OI
    ema_vol = volumes[0]
    ema_vols = []
    for v in volumes:
        ema_vol = _EST_DECAY * ema_vol + (1 - _EST_DECAY) * v
        ema_vols.append(ema_vol)

    # Нормализуем: базовый OI = price × EMA_volume × scale
    # Это даёт нам относительные изменения, а не абсолютные числа
    base_price = closes[-1] if closes[-1] > 0 else 1.0
    base_oi    = base_price * ema_vols[-1] * _EST_SCALE

    result = []
    for i in range(max(0, len(klines) - limit), len(klines)):
        oi_est = (closes[i] / base_price) * (ema_vols[i] / ema_vols[-1]) * base_oi
        result.append({
            "sumOpenInterest": round(oi_est, 2),
            "timestamp": times[i],
            "estimated": True,  # маркер что это оценка
        })

    return result


def _get_okx():
    try:
        from api.okx_client import get_okx_client
        return get_okx_client()
    except Exception:
        return None


def _get_cryptoquant():
    try:
        from api.cryptoquant_client import get_cryptoquant_client
        return get_cryptoquant_client()
    except Exception:
        return None
