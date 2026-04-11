# 🚀 FULL LAUNCH GUIDE — Полная инструкция по запуску

## С нуля до работающих ботов за 60 минут

---

## 🎯 ЧТО БУДЕМ ДЕЛАТЬ

1. **Подготовка** (10 мин) — создаём аккаунты
2. **Настройка** (20 мин) — конфигурируем всё
3. **Деплой** (20 мин) — запускаем на сервере
4. **Тестирование** (10 мин) — проверяем работу

**Итого: 60 минут до первых сигналов!**

---

## 📋 ЧЕК-ЛИСТ ПЕРЕД СТАРТОМ

### Что нужно иметь:
- [ ] Email (любой)
- [ ] GitHub аккаунт (создадим)
- [ ] Telegram (уже есть)
- [ ] ~60 минут времени

### Что НЕ нужно:
- ❌ Кредитная карта (всё бесплатно)
- ❌ Опыт программирования
- ❌ Сервер (будем использовать бесплатный)

---

## ЧАСТЬ 1: ПОДГОТОВКА (10 минут)

### Шаг 1.1: Upstash Redis (База данных)

**Зачем:** Хранить сигналы и статистику

**Действия:**
1. Открой https://console.upstash.com
2. Нажми **Sign Up** → выбери **Continue with Google**
3. После входа нажми **Create Database**
4. Настрой:
   - **Database Name:** `liquidity-bots`
   - **Region:** `US East (N. Virginia)`
   - **Type:** `Redis`
   - **Plan:** `Free` (0.25 GB, 10k cmds/day)
5. Нажми **Create**
6. Перейди в созданную базу
7. Скопируй **Redis URL** (в формате `rediss://default:password@host:port`)
8. **Сохрани в блокнот!** Это `REDIS_URL`

**Время:** 3 минуты

---

### Шаг 1.2: Telegram Bots

**Зачем:** Отправлять сигналы

