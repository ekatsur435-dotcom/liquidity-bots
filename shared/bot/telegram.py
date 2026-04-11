"""
Telegram Bot Integration
Отправка сигналов и уведомлений в Telegram
"""

import os
import asyncio
from typing import Optional, Dict, List
from dataclasses import asdict
from datetime import datetime

import aiohttp


class TelegramBot:
    """Telegram бот для отправки сигналов"""
    
    def __init__(self, 
                 bot_token: Optional[str] = None,
                 chat_id: Optional[str] = None,
                 topic_id: Optional[str] = None):
        """
        Инициализация бота
        
        Args:
            bot_token: Токен бота от @BotFather
            chat_id: ID канала/группы (например, -1003867089540)
            topic_id: ID темы (thread_id) для супергрупп
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.topic_id = topic_id or os.getenv("TELEGRAM_TOPIC_ID")
        
        if not self.bot_token:
            raise ValueError("Telegram bot token not provided")
        if not self.chat_id:
            raise ValueError("Telegram chat ID not provided")
        
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать сессию"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        """Закрыть сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def _send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Отправить сообщение в Telegram
        
        Args:
            text: Текст сообщения
            parse_mode: HTML или Markdown
        
        Returns:
            True если успешно
        """
        try:
            url = f"{self.base_url}/sendMessage"
            
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            
            # Добавляем topic_id если есть
            if self.topic_id:
                payload["message_thread_id"] = int(self.topic_id)
            
            session = await self._get_session()
            
            async with session.post(url, json=payload, timeout=30) as response:
                if response.status == 200:
                    return True
                else:
                    error_text = await response.text()
                    print(f"Telegram API error: {response.status} - {error_text}")
                    return False
        
        except Exception as e:
            print(f"Error sending Telegram message: {e}")
            return False
    
    # =========================================================================
    # SIGNAL MESSAGES
    # =========================================================================
    
    def format_short_signal(self, 
                           symbol: str,
                           score: int,
                           price: float,
                           pattern: str,
                           indicators: Dict,
                           entry: float,
                           stop_loss: float,
                           take_profits: List[tuple],
                           leverage: str,
                           risk: str,
                           valid_minutes: int = 30) -> str:
        """
        Форматировать сигнал SHORT
        
        Args:
            symbol: Торговая пара
            score: Short Score (0-100)
            price: Текущая цена
            pattern: Название паттерна
            indicators: Словарь индикаторов
            entry: Цена входа
            stop_loss: Стоп-лосс
            take_profits: Список (цена, процент_закрытия)
            leverage: Рекомендуемое плечо
            risk: Процент риска
            valid_minutes: Время действия сигнала
        
        Returns:
            HTML форматированный текст
        """
        # Эмодзи в зависимости от Score
        if score >= 85:
            score_emoji = "🔥"
            strength = "ЭКСТРЕМАЛЬНЫЙ"
        elif score >= 75:
            score_emoji = "⚡"
            strength = "СИЛЬНЫЙ"
        elif score >= 65:
            score_emoji = "✅"
            strength = "ХОРОШИЙ"
        else:
            score_emoji = "⚠️"
            strength = "СРЕДНИЙ"
        
        message = f"""
<b>{score_emoji} SHORT SIGNAL | {strength}</b>
<b>Score: {score}%</b>

<b>💎 SYMBOL:</b> <code>{symbol}</code>
<b>💰 Price:</b> ${price:,.2f}
<b>📊 Pattern:</b> {pattern}

