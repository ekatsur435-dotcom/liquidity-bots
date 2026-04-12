"""
Telegram Bot Integration
Отправка сигналов и приём команд через Webhook
"""

import os
import asyncio
from typing import Optional, Dict, List
from datetime import datetime

import aiohttp


class TelegramBot:
    """Telegram бот для отправки сигналов и приёма команд"""
    
    def __init__(self,
                 bot_token: Optional[str] = None,
                 chat_id: Optional[str] = None,
                 topic_id: Optional[str] = None):
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
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    # =========================================================================
    # WEBHOOK
    # =========================================================================

    async def setup_webhook(self, webhook_url: str) -> bool:
        """
        Зарегистрировать webhook в Telegram.
        Вызывать один раз при старте бота.
        
        Args:
            webhook_url: Полный URL вида https://your-bot.onrender.com/webhook
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/setWebhook"
            payload = {
                "url": webhook_url,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True
            }
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    print(f"✅ Webhook registered: {webhook_url}")
                    return True
                else:
                    print(f"❌ Webhook failed: {data}")
                    return False
        except Exception as e:
            print(f"Error setting webhook: {e}")
            return False

    async def delete_webhook(self) -> bool:
        """Удалить webhook (для переключения на polling)"""
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/deleteWebhook",
                json={"drop_pending_updates": True},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("ok", False)
        except Exception as e:
            print(f"Error deleting webhook: {e}")
            return False

    async def get_webhook_info(self) -> Optional[Dict]:
        """Получить информацию о текущем webhook"""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/getWebhookInfo",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("result")
        except Exception as e:
            print(f"Error getting webhook info: {e}")
            return None

    # =========================================================================
    # SEND
    # =========================================================================
    
    async def _send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            if self.topic_id:
                payload["message_thread_id"] = int(self.topic_id)
            
            session = await self._get_session()
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    return True
                else:
                    error_text = await response.text()
                    print(f"Telegram API error: {response.status} - {error_text}")
                    return False
        except Exception as e:
            print(f"Error sending Telegram message: {e}")
            return False

    async def send_message(self, text: str) -> bool:
        return await self._send_message(text)

    async def send_signal(self, direction: str, **kwargs) -> bool:
        if direction == "short":
            text = self.format_short_signal(**kwargs)
        else:
            text = self.format_long_signal(**kwargs)
        return await self._send_message(text)

    async def send_test_message(self) -> bool:
        text = "🤖 <b>Bot Connected</b>\n\nСоединение с Telegram установлено!"
        return await self._send_message(text)

    async def send_error_alert(self, error: str, context: str = "") -> bool:
        text = (
            f"<b>⚠️ BOT ERROR</b>\n\n"
            f"<b>Context:</b> {context}\n"
            f"<b>Error:</b> <code>{error}</code>\n"
            f"<b>Time:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}"
        )
        return await self._send_message(text)

    async def send_position_update(self, update_type: str, **kwargs) -> bool:
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
        if report_type == "daily":
            text = self.format_daily_report(**kwargs)
        else:
            return False
        return await self._send_message(text)

    # =========================================================================
    # FORMAT: SIGNALS
    # =========================================================================

    def _calc_pct(self, entry: float, target: float) -> float:
        if entry == 0:
            return 0.0
        return ((target - entry) / entry) * 100

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
        if score >= 85:
            score_emoji, strength = "🔥", "ЭКСТРЕМАЛЬНЫЙ"
        elif score >= 75:
            score_emoji, strength = "⚡", "СИЛЬНЫЙ"
        elif score >= 65:
            score_emoji, strength = "✅", "ХОРОШИЙ"
        else:
            score_emoji, strength = "⚠️", "СРЕДНИЙ"

        message = (
            f"\n<b>{score_emoji} SHORT SIGNAL | {strength}</b>\n"
            f"<b>Score: {score}%</b>\n\n"
            f"<b>💎 SYMBOL:</b> <code>{symbol}</code>\n"
            f"<b>💰 Price:</b> ${price:,.2f}\n"
            f"<b>📊 Pattern:</b> {pattern}\n\n"
            f"<b>📉 Indicators:</b>\n"
        )

        for name, value in indicators.items():
            if "RSI" in name:
                emoji = "🟥"
            elif "Funding" in name:
                emoji = "🟥"
            elif "L/S" in name or "Ratio" in name:
                emoji = "🟥"
            else:
                emoji = "📊"
            message += f"{emoji} {name}: {value}\n"

        message += f"\n<b>🎯 ENTRY:</b> <code>${entry:,.2f}</code>\n"
        message += f"<b>🛑 SL:</b> <code>${stop_loss:,.2f}</code> ({self._calc_pct(entry, stop_loss):+.2f}%)\n\n"
        message += "<b>🎯 Take Profits:</b>\n"
        for i, (tp_price, pct) in enumerate(take_profits[:6], 1):
            tp_pct = self._calc_pct(entry, tp_price)
            message += f"  TP{i}: <code>${tp_price:,.2f}</code> ({tp_pct:+.1f}%) | {pct}% pos\n"

        message += (
            f"\n<b>⚡ Leverage:</b> {leverage}\n"
            f"<b>💵 Risk:</b> {risk}\n"
            f"<b>⏰ Valid:</b> {valid_minutes} min\n"
            f"<b>🕐</b> {datetime.utcnow().strftime('%H:%M UTC')}\n"
        )
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
        if score >= 85:
            score_emoji, strength = "🔥", "ЭКСТРЕМАЛЬНЫЙ"
        elif score >= 75:
            score_emoji, strength = "⚡", "СИЛЬНЫЙ"
        elif score >= 65:
            score_emoji, strength = "✅", "ХОРОШИЙ"
        else:
            score_emoji, strength = "⚠️", "СРЕДНИЙ"

        message = (
            f"\n<b>{score_emoji} LONG SIGNAL | {strength}</b>\n"
            f"<b>Score: {score}%</b>\n\n"
            f"<b>💎 SYMBOL:</b> <code>{symbol}</code>\n"
            f"<b>💰 Price:</b> ${price:,.2f}\n"
            f"<b>📊 Pattern:</b> {pattern}\n\n"
            f"<b>📈 Indicators:</b>\n"
        )

        for name, value in indicators.items():
            if "RSI" in name:
                emoji = "🟩"
            elif "Funding" in name:
                emoji = "🟩"
            elif "L/S" in name or "Ratio" in name:
                emoji = "🟩"
            else:
                emoji = "📊"
            message += f"{emoji} {name}: {value}\n"

        message += f"\n<b>🎯 ENTRY:</b> <code>${entry:,.2f}</code>\n"
        message += f"<b>🛑 SL:</b> <code>${stop_loss:,.2f}</code> ({self._calc_pct(entry, stop_loss):+.2f}%)\n\n"
        message += "<b>🎯 Take Profits:</b>\n"
        for i, (tp_price, pct) in enumerate(take_profits[:6], 1):
            tp_pct = self._calc_pct(entry, tp_price)
            message += f"  TP{i}: <code>${tp_price:,.2f}</code> ({tp_pct:+.1f}%) | {pct}% pos\n"

        message += (
            f"\n<b>⚡ Leverage:</b> {leverage}\n"
            f"<b>💵 Risk:</b> {risk}\n"
            f"<b>⏰ Valid:</b> {valid_minutes} min\n"
            f"<b>🕐</b> {datetime.utcnow().strftime('%H:%M UTC')}\n"
        )
        return message

    # =========================================================================
    # FORMAT: POSITIONS
    # =========================================================================

    def format_position_opened(self, symbol, direction, entry_price,
                               size, leverage, stop_loss, take_profits, score) -> str:
        emoji = "🔴" if direction == "SHORT" else "🟢"
        message = (
            f"\n<b>{emoji} POSITION OPENED</b>\n\n"
            f"<b>💎 {symbol} {direction}</b>\n"
            f"<b>💰 Entry:</b> <code>${entry_price:,.2f}</code>\n"
            f"<b>📊 Size:</b> {size:.4f} (${size * entry_price:,.0f})\n"
            f"<b>⚡ Leverage:</b> {leverage}x\n"
            f"<b>🔥 Score:</b> {score}%\n\n"
            f"<b>🛑 SL:</b> <code>${stop_loss:,.2f}</code>\n\n"
            f"<b>🎯 Take Profits:</b>\n"
        )
        for i, (tp, pct) in enumerate(take_profits[:6], 1):
            message += f"  TP{i}: ${tp:,.2f} | {pct}%\n"
        message += f"\n<b>⏰</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}"
        return message

    def format_tp_hit(self, symbol, direction, tp_level,
                      tp_price, pnl, pnl_pct, closed_pct) -> str:
        emoji = "💰" if pnl > 0 else "😔"
        return (
            f"\n<b>{emoji} TAKE PROFIT HIT | TP{tp_level}</b>\n\n"
            f"<b>💎 {symbol} {direction}</b>\n"
            f"<b>📈 Price:</b> <code>${tp_price:,.2f}</code>\n"
            f"<b>💵 PnL:</b> <code>${pnl:+.2f}</code> ({pnl_pct:+.2f}%)\n"
            f"<b>📊 Closed:</b> {closed_pct}% of position\n\n"
            f"<b>⏰</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
        )

    def format_sl_hit(self, symbol, direction, sl_price, pnl, pnl_pct) -> str:
        return (
            f"\n<b>🛑 STOP LOSS HIT</b>\n\n"
            f"<b>💎 {symbol} {direction}</b>\n"
            f"<b>📉 Price:</b> <code>${sl_price:,.2f}</code>\n"
            f"<b>💵 PnL:</b> <code>${pnl:+.2f}</code> ({pnl_pct:+.2f}%)\n\n"
            f"<b>⏰</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
        )

    def format_daily_report(self, bot_type, date, signals, trades,
                            wins, losses, total_pnl, win_rate,
                            best_trade=None, worst_trade=None) -> str:
        emoji = "🔴" if bot_type == "SHORT" else "🟢"
        message = (
            f"\n<b>{emoji} DAILY REPORT | {bot_type} BOT</b>\n"
            f"<b>📅 Date:</b> {date}\n\n"
            f"<b>📊 Performance:</b>\n"
            f"• Signals: {signals}\n"
            f"• Trades: {trades}\n"
            f"• Win Rate: {win_rate:.0f}% ({wins}W / {losses}L)\n"
            f"• Total PnL: <code>${total_pnl:+.2f}</code>\n"
        )
        if best_trade:
            message += f"\n<b>💎 Best:</b> {best_trade.get('symbol')} +${best_trade.get('pnl', 0):.2f}"
        if worst_trade:
            message += f"\n<b>😔 Worst:</b> {worst_trade.get('symbol')} ${worst_trade.get('pnl', 0):.2f}"
        return message


# ============================================================================
# COMMAND HANDLER
# ============================================================================

class TelegramCommandHandler:
    """
    Обработчик команд Telegram.
    Работает и в личке, и в группе — отвечает туда, откуда пришла команда.
    """

    def __init__(self, bot: TelegramBot, redis_client=None, bot_state=None,
                 bot_type: str = "short"):
        self.bot = bot
        self.redis = redis_client
        self.state = bot_state
        self.bot_type = bot_type  # "short" или "long" — передаётся из Config.BOT_TYPE
        self.commands = {
            '/start':    self.cmd_start,
            '/help':     self.cmd_help,
            '/status':   self.cmd_status,
            '/signals':  self.cmd_signals,
            '/stats':    self.cmd_stats,
            '/ping':     self.cmd_ping,
            '/scan':     self.cmd_scan,
            '/pause':    self.cmd_pause,
            '/resume':   self.cmd_resume,
            '/setscore': self.cmd_set_min_score,
        }

    async def _reply(self, chat_id: str, text: str) -> bool:
        """
        Отправить ответ в конкретный chat_id.
        Это ключевой метод — отвечает ТУДА откуда пришла команда,
        а не в захардкоженный self.bot.chat_id.
        """
        try:
            url = f"{self.bot.base_url}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            session = await self.bot._get_session()
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"Reply error to {chat_id}: {e}")
            return False

    async def handle_update(self, update: Dict) -> bool:
        """
        Обработать входящий update от Telegram webhook.
        Извлекаем chat_id из сообщения — отвечаем туда же.
        Работает и в личке и в группе.
        """
        try:
            message = update.get("message") or update.get("edited_message")
            if not message:
                return False

            text = message.get("text", "").strip()
            if not text.startswith("/"):
                return False

            # ✅ Берём chat_id из самого сообщения — не из конфига!
            reply_chat_id = str(message.get("chat", {}).get("id", ""))
            if not reply_chat_id:
                return False

            # Парсим команду
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()  # /start@BotName → /start
            args = parts[1:] if len(parts) > 1 else []

            print(f"📨 Command: {cmd} from chat {reply_chat_id}")

            handler = self.commands.get(cmd)
            if handler:
                await handler(args, reply_chat_id)
                return True
            else:
                await self._reply(
                    reply_chat_id,
                    f"❓ Неизвестная команда: <code>{cmd}</code>\n"
                    f"Напиши /help для списка команд."
                )
                return False

        except Exception as e:
            print(f"Error handling update: {e}")
            return False

    # =========================================================================
    # COMMANDS — все принимают reply_chat_id и отвечают туда
    # =========================================================================

    async def cmd_start(self, args, reply_chat_id: str):
        bot_emoji = "🔴" if self.bot_type == "short" else "🟢"
        bot_name = "SHORT" if self.bot_type == "short" else "LONG"
        await self._reply(reply_chat_id,
            f"{bot_emoji} <b>Liquidity {bot_name} Bot</b>\n\n"
            "<b>Команды:</b>\n"
            "📊 /status — Статус бота\n"
            "🎯 /signals — Активные сигналы\n"
            "📉 /stats — Статистика за 7 дней\n"
            "🔍 /scan — Запустить скан сейчас\n"
            "⏸ /pause — Остановить сигналы\n"
            "▶️ /resume — Возобновить\n"
            "⚙️ /setscore 75 — Установить мин. скор\n"
            "🏓 /ping — Проверка связи\n\n"
            "Сигналы приходят автоматически!"
        )

    async def cmd_help(self, args, reply_chat_id: str):
        await self.cmd_start(args, reply_chat_id)

    async def cmd_ping(self, args, reply_chat_id: str):
        await self._reply(reply_chat_id, "🏓 Pong! Бот активен ✅")

    async def cmd_status(self, args, reply_chat_id: str):
        if self.state:
            wl = len(self.state.watchlist)
            last = self.state.last_scan.strftime("%H:%M UTC") if self.state.last_scan else "никогда"
            running = "✅ Работает" if self.state.is_running else "❌ Остановлен"
            redis_ok = "✅" if (self.redis and self.redis.health_check()) else "❌"
            score = getattr(self.state, '_min_score', 65)
            await self._reply(reply_chat_id,
                f"🤖 <b>Статус бота</b>\n\n"
                f"Состояние: {running}\n"
                f"Watchlist: {wl} монет\n"
                f"Последний скан: {last}\n"
                f"Активных сигналов: {self.state.active_signals}\n"
                f"Redis: {redis_ok}\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
            )
        else:
            await self._reply(reply_chat_id, "✅ Бот работает")

    async def cmd_signals(self, args, reply_chat_id: str):
        if self.redis:
            try:
                signals = self.redis.get_active_signals(self.bot_type)
                if signals:
                    msg = f"🎯 <b>Активные сигналы ({len(signals)}):</b>\n\n"
                    for s in signals[:8]:
                        d = "🔴" if s.get("direction") == "short" else "🟢"
                        msg += f"{d} <code>{s.get('symbol')}</code> — Score: {s.get('score')}%\n"
                    await self._reply(reply_chat_id, msg)
                    return
            except Exception as e:
                print(f"cmd_signals error: {e}")
        await self._reply(reply_chat_id, "🎯 Нет активных сигналов")

    async def cmd_stats(self, args, reply_chat_id: str):
        if self.redis:
            try:
                stats = self.redis.get_stats_range(self.bot_type, 7)
                total_signals = sum(s.get("signals", 0) for s in stats)
                total_pnl = sum(s.get("pnl", 0.0) for s in stats)
                await self._reply(reply_chat_id,
                    f"📉 <b>Статистика за 7 дней</b>\n\n"
                    f"📨 Сигналов: {total_signals}\n"
                    f"💵 P&L: ${total_pnl:+.2f}\n"
                    f"🕐 {datetime.utcnow().strftime('%d.%m.%Y %H:%M UTC')}"
                )
                return
            except Exception as e:
                print(f"cmd_stats error: {e}")
        await self._reply(reply_chat_id, "📉 Статистика пока пуста")

    async def cmd_scan(self, args, reply_chat_id: str):
        await self._reply(reply_chat_id, "🔍 Запускаю скан рынка...")
        try:
            # Импортируем scan_market из main через динамический импорт
            import importlib
            main_mod = importlib.import_module("main")
            asyncio.create_task(main_mod.scan_market())
        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка запуска скана: {e}")

    async def cmd_pause(self, args, reply_chat_id: str):
        if self.state:
            self.state.is_running = False
        await self._reply(reply_chat_id, "⏸ Бот приостановлен.\nКоманда /resume для возобновления.")

    async def cmd_resume(self, args, reply_chat_id: str):
        if self.state:
            self.state.is_running = True
        await self._reply(reply_chat_id, "▶️ Бот возобновил работу!\nСканирование активно.")

    async def cmd_set_min_score(self, args, reply_chat_id: str):
        if args and args[0].isdigit():
            score = int(args[0])
            if 50 <= score <= 95:
                try:
                    import importlib
                    main_mod = importlib.import_module("main")
                    main_mod.Config.MIN_SCORE = score
                    # Сохраняем для /status
                    if self.state:
                        self.state._min_score = score
                    await self._reply(reply_chat_id,
                        f"✅ Минимальный скор обновлён: <b>{score}%</b>\n"
                        f"Следующий скан будет фильтровать по {score}%"
                    )
                except Exception as e:
                    await self._reply(reply_chat_id, f"⚠️ Не удалось обновить: {e}")
            else:
                await self._reply(reply_chat_id, "⚠️ Скор должен быть от 50 до 95")
        else:
            await self._reply(reply_chat_id,
                "⚙️ Использование: <code>/setscore 75</code>\n"
                "Например: /setscore 75 (рекомендовано)\n"
                "Диапазон: 50–95"
            )


# ============================================================================
# DUAL BOT MANAGER
# ============================================================================

class DualTelegramManager:
    def __init__(self,
                 short_bot_token=None, short_chat_id=None, short_topic_id=None,
                 long_bot_token=None, long_chat_id=None, long_topic_id=None):
        self.short_bot = TelegramBot(
            bot_token=short_bot_token or os.getenv("SHORT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=short_chat_id or os.getenv("SHORT_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
            topic_id=short_topic_id or os.getenv("SHORT_TELEGRAM_TOPIC_ID")
        )
        self.long_bot = TelegramBot(
            bot_token=long_bot_token or os.getenv("LONG_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=long_chat_id or os.getenv("LONG_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
            topic_id=long_topic_id or os.getenv("LONG_TELEGRAM_TOPIC_ID")
        )

    async def send_signal(self, direction: str, **kwargs) -> bool:
        if direction == "short":
            return await self.short_bot.send_signal(direction="short", **kwargs)
        return await self.long_bot.send_signal(direction="long", **kwargs)

    async def test_connections(self) -> Dict[str, bool]:
        return {
            "short": await self.short_bot.send_test_message(),
            "long": await self.long_bot.send_test_message()
        }

    async def close(self):
        await self.short_bot.close()
        await self.long_bot.close()


# ============================================================================
# SINGLETON
# ============================================================================

_telegram_bot = None

def get_telegram_bot() -> TelegramBot:
    global _telegram_bot
    if _telegram_bot is None:
        _telegram_bot = TelegramBot()
    return _telegram_bot