#### Создать первого бота (Short):
1. Открой Telegram → найди [@BotFather](https://t.me/botfather)
2. Нажми **Start** или отправь `/start`
3. Отправь `/newbot`
4. Введи имя: `Liquidity Short Bot`
5. Введи username: `liq_short_bot` (должно заканчиваться на bot)
6. Получи **токен** (например: `8241319215:AAHHLVM-_wD1VApgdYllljBaTihSrCVZ6eY`)
7. **Сохрани!** Это `SHORT_TELEGRAM_BOT_TOKEN`

#### Создать второго бота (Long):
1. Снова `/newbot` в BotFather
2. Имя: `Liquidity Long Bot`
3. Username: `liq_long_bot`
4. Получи токен
5. **Сохрани!** Это `LONG_TELEGRAM_BOT_TOKEN`

**Время:** 3 минуты

---

### Шаг 1.3: Telegram Каналы

**Зачем:** Получать сигналы в канал

#### Создать канал для SHORT:
1. В Telegram нажми **New Channel**
2. Название: `🔴 LIQUIDITY SHORT Signals`
3. Тип: `Public` или `Private`
4. Создай

#### Создать канал для LONG:
1. **New Channel**
2. Название: `🟢 LIQUIDITY LONG Signals`
3. Создай

#### Добавить ботов в каналы:
1. Открой канал `🔴 LIQUIDITY SHORT Signals`
2. Нажми на название → **Administrators**
3. **Add Administrator**
4. Найди `@liq_short_bot`
5. Права: `Post messages` ✅ (остальное по умолчанию)
6. Добавь

То же самое для LONG канала с `@liq_long_bot`

#### Получить Chat ID:

**Способ 1 (Public канал):**
- Открой веб-версию Telegram
- Найди канал
- URL будет: `https://web.telegram.org/a/#-1003867089540`
- **Chat ID = -1003867089540**

**Способ 2 (Private канал):**
1. Отправь любое сообщение в канал
2. Открой в браузере:
   ```
   https://api.telegram.org/bot<ТОКЕН>/getUpdates
   ```
3. Найди: `"chat":{"id":-100xxxxxxxxx`
4. **Скопируй число с -100**

Сохрани:
- `SHORT_TELEGRAM_CHAT_ID` (для 🔴)
- `LONG_TELEGRAM_CHAT_ID` (для 🟢)

**Время:** 4 минуты

---

### ИТОГО ЧАСТИ 1 — У тебя есть:
- ✅ REDIS_URL (из Upstash)
- ✅ SHORT_TELEGRAM_BOT_TOKEN
- ✅ LONG_TELEGRAM_BOT_TOKEN
- ✅ SHORT_TELEGRAM_CHAT_ID
- ✅ LONG_TELEGRAM_CHAT_ID

**Всё сохранено в блокноте?** Отлично! Идём дальше.

---

## ЧАСТЬ 2: НАСТРОЙКА (20 минут)

### Шаг 2.1: GitHub Repository

**Зачем:** Хранить код и деплоить

**Действия:**
1. Открой https://github.com
2. Зарегистрируйся (Sign up)
3. Подтверди email
4. Нажми зелёную кнопку **+** → **New repository**
5. **Repository name:** `liquidity-bots`
6. **Public** или **Private** (как хочешь)
7. **Add a README:** ✅ (галочка)
8. Нажми **Create repository**

**Время:** 3 минуты

---

### Шаг 2.2: Загрузить код

**Способ A: Через веб (простой)**

1. Открой папку с кодом на компьютере
2. Выбери ВСЕ файлы и папки
3. Перетащи их в GitHub репозиторий (веб-интерфейс)
4. Напиши комментарий: "Initial commit"
5. Нажми **Commit changes**

**Способ B: Через командную строку (если умеешь)**
```bash
cd liquidity-bots
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/USERNAME/liquidity-bots.git
git push -u origin main
```

**Проверь:** В репозитории должны быть:
- `short-bot/`
- `long-bot/`
- `shared/`
- `wake-service/`

**Время:** 5 минут

---

### Шаг 2.3: Создать .env файлы

В репозитории создай файл `.env` (через GitHub веб):

```
# Upstash Redis
REDIS_URL=rediss://default:YOUR_PASSWORD@YOUR_HOST.upstash.io:6379

# SHORT Bot Telegram
SHORT_TELEGRAM_BOT_TOKEN=8241319215:AAHHLVM-_wD1VApgdYllljBaTihSrCVZ6eY
SHORT_TELEGRAM_CHAT_ID=-1003867089540

# LONG Bot Telegram  
LONG_TELEGRAM_BOT_TOKEN=8749850077:AAGUtT9vmjGed_VKWG7gzEMKV4w4zf8WPmc
LONG_TELEGRAM_CHAT_ID=-1003307145951

# Bot Settings
SCAN_INTERVAL=60
MIN_SHORT_SCORE=75
MIN_LONG_SCORE=75
SHORT_LEVERAGE=5-10
LONG_LEVERAGE=3-5
```

**Замени** `YOUR_PASSWORD`, `YOUR_HOST` и токены на свои данные!

**Commit changes** → **Commit directly to main branch**

**Время:** 3 минуты

---

### Шаг 2.4: Настроить GitHub Actions

1. В репозитории перейди в **Settings** → **Secrets and variables** → **Actions**
2. Нажми **New repository secret**
3. Добавь секреты по одному:
   - Name: `REDIS_URL` → Value: твой URL
   - Name: `SHORT_TELEGRAM_BOT_TOKEN` → Value: токен
   - Name: `SHORT_TELEGRAM_CHAT_ID` → Value: chat id
   - Name: `LONG_TELEGRAM_BOT_TOKEN` → Value: токен
   - Name: `LONG_TELEGRAM_CHAT_ID` → Value: chat id

**Проверь:** Должно быть 5 секретов.

**Время:** 5 минут

---

### Шаг 2.5: Включить GitHub Actions

1. Перейди в **Actions** вкладку
2. Ты увидишь workflow "Keep Bots Awake"
3. Нажми **I understand my workflows, go ahead and enable them**
4. Нажми на workflow **Keep Bots Awake**
5. Нажми **Enable workflow**

**Время:** 2 минуты

---

### ИТОГО ЧАСТИ 2 — У тебя:
- ✅ GitHub репозиторий с кодом
- ✅ Все секреты настроены
- ✅ GitHub Actions включён

---

## ЧАСТЬ 3: ДЕПЛОЙ (20 минут)

### Шаг 3.1: Render.com — SHORT Bot

1. Открой https://render.com
2. Зарегистрируйся (Sign Up, можно через GitHub)
3. Подтверди email
4. На дашборде нажми **New +** → **Web Service**
5. **Build and deploy from Git repository** → Next
6. Выбери свой репозиторий `liquidity-bots`
7. Настрой:
   - **Name:** `liq-short-bot`
   - **Root Directory:** `short-bot` ← Важно!
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
8. **Instance Type:** Free
9. Нажми **Advanced** → **Add Environment Variable**:
   ```
   REDIS_URL = rediss://...
   TELEGRAM_BOT_TOKEN = 8241319215:...
   TELEGRAM_CHAT_ID = -1003867089540
   MIN_SHORT_SCORE = 75
   SCAN_INTERVAL = 60
   SHORT_LEVERAGE = 5-10
   ```
10. Нажми **Create Web Service**

**Жди:** 5-10 минут пока соберётся и задеплоится.

**Проверь:** После деплоя открой:
```
https://liq-short-bot.onrender.com/health
```

Должно вернуть: `{"status": "healthy", ...}`

**Время:** 10 минут

---

### Шаг 3.2: Render.com — LONG Bot

1. На Render нажми **New +** → **Web Service**
2. Тот же репозиторий
3. Настрой:
   - **Name:** `liq-long-bot`
   - **Root Directory:** `long-bot` ← Важно!
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
4. **Environment Variables**:
   ```
   REDIS_URL = rediss://...
   TELEGRAM_BOT_TOKEN = 8749850077:...
   TELEGRAM_CHAT_ID = -1003307145951
   MIN_LONG_SCORE = 75
   SCAN_INTERVAL = 60
   LONG_LEVERAGE = 3-5
   ```
5. **Create Web Service**

**Проверь:**
```
https://liq-long-bot.onrender.com/health
```

**Время:** 10 минут

---

### ИТОГО ЧАСТИ 3 — У тебя:
- ✅ SHORT Bot работает на Render
- ✅ LONG Bot работает на Render
- ✅ Оба отвечают на health checks

---

## ЧАСТЬ 4: ТЕСТИРОВАНИЕ (10 минут)

### Шаг 4.1: Проверка Telegram

1. В канале `🔴 LIQUIDITY SHORT Signals` отправь:
   ```
   /start
   ```
   (это команда для бота)

2. Проверь что бот не ругается на ошибки

3. Сделай тестовый запрос:
   ```bash
   curl -X POST https://api.telegram.org/bot<SHORT_TOKEN>/sendMessage \
     -d "chat_id=<SHORT_CHAT_ID>" \
     -d "text=🤖 Test from bot"
   ```

   Должно прийти сообщение в канал.

**Время:** 2 минуты

---

### Шаг 4.2: Первое сканирование

**Запусти ручное сканирование:**

```bash
# SHORT Bot
curl -X POST https://liq-short-bot.onrender.com/api/scan

# LONG Bot
curl -X POST https://liq-long-bot.onrender.com/api/scan
```

**Или через браузер:**
- Открой: `https://liq-short-bot.onrender.com/api/scan`
- Открой: `https://liq-long-bot.onrender.com/api/scan`

**Результат:**
- Возвращает `{"message": "Scan triggered", ...}`
- Через 2-5 минут проверь Telegram каналы

**Время:** 5 минут

---

### Шаг 4.3: Проверка статуса

Открой в браузере:
```
https://liq-short-bot.onrender.com/status
https://liq-long-bot.onrender.com/status
```

Должно показать:
- `is_running: true`
- `watchlist_count: 50`
- `last_scan: ...`

**Время:** 2 минуты

---

### Шаг 4.4: Жди сигналы

**Через 5-10 минут проверь Telegram каналы:**

Если есть подходящие монеты, увидишь:
```
🔴 SHORT SIGNAL | Score: 78%
💎 SYMBOL: BTCUSDT.P
...
```

Если нет сигналов — это нормально! Значит нет идеальных сетапов прямо сейчас.

**Боты сканируют каждые 10 минут (через GitHub Actions).**

**Время:** 1 минута (проверка)

---

## 🎉 ГОТОВО! ТЕПЕРЬ У ТЕБЯ:

✅ **SHORT Bot** — сканирует перекупленность  
✅ **LONG Bot** — сканирует перепроданность  
✅ **Telegram каналы** — получаешь сигналы  
✅ **Redis база** — хранит историю  
✅ **Авто-сканирование** — каждые 10 минут  
✅ **Бесплатный хостинг** — $0  

---

## 📊 ЧТО БУДЕТ ДАЛЬШЕ

### Через 1 час:
- Боты просканируют рынок 6 раз
- Возможно 1-3 сигнала в Telegram
- Можно начать торговать вручную

### Через 1 день:
- ~144 сканирования
- 5-15 сигналов
- Статистика начнёт собираться

### Через 1 неделю:
- Понятно какие паттерны работают лучше
- Можно оптимизировать настройки
- Можно добавить авто-торговлю

---

## 🛠️ УПРАВЛЕНИЕ БОТАМИ

### Посмотреть статус:
```bash
# Health check
curl https://liq-short-bot.onrender.com/health

# Full status
curl https://liq-short-bot.onrender.com/status

# Active signals
curl https://liq-short-bot.onrender.com/api/signals

# Stats
curl https://liq-short-bot.onrender.com/api/stats?days=7
```

### Перезапустить:
1. Зайди на https://render.com
2. Найди свой сервис
3. Нажми **Manual Deploy** → **Deploy latest commit**

### Остановить:
1. В Render нажми на сервис
2. **Settings** → **Suspend**

---

## 💡 СОВЕТЫ

### Если нет сигналов:
- Уменьши `MIN_SCORE` до 70 или 65
- Подожди вечера (больше волатильности)
- Проверь что боты работают (`/health`)

### Если слишком много сигналов:
- Увеличь `MIN_SCORE` до 80
- Добавь фильтры (время, волатильность)

### Для 70%+ Win Rate:
- Используй `MIN_SCORE = 75`
- Жди только сигналы с 4+ сильными факторами
- Не торгуй если BTC сильно движется против

---

## 🆘 ПРОБЛЕМЫ И РЕШЕНИЯ

### "Bot is not running":
- Перезапусти через Render (Manual Deploy)
- Проверь логи (Logs вкладка)

### "Redis connection failed":
- Проверь `REDIS_URL` (должен начинаться с `rediss://`)
- Убедись что SSL включён в Upstash

### "Telegram error":
- Проверь токен через: `https://api.telegram.org/bot<TOKEN>/getMe`
- Убедись что бот администратор в канале
- Проверь Chat ID (должен быть с -100)

### "No signals":
- Это нормально если нет хороших сетапов
- Проверь через час/день
- Уменьши `MIN_SCORE` для теста

---

## 🎓 ЧТО ДАЛЬШЕ

1. **Торгуй по сигналам** (ручная торговля)
2. **Собирай статистику** (что работает лучше)
3. **Оптимизируй** (параметры под рынок)
4. **Добавь авто-торговлю** (Bybit API)
5. **Масштабируй** (больше монет, чаще скан)

---

## 🎉 ПОЗДРАВЛЯЮ!

**У тебя теперь есть полная автоматизированная торговая система!**

- 🔴 SHORT Bot готов
- 🟢 LONG Bot готов
- 📊 Сигналы приходят автоматически
- 💰 Можно начинать торговать

**Успехов и профита!** 🚀📈💰

---

## 📞 НУЖНА ПОМОЩЬ?

Если что-то не работает:
1. Проверь логи в Render (вкладка Logs)
2. Проверь все env variables
3. Перезапусти ботов
4. Спрашивай — помогу!

**Теперь ты трейдер с алгоритмической системой!** 🤖📈
