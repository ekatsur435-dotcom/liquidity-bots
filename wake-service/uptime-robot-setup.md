# UptimeRobot Setup Guide

## Быстрая настройка (альтернатива GitHub Actions)

### Шаг 1: Регистрация
1. Перейди на [uptimerobot.com](https://uptimerobot.com)
2. Зарегистрируй бесплатный аккаунт

### Шаг 2: Добавить мониторы

#### Short Bot Monitor:
- **Monitor Type:** HTTP(s)
- **Friendly Name:** Short Bot Health
- **URL:** `https://liq-short-bot.onrender.com/health`
- **Monitoring Interval:** 5 minutes
- **Alert Contacts:** Email

#### Long Bot Monitor:
- **Monitor Type:** HTTP(s)
- **Friendly Name:** Long Bot Health
- **URL:** `https://liq-long-bot.onrender.com/health`
- **Monitoring Interval:** 5 minutes
- **Alert Contacts:** Email

### Шаг 3: Настроить алерты

#### Если бот упал:
- Email уведомление
- Push notification (через приложение UptimeRobot)

### Преимущества UptimeRobot:
- ✅ Простая настройка (5 минут)
- ✅ Email/SMS алерты если бот падает
- ✅ 50 мониторов бесплатно
- ✅ История аптайма (графики)

### Недостатки:
- ❌ Не запускает сканирование (только будит)
- Для сканирования используй GitHub Actions или вручную

## Комбинированный подход (Рекомендуется):

1. **UptimeRobot:** Будит ботов каждые 5 минут + мониторинг
2. **GitHub Actions:** Запускает сканирование каждые 10 минут

Так боты никогда не уснут и регулярно сканируют рынок!
