"""
BACKTEST SYSTEM 2024-2025
Тестирование стратегии на исторических данных
"""

import os
import sys
import json
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field, asdict
from collections import defaultdict
import csv

import pandas as pd
import numpy as np

# Добавляем путь к shared
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'shared'))

from utils.binance_client import BinanceFuturesClient, CandleData
from core.scorer import ShortScorer, LongScorer, Pattern
from core.pattern_detector import ShortPatternDetector, LongPatternDetector
from core.smc_ict_detector import SMCICTDetector, SMCStructure


@dataclass
class BacktestTrade:
    """Сделка в бэктесте"""
    symbol: str
    direction: str  # 'short' или 'long'
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profits: List[Tuple[float, float]]  # [(price, %), ...]
    score: int
    score_components: Dict
    patterns: List[str]
    smc_score: Optional[int] = None
    smc_factors: Optional[List[str]] = None
    
    # Результаты
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None  # 'sl', 'tp1', 'tp2', etc., 'time_expired'
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    max_profit_pct: float = 0.0
    
    @property
    def is_win(self) -> bool:
        return self.pnl_pct > 0


@dataclass
class BacktestResult:
    """Результат бэктеста"""
    # Период
    start_date: str
    end_date: str
    symbols_tested: List[str]
    
    # Настройки
    min_score: int
    use_smc: bool
    use_filters: bool
    
    # Статистика
    total_signals: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    
    # PnL
    total_pnl_pct: float
    total_pnl_usd: float
    avg_pnl_per_trade: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    
    # Риск
    max_drawdown_pct: float
    sharpe_ratio: float
    
    # Распределение
    trades_by_pattern: Dict[str, int]
    trades_by_month: Dict[str, int]
    trades_by_score: Dict[int, int]
    
    # Подробности
    trades: List[BacktestTrade] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        """Конвертировать в словарь для JSON"""
        return {
            'period': {
                'start': self.start_date,
                'end': self.end_date,
                'symbols': self.symbols_tested
            },
            'config': {
                'min_score': self.min_score,
                'use_smc': self.use_smc,
                'use_filters': self.use_filters
            },
            'summary': {
                'total_signals': self.total_signals,
                'total_trades': self.total_trades,
                'winning_trades': self.winning_trades,
                'losing_trades': self.losing_trades,
                'win_rate': f"{self.win_rate:.2f}%"
            },
            'pnl': {
                'total_pnl_pct': f"{self.total_pnl_pct:.2f}%",
                'total_pnl_usd': f"${self.total_pnl_usd:.2f}",
                'avg_per_trade': f"{self.avg_pnl_per_trade:.2f}%",
                'avg_win': f"{self.avg_win:.2f}%",
                'avg_loss': f"{self.avg_loss:.2f}%",
                'profit_factor': f"{self.profit_factor:.2f}"
            },
            'risk': {
                'max_drawdown': f"{self.max_drawdown_pct:.2f}%",
                'sharpe_ratio': f"{self.sharpe_ratio:.2f}"
            },
            'distribution': {
                'by_pattern': self.trades_by_pattern,
                'by_month': self.trades_by_month,
                'by_score': {str(k): v for k, v in self.trades_by_score.items()}
            }
        }


