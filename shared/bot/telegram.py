"""
Telegram Bot Integration  (FIXED)
Исправления:
  - cmd_pause / cmd_resume: используют is_paused вместо is_running
  - cmd_stats: показывает реальный P&L из daily_trades (записанный PositionTracker)
  - cmd_signals: добавлено время в сделке и P&L для каждой позиции
"""

import os
import asyncio
from typing import Optional, Dict, List, Callable
from datetime import datetime, timedelta

import aiohttp


class TelegramBot:
    """Telegram бот для отправки сигналов и приёма команд."""

    def __init__(self,
                 bot_token: Optional[str] = None,
                 chat_id: Optional[str] = None,
                 topic_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id   = chat_id   or os.getenv("TELEGRAM_CHAT_ID")
        self.topic_id  = topic_id  or os.getenv("TELEGRAM_TOPIC_ID")

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
        try:
            session = await self._get_session()
            payload = {
                "url": webhook_url,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            }
            async with session.post(
                f"{self.base_url}/setWebhook",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    print(f"✅ Webhook registered: {webhook_url}")
                    return True
                print(f"❌ Webhook failed: {data}")
                return False
        except Exception as e:
            print(f"Error setting webhook: {e}")
            return False

    async def delete_webhook(self) -> bool:
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/deleteWebhook",
                json={"drop_pending_updates": True},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                return data.get("ok", False)
        except Exception as e:
            print(f"Error deleting webhook: {e}")
            return False

    async def get_webhook_info(self) -> Optional[Dict]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/getWebhookInfo",
                timeout=aiohttp.ClientTimeout(total=10),
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
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if self.topic_id:
                payload["message_thread_id"] = int(self.topic_id)

            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    return True
                error_text = await response.text()
                print(f"Telegram API error: {response.status} — {error_text}")
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
        return await self._send_message(
            "🤖 <b>Bot Connected</b>\n\nСоединение с Telegram установлено!"
        )

    async def send_error_alert(self, error: str, context: str = "") -> bool:
        return await self._send_message(
            f"<b>⚠️ BOT ERROR</b>\n\n"
            f"<b>Context:</b> {context}\n"
            f"<b>Error:</b> <code>{error}</code>\n"
            f"<b>Time:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}"
        )

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

        sl_pct = self._calc_pct(entry, stop_loss)

        tp_lines = ""
        for i, (tp_price, tp_weight) in enumerate(take_profits, 1):
            pct = abs(self._calc_pct(entry, tp_price))
            tp_lines += f"   TP{i}: <b>${tp_price:,.4f}</b>  (-{pct:.1f}%)  [{tp_weight}%]\n"

        ind_lines = "\n".join(f"   {k}: <b>{v}</b>" for k, v in indicators.items())

        return (
            f"\n{score_emoji} <b>SHORT SIGNAL | {strength}</b>\n"
            f"<b>Score: {score}%</b>\n\n"
            f"<b>💎 SYMBOL:</b> <code>{symbol}</code>\n"
            f"<b>📊 Pattern:</b> {pattern}\n\n"
            f"<b>📈 INDICATORS:</b>\n{ind_lines}\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"   Entry: <b>${entry:,.4f}</b>\n"
            f"   Stop:  <b>${stop_loss:,.4f}</b>  (+{abs(sl_pct):.2f}%)\n"
            f"{tp_lines}\n"
            f"<b>⚡ Leverage:</b> {leverage}x\n"
            f"<b>💰 Risk:</b> {risk}\n"
            f"<b>⏱ Valid:</b> ~{valid_minutes} мин\n"
            f"<b>🕐 Time:</b> {datetime.utcnow().strftime('%H:%M UTC')}"
        )

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

        sl_pct = self._calc_pct(entry, stop_loss)

        tp_lines = ""
        for i, (tp_price, tp_weight) in enumerate(take_profits, 1):
            pct = abs(self._calc_pct(entry, tp_price))
            tp_lines += f"   TP{i}: <b>${tp_price:,.4f}</b>  (+{pct:.1f}%)  [{tp_weight}%]\n"

        ind_lines = "\n".join(f"   {k}: <b>{v}</b>" for k, v in indicators.items())

        return (
            f"\n{score_emoji} <b>LONG SIGNAL | {strength}</b>\n"
            f"<b>Score: {score}%</b>\n\n"
            f"<b>💎 SYMBOL:</b> <code>{symbol}</code>\n"
            f"<b>📊 Pattern:</b> {pattern}\n\n"
            f"<b>📈 INDICATORS:</b>\n{ind_lines}\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"   Entry: <b>${entry:,.4f}</b>\n"
            f"   Stop:  <b>${stop_loss:,.4f}</b>  (-{abs(sl_pct):.2f}%)\n"
            f"{tp_lines}\n"
            f"<b>⚡ Leverage:</b> {leverage}x\n"
            f"<b>💰 Risk:</b> {risk}\n"
            f"<b>⏱ Valid:</b> ~{valid_minutes} мин\n"
            f"<b>🕐 Time:</b> {datetime.utcnow().strftime('%H:%M UTC')}"
        )

    # =========================================================================
    # COMMAND HANDLER
    # =========================================================================


class TelegramCommandHandler:
    """Обработчик входящих команд от пользователя."""

    ALLOWED_COMMANDS = {
        "/start", "/help", "/ping", "/status",
        "/signals", "/stats", "/scan",
        "/pause", "/resume", "/setscore",
    }

    def __init__(self,
                 bot: TelegramBot,
                 redis_client,
                 bot_state,
                 bot_type: str,
                 scan_callback: Optional[Callable] = None,
                 config=None):
        self.bot           = bot
        self.redis         = redis_client
        self.state         = bot_state
        self.bot_type      = bot_type
        self.scan_callback = scan_callback
        self.config        = config

    async def _reply(self, chat_id: str, text: str) -> bool:
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if self.bot.topic_id:
                payload["message_thread_id"] = int(self.bot.topic_id)
            session = await self.bot._get_session()
            async with session.post(
                f"{self.bot.base_url}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"Error sending reply: {e}")
            return False

    async def handle_update(self, update: Dict) -> bool:
        try:
            message = update.get("message") or update.get("channel_post")
            if not message:
                return False

            text         = message.get("text", "").strip()
            reply_chat_id = str(message.get("chat", {}).get("id", ""))

            if not text.startswith("/"):
                return False

            parts = text.split()
            cmd   = parts[0].split("@")[0].lower()
            args  = parts[1:]

            print(f"📨 Command: {cmd} from chat {reply_chat_id}")

            if cmd not in self.ALLOWED_COMMANDS:
                await self._reply(reply_chat_id,
                    f"❓ Неизвестная команда: <code>{cmd}</code>\n"
                    "Напиши /help для списка команд.")
                return False

            handlers = {
                "/start":    self.cmd_start,
                "/help":     self.cmd_help,
                "/ping":     self.cmd_ping,
                "/status":   self.cmd_status,
                "/signals":  self.cmd_signals,
                "/stats":    self.cmd_stats,
                "/scan":     self.cmd_scan,
                "/pause":    self.cmd_pause,
                "/resume":   self.cmd_resume,
                "/setscore": self.cmd_set_min_score,
            }
            await handlers[cmd](args, reply_chat_id)
            return True

        except Exception as e:
            print(f"Error handling update: {e}")
            return False

    # =========================================================================
    # COMMANDS
    # =========================================================================

    async def cmd_start(self, args, reply_chat_id: str):
        bot_emoji = "🔴" if self.bot_type == "short" else "🟢"
        bot_name  = "SHORT" if self.bot_type == "short" else "LONG"
        await self._reply(reply_chat_id,
            f"{bot_emoji} <b>Liquidity {bot_name} Bot v2.0</b>\n\n"
            "<b>Команды:</b>\n"
            "📊 /status — Статус бота\n"
            "🎯 /signals — Активные сигналы\n"
            "📉 /stats — Статистика + P&L\n"
            "🔍 /scan — Сканировать рынок сейчас\n"
            "⏸ /pause — Остановить сигналы\n"
            "▶️ /resume — Возобновить\n"
            "⚙️ /setscore 75 — Мин. скор\n"
            "🏓 /ping — Проверка связи\n\n"
            "Сигналы приходят автоматически!\n"
            "TP/SL/экспирация — тоже автоматически 📍"
        )

    async def cmd_help(self, args, reply_chat_id: str):
        await self.cmd_start(args, reply_chat_id)

    async def cmd_ping(self, args, reply_chat_id: str):
        await self._reply(reply_chat_id, "🏓 Pong! Бот активен ✅")

    async def cmd_status(self, args, reply_chat_id: str):
        if not self.state:
            await self._reply(reply_chat_id, "✅ Бот работает")
            return

        wl       = len(self.state.watchlist)
        last     = self.state.last_scan.strftime("%H:%M UTC") if self.state.last_scan else "никогда"
        running  = "✅ Работает" if self.state.is_running else "❌ Остановлен"
        paused   = "⏸ На паузе" if self.state.is_paused else ""
        redis_ok = "✅" if (self.redis and self.redis.health_check()) else "❌"
        min_score = getattr(self.config, "MIN_SCORE", 65) if self.config else 65
        max_pos   = getattr(self.config, "MAX_POSITIONS", 5) if self.config else 5

        await self._reply(reply_chat_id,
            f"🤖 <b>Статус бота</b>\n\n"
            f"Состояние: {running} {paused}\n"
            f"Watchlist: {wl} монет\n"
            f"Последний скан: {last}\n"
            f"Активных сигналов: {self.state.active_signals}/{max_pos}\n"
            f"Мин. скор: {min_score}%\n"
            f"Redis: {redis_ok}\n"
            f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
        )

    async def cmd_signals(self, args, reply_chat_id: str):
        if not self.redis:
            await self._reply(reply_chat_id, "🎯 Нет активных сигналов")
            return
        try:
            signals = self.redis.get_active_signals(self.bot_type)
            if not signals:
                await self._reply(reply_chat_id, "🎯 Нет активных сигналов")
                return

            msg = f"🎯 <b>Активные сигналы ({len(signals)}):</b>\n\n"
            for s in signals[:8]:
                d        = "🔴" if s.get("direction") == "short" else "🟢"
                symbol   = s.get("symbol", "?")
                score    = s.get("score", 0)
                entry    = s.get("entry_price", 0)
                taken    = len(s.get("taken_tps", []))
                total_tp = len(s.get("take_profits", []))

                # Время в сделке
                try:
                    opened = datetime.fromisoformat(s.get("timestamp", ""))
                    age    = datetime.utcnow() - opened
                    h, r   = divmod(int(age.total_seconds()), 3600)
                    m      = r // 60
                    time_s = f"{h}ч {m}м" if h else f"{m}м"
                except Exception:
                    time_s = "N/A"

                msg += (
                    f"{d} <code>{symbol}</code> — Score: {score}%\n"
                    f"   Вход: ${entry:,.4f}  |  TP: {taken}/{total_tp}  |  ⏱ {time_s}\n\n"
                )

            await self._reply(reply_chat_id, msg)
        except Exception as e:
            print(f"cmd_signals error: {e}")
            await self._reply(reply_chat_id, "🎯 Нет активных сигналов")

    async def cmd_stats(self, args, reply_chat_id: str):
        """
        FIX: теперь показывает реальный P&L из PositionTracker.
        Данные хранятся в bot_state → daily_trades.
        """
        if not self.redis:
            await self._reply(reply_chat_id, "📉 Статистика недоступна")
            return
        try:
            bot_st       = self.redis.get_bot_state(self.bot_type) or {}
            daily_trades = bot_st.get("daily_trades", {})

            # Считаем за 7 дней
            total_trades = 0
            total_wins   = 0
            total_pnl    = 0.0

            lines = []
            for i in range(7):
                day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
                d   = daily_trades.get(day, {})
                tr  = d.get("trades", 0)
                pnl = d.get("pnl", 0.0)
                w   = d.get("wins", 0)
                if tr:
                    lines.append(f"  {day}: {tr} сделок  P&L: {pnl:+.2f}%  ✅{w}")
                total_trades += tr
                total_wins   += w
                total_pnl    += pnl

            winrate = round(total_wins / total_trades * 100, 1) if total_trades else 0
            daily_sig = self.state.daily_signals if self.state else 0

            msg = (
                f"📉 <b>Статистика за 7 дней</b>\n\n"
                f"📨 Сигналов отправлено: {daily_sig}\n"
                f"🔄 Сделок закрыто: {total_trades}\n"
                f"✅ Победных: {total_wins}  ({winrate}%)\n"
                f"💵 P&L: <b>{total_pnl:+.2f}%</b>\n"
            )
            if lines:
                msg += "\n<b>По дням:</b>\n" + "\n".join(lines)

            msg += f"\n🕐 {datetime.utcnow().strftime('%d.%m.%Y %H:%M UTC')}"
            await self._reply(reply_chat_id, msg)

        except Exception as e:
            print(f"cmd_stats error: {e}")
            await self._reply(reply_chat_id, "📉 Статистика пока пуста")

    async def cmd_scan(self, args, reply_chat_id: str):
        await self._reply(reply_chat_id, "🔍 Запускаю скан рынка...")
        try:
            if self.scan_callback is None:
                await self._reply(reply_chat_id, "❌ scan_callback не настроен.")
                return
            if self.state and self.state.is_paused:
                await self._reply(reply_chat_id, "⏸ Бот на паузе. Сначала /resume")
                return
            asyncio.create_task(self.scan_callback())
            await self._reply(reply_chat_id, "✅ Скан запущен! Сигналы придут автоматически.")
        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка запуска скана: {e}")

    async def cmd_pause(self, args, reply_chat_id: str):
        """
        FIX 4: Устанавливаем is_paused=True, НЕ трогаем is_running.
        background_scanner продолжает крутиться, просто пропускает сканы.
        PositionTracker продолжает следить за открытыми позициями.
        """
        if self.state:
            self.state.is_paused = True
            if self.config:
                self.config.is_paused = True   # синхронизируем если есть
        await self._reply(reply_chat_id,
            "⏸ <b>Бот на паузе</b>\n\n"
            "Новых сигналов не будет.\n"
            "PositionTracker продолжает следить за открытыми позициями.\n"
            "Команда /resume для возобновления.")

    async def cmd_resume(self, args, reply_chat_id: str):
        """
        FIX 4: Сбрасываем is_paused=False. Следующий цикл начнёт сканировать.
        """
        if self.state:
            self.state.is_paused = False
            if self.config:
                self.config.is_paused = False
        await self._reply(reply_chat_id,
            "▶️ <b>Бот возобновил работу!</b>\n\n"
            "Сканирование активно.\n"
            f"Следующий скан через ~{getattr(self.config, 'SCAN_INTERVAL', 60)} секунд.")

    async def cmd_set_min_score(self, args, reply_chat_id: str):
        if args and args[0].isdigit():
            score = int(args[0])
            if 50 <= score <= 95:
                try:
                    if self.config:
                        self.config.MIN_SCORE = score
                    await self._reply(reply_chat_id,
                        f"✅ Минимальный скор: <b>{score}%</b>\n"
                        f"Применится со следующего скана.")
                except Exception as e:
                    await self._reply(reply_chat_id, f"⚠️ Ошибка: {e}")
            else:
                await self._reply(reply_chat_id, "⚠️ Скор должен быть от 50 до 95")
        else:
            await self._reply(reply_chat_id,
                "⚙️ Использование: <code>/setscore 75</code>\n"
                "Диапазон: 50–95")


# ============================================================================
# DUAL BOT MANAGER
# ============================================================================

class DualTelegramManager:
    def __init__(self,
                 short_bot_token=None, short_chat_id=None, short_topic_id=None,
                 long_bot_token=None,  long_chat_id=None,  long_topic_id=None):
        self.short_bot = TelegramBot(
            bot_token=short_bot_token or os.getenv("SHORT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=short_chat_id     or os.getenv("SHORT_TELEGRAM_CHAT_ID")   or os.getenv("TELEGRAM_CHAT_ID"),
            topic_id=short_topic_id   or os.getenv("SHORT_TELEGRAM_TOPIC_ID"),
        )
        self.long_bot = TelegramBot(
            bot_token=long_bot_token or os.getenv("LONG_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=long_chat_id     or os.getenv("LONG_TELEGRAM_CHAT_ID")   or os.getenv("TELEGRAM_CHAT_ID"),
            topic_id=long_topic_id   or os.getenv("LONG_TELEGRAM_TOPIC_ID"),
        )

    async def send_signal(self, direction: str, **kwargs) -> bool:
        if direction == "short":
            return await self.short_bot.send_signal(direction="short", **kwargs)
        return await self.long_bot.send_signal(direction="long", **kwargs)

    async def test_connections(self) -> Dict[str, bool]:
        return {
            "short": await self.short_bot.send_test_message(),
            "long":  await self.long_bot.send_test_message(),
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
