# 🏗️ ARCHITECTURE: Dual Separate Bots + Upstash + Render

## Схема: Два независимых бота

```
┌─────────────────────────────────────────────────────────────────────┐
│                         SYSTEM OVERVIEW                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐│
│  │                    RENDER.COM (Free Tier)                       ││
│  │  ┌──────────────────┐        ┌──────────────────┐             ││
│  │  │  🔴 SHORT BOT     │        │  🟢 LONG BOT      │             ││
│  │  │  (liq-short-bot)  │        │  (liq-long-bot)   │             ││
│  │  │                   │        │                   │             ││
│  │  │ • FastAPI app     │        │ • FastAPI app     │             ││
│  │  │ • Scanner (1min)  │        │ • Scanner (1min)  │             ││
│  │  │ • Score calc      │        │ • Score calc      │             ││
│  │  │ • Signal gen      │        │ • Signal gen      │             ││
│  │  │ • Telegram alerts │        │ • Telegram alerts │             ││
│  │  └─────────┬────────┘        └─────────┬────────┘             ││
│  │            │                            │                       ││
│  │            └────────────┬───────────────┘                       ││
│  │                       │                                        ││
│  │            ┌───────────▼────────────┐                           ││
│  │            │    UPSTASH REDIS     │                           ││
│  │            │    (Free Tier)       │                           ││
│  │            │                      │                           ││
│  │            │ • Signal history     │                           ││
│  │            │ • Position tracking  │                           ││
│  │            │ • State management   │                           ││
│  │            │ • Cross-bot sync     │                           ││
│  │            └───────────┬────────────┘                           ││
│  │                       │                                        ││
│  └───────────────────────┼────────────────────────────────────────┘│
│                          │                                         │
│  ┌───────────────────────▼─────────────────────────────────────────┐│
│  │                  WAKEUP SERVICE (Cron-Job)                      ││
│  │  ┌──────────────────────────────────────────────────────────┐  ││
│  │  │  UptimeRobot / Cron-Job.org / GitHub Actions (Free)      │  ││
│  │  │                                                          │  ││
│  │  │ • Ping every 5 minutes                                   │  ││
│  │  │ • Keep both bots awake                                   │  ││
│  │  │ • Health checks                                          │  ││
│  │  └──────────────────────────────────────────────────────────┘  ││
│  └─────────────────────────────────────────────────────────────────┘│
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 📁 Структура проекта (ДВА ОТДЕЛЬНЫХ БОТА)

```
LIQUIDITY_BOTS/
│
├── 📁 short-bot/                    # 🔴 SHORT BOT
│   ├── 📁 src/
│   │   ├── main.py                  # FastAPI app entry
│   │   ├── scanner.py             # Perp scanner (overbought)
│   │   ├── scorer.py                # Short Score 0-100
│   │   ├── patterns.py              # 4 SHORT patterns
│   │   ├── signal_gen.py            # Generate signals
│   │   ├── telegram.py              # Telegram alerts
│   │   └── config.py                # Bot config
│   │
│   ├── 📁 api/                      # Bybit integration
│   │   └── bybit_client.py
│   │
│   ├── requirements.txt
│   ├── render.yaml                  # Render deploy config
│   ├── Dockerfile
│   └── .env.example
│
├── 📁 long-bot/                     # 🟢 LONG BOT  
│   ├── 📁 src/
│   │   ├── main.py                  # FastAPI app entry
│   │   ├── scanner.py             # Perp scanner (oversold)
│   │   ├── scorer.py                # Long Score 0-100
│   │   ├── patterns.py              # 4 LONG patterns
│   │   ├── signal_gen.py            # Generate signals
│   │   ├── telegram.py              # Telegram alerts
│   │   └── config.py                # Bot config
│   │
│   ├── 📁 api/                      # Bybit integration
│   │   └── bybit_client.py
│   │
│   ├── requirements.txt
│   ├── render.yaml                  # Render deploy config
│   ├── Dockerfile
│   └── .env.example
│
├── 📁 shared/                       # 📚 Shared resources
│   ├── 📁 upstash/
│   │   ├── redis_client.py          # Upstash Redis wrapper
│   │   ├── models.py                # Data models
│   │   └── state_manager.py         # Bot state management
│   │
│   ├── 📁 utils/
│   │   ├── binance_client.py        # Shared Binance API
│   │   ├── indicators.py            # Technical indicators
│   │   └── helpers.py
│   │
│   └── 📁 monitoring/
│       ├── health_check.py          # Health check endpoint
│       └── wake_service.py          # Wakeup logic
│
├── 📁 wake-service/                 # ⏰ Keep bots awake
│   ├── github-actions-wake.yml      # GitHub Actions cron
│   └── uptime-robot-setup.md        # UptimeRobot guide
│
├── 📁 docs/                         # 📚 Documentation
│   ├── deployment-guide.md
│   ├── upstash-setup.md
│   └── troubleshooting.md
│
├── 📁 scripts/
│   ├── deploy-both.sh
│   └── setup-upstash.py
│
└── README.md                        # Root readme
```

---

## 🔧 UPSTASH REDIS (База данных)

### Зачем нужна:
- **Signal history** — история всех сигналов
- **Position tracking** — открытые позиции
- **State persistence** — состояние при рестарте
- **Cross-bot sync** — синхронизация между ботами (если нужна)
- **Rate limiting** — не спамить API

### Структура данных в Redis:

```python
# Сигналы SHORT
"short:signals:BTCUSDT" → [
    {
        "timestamp": "2026-04-11T14:32:00Z",
        "score": 78,
        "price": 73500.0,
        "pattern": "MEGA_SHORT",
        "status": "active"  # active, expired, executed
    }
]

