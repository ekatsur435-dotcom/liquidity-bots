"""
🌅 Session High/Low SL + 📊 VWAP/Anchored VWAP SL
+ 📈 Volatility-Adaptive ATR Bounds
+ 🔗 Multi-TF SL Confluence Score

Использование:
    from core.session_vwap_sl import (
        calculate_session_sl, calculate_vwap_sl,
        get_adaptive_atr_bounds, calculate_adx_from_candles,
        calculate_sl_confluence
    )
"""

from typing import Optional, List, Tuple
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _candle_ts_sec(c) -> int:
    """Timestamp свечи → Unix секунды (поддерживает ms и s)."""
    ts = 0
    if hasattr(c, 'timestamp'):
        ts = int(c.timestamp)
    elif isinstance(c, (list, tuple)) and c:
        try:
            ts = int(c[0])
        except (ValueError, TypeError):
            return 0
    # Binance/Bybit возвращают миллисекунды → конвертируем
    return ts // 1000 if ts > 1_000_000_000_000 else ts


def _candle_fields(c) -> Optional[dict]:
    """OHLCV + ts из CandleData-объекта или списка."""
    try:
        if hasattr(c, 'open'):
            return {
                'ts':     _candle_ts_sec(c),
                'open':   float(c.open),
                'high':   float(c.high),
                'low':    float(c.low),
                'close':  float(c.close),
                'volume': float(getattr(c, 'volume', 0) or 0),
            }
        if isinstance(c, (list, tuple)) and len(c) >= 5:
            # [ts, o, h, l, c, v] (Binance) или [o, h, l, c, v] без ts
            if len(c) >= 6:
                return {
                    'ts':     _candle_ts_sec(c),
                    'open':   float(c[1]), 'high': float(c[2]),
                    'low':    float(c[3]), 'close': float(c[4]),
                    'volume': float(c[5]),
                }
            return {
                'ts':     0,
                'open':   float(c[0]), 'high': float(c[1]),
                'low':    float(c[2]), 'close': float(c[3]),
                'volume': float(c[4]) if len(c) > 4 else 0.0,
            }
    except (ValueError, TypeError, IndexError):
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. SESSION HIGH / LOW SL
# ─────────────────────────────────────────────────────────────────────────────