<b>📉 Indicators:</b>
"""
        
        # Добавляем индикаторы
        for name, value in indicators.items():
            if "RSI" in name:
                emoji = "🟥" if value > 70 else "🟨"
            elif "Funding" in name:
                emoji = "🟥" if value > 0 else "🟨"
            elif "L/S" in name or "Ratio" in name:
                emoji = "🟥" if value > 60 else "🟨"
            else:
                emoji = "📊"
            
            message += f"{emoji} {name}: {value}\n"
        
        # Уровни
        message += f"\n<b>🎯 ENTRY:</b> <code>${entry:,.2f}</code>\n"
        message += f"<b>🛑 SL:</b> <code>${stop_loss:,.2f}</code> ({self._calc_pct(entry, stop_loss):+.2f}%)\n\n"
        
        # Take Profits
        message += "<b>🎯 Take Profits:</b>\n"
        for i, (tp_price, pct) in enumerate(take_profits[:6], 1):
            tp_pct = self._calc_pct(entry, tp_price)
            message += f"  TP{i}: <code>${tp_price:,.2f}</code> ({tp_pct:+.1f}%) | {pct}% pos\n"
        
        # Риск-менеджмент
        message += f"\n<b>⚡ Rec. Leverage:</b> {leverage}\n"
        message += f"<b>💵 Risk:</b> {risk}\n"
        message += f"<b>⏰ Valid for:</b> {valid_minutes} minutes\n"
        message += f"<b>🕐 Time:</b> {datetime.utcnow().strftime('%H:%M UTC')}\n"
        
        return message
    
    def format_long_signal(self,
                          symbol: str,
                          score: int,
                          price: float,
                          pattern: str,
                          indicators: Dict,
                          entry: float,
                          stop_loss: float,
                          take_profits: List[tuple],
                          leverage: str,
                          risk: str,
                          valid_minutes: int = 30) -> str:
        """Форматировать сигнал LONG (зеркальный SHORT)"""
        
        if score >= 85:
            score_emoji = "🔥"
            strength = "ЭКСТРЕМАЛЬНЫЙ"
        elif score >= 75:
            score_emoji = "⚡"
            strength = "СИЛЬНЫЙ"
        elif score >= 65:
            score_emoji = "✅"
            strength = "ХОРОШИЙ"
        else:
            score_emoji = "⚠️"
            strength = "СРЕДНИЙ"
        
        message = f"""
<b>{score_emoji} LONG SIGNAL | {strength}</b>
<b>Score: {score}%</b>

<b>💎 SYMBOL:</b> <code>{symbol}</code>
<b>💰 Price:</b> ${price:,.2f}
<b>📊 Pattern:</b> {pattern}

<b>📈 Indicators:</b>
"""
        
        for name, value in indicators.items():
            if "RSI" in name:
                emoji = "🟩" if value < 30 else "🟨"
            elif "Funding" in name:
                emoji = "🟩" if value < 0 else "🟨"
            elif "L/S" in name or "Ratio" in name:
                emoji = "🟩" if value < 40 else "🟨"
            else:
                emoji = "📊"
            
            message += f"{emoji} {name}: {value}\n"
        
        message += f"\n<b>🎯 ENTRY:</b> <code>${entry:,.2f}</code>\n"
        message += f"<b>🛑 SL:</b> <code>${stop_loss:,.2f}</code> ({self._calc_pct(entry, stop_loss):+.2f}%)\n\n"
        
        message += "<b>🎯 Take Profits:</b>\n"
        for i, (tp_price, pct) in enumerate(take_profits[:6], 1):
            tp_pct = self._calc_pct(entry, tp_price)
            message += f"  TP{i}: <code>${tp_price:,.2f}</code> ({tp_pct:+.1f}%) | {pct}% pos\n"
        
        message += f"\n<b>⚡ Rec. Leverage:</b> {leverage}\n"
        message += f"<b>💵 Risk:</b> {risk}\n"
        message += f"<b>⏰ Valid for:</b> {valid_minutes} minutes\n"
        message += f"<b>🕐 Time:</b> {datetime.utcnow().strftime('%H:%M UTC')}\n"
        
        return message
    
    def _calc_pct(self, entry: float, target: float) -> float:
        """Расчёт процентного изменения"""
        if entry == 0:
            return 0.0
        return ((target - entry) / entry) * 100
    
    # =========================================================================
    # POSITION UPDATES
    # =========================================================================
    
    def format_position_opened(self,
                              symbol: str,
                              direction: str,
                              entry_price: float,
                              size: float,
                              leverage: int,
                              stop_loss: float,
                              take_profits: List[tuple],
                              score: int) -> str:
        """Сообщение об открытии позиции"""
        
        emoji = "🔴" if direction == "SHORT" else "🟢"
        
        message = f"""
<b>{emoji} POSITION OPENED</b>

<b>💎 {symbol} {direction}</b>
<b>💰 Entry:</b> <code>${entry_price:,.2f}</code>
<b>📊 Size:</b> {size:.4f} (${size * entry_price:,.0f})
<b>⚡ Leverage:</b> {leverage}x
<b>🔥 Score:</b> {score}%

<b>🛑 SL:</b> <code>${stop_loss:,.2f}</code>

