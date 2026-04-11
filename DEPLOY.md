# 🚀 DEPLOYMENT GUIDE — Dual Bot System

## Что разворачиваем

- 🔴 **SHORT Bot** — ищет перекупленные монеты
- 🟢 **LONG Bot** — ищет перепроданные монеты
- 📊 **Upstash Redis** — база данных (бесплатно)
- ⏰ **GitHub Actions** — чтобы боты не засыпали

**Стоимость: $0**

---

## 📋 Пошаговая инструкция

### Шаг 1: Upstash Redis (База данных)

1. Перейди на [console.upstash.com](https://console.upstash.com)
2. Зарегистрируйся (Google/GitHub)
3. Создай новую базу:
   - **Name:** `liquidity-bots`
   - **Region:** `us-east-1` (ближе к Render)
4. Получи `REDIS_URL` (в формате `rediss://...`)
5. **Сохрани его — понадобится в Шаге 4**

---

### Шаг 2: Telegram Bots

#### Создать ботов:
1. Открой [@BotFather](https://t.me/botfather) в Telegram
2. Отправь `/newbot`
3. Введи имена:
   - **Short Bot:** `LiquidityShortBot`
   - **Long Bot:** `LiquidityLongBot`
4. Получи **токены** (например: `8241319215:AAHHLVM-...`)
5. **Сохрани токены**

#### Создать каналы:
1. Создай два канала:
   - `🔴 LIQUIDITY SHORT Signals`
   - `🟢 LIQUIDITY LONG Signals`
2. Добавь ботов администраторами в каналы
3. Получи **Chat ID**:
   - Отправь любое сообщение в канал
   - Перейди по ссылке: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Найди `"chat":{"id":-100xxxxxxxxx` — это Chat ID
4. **Сохрани Chat ID**

---

### Шаг 3: GitHub Repository

1. Создай новый репозиторий на GitHub
2. Загрузи все файлы из папки `LIQUIDITY_LONG_BOT/`
3. Структура должна быть:
```
repo/
├── short-bot/
│   ├── src/
│   │   └── main.py
│   ├── requirements.txt
│   └── render.yaml
├── long-bot/
│   ├── src/
│   │   └── main.py
│   ├── requirements.txt
│   └── render.yaml
├── shared/
│   ├── core/
│   │   ├── scorer.py
│   │   └── pattern_detector.py
│   ├── upstash/
│   │   └── redis_client.py
│   ├── utils/
│   │   └── binance_client.py
│   └── bot/
│       └── telegram.py
├── wake-service/
│   └── .github/
│       └── workflows/
│           └── wake-bots.yml
├── .env.example
└── DEPLOY.md
```

---

### Шаг 4: Render (Сервер)

#### SHORT Bot:
1. Перейди на [render.com](https://render.com)
2. New → Web Service
3. Connect GitHub repository
4. Настройки:
   - **Name:** `liq-short-bot`
   - **Root Directory:** `short-bot`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
5. **Environment Variables:**
   ```
   REDIS_URL=rediss://default:xxx@xxx.upstash.io:6379
   TELEGRAM_BOT_TOKEN=8241319215:AAHHLVM-...
   TELEGRAM_CHAT_ID=-1003867089540
   TELEGRAM_TOPIC_ID=6314  # Опционально
   MIN_SHORT_SCORE=65
   SCAN_INTERVAL=60
   SHORT_LEVERAGE=5-10
   ```
6. Click **Create Web Service**

#### LONG Bot:
1. New → Web Service
2. Настройки:
   - **Name:** `liq-long-bot`
   - **Root Directory:** `long-bot`
   - **Runtime:** Python 3
3. **Environment Variables:**
   ```
   REDIS_URL=rediss://default:xxx@xxx.upstash.io:6379
   TELEGRAM_BOT_TOKEN=8749850077:AAGUtT9...
   TELEGRAM_CHAT_ID=-1003307145951
   MIN_LONG_SCORE=65
   SCAN_INTERVAL=60
   LONG_LEVERAGE=3-5
   ```
4. Click **Create Web Service**

#### Проверка:
- Открой `https://liq-short-bot.onrender.com/health`
- Должен вернуть: `{"status": "healthy", ...}`
- Открой `https://liq-long-bot.onrender.com/health`
- Должен вернуть: `{"status": "healthy", ...}`

---

### Шаг 5: GitHub Actions (Wake Service)

1. В репозитории перейди в **Settings → Secrets → Actions**
2. Добавь **Repository Secrets** (не нужны для wake, но для будущего):
   - `REDIS_URL`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

3. GitHub Actions файл уже создан: `.github/workflows/wake-bots.yml`

4. Перейди в **Actions** вкладку
5. Выбери workflow **"Keep Bots Awake"**
6. Click **Enable workflow**

**Что делает:**
- Каждые 10 минут будит оба бота
- Запускает сканирование рынка
- Бесплатно (2000 минут в месяц)

---

### Шаг 6: Тестирование

#### Тест Telegram:
```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d "chat_id=<CHAT_ID>" \
  -d "text=🤖 Bot test message"
```

#### Тест Scan (Short):
```bash
curl -X POST https://liq-short-bot.onrender.com/api/scan
```

#### Тест Scan (Long):
```bash
curl -X POST https://liq-long-bot.onrender.com/api/scan
```

#### Проверь каналы в Telegram:
- Через 1-2 минуты должны прийти сигналы (если есть подходящие монеты)

---

## 🔧 Ручное управление

### Запустить сканирование:
```bash
# Short Bot
curl -X POST https://liq-short-bot.onrender.com/api/scan

# Long Bot  
curl -X POST https://liq-long-bot.onrender.com/api/scan
```

### Посмотреть сигналы:
```bash
# Активные сигналы Short
curl https://liq-short-bot.onrender.com/api/signals

# Активные сигналы Long
curl https://liq-long-bot.onrender.com/api/signals

# Сигналы по паре
curl https://liq-short-bot.onrender.com/api/signals/BTCUSDT
```

### Статистика:
```bash
# Short Bot статистика за 7 дней
curl https://liq-short-bot.onrender.com/api/stats?days=7

# Long Bot статистика за 7 дней
curl https://liq-long-bot.onrender.com/api/stats?days=7
```

---

## 📊 Мониторинг

### Health Check:
- **Short:** https://liq-short-bot.onrender.com/health
- **Long:** https://liq-long-bot.onrender.com/health

### Status:
- **Short:** https://liq-short-bot.onrender.com/status
- **Long:** https://liq-long-bot.onrender.com/status

### UptimeRobot (дополнительно):
1. Зарегистрируйся на [uptimerobot.com](https://uptimerobot.com)
2. Добавь два монитора (health check URLs)
3. Настрой email-алерты

---

## 🛠️ Устранение проблем

### Бот не отвечает:
```bash
# Проверь health
curl https://liq-short-bot.onrender.com/health

# Перезапусти через Render dashboard
# Click "Manual Deploy" → "Deploy latest commit"
```

### Нет сигналов:
- Проверь `MIN_SCORE` — может быть слишком высоким
- Проверь Telegram Chat ID
- Посмотри логи в Render Dashboard

### Redis ошибки:
- Проверь `REDIS_URL` — должен начинаться с `rediss://`
- Убедись что SSL включён
- Проверь лимиты Upstash (10,000 запросов/день)

### Telegram не работает:
- Проверь токен: `https://api.telegram.org/bot<TOKEN>/getMe`
- Убедись что бот добавлен в канал администратором
- Проверь Chat ID (должен быть с `-100`)

---

## 🚀 Масштабирование (после теста)

### Если всё работает:
1. **Увеличь watchlist:**
   - В `main.py` измени `min_volume_usdt` с 50M на 10M
   - Увеличь `symbols[:50]` до `symbols[:100]`

2. **Уменьши интервал:**
   - `SCAN_INTERVAL=30` (вместо 60)

3. **Добавь авто-торговлю:**
   - Интегрируй Bybit API
   - Автоматическое открытие позиций по сигналам

---

## ✅ Чек-лист деплоя

- [ ] Upstash Redis создан
- [ ] Telegram боты созданы
- [ ] Telegram каналы созданы
- [ ] GitHub репозиторий создан
- [ ] Render Short Bot деплойнут
- [ ] Render Long Bot деплойнут
- [ ] GitHub Actions включён
- [ ] Health check работает
- [ ] Тестовый сигнал пришёл
- [ ] Мониторинг настроен

---

## 🎉 Готово!

После деплоя у тебя будет:
- 🔴 SHORT бот — сканирует перекупленность
- 🟢 LONG бот — сканирует перепроданность
- 📊 Сигналы в Telegram каждые 10 минут
- 💾 База данных в Upstash
- ⏰ Авто-пробуждение через GitHub Actions

**Стоимость: $0/месяц** 🎊

---

## 📞 Поддержка

Если что-то не работает:
1. Проверь логи в Render Dashboard
2. Проверь статус `https://your-bot.onrender.com/health`
3. Убедись что все env variables заполнены
4. Перезапусти боты через Render

**Успехов в торговле!** 🚀
