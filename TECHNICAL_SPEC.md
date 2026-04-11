# 🔧 TECHNICAL SPECIFICATION — Liquidity Long Bot

## 📁 Структура проекта

```
LIQUIDITY_LONG_BOT/
├── 📄 PROJECT_PLAN.md          # Этот документ
├── 📄 TECHNICAL_SPEC.md        # Техническая спецификация
├── 📄 ARCHITECTURE.md          # Архитектура системы
├── 📄 PATTERNS.md              # Детальное описание паттернов
├── 📄 SCORING.md               # Алгоритм расчета Long Score
├── 📄 RISK_MGMT.md             # Риск-менеджмент
├── 📄 API_INTEGRATION.md       # Интеграция с биржами
├── 📄 TELEGRAM_BOT.md          # Спецификация Telegram бота
├── 📄 DEPLOYMENT.md            # Инструкции по деплою
│
├── 📁 src/                     # Исходный код
│   ├── 📁 core/               # Ядро системы
│   │   ├── scanner.py         # Сканер монет
│   │   ├── scorer.py          # Расчет Long Score
│   │   ├── pattern_detector.py # Детектор паттернов
│   │   ├── signal_generator.py # Генератор сигналов
│   │   └── models.py          # Data models
│   │
│   ├── 📁 data/               # Работа с данными
│   │   ├── binance_client.py  # Binance API клиент
│   │   ├── bybit_client.py    # Bybit API клиент
│   │   ├── data_aggregator.py # Агрегатор данных
│   │   └── cache.py           # Кэширование
│   │
│   ├── 📁 indicators/         # Технические индикаторы
│   │   ├── rsi.py             # RSI
│   │   ├── supertrend.py      # SuperTrend
│   │   ├── volume_profile.py  # Профиль объема
│   │   ├── delta.py           # Дельта анализ
│   │   └── wyckoff.py         # Wyckoff паттерны
│   │
│   ├── 📁 risk/               # Риск-менеджмент
│   │   ├── position_sizer.py  # Размер позиции
│   │   ├── sl_calculator.py   # Расчет SL
│   │   ├── tp_calculator.py   # Расчет TP
│   │   └── risk_manager.py    # Главный менеджер рисков
│   │
│   ├── 📁 execution/          # Исполнение ордеров
│   │   ├── order_executor.py  # Исполнитель ордеров
│   │   ├── bybit_executor.py  # Bybit специфика
│   │   └── trailing_stop.py   # Трейлинг-стоп
│   │
│   ├── 📁 bot/                # Telegram бот
│   │   ├── bot.py             # Основной бот
│   │   ├── handlers.py        # Обработчики команд
│   │   ├── notifications.py   # Уведомления
│   │   └── formatter.py       # Форматирование сообщений
│   │
│   ├── 📁 web/                # Web dashboard (опционально)
│   │   ├── app.py             # Flask/FastAPI app
│   │   ├── routes.py          # Routes
│   │   └── templates/         # HTML templates
│   │
│   ├── 📁 utils/              # Утилиты
│   │   ├── config.py          # Конфигурация
│   │   ├── logger.py          # Логирование
│   │   ├── validators.py        # Валидация
│   │   └── helpers.py         # Хелперы
│   │
│   └── main.py                # Точка входа
│
├── 📁 tests/                   # Тесты
│   ├── test_scanner.py
│   ├── test_scorer.py
│   ├── test_patterns.py
│   └── test_integration.py
│
├── 📁 config/                # Конфигурации
│   ├── .env.example          # Пример env
│   ├── settings.yaml         # Настройки
│   └── pairs.json            # Список монет
│
├── 📁 docs/                    # Дополнительная документация
│   ├── CHANGELOG.md
│   └── TODO.md
│
├── 📁 scripts/                 # Вспомогательные скрипты
│   ├── setup.sh              # Установка
│   ├── deploy.sh             # Деплой
│   └── backtest.py           # Бэктестинг
│
├── requirements.txt            # Python зависимости
├── Dockerfile                # Docker образ
├── docker-compose.yml        # Docker Compose
├── render.yaml               # Render.com конфиг
└── README.md                 # Основной README
```