<b>🎯 Take Profits:</b>
"""
        for i, (tp, pct) in enumerate(take_profits[:6], 1):
            message += f"  TP{i}: ${tp:,.2f} | {pct}%\n"
        
        message += f"\n<b>⏰ Time:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}"
        
        return message
    
    def format_tp_hit(self,
                     symbol: str,
                     direction: str,
                     tp_level: int,
                     tp_price: float,
                     pnl: float,
                     pnl_pct: float,
                     closed_pct: float) -> str:
        """Сообщение о достижении TP"""
        
        emoji = "💰" if pnl > 0 else "😔"
        
        return f"""
<b>{emoji} TAKE PROFIT HIT | TP{tp_level}</b>

<b>💎 {symbol} {direction}</b>
<b>📈 Price:</b> <code>${tp_price:,.2f}</code>
<b>💵 PnL:</b> <code>${pnl:+.2f}</code> ({pnl_pct:+.2f}%)
<b>📊 Closed:</b> {closed_pct}% of position

<b>⏰ Time:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}
"""
    
    def format_sl_hit(self,
                     symbol: str,
                     direction: str,
                     sl_price: float,
                     pnl: float,
                     pnl_pct: float) -> str:
        """Сообщение о срабатывании SL"""
        
        return f"""
<b>🛑 STOP LOSS HIT</b>

<b>💎 {symbol} {direction}</b>
<b>📉 Price:</b> <code>${sl_price:,.2f}</code>
<b>💵 PnL:</b> <code>${pnl:+.2f}</code> ({pnl_pct:+.2f}%)

<b>⏰ Time:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}
"""
    
    # =========================================================================
    # DAILY REPORTS
    # =========================================================================
    
    def format_daily_report(self,
                           bot_type: str,
                           date: str,
                           signals: int,
                           trades: int,
                           wins: int,
                           losses: int,
                           total_pnl: float,
                           win_rate: float,
                           best_trade: Optional[Dict] = None,
                           worst_trade: Optional[Dict] = None) -> str:
        """Ежедневный отчёт"""
        
        emoji = "🔴" if bot_type == "SHORT" else "🟢"
        
        message = f"""
<b>{emoji} DAILY REPORT | {bot_type} BOT</b>
<b>📅 Date:</b> {date}

<b>📊 Performance:</b>
• Signals: {signals}
• Trades: {trades}
• Win Rate: {win_rate:.0f}% ({wins}W / {losses}L)
• Total PnL: <code>${total_pnl:+.2f}</code> ({(total_pnl/1000)*100:+.2f}%)
"""
        
        if best_trade:
            message += f"\n<b>💎 Best Trade:</b> {best_trade.get('symbol', 'N/A')} +${best_trade.get('pnl', 0):.2f}"
        
        if worst_trade:
            message += f"\n<b>😔 Worst Trade:</b> {worst_trade.get('symbol', 'N/A')} ${worst_trade.get('pnl', 0):.2f}"
        
        return message
    
    # =========================================================================
    # SEND METHODS
    # =========================================================================
    
    async def send_signal(self, direction: str, **kwargs) -> bool:
        """
        Отправить сигнал
        
        Args:
            direction: 'short' или 'long'
            **kwargs: Параметры для format_xxx_signal
        """
        if direction == "short":
            text = self.format_short_signal(**kwargs)
        else:
            text = self.format_long_signal(**kwargs)
        
        return await self._send_message(text)
    
    async def send_position_update(self, update_type: str, **kwargs) -> bool:
        """
        Отправить обновление позиции
        
        Args:
            update_type: 'opened', 'tp_hit', 'sl_hit'
        """
        if update_type == "opened":
            text = self.format_position_opened(**kwargs)
        elif update_type == "tp_hit":
            text = self.format_tp_hit(**kwargs)
        elif update_type == "sl_hit":
            text = self.format_sl_hit(**kwargs)
        else:
            return False
        
        return await self._send_message(text)
    
    async def send_report(self, report_type: str, **kwargs) -> bool:
        """
        Отправить отчёт
        
        Args:
            report_type: 'daily', 'weekly', etc.
        """
        if report_type == "daily":
            text = self.format_daily_report(**kwargs)
        else:
            return False
        
        return await self._send_message(text)
    
    async def send_test_message(self) -> bool:
        """Тестовое сообщение для проверки"""
        text = "🤖 <b>Bot Test</b>\n\nСоединение с Telegram установлено!"
        return await self._send_message(text)
    
    async def send_error_alert(self, error: str, context: str = "") -> bool:
        """Отправить алерт об ошибке"""
        text = f"""
