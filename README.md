# 🤖 Liquidity Bots v5.0 — SMC/ICT Trading System

## 🔧 КРИТИЧЕСКИЕ ИСПРАВЛЕНИЯ v5.0

| # | Баг | Файл | Статус |
|---|-----|------|--------|
| 1 | `tbs_detected` → `tbs_found` (NameError) | long-bot/main.py | ✅ |
| 2 | MarketContextFilter не инициализирован | оба бота lifespan() | ✅ |
| 3 | TP формат SWEEP пути [(float,int)] | long-bot/main.py | ✅ |
| 4 | Circular import `_pnl` zombie cleanup | position_tracker.py | ✅ |
| 5 | BINGX_SECRET_KEY → BINGX_API_SECRET | .env.example | ✅ |
| 6 | Redis `.set()/.get()/.keys()` отсутствуют | redis_client.py | ✅ |
| 7 | get_redis_client не импортирован | micro_trailing_stop.py | ✅ |
| 8 | SL Buffer 1.0% → 1.5% | Config | ✅ |
| 9 | MIN_SCORE LONG 60→65, SHORT 65→67 | Config | ✅ |
| 10 | TP1 1.5%→2.5% (R:R был 1.25:1 → стало 1.67:1) | Config TP_LEVELS | ✅ |
| 11 | BREAKEVEN_AFTER_TP 1→2 | position_tracker.py | ✅ |
| 12 | Elliott confidence 0.5→0.7 | оба бота | ✅ |
| 13 | Score > 100 возможен → cap 100 | long-bot/main.py | ✅ |
| 14 | SL Cooldown 2ч после стопа по символу | scan_market + PT | ✅ |
| 15 | SHORT bot MarketContextFilter import | short-bot/main.py | ✅ |
| 16 | BingX 109400 → auto sync + log | bingx_client.py | ✅ |

## 🚀 Быстрый деплой на Render

```bash
git init && git add . && git commit -m "v5.0 — critical fixes"
git remote add origin https://github.com/your/repo.git
git push -u origin main
```

## ⚙️ Конфигурация .env (критичные переменные)

| Переменная | v4.0 (БАГ) | v5.0 (ИСПРАВЛЕНО) |
|---|---|---|
| `BINGX_API_SECRET` | был BINGX_SECRET_KEY | ✅ BINGX_API_SECRET |
| `LONG_SL_BUFFER` | 1.0% | ✅ 1.5% |
| `SHORT_SL_BUFFER` | 1.0% | ✅ 1.5% |
| `MIN_LONG_SCORE` | 60 | ✅ 65 |
| `MIN_SHORT_SCORE` | 65 | ✅ 67 |