---

## 🎯 Модули системы

### 1. Core Module (`src/core/`)

#### `scanner.py`
Сканирует все фьючерсы на Binance/Bybit и находит перепроданные монеты.

**Класс:** `MarketScanner`

**Методы:**
```python
class MarketScanner:
    def __init__(self, config: ScannerConfig)
    
    async def scan_all(self) -> List[ScannedCoin]
    # Сканирует все монеты из watchlist
    
    async def scan_single(self, symbol: str) -> ScannedCoin
    # Сканирует одну монету
    
    def filter_by_volume(self, coins: List[ScannedCoin], min_volume: float) -> List[ScannedCoin]
    # Фильтр по минимальному объему
    
    def filter_by_score(self, coins: List[ScannedCoin], min_score: float) -> List[ScannedCoin]
    # Фильтр по минимальному скору
```

**Структура данных:**
```python
@dataclass
class ScannedCoin:
    symbol: str
    price: float
    rsi_1h: float
    funding_rate: float
    funding_accumulated: float
    long_short_ratio: float
    open_interest: float
    oi_change_4h: float
    volume_24h: float
    delta_1h: float
    hourly_deltas: List[float]  # Последние 7 часов
    timestamp: datetime
```

---

#### `scorer.py`
Рассчитывает Long Score на основе всех параметров.

**Класс:** `LongScorer`

**Методы:**
```python
class LongScorer:
    def __init__(self, config: ScoringConfig)
    
    def calculate_score(self, coin: ScannedCoin, patterns: List[Pattern]) -> ScoreResult
    # Основной метод расчета скора
    
    def calculate_rsi_component(self, rsi: float) -> int
    # Компонент RSI (0-20 очков)
    
    def calculate_funding_component(self, funding: float) -> int
    # Компонент фандинга (0-15 очков)
    
    def calculate_ratio_component(self, long_ratio: float) -> int
    # Компонент L/S ratio (0-15 очков)
    
    def calculate_oi_component(self, oi_change: float, price_change: float) -> int
    # Компонент OI (0-15 очков)
    
    def calculate_delta_component(self, hourly_deltas: List[float], price_trend: str) -> int
    # Компонент дельты (0-20 очков)
    
    def calculate_pattern_component(self, patterns: List[Pattern]) -> int
    # Компонент паттернов (0-30 очков)
```

**Структура результата:**
```python
@dataclass
class ScoreResult:
    total_score: int           # 0-100
    max_possible: int          # 100
    is_valid: bool             # True если >= min_score
    components: Dict[str, int]   # Распределение очков
    confidence: str            # 'low', 'medium', 'high', 'very_high'
    reasons: List[str]         # Почему этот скор
```

---

#### `pattern_detector.py`
Обнаруживает паттерны на 15m таймфрейме.

**Класс:** `PatternDetector`

**Методы:**
```python
class PatternDetector:
    def __init__(self, config: PatternConfig)
    
    def detect_all(self, ohlcv_15m: DataFrame, delta_data: List[float]) -> List[Pattern]
    # Обнаруживает все паттерны
    
    def detect_rejection_long(self, df: DataFrame, delta: List[float]) -> Optional[Pattern]
    # REJECTION LONG: отбой от поддержки
    
    def detect_trap_short(self, df: DataFrame, delta: List[float]) -> Optional[Pattern]
    # TRAP SHORT: ловушка для шортистов
    
    def detect_mega_long(self, df: DataFrame, delta: List[float], volume: List[float]) -> Optional[Pattern]
    # MEGA LONG: доминация покупателей
    
    def detect_accumulation(self, df: DataFrame, delta: List[float], volume: List[float]) -> Optional[Pattern]
    # ACCUMULATION: накопление крупным игроком
```