<b>⚠️ BOT ERROR</b>

<b>Context:</b> {context}
<b>Error:</b> <code>{error}</code>

<b>Time:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}
"""
        return await self._send_message(text)


# ============================================================================
# DUAL BOT MANAGER
# ============================================================================

class DualTelegramManager:
    """
    Менеджер для двух Telegram ботов (SHORT и LONG)
    Может использовать один бот с двумя темами или два разных бота
    """
    
    def __init__(self,
                 short_bot_token: Optional[str] = None,
                 short_chat_id: Optional[str] = None,
                 short_topic_id: Optional[str] = None,
                 long_bot_token: Optional[str] = None,
                 long_chat_id: Optional[str] = None,
                 long_topic_id: Optional[str] = None):
        """
        Инициализация
        
        Можно использовать:
        - Один бот, один чат, две темы (topic_id)
        - Один бот, два разных чата
        - Два разных бота
        """
        # SHORT bot
        self.short_bot = TelegramBot(
            bot_token=short_bot_token or os.getenv("SHORT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=short_chat_id or os.getenv("SHORT_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
            topic_id=short_topic_id or os.getenv("SHORT_TELEGRAM_TOPIC_ID")
        )
        
        # LONG bot
        self.long_bot = TelegramBot(
            bot_token=long_bot_token or os.getenv("LONG_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=long_chat_id or os.getenv("LONG_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
            topic_id=long_topic_id or os.getenv("LONG_TELEGRAM_TOPIC_ID")
        )
    
    async def send_signal(self, direction: str, **kwargs) -> bool:
        """Отправить сигнал в соответствующий канал"""
        if direction == "short":
            return await self.short_bot.send_signal(direction="short", **kwargs)
        else:
            return await self.long_bot.send_signal(direction="long", **kwargs)
    
    async def test_connections(self) -> Dict[str, bool]:
        """Проверить соединения с обоими ботами"""
        results = {}
        
        results["short"] = await self.short_bot.send_test_message()
        results["long"] = await self.long_bot.send_test_message()
        
        return results
    
    async def close(self):
        """Закрыть все соединения"""
        await self.short_bot.close()
        await self.long_bot.close()


# ============================================================================
# SINGLETON
# ============================================================================

_telegram_bot = None
_dual_manager = None

def get_telegram_bot() -> TelegramBot:
    """Получить singleton TelegramBot"""
    global _telegram_bot
    if _telegram_bot is None:
        _telegram_bot = TelegramBot()
    return _telegram_bot

def get_dual_manager() -> DualTelegramManager:
    """Получить singleton DualTelegramManager"""
    global _dual_manager
    if _dual_manager is None:
        _dual_manager = DualTelegramManager()
    return _dual_manager


# ============================================================================
# EXAMPLE
# ============================================================================

async def test():
    """Тест Telegram бота"""
    import os
    
    # Проверяем переменные окружения
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не установлены")
        print("Установите их для тестирования")
        return
    
    bot = TelegramBot(token, chat_id)
    
    # Тестовое сообщение
    print("Отправка тестового сообщения...")
    success = await bot.send_test_message()
    
    if success:
        print("✅ Тестовое сообщение отправлено!")
    else:
        print("❌ Ошибка отправки")
    
    # Тестовый сигнал
    print("\nОтправка тестового сигнала...")
    success = await bot.send_signal(
        direction="short",
        symbol="BTCUSDT.P",
        score=78,
        price=73500.0,
        pattern="MEGA SHORT",
        indicators={
            "RSI": "78.5 (перекуплен)",
            "Funding": "+0.42% (лонги платят)",
            "L/S Ratio": "72% лонгов",
            "OI Change": "+18% за 4д"
        },
        entry=73500.0,
        stop_loss=74200.0,
        take_profits=[
            (72400.0, 25),
            (71295.0, 25),
            (69825.0, 20),
            (68870.0, 15),
            (67253.0, 10),
            (64573.0, 5)
        ],
        leverage="5-10x",
        risk="≤1% deposit"
    )
    
    if success:
        print("✅ Тестовый сигнал отправлен!")
    else:
        print("❌ Ошибка отправки сигнала")
    
    await bot.close()


if __name__ == "__main__":
    asyncio.run(test())