# Сигналы LONG
"long:signals:ETHUSDT" → [
    {
        "timestamp": "2026-04-11T15:45:00Z", 
        "score": 72,
        "price": 3250.0,
        "pattern": "ACCUMULATION",
        "status": "active"
    }
]

# Позиции (если авто-торговля)
"short:positions:BTCUSDT" → {
    "entry_price": 73500.0,
    "size": 0.01,
    "leverage": 10,
    "sl": 74200.0,
    "tps": [72400, 71500, ...],
    "opened_at": "2026-04-11T14:35:00Z",
    "pnl": 0.0
}

# Состояние ботов
"short:bot:state" → {
    "last_scan": "2026-04-11T14:32:00Z",
    "active_signals": 3,
    "daily_signals": 12,
    "status": "running"
}

"long:bot:state" → {
    "last_scan": "2026-04-11T15:45:00Z",
    "active_signals": 2,
    "daily_signals": 8,
    "status": "running"
}

# Статистика
"short:stats:daily:2026-04-11" → {
    "signals": 12,
    "trades": 3,
    "wins": 2,
    "losses": 1,
    "pnl": 0.045  # +4.5%
}

"long:stats:daily:2026-04-11" → {
    "signals": 8,
    "trades": 2,
    "wins": 1,
    "losses": 1,
    "pnl": 0.012  # +1.2%
}
```

### Upstash настройка (Free Tier):
- **Лимит:** 10,000 запросов/день
- **Хранение:** До 256MB
- **TTL:** Авто-очистка старых данных
- **SSL:** Шифрованное соединение

---

## ⏰ WAKEUP SERVICE (Чтобы не засыпали)

### Проблема:
- Render free tier — спит после 15 мин неактивности
- Нужно будить каждые 5-10 минут

### Решение: 3 варианта (все бесплатные)

#### Вариант 1: GitHub Actions (Рекомендуется) ⭐
```yaml
# .github/workflows/wake-bots.yml
name: Wake Up Bots

on:
  schedule:
    - cron: '*/5 * * * *'  # Every 5 minutes
  workflow_dispatch:

jobs:
  wake-short-bot:
    runs-on: ubuntu-latest
    steps:
      - name: Ping Short Bot
        run: curl -s https://liq-short-bot.onrender.com/health
      
      - name: Trigger Scan
        run: curl -s -X POST https://liq-short-bot.onrender.com/trigger-scan

  wake-long-bot:
    runs-on: ubuntu-latest
    steps:
      - name: Ping Long Bot
        run: curl -s https://liq-long-bot.onrender.com/health
      
      - name: Trigger Scan
        run: curl -s -X POST https://liq-long-bot.onrender.com/trigger-scan
```

**Плюсы:**
- Полностью бесплатно
- 2000 минут в месяц (хватит с запасом)
- Логи в GitHub

---

#### Вариант 2: UptimeRobot (Простой)
```
1. Регистрация на uptimerobot.com
2. Добавить мониторы:
   - https://liq-short-bot.onrender.com/health (каждые 5 мин)
   - https://liq-long-bot.onrender.com/health (каждые 5 мин)
3. Боты получают пинг → не засыпают
```

**Плюсы:**
- 50 мониторов бесплатно
- Email/SMS алерты если бот упал
- Простая настройка

---

#### Вариант 3: Cron-Job.org (Надёжный)
```
1. Регистрация на cron-job.org
2. Создать 2 job:
   - URL: https://liq-short-bot.onrender.com/health
   - Schedule: Every 5 minutes
   - URL: https://liq-long-bot.onrender.com/health
   - Schedule: Every 5 minutes