**Структура паттерна:**
```python
@dataclass
class Pattern:
    name: str                  # 'REJECTION_LONG', 'TRAP_SHORT', etc.
    type: str                  # 'long'
    strength: int              # 10-30 очков
    freshness: int             # Минут назад
    candles_ago: int           # На какой свече обнаружен (0=текущая)
    volume_multiplier: float   # Относительно среднего
    delta_at_trigger: float    # Дельта на свече паттерна
    entry_price: float         # Рекомендуемый вход
    stop_loss: float           # Рекомендуемый SL
    confidence: str            # 'weak', 'moderate', 'strong'
    description: str           # Описание для пользователя
```

---

#### `signal_generator.py`
Генерирует торговые сигналы на основе скора и паттернов.

**Класс:** `SignalGenerator`

**Методы:**
```python
class SignalGenerator:
    def __init__(self, scorer: LongScorer, risk_manager: RiskManager)
    
    def generate_signal(self, coin: ScannedCoin) -> Optional[LongSignal]
    # Генерация сигнала (или None если не проходит)
    
    def validate_signal(self, signal: LongSignal) -> Tuple[bool, List[str]]
    # Валидация сигнала (дедупликация, фильтры)
    
    def prioritize_signals(self, signals: List[LongSignal]) -> List[LongSignal]
    # Сортировка по приоритету (скор, свежесть паттерна)
    
    def format_signal_message(self, signal: LongSignal) -> str
    # Форматирование для Telegram
```

**Структура сигнала:**
```python
@dataclass
class LongSignal:
    symbol: str
    direction: str = 'LONG'
    score: ScoreResult
    entry_price: float
    patterns: List[Pattern]
    stop_loss: float
    take_profits: List[Tuple[float, float]]  # [(price, percentage), ...]
    recommended_leverage: int
    max_position_size: float
    timestamp: datetime
    expiry: datetime           # Сигнал действителен до
    risk_reward_ratio: float
```

---

### 2. Data Module (`src/data/`)

#### `binance_client.py`
Клиент для Binance Futures API.

**Класс:** `BinanceFuturesClient`

**Методы:**
```python
class BinanceFuturesClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False)
    
    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> DataFrame
    # Получение свечей
    
    async def get_funding_rate(self, symbol: str) -> Dict
    # Текущий фандинг
    
    async def get_funding_history(self, symbol: str, limit: int = 4) -> List[Dict]
    # История фандинга (накопленный)
    
    async def get_open_interest(self, symbol: str, timeframe: str = '1h') -> Dict
    # Открытый интерес
    
    async def get_long_short_ratio(self, symbol: str) -> Dict
    # Соотношение лонгов/шортов
    
    async def get_delta(self, symbol: str, timeframe: str = '1h') -> float
    # Дельта (buy volume - sell volume)
    
    async def get_24h_stats(self, symbol: str) -> Dict
    # Статистика за 24ч
    
    async def get_all_symbols(self) -> List[str]
    # Список всех фьючерсов
```

---

#### `bybit_client.py`
Клиент для Bybit API (трейдинг).

**Класс:** `BybitTradingClient`

**Методы:**
```python
class BybitTradingClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True)
    
    async def get_balance(self, coin: str = 'USDT') -> float
    # Баланс
    
    async def set_leverage(self, symbol: str, leverage: int)
    # Установка плеча
    
    async def open_position(
        self, 
        symbol: str, 
        side: str,           # 'Buy' для LONG
        qty: float,
        leverage: int,
        stop_loss: float,
        take_profits: List[Tuple[float, float]]
    ) -> Dict
    # Открытие позиции
    
    async def close_position(self, symbol: str, qty: float = None)
    # Закрытие позиции
    
    async def update_stop_loss(self, symbol: str, new_sl: float)
    # Обновление SL
    
    async def get_positions(self) -> List[Dict]
    # Активные позиции
    
    async def get_order_history(self, symbol: str = None) -> List[Dict]
    # История ордеров
```

---

### 3. Risk Module (`src/risk/`)

#### `position_sizer.py`
Расчет размера позиции на основе риска.

**Класс:** `PositionSizer`