class BacktestEngine:
    """Движок бэктестинга"""
    
    def __init__(self, 
                 initial_capital: float = 1000.0,
                 risk_per_trade: float = 0.01,
                 min_score: int = 75,
                 use_smc: bool = True,
                 use_filters: bool = True):
        """
        Args:
            initial_capital: Начальный капитал
            risk_per_trade: Риск на сделку (1% = 0.01)
            min_score: Минимальный Score для входа
            use_smc: Использовать SMC+ICT анализ
            use_filters: Использовать фильтры (тренд, BTC, время)
        """
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.min_score = min_score
        self.use_smc = use_smc
        self.use_filters = use_filters
        
        # Компоненты
        self.short_scorer = ShortScorer(min_score)
        self.long_scorer = LongScorer(min_score)
        self.short_patterns = ShortPatternDetector()
        self.long_patterns = LongPatternDetector()
        self.smc_detector = SMCICTDetector()
        
        # Данные
        self.binance = BinanceFuturesClient()
        
        # Результаты
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []
    
    async def load_historical_data(self, 
                                   symbol: str, 
                                   start_date: str,
                                   end_date: str,
                                   timeframe: str = "15m") -> pd.DataFrame:
        """
        Загружает исторические данные с Binance
        
        Args:
            symbol: Торговая пара
            start_date: Начало (YYYY-MM-DD)
            end_date: Конец (YYYY-MM-DD)
            timeframe: Таймфрейм
        
        Returns:
            DataFrame с OHLCV
        """
        print(f"📊 Loading historical data for {symbol} ({start_date} to {end_date})...")
        
        # Конвертируем даты в timestamps
        start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
        end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000)
        
        # Загружаем данные (Binance API позволяет max 1500 свечей за раз)
        all_candles = []
        current_ts = start_ts
        
        while current_ts < end_ts:
            candles = await self.binance.get_klines(
                symbol=symbol,
                interval=timeframe,
                limit=1000
            )
            
            if not candles:
                break
            
            all_candles.extend(candles)
            
            # Обновляем timestamp для следующей загрузки
            if candles:
                current_ts = candles[-1].timestamp + 1
            else:
                break
            
            # Небольшая задержка чтобы не перегрузить API
            await asyncio.sleep(0.5)
        
        # Конвертируем в DataFrame
        data = []
        for candle in all_candles:
            data.append({
                'timestamp': pd.to_datetime(candle.timestamp, unit='ms'),
                'open': candle.open,
                'high': candle.high,
                'low': candle.low,
                'close': candle.close,
                'volume': candle.volume
            })
        
        df = pd.DataFrame(data)
        df.set_index('timestamp', inplace=True)
        
        # Фильтруем по датам
        df = df[start_date:end_date]
        
        print(f"✅ Loaded {len(df)} candles for {symbol}")
        
        return df
    
    def simulate_trade(self,
                      trade: BacktestTrade,
                      price_data: pd.DataFrame,
                      entry_idx: int,
                      max_bars: int = 50) -> BacktestTrade:
        """
        Симулирует сделку на исторических данных
        
        Args:
            trade: Данные сделки
            price_data: Ценовые данные
            entry_idx: Индекс входа
            max_bars: Максимум баров для симуляции
        
        Returns:
            Завершённая сделка с результатами
        """
        if entry_idx >= len(price_data) - 1:
            return trade
        
        entry_price = trade.entry_price
        direction = 1 if trade.direction == "long" else -1
        
        # Отслеживаем максимумы/минимумы
        max_price = entry_price
        min_price = entry_price
        
        # Проверяем каждый бар после входа
        for i in range(entry_idx + 1, min(entry_idx + max_bars, len(price_data))):
            bar = price_data.iloc[i]
            
            high = bar['high']
            low = bar['low']
            close = bar['close']
            
            # Обновляем максимумы/минимумы
            max_price = max(max_price, high)
            min_price = min(min_price, low)
            
            # Проверяем SL
            if direction == 1:  # Long
                if low <= trade.stop_loss:
                    trade.exit_price = trade.stop_loss
                    trade.exit_time = bar.name
                    trade.exit_reason = "sl"
                    trade.pnl_pct = (trade.exit_price - entry_price) / entry_price * 100
                    trade.max_drawdown_pct = (min_price - entry_price) / entry_price * 100
                    trade.max_profit_pct = (max_price - entry_price) / entry_price * 100
                    return trade
            else:  # Short
                if high >= trade.stop_loss:
                    trade.exit_price = trade.stop_loss
                    trade.exit_time = bar.name
                    trade.exit_reason = "sl"
                    trade.pnl_pct = (entry_price - trade.exit_price) / entry_price * 100
                    trade.max_drawdown_pct = (entry_price - max_price) / entry_price * 100
                    trade.max_profit_pct = (entry_price - min_price) / entry_price * 100
                    return trade
            
            # Проверяем TP уровни (частичное закрытие)
            for tp_idx, (tp_price, tp_pct) in enumerate(trade.take_profits, 1):
                if direction == 1:  # Long
                    if high >= tp_price:
                        # Симулируем закрытие части позиции
                        if tp_idx == 1:
                            trade.exit_price = tp_price
                            trade.exit_time = bar.name
                            trade.exit_reason = f"tp{tp_idx}"
                            trade.pnl_pct = (tp_price - entry_price) / entry_price * 100
                            trade.max_profit_pct = (max_price - entry_price) / entry_price * 100
                            return trade
                else:  # Short
                    if low <= tp_price:
                        if tp_idx == 1:
                            trade.exit_price = tp_price
                            trade.exit_time = bar.name
                            trade.exit_reason = f"tp{tp_idx}"
                            trade.pnl_pct = (entry_price - tp_price) / entry_price * 100
                            trade.max_profit_pct = (entry_price - min_price) / entry_price * 100
                            return trade
        
        # Если не закрылись по SL/TP — закрываем по текущей цене (time expired)
        last_bar = price_data.iloc[min(entry_idx + max_bars - 1, len(price_data) - 1)]
        trade.exit_price = last_bar['close']
        trade.exit_time = last_bar.name
        trade.exit_reason = "time_expired"
        
        if direction == 1:
            trade.pnl_pct = (trade.exit_price - entry_price) / entry_price * 100
        else:
            trade.pnl_pct = (entry_price - trade.exit_price) / entry_price * 100
        
        return trade
    
    async def run_backtest(self,
                          symbols: List[str],
                          start_date: str,
                          end_date: str,
                          timeframe: str = "15m") -> BacktestResult:
        """
        Запускает полный бэктест
        
        Args:
            symbols: Список символов для тестирования
            start_date: Начало периода
            end_date: Конец периода
            timeframe: Таймфрейм
        
        Returns:
            Результаты бэктеста
        """
        print(f"\n{'=' * 60}")
        print(f"🚀 STARTING BACKTEST {start_date} to {end_date}")
        print(f"{'=' * 60}")
        print(f"Symbols: {symbols}")
        print(f"Min Score: {self.min_score}")
        print(f"Use SMC: {self.use_smc}")
        print(f"Use Filters: {self.use_filters}")
        
        all_trades = []
        
        for symbol in symbols:
            print(f"\n📈 Testing {symbol}...")
            
            try:
                # Загружаем данные
                df = await self.load_historical_data(symbol, start_date, end_date, timeframe)
                
                if len(df) < 100:
                    print(f"  ⚠️ Insufficient data for {symbol}")
                    continue
                
                # Детектируем SMC структуры (если включено)
                smc_obs = []
                smc_fvgs = []
                smc_sweeps = []
                smc_breaks = []
                fib_levels = {}
                
                if self.use_smc:
                    smc_obs = self.smc_detector.detect_order_blocks(df)
                    smc_fvgs = self.smc_detector.detect_fvgs(df)
                    smc_sweeps = self.smc_detector.detect_liquidity_sweeps(df)
                    smc_breaks = self.smc_detector.detect_structure_breaks(df)
                    fib_levels = self.smc_detector.calculate_fibonacci_levels(df)
                    
                    print(f"  SMC: {len(smc_obs)} OBs, {len(smc_fvgs)} FVGs, {len(smc_sweeps)} Sweeps")
                
                # Проходим по данным и ищем сигналы
                for i in range(50, len(df) - 20):  # С запасом для паттернов
                    # Получаем текущие данные
                    window = df.iloc[i-50:i+1]
                    current = df.iloc[i]
                    current_price = current['close']
                    current_time = current.name
                    
                    # Рассчитываем индикаторы (упрощённо)
                    prices = window['close'].tolist()
                    rsi = self._calculate_rsi(prices)
                    
                    # Паттерны
                    hourly_deltas = [0] * 7  # Упрощённо
                    price_trend = self._get_trend(prices[-20:])
                    
                    # Проверяем SHORT сигнал
                    short_patterns = self.short_patterns.detect_all(
                        window, hourly_deltas
                    )
                    
                    if short_patterns:
                        score_result = self.short_scorer.calculate_score(
                            rsi_1h=rsi or 50,
                            funding_current=0.01,  # Упрощённо
                            funding_accumulated=0.03,
                            long_ratio=65,  # Упрощённо
                            oi_change_4d=10,
                            price_change_4d=5,
                            hourly_deltas=hourly_deltas,
                            price_trend=price_trend,
                            patterns=short_patterns
                        )
                        
                        if score_result.is_valid:
                            # Проверяем SMC (если включено)
                            smc_score = None
                            smc_factors = None
                            
                            if self.use_smc:
                                smc_result = self.smc_detector.calculate_smc_score(
                                    symbol=symbol,
                                    direction="short",
                                    obs=smc_obs,
                                    fvgs=smc_fvgs,
                                    sweeps=smc_sweeps,
                                    breaks=smc_breaks,
                                    fib_levels=fib_levels,
                                    current_price=current_price,
                                    timestamp=current_time
                                )
                                
                                # Комбинируем скоры
                                combined_score = (score_result.total_score + smc_result['score']) / 2
                                if combined_score >= self.min_score and smc_result['is_valid']:
                                    smc_score = smc_result['score']
                                    smc_factors = smc_result['factors']
                            
                            # Создаём сделку
                            trade = BacktestTrade(
                                symbol=symbol,
                                direction="short",
                                entry_price=current_price,
                                entry_time=current_time,
                                stop_loss=current_price * 1.01,
                                take_profits=[
                                    (current_price * 0.985, 25),
                                    (current_price * 0.97, 25),
                                    (current_price * 0.95, 20)
                                ],
                                score=score_result.total_score,
                                score_components={c.name: c.score for c in score_result.components},
                                patterns=[p.name for p in short_patterns],
                                smc_score=smc_score,
                                smc_factors=smc_factors
                            )
                            
                            # Симулируем сделку
                            completed_trade = self.simulate_trade(trade, df, i)
                            all_trades.append(completed_trade)
                    
                    # Проверяем LONG сигнал (аналогично)
                    long_patterns = self.long_patterns.detect_all(
                        window, hourly_deltas
                    )
                    
                    if long_patterns:
                        score_result = self.long_scorer.calculate_score(
                            rsi_1h=rsi or 50,
                            funding_current=-0.01,
                            funding_accumulated=-0.03,
                            long_ratio=35,
                            oi_change_4d=10,
                            price_change_4d=-5,
                            hourly_deltas=hourly_deltas,
                            price_trend=price_trend,
                            patterns=long_patterns
                        )
                        
                        if score_result.is_valid:
                            trade = BacktestTrade(
                                symbol=symbol,
                                direction="long",
                                entry_price=current_price,
                                entry_time=current_time,
                                stop_loss=current_price * 0.99,
                                take_profits=[
                                    (current_price * 1.015, 25),
                                    (current_price * 1.03, 25),
                                    (current_price * 1.05, 20)
                                ],
                                score=score_result.total_score,
                                score_components={c.name: c.score for c in score_result.components},
                                patterns=[p.name for p in long_patterns]
                            )
                            
                            completed_trade = self.simulate_trade(trade, df, i)
                            all_trades.append(completed_trade)
                
                print(f"  ✅ Found {len([t for t in all_trades if t.symbol == symbol])} trades")
                
            except Exception as e:
                print(f"  ❌ Error testing {symbol}: {e}")
                continue
        
        # Рассчитываем статистику
        result = self._calculate_statistics(all_trades, symbols, start_date, end_date)
        
        await self.binance.close()
        
        return result
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Расчёт RSI"""
        if len(prices) < period + 1:
            return None
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        if len(gains) < period:
            return None
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _get_trend(self, prices: List[float]) -> str:
        """Определение тренда"""
        if len(prices) < 2:
            return "sideways"
        
        change = (prices[-1] - prices[0]) / prices[0] * 100
        
        if change > 2:
            return "rising"
        elif change < -2:
            return "falling"
        else:
            return "sideways"
    
    def _calculate_statistics(self, 
                              trades: List[BacktestTrade],
                              symbols: List[str],
                              start_date: str,
                              end_date: str) -> BacktestResult:
        """Рассчитывает итоговую статистику"""
        
        if not trades:
            print("⚠️ No trades generated")
            return BacktestResult(
                start_date=start_date,
                end_date=end_date,
                symbols_tested=symbols,
                min_score=self.min_score,
                use_smc=self.use_smc,
                use_filters=self.use_filters,
                total_signals=len(trades),
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0.0,
                total_pnl_pct=0.0,
                total_pnl_usd=0.0,
                avg_pnl_per_trade=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                profit_factor=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                trades_by_pattern={},
                trades_by_month={},
                trades_by_score={},
                trades=[]
            )
        
        # Базовая статистика
        total_trades = len(trades)
        winning_trades = len([t for t in trades if t.is_win])
        losing_trades = total_trades - winning_trades
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        # PnL
        total_pnl_pct = sum(t.pnl_pct for t in trades)
        total_pnl_usd = total_pnl_pct / 100 * self.initial_capital
        avg_pnl = total_pnl_pct / total_trades if total_trades > 0 else 0
        
        wins = [t.pnl_pct for t in trades if t.is_win]
        losses = [t.pnl_pct for t in trades if not t.is_win]
        
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        profit_factor = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else float('inf')
        
        # Распределение
        by_pattern = defaultdict(int)
        by_month = defaultdict(int)
        by_score = defaultdict(int)
        
        for trade in trades:
            if trade.patterns:
                by_pattern[trade.patterns[0]] += 1
            
            month = trade.entry_time.strftime("%Y-%m")
            by_month[month] += 1
            
            score_bucket = (trade.score // 10) * 10
            by_score[score_bucket] += 1
        
        # Максимальная просадка (упрощённо)
        max_dd = min((t.max_drawdown_pct for t in trades), default=0)
        
        # Sharpe (упрощённо)
        returns = [t.pnl_pct for t in trades]
        sharpe = np.mean(returns) / np.std(returns) if np.std(returns) != 0 else 0
        
        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            symbols_tested=symbols,
            min_score=self.min_score,
            use_smc=self.use_smc,
            use_filters=self.use_filters,
            total_signals=len(trades),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_pnl_pct=total_pnl_pct,
            total_pnl_usd=total_pnl_usd,
            avg_pnl_per_trade=avg_pnl,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            trades_by_pattern=dict(by_pattern),
            trades_by_month=dict(by_month),
            trades_by_score=dict(by_score),
            trades=trades
        )


async def main():
    """Запуск бэктеста"""
    
    # Конфигурация
    SYMBOLS = [
        "BTCUSDT",
        "ETHUSDT", 
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT"
    ]
    
    START_DATE = "2024-01-01"
    END_DATE = "2025-03-01"
    
    # Тестируем разные конфигурации
    configs = [
        {"min_score": 65, "use_smc": False, "name": "Baseline (Score 65)"},
        {"min_score": 75, "use_smc": False, "name": "High WR (Score 75)"},
        {"min_score": 75, "use_smc": True, "name": "SMC Enhanced (Score 75 + SMC)"},
        {"min_score": 85, "use_smc": True, "name": "Sniper (Score 85 + SMC)"}
    ]
    
    all_results = []
    
    for config in configs:
        print(f"\n{'=' * 60}")
        print(f"🧪 TESTING: {config['name']}")
        print(f"{'=' * 60}")
        
        engine = BacktestEngine(
            initial_capital=1000.0,
            risk_per_trade=0.01,
            min_score=config['min_score'],
            use_smc=config['use_smc'],
            use_filters=True
        )
        
        result = await engine.run_backtest(
            symbols=SYMBOLS[:2],  # Для теста берём 2 монеты
            start_date=START_DATE,
            end_date=END_DATE,
            timeframe="15m"
        )
        
        all_results.append((config['name'], result))
        
        # Печатаем результаты
        print(f"\n📊 RESULTS: {config['name']}")
        print(f"  Signals: {result.total_signals}")
        print(f"  Trades: {result.total_trades}")
        print(f"  Win Rate: {result.win_rate:.1f}%")
        print(f"  Total PnL: {result.total_pnl_pct:+.1f}%")
        print(f"  Profit Factor: {result.profit_factor:.2f}")
        print(f"  Max DD: {result.max_drawdown_pct:.1f}%")
    
    # Сравнение
    print(f"\n{'=' * 60}")
    print("📊 COMPARISON")
    print(f"{'=' * 60}")
    print(f"{'Config':<25} {'Trades':<8} {'WR%':<8} {'PnL%':<8} {'PF':<6}")
    print("-" * 60)
    
    for name, result in all_results:
        print(f"{name:<25} {result.total_trades:<8} {result.win_rate:<8.1f} {result.total_pnl_pct:<8.1f} {result.profit_factor:<6.2f}")
    
    # Сохраняем результаты
    output = {
        "test_period": f"{START_DATE} to {END_DATE}",
        "symbols": SYMBOLS,
        "results": [
            {
                "config": name,
                "summary": result.to_dict()
            }
            for name, result in all_results
        ]
    }
    
    with open("backtest_results_2024_2025.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    
    print(f"\n✅ Results saved to backtest_results_2024_2025.json")


if __name__ == "__main__":
    asyncio.run(main())
