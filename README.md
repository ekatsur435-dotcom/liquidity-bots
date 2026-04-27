# 🤖 Liquidity Bots v6.0 — PRODUCTION READY

## 🔴 ЧТО ИСПРАВЛЕНО В v6.0 (по сравнению с v4.0/v5.0)

| # | Баг/Проблема | Файл | Статус |
|---|-------------|------|--------|
| 1 | Dashboard показывал неверный P&L (-69%) | dashboard/app.py | ✅ FIXED |
| 2 | `pnl` vs `pnl_pct` mismatch | position_tracker.py | ✅ FIXED |
| 3 | SHORT бот молчал весь день | short-bot/main.py | ✅ FIXED |
| 4 | MarketContextFilter не инициализирован | оба бота | ✅ FIXED |
| 5 | `tbs_detected` NameError | long-bot/main.py | ✅ FIXED |
| 6 | Circular import в zombie cleanup | position_tracker.py | ✅ FIXED |
| 7 | Redis `.set()/.get()/.keys()` отсутствуют | redis_client.py | ✅ FIXED |
| 8 | MicroTrailing `get_redis_client` не импортирован | micro_trailing_stop.py | ✅ FIXED |
| 9 | BingX 109400 без auto-retry | bingx_client.py | ✅ FIXED |
| 10 | BINGX_SECRET_KEY vs BINGX_API_SECRET | .env.example | ✅ FIXED |
| 11 | SL 1.0% → 1.5% (слишком тесно) | Config | ✅ FIXED |
| 12 | MIN_SCORE 60→65 (шум проходил) | Config | ✅ FIXED |
| 13 | TP1 1.5%→2.5% (R:R был 1.25:1) | Config TP_LEVELS | ✅ FIXED |
| 14 | BREAKEVEN после TP1→TP2 | position_tracker.py | ✅ FIXED |
| 15 | Elliott confidence 0.5→0.7 | оба бота | ✅ FIXED |
| 16 | SWEEP path TP format mismatch | long-bot/main.py | ✅ FIXED |
| 17 | Score > 100 не ограничивался | long-bot/main.py | ✅ FIXED |
| 18 | SL Cooldown 2ч после стопа | position_tracker.py | ✅ NEW |
| 19 | Dashboard flush_stats API endpoint | dashboard/app.py | ✅ NEW |

## 🚀 ДЕПЛОЙ НА RENDER (пошагово)

### 1. Загрузить в GitHub
```bash
git init
git add .
git commit -m "v6.0 — production ready"
git remote add origin https://github.com/YOUR/repo.git
git push -u origin main
```

### 2. Создать 3 сервиса на Render

**LONG Bot** (`long-bot/`)
- Type: Web Service
- Root Directory: `long-bot`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`

**SHORT Bot** (`short-bot/`)
- Root Directory: `short-bot`
- Start Command: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`

**Dashboard** (`dashboard/`)
- Root Directory: `dashboard`
- Start Command: `gunicorn -b 0.0.0.0:$PORT app:app`

### 3. ENV переменные
Скопировать из `.env.example` в Render → Environment каждого сервиса.
⚠️ Убедись: `BINGX_API_SECRET` (не BINGX_SECRET_KEY!)

### 4. После деплоя — очистить старые данные Redis
```
POST https://trading-dashboard-fxyv.onrender.com/api/flush_stats
```
Или через Upstash Dashboard → Flush Database

### 5. UptimeRobot
Добавить мониторинг `/health` для каждого сервиса (каждые 5 мин).

## 📊 ПОЧЕМУ DASHBOARD ПОКАЗЫВАЛ -69%

**Причина**: `position_tracker.py` сохранял P&L как `pnl_pct`, 
а `dashboard/app.py` читал поле `pnl`. Оба поля теперь сохраняются.

**Реальные результаты на BingX** (из истории позиций):
- SHORT: GALAUSDT +97 USDT, SAGAUSDT +126 USDT, ARIAUSDT +153 USDT ✅
- LONG: ZBTUSDT +71 USDT, MASKUSDT +18 USDT, ORCAUSDT +24 USDT ✅

Бот торговал прибыльно, но дашборд показывал неверно!

## 📱 КАК ЧИТАТЬ TELEGRAM СООБЩЕНИЯ

### Бот ВОШЁЛ в сделку — после сигнала придёт:
```
🤖 AUTO-TRADE [DEMO]
🔴 #TLMUSDT SHORT
📍 Entry: 0.00192
🛑 SL: 0.0019488
📊 Size: 2395833.0 | 22x | 0.069% risk
🎯 Score: 99%
🆔 OrderID: 2048045392106356737
```

### Бот НЕ вошёл — причины:
```
⏸ [DEMO] #BANKUSDT: дневной лимит (-10.42% ≤ -5.0%)
⏭ SKIP — ACE-USDT offline/delisted
📡 LONG TG-only: BANANAUSDT Score=92% [max positions]
```

### Все уведомления приходят ОТВЕТАМИ на исходный сигнал (thread)

## ⚙️ КЛЮЧЕВЫЕ ПАРАМЕТРЫ v6.0

| Параметр | v4.0 (баг) | v6.0 (fix) | Почему |
|---------|-----------|-----------|--------|
| SL Buffer | 1.0% | **1.5%** | Крипто ATR > 1% нормально |
| TP1 | 1.5% | **2.5%** | R:R был 1.25:1 → стал 1.67:1 |
| MIN_LONG_SCORE | 60 | **65** | Меньше шумовых сигналов |
| MIN_SHORT_SCORE | 65 | **67** | Шорт требует выше уверенности |
| Breakeven | после TP1 | **после TP2** | TP1 ретест — норма, не стоп |
| Elliott block | confidence>0.5 | **>0.7** | Меньше ложных блоков |

## 🔍 ПОЧЕМУ SHORT БОТ МОЛЧИТ

SHORT бот молчит когда:
1. Рынок уже сильно упал (RSI < 45) — нечего шортить сверху
2. BTC растёт > +4%/1h — блок BTC filter
3. Азиатская сессия 03-06 UTC — блок сессии
4. Daily risk limit достигнут (-5%)

**Это НОРМАЛЬНО** — SHORT ищет перекупленность (RSI > 65),
а не продолжение падения. Мониторируй в период роста.

## 📁 Структура проекта

```
liquidity-bots-v6/
├── long-bot/src/main.py      ← LONG Bot v6.0
├── short-bot/src/main.py     ← SHORT Bot v6.0  
├── dashboard/app.py          ← Trading Dashboard
├── shared/
│   ├── core/
│   │   ├── scorer.py         ← Dual scorer (LONG/SHORT)
│   │   ├── position_tracker.py ← TP/SL/trailing
│   │   ├── market_context.py ← BTC filter, sessions
│   │   ├── short_filter.py   ← SHORT-специфичные фильтры
│   │   └── ...
│   ├── execution/
│   │   ├── auto_trader.py    ← BingX ордера
│   │   ├── limit_executor.py ← Лимитки с TTL
│   │   └── micro_trailing_stop.py ← Trailing
│   ├── api/bingx_client.py   ← BingX API
│   ├── bot/telegram.py       ← Telegram + thread replies
│   └── upstash/redis_client.py ← Redis (с proxy методами)
└── .env.example              ← Все переменные
```