```python
class PositionSizer:
    def __init__(self, risk_per_trade: float = 0.01)
    
    def calculate_position_size(
        self, 
        balance: float,
        entry_price: float,
        stop_loss: float,
        leverage: int,
        risk_percent: float = None
    ) -> PositionSize
    # Расчет размера позиции
    
    def validate_position(self, size: PositionSize, max_positions: int, current_positions: int) -> bool
    # Проверка лимитов
```

**Структура:**
```python
@dataclass
class PositionSize:
    quantity: float           # Количество контрактов
    margin_required: float    # Требуемая маржа
    risk_amount: float        # Сумма риска в USD
    risk_percent: float       # Процент риска
    max_leverage: int         # Максимальное рекомендуемое плечо
    notional_value: float     # Номинальная стоимость
```

---

#### `sl_calculator.py`
Расчет Stop Loss уровней.

**Класс:** `StopLossCalculator`

```python
class StopLossCalculator:
    def __init__(self, config: SLConfig)
    
    def calculate_sl(
        self,
        entry_price: float,
        ohlcv: DataFrame,
        atr: float,
        pattern: Pattern = None,
        method: str = 'smart'  # 'fixed', 'atr', 'structure', 'smart'
    ) -> float
    # Расчет SL
    
    def calculate_structure_sl(self, entry: float, df: DataFrame, buffer_pct: float) -> float
    # SL за структурный свинг-лой
    
    def calculate_atr_sl(self, entry: float, atr: float, multiplier: float) -> float
    # ATR-based SL
    
    def validate_sl_distance(self, entry: float, sl: float, min_atr_mult: float, atr: float) -> bool
    # Проверка минимальной дистанции
```

---

#### `tp_calculator.py`
Расчет Take Profit уровней (6 уровней как у Статхэма).

**Класс:** `TakeProfitCalculator`

```python
class TakeProfitCalculator:
    def __init__(self, config: TPConfig)
    
    def calculate_tp_levels(
        self,
        entry_price: float,
        stop_loss: float,
        risk_reward: float = 2.0,
        method: str = 'fibonacci'  # 'fixed', 'fibonacci', 'atr', 'vwap'
    ) -> List[TakeProfitLevel]
    # Расчет уровней TP
    
    def calculate_smart_tp(
        self,
        entry: float,
        resistance_levels: List[float],
        vwap: float,
        fib_618: float,
        fib_786: float
    ) -> List[TakeProfitLevel]
    # Умные TP на основе уровней
```

**Структура:**
```python
@dataclass
class TakeProfitLevel:
    level: int              # 1-6
    price: float
    percentage: float       # % от позиции для закрытия
    r_ratio: float          # Risk/Reward ratio
    description: str        # "TP1 - 1.5R", "TP2 - 3R", etc.
```

---

### 4. Execution Module (`src/execution/`)

#### `order_executor.py`
Исполнение ордеров с управлением.

**Класс:** `OrderExecutor`

```python
class OrderExecutor:
    def __init__(self, bybit_client: BybitTradingClient, risk_manager: RiskManager)
    
    async def execute_signal(self, signal: LongSignal) -> ExecutionResult
    # Исполнение сигнала
    
    async def open_long_position(self, signal: LongSignal) -> Dict
    # Открытие LONG позиции
    
    async def close_position_partial(self, symbol: str, percentage: float)
    # Частичное закрытие
    
    async def move_stop_to_entry(self, symbol: str, entry_price: float)
    # Перенос SL на точку входа (Break-Even)
    
    async def update_trailing_stop(self, symbol: str, current_price: float, activation_price: float)
    # Обновление трейлинг-стопа
```

---

#### `trailing_stop.py`
Управление трейлинг-стопом.

**Класс:** `TrailingStopManager`

```python
class TrailingStopManager:
    def __init__(self, activation_pct: float, trail_pct: float)
    
    def should_activate(self, entry_price: float, current_price: float) -> bool
    # Проверка активации трейлинга
    
    def calculate_new_sl(self, highest_price: float, current_sl: float) -> float
    # Расчет нового SL
    
    def update_trail(self, position: Position, current_price: float) -> Optional[float]
    # Обновление трейлинг-стопа
```

---