```

**Плюсы:**
- Очень надёжный
- Детальные логи
- Неограниченное количество job'ов

---

## 🚀 DEPLOYMENT: Render + Upstash

### Шаг 1: Upstash Redis
```bash
# 1. Регистрация на console.upstash.com
# 2. Создать новую базу данных
# 3. Получить:
#    - REDIS_HOST (something.upstash.io)
#    - REDIS_PORT (6379)
#    - REDIS_PASSWORD
#    - REDIS_URL (redis://default:pass@host:port)
```

### Шаг 2: Short Bot на Render
```yaml
# short-bot/render.yaml
services:
  - type: web
    name: liq-short-bot
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn src.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
      - key: REDIS_URL
        sync: false  # Заполни вручную из Upstash
      - key: BYBIT_API_KEY
        sync: false
      - key: BYBIT_API_SECRET
        sync: false
      - key: TELEGRAM_BOT_TOKEN
        sync: false
      - key: TELEGRAM_CHAT_ID
        sync: false
      - key: SCAN_INTERVAL
        value: 60  # seconds
      - key: MIN_SHORT_SCORE
        value: 65
    healthCheckPath: /health
```

### Шаг 3: Long Bot на Render
```yaml
# long-bot/render.yaml
services:
  - type: web
    name: liq-long-bot
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn src.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
      - key: REDIS_URL
        sync: false
      - key: BYBIT_API_KEY
        sync: false
      - key: BYBIT_API_SECRET
        sync: false
      - key: TELEGRAM_BOT_TOKEN
        sync: false
      - key: TELEGRAM_CHAT_ID
        sync: false
      - key: SCAN_INTERVAL
        value: 60
      - key: MIN_LONG_SCORE
        value: 65
    healthCheckPath: /health
```

### Шаг 4: GitHub Actions Wake Service
```yaml
# .github/workflows/wake-service.yml
name: Keep Bots Awake

on:
  schedule:
    - cron: '*/10 * * * *'  # Every 10 minutes
  workflow_dispatch:

jobs:
  wake-bots:
    runs-on: ubuntu-latest
    steps:
      - name: Wake Short Bot
        run: |
          curl -fsS https://liq-short-bot.onrender.com/health > /dev/null
          echo "Short bot awake"
      
      - name: Wake Long Bot
        run: |
          curl -fsS https://liq-long-bot.onrender.com/health > /dev/null
          echo "Long bot awake"
      
      - name: Trigger Short Scan
        run: |
          curl -fsS -X POST https://liq-short-bot.onrender.com/api/scan > /dev/null
          echo "Short scan triggered"
      
      - name: Trigger Long Scan
        run: |
          curl -fsS -X POST https://liq-long-bot.onrender.com/api/scan > /dev/null
          echo "Long scan triggered"
```

---

## 📡 API ENDPOINTS (каждый бот)

### Health & Status:
```
GET  /health              → {"status": "ok", "last_scan": "..."}
GET  /status              → Полный статус бота
GET  /api/stats           → Статистика торговли
```

### Scanning:
```
POST /api/scan            → Запустить ручное сканирование
GET  /api/signals         → Активные сигналы
GET  /api/signals/history → История сигналов
```

### Bybit Integration (опционально):
```
POST /api/positions/open  → Открыть позицию
POST /api/positions/close → Закрыть позицию
GET  /api/positions       → Активные позиции
```

### Config:
```
GET  /api/config          → Текущие настройки
POST /api/config/update   → Обновить настройки
```

---

## 📱 TELEGRAM: Как будут выглядеть сигналы

### Отдельные каналы (рекомендуется):

**Канал 1: 🔴 LIQUIDITY SHORT**
```
🔴 SHORT SIGNAL | Score: 78%

💎 BTCUSDT.P @ $73,500
📊 Pattern: MEGA SHORT (2 св. назад)

📉 Indicators:
• RSI: 78 (перекуплен) +20
• Funding: +0.45% (лонги платят) +15
• L/S Ratio: 72% лонгов +15
• Delta: -$5.2M (продажи) +20
• OI: +18% за 4д +10

🎯 ENTRY: $73,500
🛑 SL: $74,200 (+0.95%)
🎯 TP1: $72,400 (-1.5%) | 25%
🎯 TP2: $71,295 (-3.0%) | 25%
🎯 TP3: $69,825 (-5.0%) | 20%
🎯 TP4: $68,870 (-6.3%) | 15%
🎯 TP5: $67,253 (-8.5%) | 10%
🎯 TP6: $64,573 (-12.2%) | 5%

⚡ Rec. Leverage: 5-10x
💵 Risk: ≤1% deposit
⏰ Valid for: 30 minutes

