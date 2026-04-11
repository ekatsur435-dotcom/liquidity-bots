# 🎯 PATTERN DETECTION GUIDE — Liquidity Long Bot

## Обзор

Этот документ описывает 4 основных паттерна для входа в LONG позицию, которые бот будет детектировать на 15-минутном таймфрейме.

---

## 🟢 1. REJECTION LONG (Отбой от поддержки)

### Описание
Цена коснулась сильного уровня поддержки и отбилась вверх с бычьей свечой. Продавцы не смогли пробить уровень, покупатели перехватили инициативу.

### Визуальные признаки:
```
До:                    После:
    │    ┌─┐              │        ┌──┐
    │    │ │ ← Resistance│        │  │
    │    │ │              │    ┌──┐  │
    │ ┌──┘ │              │    │  │  │
    │ │    │ ← Entry     │ ┌──┘  │  │
    └─┘    │              └─┘     └──┘
      Support                
      [Длинный нижний фитиль]  [Бычье закрытие]
```

### Технические критерии:

```python
REJECTION_LONG_CRITERIA = {
    # Свеча паттерна
    'body_size': '< 30% of total range',  # Маленькое тело
    'lower_wick': '> 50% of total range', # Длинный нижний фитиль
    'close_position': '> 75% of range',   # Закрытие в верхней части
    
    # Уровень поддержки
    'support_level': 'POC or HVN or OB or FVG or Daily Low',
    'touch_accuracy': 'within 0.2% of level',
    
    # Дельта
    'delta_positive': True,
    'delta_min': '+2% of volume',  # Минимальная положительная дельта
    
    # Объём
    'volume_vs_avg': '> 1.5x average',  # Выше среднего
    
    # Контекст
    'prior_trend': 'declining',  # До этого был тренд вниз
    'rsi_1h': '< 40',  # Перепроданность на старшем ТФ
}
```

### Псевдокод детекции:

```python
def detect_rejection_long(candles_15m: DataFrame, delta_data: List[float]):
    """
    Detect REJECTION LONG pattern
    
    Args:
        candles_15m: DataFrame with OHLCV for 15m timeframe
        delta_data: Delta values for each 15m candle
    
    Returns:
        Pattern object or None
    """
    # Get last 3 candles
    current = candles_15m.iloc[-1]
    prev = candles_15m.iloc[-2]
    
    # 1. Check if price touched support level
    support_levels = get_support_levels()  # POC, OB, FVG, Daily Low
    touched_level = None
    
    for level in support_levels:
        if abs(current['low'] - level) / level < 0.002:  # Within 0.2%
            touched_level = level
            break
    
    if not touched_level:
        return None
    
    # 2. Analyze the rejection candle
    total_range = current['high'] - current['low']
    body_size = abs(current['close'] - current['open'])
    lower_wick = min(current['open'], current['close']) - current['low']
    upper_wick = current['high'] - max(current['open'], current['close'])
    
    # Check candle structure
    is_hammer = (
        body_size / total_range < 0.3 and           # Small body
        lower_wick / total_range > 0.5 and          # Long lower wick
        current['close'] > current['open'] and       # Bullish close
        current['close'] > current['low'] + total_range * 0.75  # Close in upper 25%
    )
    
    # Alternative: Dragonfly Doji or Bullish Engulfing
    is_dragonfly = (
        body_size / total_range < 0.1 and           # Very small body
        lower_wick / total_range > 0.7 and          # Very long lower wick
        upper_wick / total_range < 0.1              # Tiny upper wick
    )
    
    is_engulfing = (
        current['close'] > prev['open'] and          # Close above prev open
        current['open'] < prev['close'] and          # Open below prev close
        current['close'] > current['open']           # Bullish candle
    )
    
    if not (is_hammer or is_dragonfly or is_engulfing):
        return None
    
    # 3. Check volume
    avg_volume = candles_15m['volume'].tail(20).mean()
    if current['volume'] < avg_volume * 1.3:
        return None  # Volume too low
    
    # 4. Check delta
    current_delta = delta_data[-1]
    if current_delta < 0:
        return None  # Must be positive delta (more buying)
    
    # 5. Check prior context (declining trend)
    price_5_candles_ago = candles_15m.iloc[-5]['close']
    if current['close'] > price_5_candles_ago * 0.98:
        # Price hasn't declined much, not a good rejection
        return None
    
    # Pattern detected!
    return Pattern(
        name='REJECTION_LONG',
        type='long',
        strength=15,  # Points
        freshness=0,   # Current candle
        candles_ago=0,
        volume_multiplier=current['volume'] / avg_volume,
        delta_at_trigger=current_delta,
        entry_price=current['close'],
        stop_loss=min(current['low'], touched_level) * 0.995,
        confidence='strong' if is_engulfing else 'moderate',
        description=f'Price rejected support at {touched_level:.2f} with strong bullish candle'
    )
```