### 5. Bot Module (`src/bot/`)

#### `bot.py`
Telegram бот для управления.

**Класс:** `LongBot`

```python
class LongBot:
    def __init__(self, token: str, signal_generator: SignalGenerator, executor: OrderExecutor)
    
    async def start(self)
    # Запуск бота
    
    async def send_signal_alert(self, signal: LongSignal)
    # Отправка сигнала
    
    async def send_position_update(self, position: Position, pnl: float)
    # Обновление позиции
    
    async def send_error_alert(self, error: Exception)
    # Ошибка
    
    # Handlers:
    async def cmd_start(self, message: types.Message)
    async def cmd_status(self, message: types.Message)
    async def cmd_positions(self, message: types.Message)
    async def cmd_stats(self, message: types.Message)
    async def cmd_stop(self, message: types.Message)
```

---

## 🔄 Workflow (Поток данных)

```
┌────────────────────────────────────────────────────────────────┐
│                        WORKFLOW                                 │
└────────────────────────────────────────────────────────────────┘

1. SCHEDULER (каждые 60 секунд)
   └─> trigger_scan()

2. SCANNER
   └─> scan_all_symbols()
       ├─> Binance API: OHLCV, Funding, OI, L/S Ratio
       ├─> Calculate hourly deltas
       └─> Filter by volume & basic criteria
           └─> List[ScannedCoin]

3. FOR EACH COIN:
   
   a) PATTERN DETECTOR
      └─> detect_patterns()
          ├─> Load 15m candles
          ├─> Check REJECTION_LONG
          ├─> Check TRAP_SHORT
          ├─> Check MEGA_LONG
          ├─> Check ACCUMULATION
          └─> List[Pattern]
   
   b) SCORER
      └─> calculate_score()
          ├─> RSI component
          ├─> Funding component
          ├─> L/S Ratio component
          ├─> OI component
          ├─> Delta component
          └─> Pattern component
              └─> ScoreResult
   
   c) CHECK: score >= 65%?
      └─> NO: skip
      └─> YES: continue

4. SIGNAL GENERATOR
   └─> generate_signal()
       ├─> RiskManager.calculate_position_size()
       ├─> SLCalculator.calculate_sl()
       ├─> TPCalculator.calculate_tp_levels()
       └─> LongSignal

5. RISK VALIDATION
   └─> validate_signal()
       ├─> Check: max positions not exceeded?
       ├─> Check: symbol not already in position?
       ├─> Check: recent signal for this symbol?
       └─> YES/NO

6. EXECUTION (если auto-trading enabled)
   └─> execute_signal()
       ├─> Bybit: set_leverage()
       ├─> Bybit: open_position()
       ├─> Save to state
       └─> ExecutionResult

7. NOTIFICATION
   └─> Telegram
       ├─> Signal alert (score >= 65%)
       ├─> Entry confirmation
       ├─> TP hit notification
       ├─> SL hit notification
       └─> Position updates

8. MONITORING (каждые 30 секунд)
   └─> check_open_positions()
       ├─> Update PnL
       ├─> Check TP levels hit
       ├─> Update trailing stops
       └─> Send notifications
```

---

## 📡 API Endpoints (если Web Dashboard)

### REST API:

```
GET  /api/status              → Статус бота
GET  /api/signals             → Активные сигналы
GET  /api/signals/history     → История сигналов
GET  /api/positions           → Открытые позиции
GET  /api/positions/history   → История позиций
GET  /api/stats               → Статистика торговли
GET  /api/scanner/status      → Статус сканера
POST /api/scanner/force        → Принудительный скан
POST /api/settings            → Обновление настроек
POST /api/webhook/tradingview → Webhook от TradingView
```

### WebSocket (для real-time updates):

```
ws://host/ws/signals          → Поток сигналов
ws://host/ws/positions        → Обновления позиций
ws://host/ws/pnl              → Обновления PnL
```

---

## 📊 Database Schema (если нужна БД)

