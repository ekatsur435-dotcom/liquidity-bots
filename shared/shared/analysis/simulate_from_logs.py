"""
📊 Симуляция на основе данных из логов пользователя
"""

from backtest_trailing import TradeRecord, TrailingBacktester


def create_sample_trades():
    """Создание тестовых сделок на основе реальных логов"""
    trades = []
    
    # PUMPBTCUSDT SHORT — хорошая сделка, но трейлинг выбил
    trades.append(TradeRecord(
        symbol="PUMPBTCUSDT",
        direction="short",
        entry_price=0.032640,
        initial_sl=0.033100,
        final_sl=0.030381,
        close_price=0.031110,
        close_type="sl",
        taken_tps=4,
        pnl_pct=3.94,
        duration_min=9
    ))
    
    # METUSDT LONG — быстрый SL
    trades.append(TradeRecord(
        symbol="METUSDT",
        direction="long",
        entry_price=0.174550,
        initial_sl=0.171319,
        final_sl=0.171319,
        close_price=0.171200,
        close_type="sl",
        taken_tps=0,
        pnl_pct=-1.92,
        duration_min=1
    ))
    
    return trades


def main():
    trades = create_sample_trades()
    backtester = TrailingBacktester(trades)
    results = backtester.run_analysis()
    return results


if __name__ == "__main__":
    main()