def calculate_session_sl(
    candles_1h: List,
    side: str,          # "long" | "short"
    price: float,
    atr_price: float,
    min_sl_dist: float,
    max_sl_dist: float,
) -> Optional[float]:
    """
    SL за High/Low Азиатской сессии (00:00–08:00 UTC).

    LONG  → SL ниже Asia Session Low  × 0.997
    SHORT → SL выше Asia Session High × 1.003

    Если свечей текущего дня < 3, берём предыдущий день.
    """
    if not candles_1h or len(candles_1h) < 3:
        return None
    try:
        now_utc = datetime.now(timezone.utc)
        # Полночь текущего UTC-дня
        day0_ts = int(datetime(
            now_utc.year, now_utc.month, now_utc.day,
            tzinfo=timezone.utc
        ).timestamp())

        # Asia session: [00:00, 08:00) UTC
        asia_start = day0_ts
        asia_end   = day0_ts + 8 * 3600
        # Предыдущий день
        prev_asia_start = day0_ts - 86400
        prev_asia_end   = day0_ts - 86400 + 8 * 3600

        def _collect(start_ts: int, end_ts: int):
            highs, lows = [], []
            for c in candles_1h:
                f = _candle_fields(c)
                if not f or f['ts'] == 0:
                    continue
                if start_ts <= f['ts'] < end_ts:
                    highs.append(f['high'])
                    lows.append(f['low'])
            return highs, lows

        # Пробуем текущую сессию
        highs, lows = _collect(asia_start, asia_end)
        # Если сессия ещё не началась или мало данных — берём предыдущую
        if len(lows) < 2:
            highs, lows = _collect(prev_asia_start, prev_asia_end)
        if not lows:
            return None

        session_low  = min(lows)
        session_high = max(highs)

        if side == "long":
            candidate = session_low * 0.997      # буфер −0.3%
            dist = price - candidate
        else:
            candidate = session_high * 1.003     # буфер +0.3%
            dist = candidate - price

        if min_sl_dist <= dist <= max_sl_dist:
            tag = "Low" if side == "long" else "High"
            val = session_low if side == "long" else session_high
            print(f"🌅 [SESSION-SL] {side.upper()}: "
                  f"Asia {tag}={val:.6f} → SL={candidate:.6f} "
                  f"(dist={dist/price*100:.2f}%)")
            return candidate

    except Exception as e:
        print(f"[SESSION-SL] Error: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. VWAP / ANCHORED VWAP SL
# ─────────────────────────────────────────────────────────────────────────────

def calculate_vwap_sl(
    candles_1h: List,
    side: str,
    price: float,
    min_sl_dist: float,
    max_sl_dist: float,
) -> Optional[float]:
    """
    Anchored VWAP от начала текущего UTC-дня.
    Если сегодняшних свечей < 4, берём последние 24.

    LONG  → SL = VWAP × 0.997  (если VWAP < цены)
    SHORT → SL = VWAP × 1.003  (если VWAP > цены)
    """
    if not candles_1h or len(candles_1h) < 4:
        return None
    try:
        now_utc = datetime.now(timezone.utc)
        day0_ts = int(datetime(
            now_utc.year, now_utc.month, now_utc.day,
            tzinfo=timezone.utc
        ).timestamp())

        today_candles, all_parsed = [], []
        for c in candles_1h:
            f = _candle_fields(c)
            if not f:
                continue
            all_parsed.append(f)
            if f['ts'] >= day0_ts:
                today_candles.append(f)

        use = today_candles if len(today_candles) >= 4 else all_parsed[-24:]
        if len(use) < 4:
            return None

        # VWAP = Σ(typical_price × volume) / Σ(volume)
        cum_tpv = sum((f['high'] + f['low'] + f['close']) / 3 * f['volume']
                      for f in use if f['volume'] > 0)
        cum_vol = sum(f['volume'] for f in use if f['volume'] > 0)
        if cum_vol == 0:
            return None

        vwap = cum_tpv / cum_vol

        if side == "long":
            if vwap < price * 0.999:    # VWAP ниже цены — валидная поддержка
                candidate = vwap * 0.997
                dist = price - candidate
            else:
                return None
        else:
            if vwap > price * 1.001:    # VWAP выше цены — валидное сопротивление
                candidate = vwap * 1.003
                dist = candidate - price
            else:
                return None

        if min_sl_dist <= dist <= max_sl_dist:
            print(f"📊 [VWAP-SL] {side.upper()}: "
                  f"VWAP={vwap:.6f} → SL={candidate:.6f} "
                  f"(dist={dist/price*100:.2f}%)")
            return candidate

    except Exception as e:
        print(f"[VWAP-SL] Error: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. VOLATILITY-ADAPTIVE ATR BOUNDS (через ADX)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_adx_from_candles(candles) -> float:
    """
    Упрощённый ADX из OHLCV-свечей (последние 20 баров).
    Returns ADX ∈ [10, 80]. Default = 20.0 (нейтральный).
    """
    try:
        data = [f for c in (candles or []) for f in [_candle_fields(c)] if f]
        if len(data) < 14:
            return 20.0
        data = data[-20:]

        tr_list, plus_dm, minus_dm = [], [], []
        for i in range(1, len(data)):
            h, l, pc = data[i]['high'], data[i]['low'], data[i - 1]['close']
            ph, pl   = data[i - 1]['high'], data[i - 1]['low']

            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)

            up_move   = h - ph
            down_move = pl - l
            plus_dm.append(up_move  if up_move  > down_move and up_move  > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        atr_avg = sum(tr_list) / len(tr_list) or 1e-10
        plus_di  = 100 * (sum(plus_dm)  / len(plus_dm))  / atr_avg if plus_dm  else 0
        minus_di = 100 * (sum(minus_dm) / len(minus_dm)) / atr_avg if minus_dm else 0
        di_sum   = plus_di + minus_di
        dx       = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0

        return float(max(10.0, min(80.0, dx)))
    except Exception:
        return 20.0


def get_adaptive_atr_bounds(
    atr_price: float,
    adx: float,
) -> Tuple[float, float]:
    """
    Адаптивные границы SL на основе ADX (сила тренда):

    ADX ≥ 30 (сильный тренд):    [0.6×, 2.5×]  — тайтный SL, лучший R:R
    ADX 25–29 (умеренный тренд): [0.7×, 3.0×]
    ADX 20–24 (нейтральный):     [0.8×, 4.0×]  — дефолт
    ADX 15–19 (слабый):          [0.9×, 4.5×]
    ADX < 15 (боковик):          [1.0×, 5.0×]  — широкий SL vs whipsaws

    Returns: (min_sl_dist, max_sl_dist) в единицах цены
    """
    if adx >= 30:
        lo, hi = 0.6, 2.5
    elif adx >= 25:
        lo, hi = 0.7, 3.0
    elif adx >= 20:
        lo, hi = 0.8, 4.0
    elif adx >= 15:
        lo, hi = 0.9, 4.5
    else:
        lo, hi = 1.0, 5.0
    return atr_price * lo, atr_price * hi


# ─────────────────────────────────────────────────────────────────────────────
# 4. MULTI-TF SL CONFLUENCE SCORE
# ─────────────────────────────────────────────────────────────────────────────

def calculate_sl_confluence(
    sl_candidates: List[Optional[float]],
    price: float,
    tolerance_pct: float = 0.6,
) -> Tuple[int, int]:
    """
    Считает, сколько SL-кандидатов кластеризуются в одной зоне (±tolerance_pct%).

    Returns: (max_confluence_count, score_bonus)
        2 кандидата совпали → +5
        3+ кандидата совпали → +10
    """
    valid = [sl for sl in sl_candidates if sl is not None and sl > 0]
    if len(valid) < 2:
        return 0, 0

    tolerance = price * tolerance_pct / 100
    max_cluster = 1
    for sl in valid:
        cluster = sum(1 for other in valid if abs(other - sl) <= tolerance)
        max_cluster = max(max_cluster, cluster)

    bonus = 10 if max_cluster >= 3 else (5 if max_cluster >= 2 else 0)
    return max_cluster, bonus