```sql
-- Signals table
CREATE TABLE signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(10) DEFAULT 'LONG',
    score INTEGER NOT NULL,
    entry_price DECIMAL(18,8) NOT NULL,
    stop_loss DECIMAL(18,8) NOT NULL,
    patterns JSONB,
    timestamp TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'active',  -- active, expired, executed, cancelled
    expiry TIMESTAMP
);

-- Positions table
CREATE TABLE positions (
    id SERIAL PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    symbol VARCHAR(20) NOT NULL,
    entry_price DECIMAL(18,8) NOT NULL,
    quantity DECIMAL(18,8) NOT NULL,
    leverage INTEGER NOT NULL,
    stop_loss DECIMAL(18,8) NOT NULL,
    take_profits JSONB,
    status VARCHAR(20) DEFAULT 'open',  -- open, partial, closed
    opened_at TIMESTAMP DEFAULT NOW(),
    closed_at TIMESTAMP,
    pnl DECIMAL(18,8),
    pnl_percent DECIMAL(8,4)
);

-- Trades table (individual fills)
CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    position_id INTEGER REFERENCES positions(id),
    side VARCHAR(10) NOT NULL,  -- buy, sell
    price DECIMAL(18,8) NOT NULL,
    quantity DECIMAL(18,8) NOT NULL,
    fee DECIMAL(18,8),
    timestamp TIMESTAMP DEFAULT NOW()
);

-- Statistics table (daily/weekly aggregated)
CREATE TABLE statistics (
    id SERIAL PRIMARY KEY,
    period VARCHAR(20) NOT NULL,  -- daily, weekly, monthly
    date DATE NOT NULL,
    total_signals INTEGER DEFAULT 0,
    total_positions INTEGER DEFAULT 0,
    winning_positions INTEGER DEFAULT 0,
    losing_positions INTEGER DEFAULT 0,
    total_pnl DECIMAL(18,8) DEFAULT 0,
    win_rate DECIMAL(5,2) DEFAULT 0,
    avg_rr DECIMAL(5,2) DEFAULT 0
);
```

---

## 🔌 Integration Points

### TradingView Webhook:
```json
{
  "event": "long_signal",
  "symbol": "{{ticker}}",
  "price": "{{close}}",
  "rsi": "{{rsi}}",
  "score": "{{score}}",
  "patterns": "{{patterns}}",
  "timestamp": "{{time}}"
}
```

### Telegram Webhook:
```
POST https://api.telegram.org/bot{TOKEN}/setWebhook
{
  "url": "https://yourbot.com/webhook/telegram"
}
```

### Bybit Webhook:
Бот сам делает HTTP запросы к Bybit API, webhook не нужен.

---

## 🧪 Testing Strategy

### Unit Tests:
```
tests/test_scanner.py         # Тестирование сканера
  ├─ test_scan_single()
  ├─ test_filter_by_volume()
  └─ test_filter_by_score()

tests/test_scorer.py          # Тестирование скоринга
  ├─ test_rsi_component()
  ├─ test_funding_component()
  └─ test_total_score_calculation()

tests/test_patterns.py        # Тестирование паттернов
  ├─ test_rejection_long_detection()
  ├─ test_trap_short_detection()
  └─ test_mega_long_detection()

tests/test_risk.py            # Тестирование риск-менеджмента
  ├─ test_position_sizing()
  ├─ test_sl_calculation()
  └─ test_tp_calculation()
```

### Integration Tests:
```
tests/test_integration.py
  ├─ test_binance_api_connection()
  ├─ test_bybit_api_connection()
  ├─ test_full_workflow()
  └─ test_telegram_notifications()
```

### Backtesting:
```python
# scripts/backtest.py
python backtest.py \
  --start-date 2024-01-01 \
  --end-date 2024-12-31 \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT \
  --min-score 65 \
  --output results/
```

---

## 📦 Dependencies

### Core:
```
python-telegram-bot==20.7
pybit==5.7.0
python-binance==1.0.19
pandas==2.1.4
numpy==1.26.3
aiohttp==3.9.1
asyncio==3.4.3
```

### Indicators:
```
ta-lib==0.4.28  # или
pandas-ta==0.3.14b0
```

