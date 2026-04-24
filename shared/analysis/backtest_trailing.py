"""
📊 Backtest Trailing Stop Analysis — Сравнение старого vs Micro-Step

Анализирует сделки из логов и считает:
- Сколько SL было выбито из-за агрессивного трейлинга
- Сколько TP было взято полностью
- Как бы сработал Micro-Step Trailing

Использование:
python shared/analysis/backtest_trailing.py --logs logs/bot.log --days 7
"""

import re
import json
import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from collections import defaultdict
import os
import sys


@dataclass
class TradeRecord:
    """Запись о сделке из логов"""
    symbol: str
    direction: str  # "long" | "short"
    entry_price: float
    initial_sl: float
    final_sl: Optional[float] = None
    close_price: Optional[float] = None
    taken_tps: int = 0
    max_tps_available: int = 6
    close_type: str = ""  # "sl", "tp", "manual"
    pnl_pct: float = 0.0
    duration_min: int = 0
    timestamp: datetime = None
    
    # Данные о трейлинге
    trail_moves: List[Dict] = field(default_factory=list)
    
    def was_sl_hit(self) -> bool:
        return self.close_type == "sl"
    
    def was_profitable(self) -> bool:
        return self.pnl_pct > 0


@dataclass
class MicroStepSimulation:
    """Результат симуляции Micro-Step Trailing"""
    trade: TradeRecord
    would_survive: bool  # Выжила бы сделка?
    final_sl_micro: float  # Какой был бы SL
    estimated_pnl: float  # Оценочный P&L
    saved_by_micro: bool  # Спасена ли микро-шагами?
    