[📊 Chart] [🚀 Auto-Trade] [❌ Ignore]
```

**Канал 2: 🟢 LIQUIDITY LONG**
```
🟢 LONG SIGNAL | Score: 72%

💎 ETHUSDT.P @ $3,250
📊 Pattern: ACCUMULATION (1 св. назад)

📈 Indicators:
• RSI: 24 (перепродан) +18
• Funding: -0.38% (шорты платят) +15
• L/S Ratio: 28% лонгов +15
• Delta: +$3.1M (покупки) +20
• OI: +12% при падении +10

🎯 ENTRY: $3,250
🛑 SL: $3,180 (-2.15%)
🎯 TP1: $3,299 (+1.5%) | 25%
🎯 TP2: $3,348 (+3.0%) | 25%
🎯 TP3: $3,413 (+5.0%) | 20%
🎯 TP4: $3,455 (+6.3%) | 15%
🎯 TP5: $3,526 (+8.5%) | 10%
🎯 TP6: $3,647 (+12.2%) | 5%

⚡ Rec. Leverage: 3-5x
💵 Risk: ≤1% deposit
⏰ Valid for: 30 minutes

[📊 Chart] [🚀 Auto-Trade] [❌ Ignore]
```

---

## 💰 СТОИМОСТЬ (Всё бесплатно)

| Сервис | Стоимость | Лимиты |
|--------|-----------|--------|
| **Render** | $0 | 750 часов/мес (достаточно для 2 ботов) |
| **Upstash** | $0 | 10,000 запросов/день, 256MB |
| **GitHub Actions** | $0 | 2000 минут/мес |
| **Telegram Bot** | $0 | Без лимитов |
| **UptimeRobot** | $0 | 50 мониторов |
| **Bybit Testnet** | $0 | Бесплатно |
| **Binance API** | $0 | 1200 запросов/мин |

**Итого: $0/месяц**

---

## 🔄 ПЛАН РАЗРАБОТКИ

### Неделя 1: Инфраструктура
- [ ] Настроить Upstash Redis
- [ ] Создать структуру short-bot/
- [ ] Создать структуру long-bot/
- [ ] Shared библиотеки (Redis, Binance)
- [ ] Деплой "Hello World" на Render

### Неделя 2: Short Bot MVP
- [ ] Scanner (поиск перекупленности)
- [ ] Short Scorer (алгоритм)
- [ ] 4 SHORT паттерна
- [ ] Telegram сигналы
- [ ] Redis интеграция

### Неделя 3: Long Bot MVP
- [ ] Scanner (поиск перепроданности)
- [ ] Long Scorer (алгоритм)
- [ ] 4 LONG паттерна
- [ ] Telegram сигналы
- [ ] Redis интеграция

### Неделя 4: Wake Service & Polish
- [ ] GitHub Actions wakeup
- [ ] Health checks
- [ ] Мониторинг
- [ ] Тестирование обоих ботов
- [ ] Оптимизация

### Неделя 5: Bybit Integration (опционально)
- [ ] Bybit API клиент
- [ ] Авто-торговля
- [ ] Position tracking
- [ ] Risk management

---

## ✅ ИТОГОВАЯ КАРТИНА

```
┌────────────────────────────────────────────────────────────────┐
│                    DUAL BOT SYSTEM                              │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│  🔴 SHORT BOT (liq-short-bot.onrender.com)                     │
│  ├── Сканирует: Перекупленные монеты (RSI > 70)                │
│  ├── Score: 0-100%                                              │
│  ├── Паттерны: REJECTION, TRAP, MEGA, DISTRIBUTION            │
│  ├── Сигналы: Telegram канал 🔴 LIQUIDITY SHORT                │
│  └── Данные: Upstash Redis (история, позиции)                 │
│                                                                 │
│  🟢 LONG BOT (liq-long-bot.onrender.com)                        │
│  ├── Сканирует: Перепроданные монеты (RSI < 30)                │
│  ├── Score: 0-100%                                              │
│  ├── Паттерны: REJECTION, TRAP, MEGA, ACCUMULATION            │
│  ├── Сигналы: Telegram канал 🟢 LIQUIDITY LONG                 │
│  └── Данные: Upstash Redis (история, позиции)                 │
│                                                                 │
│  ⏰ WAKE SERVICE (GitHub Actions)                               │
│  └── Будит оба бота каждые 5-10 минут                          │
│                                                                 │
│  💰 COST: $0/month (всё бесплатно)                              │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

---

**Готов начать разработку?** 🚀

Скажи:
1. **«Начинаем»** — создам структуру папок и начну кодить
2. **«Сначала инфраструктура»** — настроим Upstash и Render
3. **«Сначала SHORT»** — начнём с short-bot (DUMP Signals)