### Web (опционально):
```
fastapi==0.109.0
uvicorn==0.27.0
websockets==12.0
```

### Database (опционально):
```
asyncpg==0.29.0  # PostgreSQL
redis==5.0.1     # Redis для кэша
```

### Utils:
```
python-dotenv==1.0.0
pydantic==2.5.3
loguru==0.7.2
schedule==1.2.1
```

---

## 🚀 Deployment Options

### 1. Render.com (рекомендуется):
```yaml
# render.yaml
services:
  - type: web
    name: liquidity-long-bot
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python src/main.py
    envVars:
      - key: BYBIT_API_KEY
        sync: false
      - key: BYBIT_API_SECRET
        sync: false
      - key: TELEGRAM_BOT_TOKEN
        sync: false
```

### 2. PythonAnywhere:
```bash
# Setup script
pip install -r requirements.txt
# Configure as "Always on task"
```

### 3. VPS / Dedicated Server:
```bash
# Using Docker
docker-compose up -d

# Or systemd service
sudo systemctl enable liquidity-long-bot
sudo systemctl start liquidity-long-bot
```

### 4. Railway:
```yaml
# railway.yaml
build:
  builder: NIXPACKS
deploy:
  startCommand: python src/main.py
```

---

## 📈 Monitoring & Alerts

### Health Checks:
```python
# Каждые 5 минут
if scanner.last_scan_time > 300:  # 5 min
    alert("Scanner not running!")

if bybit_client.connection_status != 'connected':
    alert("Bybit connection lost!")
```

### Metrics to Track:
- Signals generated per hour/day
- Signal accuracy (predicted vs actual)
- Win rate by pattern type
- Average R:R ratio
- Slippage on entries
- API latency
- System uptime

---

## 🎓 Implementation Priority

### Phase 1 (MVP): Core Logic
- [ ] Scanner + Binance client
- [ ] Basic Long Score (RSI + Funding + L/S Ratio)
- [ ] Simple pattern detection (MEGA LONG)
- [ ] Telegram notifications
- [ ] Manual trading (только сигналы)

### Phase 2: Risk Management
- [ ] Position sizing
- [ ] SL/TP calculation
- [ ] Pattern: REJECTION LONG, TRAP SHORT
- [ ] Advanced scoring (delta, OI)

### Phase 3: Auto-Trading
- [ ] Bybit integration
- [ ] Order execution
- [ ] Position tracking
- [ ] Trailing stops

### Phase 4: Advanced Features
- [ ] All 4 patterns with full logic
- [ ] Dashboard
- [ ] Backtesting framework
- [ ] Multi-exchange support

---

## 📝 Notes

### From Statham System (v130) to adapt:
- Smart SL logic (structure-based)
- 6-level TP system
- VWAP integration for entries
- Wyckoff patterns (LPS, SoS for LONG)
- MTF analysis (multi-timeframe)
- Volume profile (POC, HVN)

### Key differences from SHORT system:
- RSI threshold inverted (<30 vs >70)
- Funding inverted (negative is good for LONG)
- L/S ratio inverted (<35% longs is good)
- Delta inverted (positive delta on decline is good)
- SL placed below structure vs above
- TP levels are above entry vs below

---

## ✅ Checklist for Implementation

### Pre-development:
- [ ] Read and understand all 7 documentation files
- [ ] Set up development environment
- [ ] Create testnet accounts (Bybit)
- [ ] Get API keys (Binance, Bybit)
- [ ] Create Telegram bot and get token

### Development:
- [ ] Implement data clients (Binance, Bybit)
- [ ] Implement core modules (scanner, scorer, patterns)
- [ ] Add risk management
- [ ] Add Telegram bot
- [ ] Test on historical data
- [ ] Paper trading phase
- [ ] Live trading (small size)

### Post-launch:
- [ ] Monitor for 2 weeks
- [ ] Collect statistics
- [ ] Optimize parameters
- [ ] Scale up position sizes

---

**Author:** Liquidity Long Bot Project  
**Version:** 1.0  
**Date:** April 2026