### Очки в Long Score:
- **Base:** +15 очков
- **If engulfing pattern:** +20 очков
- **If at Daily Low:** +5 бонус
- **If at Order Block:** +5 бонус
- **If RSI 1h < 30:** +5 бонус

---

## 🟢 2. TRAP SHORT (Ловушка для шортистов)

### Описание
Резкий слив ниже ключевого уровня выбивает стопы шортистов, но цена быстро возвращается и закрывается выше. Шортисты, открывшие позиции на пробое, оказываются в ловушке.

### Визуальные признаки:
```
    До манипуляции:       Манипуляция:           После (восстановление):
    
    │    ┌──┐              │      ┌──┐           │            ┌──┐
    │    │  │ ← Support    │  ┌───┘  │ ← Fakeout│       ┌────┘  │
    │ ┌──┘  │              │  │      │ (stop    │  ┌────┘       │
    │ │     │              │  │      │  hunt)   │  │            │
    └─┘     └──            └──┘      └──────    └──┘            └──
    
    [Боковик]             [Пробой вниз]          [Возврат и закрытие выше]
```

### Технические критерии:

```python
TRAP_SHORT_CRITERIA = {
    # Фаза манипуляции (предыдущая свеча)
    'manipulation': {
        'breaks_support': True,           # Пробивает поддержку
        'break_magnitude': '> 0.5%',      # На более чем 0.5%
        'high_volume': '> 2x average',     # Высокий объём (вынос стопов)
    },
    
    # Фаза возврата (текущая свеча)
    'recovery': {
        'closes_above_support': True,     # Закрывается выше уровня
        'closes_bullish': True,           # Бычье закрытие
        'body_size': '> 50% of range',    # Большое тело
        'lower_wick': '< 30% of range',   # Короткий нижний фитиль
    },
    
    # Дельта
    'manipulation_delta': 'negative',     # На манипуляции дельта отрицательная (продажи)
    'recovery_delta': 'positive',         # На возврате дельта положительная (покупки)
    
    # Контекст
    'oi_growing': True,                   # OI растёт (шортисты заходят в ловушку)
}
```

### Псевдокод детекции:

```python
def detect_trap_short(candles_15m: DataFrame, delta_data: List[float], oi_data: List[float]):
    """
    Detect TRAP SHORT pattern (Bull Trap for short sellers)
    """
    # Need at least 2 candles
    if len(candles_15m) < 2:
        return None
    
    current = candles_15m.iloc[-1]   # Recovery candle
    prev = candles_15m.iloc[-2]      # Manipulation candle
    
    # Define support level (from previous structure)
    support_level = find_recent_support(candles_15m.iloc[-10:-2])
    
    # Phase 1: Check manipulation candle
    # Price broke below support
    manipulation_break = prev['low'] < support_level * 0.995
    
    # High volume on manipulation (stop hunt)
    avg_volume = candles_15m['volume'].tail(20).mean()
    high_volume = prev['volume'] > avg_volume * 1.8
    
    # Negative delta on manipulation (selling pressure)
    manip_delta = delta_data[-2]
    manip_selling = manip_delta < -0.02  # At least -2% delta
    
    if not (manipulation_break and high_volume and manip_selling):
        return None
    
    # Phase 2: Check recovery candle
    # Closes above support level
    closes_above = current['close'] > support_level
    
    # Bullish close
    bullish_close = current['close'] > current['open']
    
    # Strong body
    total_range = current['high'] - current['low']
    body_size = abs(current['close'] - current['open'])
    strong_body = body_size / total_range > 0.5
    
    # Short lower wick (not much rejection above)
    lower_wick = min(current['open'], current['close']) - current['low']
    short_lower_wick = lower_wick / total_range < 0.3
    
    if not (closes_above and bullish_close and strong_body and short_lower_wick):
        return None
    
    # Phase 3: Check delta shift
    recovery_delta = delta_data[-1]
    delta_shift = recovery_delta > 0.01  # Positive delta on recovery
    
    if not delta_shift:
        return None
    
    # Phase 4: Check OI (optional confirmation)
    # Open Interest should have grown during manipulation
    # (short sellers opening positions)
    if len(oi_data) >= 2:
        oi_growth = oi_data[-1] > oi_data[-2] * 1.02
        if not oi_growth:
            return None  # Not enough short interest trapped
    
    # Calculate quality score
    quality = 'strong'
    if current['close'] > prev['open']:  # Engulfing previous candle
        quality = 'very_strong'
    
    return Pattern(
        name='TRAP_SHORT',
        type='long',
        strength=20 if quality == 'very_strong' else 15,
        freshness=0,
        candles_ago=0,
        volume_multiplier=prev['volume'] / avg_volume,
        delta_at_trigger=recovery_delta,
        entry_price=current['close'],
        stop_loss=min(prev['low'], current['low']) * 0.998,
        confidence=quality,
        description=f'Trap for short sellers at {support_level:.2f}. Price fakeout then recovery.'
    )
```

### Очки в Long Score:
- **Base:** +15 очков
- **If engulfs manipulation candle:** +20 очков
- **If OI grew during trap:** +5 бонус
- **If daily delta turned positive:** +5 бонус

---

## 💥 3. MEGA LONG (Мега доминация покупателей)

### Описание
Начинается массивный откуп после дампа. Покупатели доминируют по объёму на протяжении нескольких часов подряд, дельта положительная, цена растёт уверенно.

### Визуальные признаки:
```
    Дамп:                   MEGA LONG начинается:
    
    │                       │              ┌───┐
    │    ┌──┐               │         ┌───┘   │
    │ ┌──┘  │← Последний    │    ┌────┘       │
    │ │     │   лой дампа   │ ┌──┘            │
    └─┘     └──             └─┘               └──
    [Падение]               [5-7 зелёных часов подряд]
                            [Объём растёт каждый час]
                            [Дельта положительная]
```

### Технические критерии:

```python
MEGA_LONG_CRITERIA = {
    # Последние часы (минимум 4 из 5)
    'green_candles': '>= 4 out of 5',     # Зелёные свечи
    'positive_delta': '>= 4 out of 5',    # Положительная дельта
    'rising_volume': 'each hour > previous',  # Объём растёт
    
    # Сила
    'price_recovery': '> 3% from low',    # Отскок более 3%
    'rsi_hourly': '< 50 but rising',     # RSI восстанавливается
    
    # Подтверждение
    'volume_vs_avg': '> 2x average',      # Объём в 2x выше среднего
    'oi_declining': True,                 # OI падает (шортисты закрываются)
}
```

### Псевдокод детекции:

```python
def detect_mega_long(
    candles_1h: DataFrame,      # 1h candles for context
    candles_15m: DataFrame,       # 15m candles for entry
    delta_1h: List[float],
    oi_data: List[float]
):
    """
    Detect MEGA LONG pattern (strong buyer dominance)
    """
    if len(candles_1h) < 6:
        return None
    
    # Check last 5 hours
    recent_5h = candles_1h.tail(5)
    recent_delta = delta_1h[-5:]
    
    # Count green candles
    green_candles = sum(1 for _, candle in recent_5h.iterrows() 
                       if candle['close'] > candle['open'])
    
    # Count positive delta hours
    positive_delta_hours = sum(1 for d in recent_delta if d > 0)
    
    # Check rising volume trend
    volumes = recent_5h['volume'].tolist()
    rising_volume = all(volumes[i] > volumes[i-1] * 0.9 for i in range(1, len(volumes)))
    avg_volume = candles_1h['volume'].tail(20).mean()
    high_volume = volumes[-1] > avg_volume * 2
    
    # Check price recovery
    low_5h_ago = candles_1h.iloc[-6]['low']
    current_price = candles_1h.iloc[-1]['close']
    recovery_pct = (current_price - low_5h_ago) / low_5h_ago * 100
    
    if green_candles < 4:
        return None
    
    if positive_delta_hours < 4:
        return None
    
    if recovery_pct < 2:
        return None
    
    # Optional: Check OI declining (shorts closing)
    oi_declining = False
    if len(oi_data) >= 6:
        oi_declining = oi_data[-1] < oi_data[-5] * 0.98
    
    # Find best entry on 15m
    # Look for the first pullback after strong move
    recent_15m = candles_15m.tail(20)
    entry_candle = None
    
    for i in range(len(recent_15m) - 1, -1, -1):
        candle = recent_15m.iloc[i]
        # Find a small pullback in uptrend
        if (candle['close'] < candle['open'] and  # Red candle
            candle['volume'] < avg_volume * 1.5):  # Not high volume
            entry_candle = candle
            break
    
    if not entry_candle:
        entry_candle = candles_15m.iloc[-1]  # Use current
    
    return Pattern(
        name='MEGA_LONG',
        type='long',
        strength=25,
        freshness=0,
        candles_ago=0,
        volume_multiplier=volumes[-1] / avg_volume,
        delta_at_trigger=recent_delta[-1],
        entry_price=entry_candle['close'],
        stop_loss=candles_1h['low'].tail(5).min() * 0.995,
        confidence='very_strong' if oi_declining else 'strong',
        description=f'MEGA LONG: {green_candles}/5 green hours, {recovery_pct:.1f}% recovery'
    )
```

### Очки в Long Score:
- **Base:** +25 очков (самый высокий паттерн)
- **If 5/5 green hours:** +30 очков
- **If OI declining:** +5 бонус
- **If recovery > 5%:** +5 бонус
- **If volume > 3x average:** +5 бонус

---

## 🟣 4. ACCUMULATION (Накопление крупного игрока)

### Описание
Крупный игрок (whale) тихо накапливает позицию. Цена боковит или медленно падает, но объём огромный и дельта сильно положительная. Похоже на Wyckoff Spring или Sign of Strength.

### Визуальные признаки:
```
    Обычный боковик:        ACCUMULATION:
    
    │  ┌┐  ┌┐               │     ┌─┐
    │  ││  ││ ← Маленький    │ ┌─┐ │ │ ┌─┐
    │  └┘  └┘   объём        │ │ │ │ │ │ │  ← Маленькие тела
    │                        │ └─┘ └─┘ └─┘
    └──────────              └────────────────
                             
    [Низкий объём]           [Огромный объём]
                             [Дельта +5-10%]
                             [Крупный игрок покупает]
```

### Технические критерии:

```python
ACCUMULATION_CRITERIA = {
    # Ценовое действие
    'price_action': 'sideways or slowly declining',
    'range': '< 2% over 10 candles',  # Цена в боковике
    'body_size': '< 30% of range on average',  # Маленькие тела
    
    # Объём
    'volume_vs_avg': '> 2.5x average',  # В 2.5 раза выше среднего
    'consistent_high_volume': '8+ candles with high vol',
    
    # Дельта
    'delta_positive': True,
    'delta_strength': '> +5% average',  # Сильная положительная дельта
    'cumulative_delta': 'rising consistently',  # Нарастающая дельта
    
    # Контекст
    'after_decline': True,  # После периода падения
    'rsi_1h': '< 35',  # Перепроданность
    'oi_stable': 'not growing',  # OI не растёт (не шорты)
}
```

### Псевдокод детекции:

```python
def detect_accumulation(
    candles_15m: DataFrame,
    delta_15m: List[float],
    candles_1h: DataFrame
):
    """
    Detect ACCUMULATION pattern (Wyckoff Spring/SoS)
    """
    if len(candles_15m) < 12:
        return None
    
    # Check recent 10 candles
    recent = candles_15m.tail(10)
    
    # 1. Price in range (sideways)
    price_high = recent['high'].max()
    price_low = recent['low'].min()
    price_range_pct = (price_high - price_low) / price_low * 100
    
    if price_range_pct > 2.5:  # Too much movement
        return None
    
    # 2. Small bodies (indecision/controlled action)
    body_sizes = []
    for _, candle in recent.iterrows():
        body = abs(candle['close'] - candle['open'])
        range_size = candle['high'] - candle['low']
        if range_size > 0:
            body_sizes.append(body / range_size)
    
    avg_body_ratio = sum(body_sizes) / len(body_sizes)
    
    if avg_body_ratio > 0.4:  # Bodies too large
        return None
    
    # 3. High volume
    avg_volume = candles_15m['volume'].tail(40).mean()
    recent_volumes = recent['volume'].tolist()
    
    high_volume_count = sum(1 for v in recent_volumes if v > avg_volume * 2)
    
    if high_volume_count < 6:  # At least 6 candles with high volume
        return None
    
    # 4. Strong positive delta
    recent_delta = delta_15m[-10:]
    avg_delta = sum(recent_delta) / len(recent_delta)
    
    if avg_delta < 0.03:  # Must be at least +3% average delta
        return None
    
    # 5. Check cumulative delta is rising
    cumulative = [sum(recent_delta[:i+1]) for i in range(len(recent_delta))]
    delta_trend = all(cumulative[i] >= cumulative[i-1] * 0.95 
                     for i in range(1, len(cumulative)))
    
    if not delta_trend:
        return None
    
    # 6. Context: after decline
    price_20_candles_ago = candles_15m.iloc[-25]['close']
    current_price = candles_15m.iloc[-1]['close']
    prior_decline = current_price < price_20_candles_ago * 0.95
    
    if not prior_decline:
        return None  # Should be after some decline
    
    # Find best entry (last small red candle or current)
    entry_candle = candles_15m.iloc[-1]
    
    for i in range(1, min(4, len(recent))):
        candle = candles_15m.iloc[-i]
        if candle['close'] < candle['open']:  # Red candle
            entry_candle = candle
            break
    
    return Pattern(
        name='ACCUMULATION',
        type='long',
        strength=30,  # Highest score
        freshness=0,
        candles_ago=0,
        volume_multiplier=recent_volumes[-1] / avg_volume,
        delta_at_trigger=recent_delta[-1],
        entry_price=entry_candle['close'],
        stop_loss=price_low * 0.998,
        confidence='very_strong',
        description=f'ACCUMULATION: Whale accumulating, {high_volume_count}/10 high vol candles, avg delta +{avg_delta*100:.1f}%'
    )
```

### Очки в Long Score:
- **Base:** +30 очков (максимум за паттерн)
- **If at Wyckoff Spring area:** +5 бонус
- **If cumulative delta rising:** +5 бонус
- **If volume > 3x average:** +5 бонус

---

## 🔄 Pattern Priority Matrix

Если несколько паттернов обнаружены одновременно:

| Priority | Pattern | Score | Reason |
|----------|---------|-------|--------|
| 1 | ACCUMULATION | 30 | Whale buying = strongest signal |
| 2 | MEGA LONG | 25 | Strong momentum |
| 3 | TRAP SHORT | 20 | Short squeeze potential |
| 4 | REJECTION LONG | 15 | Good entry at support |

### Combined Patterns Bonus:
- ACCUMULATION + MEGA LONG = +5 extra points
- TRAP SHORT + REJECTION LONG = +3 extra points
- All 4 patterns together = +10 extra points (rare)

---

## 📊 Pattern Validation Checklist

### Before confirming any pattern:

- [ ] **Timeframe:** 15m candles (not 1h, not 5m)
- [ ] **Volume:** Above average (min 1.5x)
- [ ] **Delta:** Correct direction for pattern type
- [ ] **Context:** After decline / at support level
- [ ] **RSI 1h:** < 50 (preferably < 40)
- [ ] **Freshness:** Within last 1-3 candles
- [ ] **No conflicting patterns:** No strong SHORT signals active

### After pattern detection:

- [ ] **Entry price:** Calculated and reasonable
- [ ] **Stop loss:** Below support/structure
- [ ] **Distance SL-Entry:** At least 0.8-1.5 ATR
- [ ] **Risk/Reward:** Minimum 1:2
- [ ] **Position size:** Within risk limits

---

## 🎓 Examples from Real Charts

### Example 1: REJECTION LONG
```
BTC/USDT, 15m, Support at $67,000

Candle 1 (14:00): Low $66,850, Close $67,100
- Touched support at $67,000
- Long lower wick (250 points)
- Bullish close above support
- Volume 1.8x average
- Delta +$2.1M

Result: Pattern confirmed, entry $67,100, SL $66,800
Next 4 hours: Price moved to $68,500 (+2.1%)
```

### Example 2: TRAP SHORT
```
ETH/USDT, 15m, Support at $3,500

Candle 1 (10:00): Break below $3,500 to $3,480
- High volume (stop hunt)
- Delta -$1.8M (selling)

Candle 2 (10:15): Recovery to $3,520
- Close above $3,500
- Bullish engulfing
- Delta +$2.5M (buying)
- OI grew during manipulation

Result: Pattern confirmed, entry $3,520, SL $3,470
Next 2 hours: Price moved to $3,650 (+3.7%)
```

### Example 3: ACCUMULATION
```
SOL/USDT, 15m, After decline from $145 to $138

10 candles (09:00-11:30): Price range $138.20-$139.50
- Average body size: 12% of range (very small)
- Volume: 3.2x average
- Average delta: +$850K per candle (+6.2%)
- Cumulative delta: rising consistently

Result: Pattern confirmed, entry $139.00, SL $137.80
Next 6 hours: Price moved to $152 (+9.4%)
```

---

## ⚠️ False Pattern Warnings

### REJECTION LONG:
- ❌ Price breaks support on next candle
- ❌ Low volume (no interest)
- ❌ Negative delta on rejection candle
- ❌ In strong downtrend (no context)

### TRAP SHORT:
- ❌ No return above support
- ❌ Continues lower on high volume
- ❌ Delta stays negative
- ❌ OI declining (no shorts to trap)

### MEGA LONG:
- ❌ Less than 4 green candles
- ❌ Delta turns negative
- ❌ Volume declining
- ❌ OI rising (new shorts entering)

### ACCUMULATION:
- ❌ Price breaks out down on high volume
- ❌ Delta turns negative
- ❌ Range expands significantly
- ❌ After strong uptrend (distribution, not accumulation)

---

## 📚 References

1. Wyckoff Method - Spring and Sign of Strength
2. SMC (Smart Money Concepts) - Order Blocks and Fair Value Gaps
3. Volume Profile - POC, HVN (High Volume Nodes)
4. Delta Analysis - CVD (Cumulative Volume Delta)
5. Open Interest - Short squeeze dynamics

---

## 🚀 Implementation Notes

### Recommended Settings:
```python
PATTERN_CONFIG = {
    'rejection_long': {
        'enabled': True,
        'min_score': 15,
        'max_age_candles': 1,  # Must be current candle
        'min_volume_mult': 1.5,
    },
    'trap_short': {
        'enabled': True,
        'min_score': 15,
        'max_age_candles': 1,
        'min_volume_mult': 1.8,
    },
    'mega_long': {
        'enabled': True,
        'min_score': 25,
        'max_age_candles': 2,  # Can be 1-2 candles old
        'min_green_hours': 4,
    },
    'accumulation': {
        'enabled': True,
        'min_score': 30,
        'max_age_candles': 3,
        'min_high_vol_candles': 6,
    }
}
```

---

**Document Version:** 1.0  
**Last Updated:** April 2026