class TrailingBacktester:
    """
    📊 Бэктестер трейлинга
    
    Сравнивает:
    1. Актуальный трейлинг (агрессивный)
    2. Micro-Step Trailing (консервативный)
    """
    
    # Micro-Step настройки (из ENV или дефолт)
    TP1_LOCK = float(os.getenv("TRAIL_TP1_LOCK", "0.3"))
    TP2_LOCK = float(os.getenv("TRAIL_TP2_LOCK", "0.8"))
    TP3_LOCK = float(os.getenv("TRAIL_TP3_LOCK", "1.5"))
    TP4_LOCK = float(os.getenv("TRAIL_TP4_LOCK", "2.5"))
    TP5_LOCK = float(os.getenv("TRAIL_TP5_LOCK", "4.0"))
    TP6_LOCK = float(os.getenv("TRAIL_TP6_LOCK", "6.0"))
    
    def __init__(self, trades: List[TradeRecord]):
        self.trades = trades
        self.stats = {
            "total": 0,
            "sl_hit": 0,
            "tp_full": 0,
            "saved_by_micro": 0,
            "lost_by_micro": 0,
            "neutral": 0,
        }
    
    def simulate_micro_step(self, trade: TradeRecord) -> MicroStepSimulation:
        """
        Симуляция Micro-Step Trailing для сделки
        """
        # Рассчитываем, какой SL был бы с Micro-Step
        entry = trade.entry_price
        direction = trade.direction
        taken_tps = trade.taken_tps
        
        # Выбираем % блокировки на основе взятых TP
        lock_pcts = {
            0: 0.0,      # Нет TP = нет трейлинга
            1: self.TP1_LOCK,
            2: self.TP2_LOCK,
            3: self.TP3_LOCK,
            4: self.TP4_LOCK,
            5: self.TP5_LOCK,
            6: self.TP6_LOCK,
        }
        
        lock_pct = lock_pcts.get(taken_tps, self.TP6_LOCK)
        
        # Рассчитываем SL
        if direction == "long":
            micro_sl = entry * (1 + lock_pct / 100)
        else:
            micro_sl = entry * (1 - lock_pct / 100)
        
        # Проверяем, выжила бы сделка
        close_price = trade.close_price
        would_survive = False
        
        if close_price and trade.close_type == "sl":
            # Сделка закрылась по SL
            if direction == "long":
                # Для LONG: если цена закрытия выше micro_SL — выжила бы
                would_survive = close_price >= micro_sl
            else:
                # Для SHORT: если цена закрытия ниже micro_SL — выжила бы
                would_survive = close_price <= micro_sl
        elif trade.close_type == "tp":
            # Сделка закрылась по TP — точно выжила
            would_survive = True
        
        # Оценочный P&L (если бы выжила)
        estimated_pnl = 0.0
        if would_survive and close_price:
            if direction == "long":
                estimated_pnl = (close_price - entry) / entry * 100
            else:
                estimated_pnl = (entry - close_price) / entry * 100
        
        # Сохранена ли микро-шагами?
        saved_by_micro = (
            trade.close_type == "sl" and 
            would_survive and 
            taken_tps >= 1  # Спасено только если были взяты TP
        )
        
        return MicroStepSimulation(
            trade=trade,
            would_survive=would_survive,
            final_sl_micro=micro_sl,
            estimated_pnl=estimated_pnl,
            saved_by_micro=saved_by_micro
        )
    
    def run_analysis(self) -> Dict:
        """Запуск полного анализа"""
        print(f"\n{'='*60}")
        print(f"📊 TRAILING STOP BACKTEST — {len(self.trades)} trades")
        print(f"{'='*60}")
        
        simulations = []
        saved_trades = []
        
        for trade in self.trades:
            self.stats["total"] += 1
            
            if trade.was_sl_hit():
                self.stats["sl_hit"] += 1
            elif trade.taken_tps == trade.max_tps_available:
                self.stats["tp_full"] += 1
            
            # Симуляция Micro-Step
            sim = self.simulate_micro_step(trade)
            simulations.append(sim)
            
            if sim.saved_by_micro:
                self.stats["saved_by_micro"] += 1
                saved_trades.append(sim)
                print(f"💚 [{trade.symbol}] БЫЛО SL → Micro-Step спас! "
                      f"({trade.taken_tps} TP взято, SL={sim.final_sl_micro:.6f})")
            elif trade.was_sl_hit() and not sim.would_survive:
                # Всё равно бы выбило — закономерный SL
                pass
        
        # Расчёт метрик
        sl_rate = self.stats["sl_hit"] / max(1, self.stats["total"]) * 100
        saved_rate = self.stats["saved_by_micro"] / max(1, self.stats["sl_hit"]) * 100
        
        print(f"\n{'='*60}")
        print(f"📈 RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"Всего сделок:        {self.stats['total']}")
        print(f"SL выбито:           {self.stats['sl_hit']} ({sl_rate:.1f}%)")
        print(f"Все TP взяты:        {self.stats['tp_full']}")
        print(f"")
        print(f"💚 Спасено Micro-Step: {self.stats['saved_by_micro']} сделок")
        print(f"   ({saved_rate:.1f}% от всех SL)")
        
        if saved_trades:
            total_saved_pnl = sum(s.estimated_pnl for s in saved_trades)
            print(f"💰 Потенциально спасено: {total_saved_pnl:+.2f}% P&L")
        
        return {
            "stats": self.stats,
            "simulations": simulations,
            "saved_trades": saved_trades,
            "sl_rate": sl_rate,
            "saved_rate": saved_rate,
        }


class LogParser:
    """Парсер логов бота"""
    
    # Регулярки для парсинга
    PT_PATTERN = re.compile(
        r'\[PT\]\[(LONG|SHORT)\]\[(\w+)\].*?'
        r'цена=([\d.]+).*?'
        r'вход=([\d.]+).*?'
        r'SL=([\d.]+)',
        re.IGNORECASE
    )
    
    SL_HIT_PATTERN = re.compile(
        r'\[PT\]\[(\w+)\].*?🛑 SL HIT!.*?'
        r'цена=([\d.]+).*?'
        r'sl=([\d.]+).*?'
        r'TP взято=(\d+)',
        re.IGNORECASE
    )
    
    TRAIL_PATTERN = re.compile(
        r'Стоп передвинут.*?'
        r'\[(\w+)\].*?'
        r'Было SL:\s*\$?([\d.]+).*?'
        r'Теперь SL:\s*\$?([\d.]+)',
        re.IGNORECASE
    )
    
    RECORD_PATTERN = re.compile(
        r'\[PT\]\[RECORD\]\[(\w+)\].*?'
        r'tp_level=(\w+).*?'
        r'pnl=([\-\d.]+)%',
        re.IGNORECASE
    )
    
    def __init__(self, log_content: str):
        self.content = log_content
        self.trades: Dict[str, TradeRecord] = {}
    
    def parse(self) -> List[TradeRecord]:
        """Парсинг всех сделок из логов"""
        print("🔍 Parsing logs...")
        
        # Парсим открытия позиций
        for match in self.PT_PATTERN.finditer(self.content):
            direction, symbol, price, entry, sl = match.groups()
            
            if symbol not in self.trades:
                self.trades[symbol] = TradeRecord(
                    symbol=symbol,
                    direction=direction.lower(),
                    entry_price=float(entry),
                    initial_sl=float(sl),
                    timestamp=datetime.now()  # TODO: парсить реальное время
                )
        
        # Парсим SL HIT
        for match in self.SL_HIT_PATTERN.finditer(self.content):
            symbol, price, sl, tps = match.groups()
            
            if symbol in self.trades:
                trade = self.trades[symbol]
                trade.close_price = float(price)
                trade.close_type = "sl"
                trade.taken_tps = int(tps)
        
        # Парсим записи (TP закрытия)
        for match in self.RECORD_PATTERN.finditer(self.content):
            symbol, level, pnl = match.groups()
            
            if symbol in self.trades:
                trade = self.trades[symbol]
                if level == "SL":
                    trade.close_type = "sl"
                    trade.pnl_pct = float(pnl)
                else:
                    # TP закрытие
                    trade.taken_tps = max(trade.taken_tps, 1)
                    if trade.close_type != "sl":
                        trade.close_type = "tp"
                        trade.pnl_pct = float(pnl)
        
        # Парсим движения трейлинга
        for match in self.TRAIL_PATTERN.finditer(self.content):
            symbol, old_sl, new_sl = match.groups()
            
            if symbol in self.trades:
                trade = self.trades[symbol]
                trade.trail_moves.append({
                    "from": float(old_sl),
                    "to": float(new_sl),
                    "at": datetime.now()
                })
                trade.final_sl = float(new_sl)
        
        trades = list(self.trades.values())
        print(f"✅ Found {len(trades)} trades")
        return trades


def main():
    parser = argparse.ArgumentParser(description="Backtest Trailing Stop Analysis")
    parser.add_argument("--logs", default="logs/bot.log", help="Path to log file")
    parser.add_argument("--days", type=int, default=7, help="Days to analyze")
    args = parser.parse_args()
    
    # Читаем логи
    log_path = args.logs
    if not os.path.exists(log_path):
        # Ищем в разных местах
        possible_paths = [
            "logs/bot.log",
            "long-bot/logs/bot.log",
            "short-bot/logs/bot.log",
            "/var/log/bot.log",
        ]
        for p in possible_paths:
            if os.path.exists(p):
                log_path = p
                break
        else:
            print(f"❌ Log file not found: {log_path}")
            print("Searching for logs...")
            # Пробуем найти любые логи
            import subprocess
            result = subprocess.run(
                ["find", ".", "-name", "*.log", "-type", "f"],
                capture_output=True,
                text=True
            )
            if result.stdout:
                print("Found log files:")
                print(result.stdout[:1000])
            sys.exit(1)
    
    print(f"📁 Reading logs from: {log_path}")
    
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Парсим
    parser = LogParser(content)
    trades = parser.parse()
    
    if not trades:
        print("⚠️ No trades found in logs")
        return
    
    # Анализируем
    backtester = TrailingBacktester(trades)
    results = backtester.run_analysis()
    
    # Сохраняем результаты
    output = {
        "timestamp": datetime.now().isoformat(),
        "days_analyzed": args.days,
        "results": results["stats"],
        "saved_trades": [
            {
                "symbol": s.trade.symbol,
                "direction": s.trade.direction,
                "taken_tps": s.trade.taken_tps,
                "actual_pnl": s.trade.pnl_pct,
                "estimated_pnl": s.estimated_pnl,
                "micro_sl": s.final_sl_micro,
            }
            for s in results["saved_trades"]
        ]
    }
    
    output_path = "shared/analysis/backtest_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    
    print(f"\n💾 Results saved to: {output_path}")


if __name__ == "__main__":
    main()
