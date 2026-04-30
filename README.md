# 🤖 Liquidity Bots v5.1 — Deploy Ready

## 🚀 Деплой на Render (5 шагов)

```
1. Залей этот архив на GitHub (или форкни → замени файлы)
2. Render.com → New → Blueprint → подключи репо
3. Render автоматически создаёт 3 сервиса из render.yaml
4. Вручную добавь SECRET ENV переменные (ключи API)
5. Deploy → смотри логи
```

## 🔑 Обязательные ENV Variables (секреты — вводить вручную)

### LONG Bot & SHORT Bot (одинаковые):
| Variable | Где взять |
|----------|-----------|
| `BINGX_API_KEY` | BingX → API Management |
| `BINGX_API_SECRET` | BingX → API Management |
| `BINANCE_API_KEY` | Binance → API (read-only) |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | ID твоего чата |
| `REDIS_URL` | Upstash → Redis → Connect → **rediss://** URL |

### Dashboard (отдельный формат Redis!):
| Variable | Где взять |
|----------|-----------|
| `UPSTASH_REDIS_REST_URL` | Upstash → Redis → REST API → **https://** URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash → REST API → Token |

> ⚠️ REDIS_URL и UPSTASH_REDIS_REST_URL — это РАЗНЫЕ URL одного Redis!
> В Upstash: Connection String (rediss://) для ботов, REST API URL (https://) для дашборда

## ✅ Что исправлено в v5.1

### 🔴 Критические баги
1. **execute() отсутствовал** → Dashboard показывал 0 позиций, неверный P&L
2. **SHORT бот молчал** → не работал Telegram (SHORT_TELEGRAM_BOT_TOKEN не задан → fallback добавлен)
3. **Символ ATOMUSDT → must end with -USDT** → исправлен формат для ордеров
4. **109425 спам в логах** → добавлен whitelist фильтр (только BingX символы)
5. **MicroTrailingStop crash** → импорт redis_client исправлен

### 🟠 Высокие (Win Rate)
6. SL 1.0% → **1.5%** — меньше ложных стопов
7. MIN_SCORE LONG: 60 → **65**, SHORT: 67 → **62**
8. TP1: 1.5% → **2.5%** — реалистичный R:R 1.67:1
9. BingX watchlist pre-filter — сканируем только листингованные монеты

## 📊 Telegram команды
```
/status    → статус бота
/positions → открытые позиции
/balance   → баланс BingX
/sync      → синхронизация с биржей
/pause     → пауза торговли
/resume    → возобновить
/ping      → проверка связи
```
