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


# ============================================================================
# SMART PRICE FORMATTER
# ============================================================================

def fmt_price(price: float) -> str:
    """
    Умное форматирование цены — количество знаков зависит от величины:
      >= 1000       →  $1,234.56        (2 знака)
      >= 1          →  $1.3350          (4 знака)
      >= 0.01       →  $0.013350        (6 знаков)
      >= 0.0001     →  $0.00013350      (8 знаков)
      < 0.0001      →  $0.000000001335  (12 знаков, для PEPE/SHIB)
    """
    if price == 0:
        return "$0"
    abs_p = abs(price)
    if abs_p >= 1000:
        return f"${price:,.2f}"
    elif abs_p >= 1:
        return f"${price:,.4f}"
    elif abs_p >= 0.01:
        return f"${price:,.6f}"
    elif abs_p >= 0.0001:
        return f"${price:,.8f}"
    else:
        return f"${price:,.12f}"



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
            tp_lines += f"   TP{i}: <b>{fmt_price(tp_price)}</b>  (-{pct:.1f}%)  [{tp_weight}%]\n"

        ind_lines = "\n".join(f"   {k}: <b>{v}</b>" for k, v in indicators.items())

        return (
            f"\n{score_emoji} <b>SHORT SIGNAL | {strength}</b>\n"
            f"<b>Score: {score}%</b>\n\n"
            f"<b>💎 SYMBOL:</b> <code>{symbol}</code>\n"
            f"<b>📊 Pattern:</b> {pattern}\n\n"
            f"<b>📈 INDICATORS:</b>\n{ind_lines}\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"   Entry: <b>{fmt_price(entry)}</b>\n"
            f"   Stop:  <b>{fmt_price(stop_loss)}</b>  (+{abs(sl_pct):.2f}%)\n"
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
            tp_lines += f"   TP{i}: <b>{fmt_price(tp_price)}</b>  (+{pct:.1f}%)  [{tp_weight}%]\n"

        ind_lines = "\n".join(f"   {k}: <b>{v}</b>" for k, v in indicators.items())

        return (
            f"\n{score_emoji} <b>LONG SIGNAL | {strength}</b>\n"
            f"<b>Score: {score}%</b>\n\n"
            f"<b>💎 SYMBOL:</b> <code>{symbol}</code>\n"
            f"<b>📊 Pattern:</b> {pattern}\n\n"
            f"<b>📈 INDICATORS:</b>\n{ind_lines}\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"   Entry: <b>{fmt_price(entry)}</b>\n"
            f"   Stop:  <b>{fmt_price(stop_loss)}</b>  (-{abs(sl_pct):.2f}%)\n"
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
        "/pause", "/resume", "/setscore", "/closeall", "/close_all",
        "/clearpos", "/balance", "/positions",
        "/emergency_stop", "/reset_stats", "/cleanup", "/clean", "/logs",
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
        """
        Отправить ответ в любой чат.
        FIX: message_thread_id добавляется ТОЛЬКО если отвечаем в группу бота.
        В личке (другой chat_id) topic_id не нужен — Telegram отклоняет такие запросы.
        """
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            # Добавляем topic только когда пишем в ГРУППУ (chat_id совпадает с ботовым)
            is_group_chat = str(chat_id) == str(self.bot.chat_id)
            if self.bot.topic_id and is_group_chat:
                payload["message_thread_id"] = int(self.bot.topic_id)

            session = await self.bot._get_session()
            async with session.post(
                f"{self.bot.base_url}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    print(f"[Telegram reply] Error {resp.status}: {err[:120]}")
                return resp.status == 200
        except Exception as e:
            print(f"Error sending reply: {e}")
            return False

    async def handle_update(self, update: Dict) -> bool:
        try:
            message = update.get("message") or update.get("channel_post")
            if not message:
                return False

            text          = message.get("text", "").strip()
            reply_chat_id = str(message.get("chat", {}).get("id", ""))
            user_id       = str(message.get("from", {}).get("id", ""))
            chat_type     = message.get("chat", {}).get("type", "private")

            if not text.startswith("/"):
                return False

            parts = text.split()
            cmd   = parts[0].split("@")[0].lower()
            args  = parts[1:]

            print(f"📨 Command: {cmd} from chat {reply_chat_id} (user {user_id}, type={chat_type})")

            # ── Безопасность: в личке отвечаем только ADMIN_USER_ID ──────────
            # В группе — всем (группа защищена самим фактом наличия invite)
            if chat_type == "private":
                admin_ids_raw = os.getenv("ADMIN_USER_IDS", "")
                if admin_ids_raw:
                    allowed = {s.strip() for s in admin_ids_raw.split(",")}
                    if user_id not in allowed:
                        print(f"⛔ Unauthorized private access from user {user_id}")
                        return False
                # Если ADMIN_USER_IDS не задан — пропускаем всех (обратная совместимость)

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
                "/clearpos": self.cmd_clearpos,
                "/closeall": self.cmd_closeall,
                "/close_all": self.cmd_closeall,
                "/balance":  self.cmd_balance,
                "/positions": self.cmd_positions,
                "/emergency_stop": self.cmd_emergency_stop,
                "/reset_stats": self.cmd_reset_stats,
                "/cleanup": self.cmd_cleanup,
                "/clean": self.cmd_clean,
                "/logs": self.cmd_logs,
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
            f"{bot_emoji} <b>Liquidity {bot_name} Bot v2.1</b>\n\n"
            "<b>📋 Команды:</b>\n"
            "📊 /status — Статус бота\n"
            "🎯 /signals — Активные сигналы\n"
            "📉 /stats — Статистика + P&L\n"
            "🔍 /scan — Сканировать рынок сейчас\n\n"
            "<b>💰 Биржа:</b>\n"
            "💳 /balance — Баланс BingX\n"
            "📈 /positions — Открытые позиции\n\n"
            "<b>⚙️ Управление:</b>\n"
            "⏸ /pause — Остановить новые сигналы\n"
            "▶️ /resume — Возобновить\n"
            "🗑 /clearpos — Сбросить застрявшие позиции\n"
            "⚙️ /setscore 75 — Мин. скор\n"
            "🏓 /ping — Проверка связи\n\n"
            "Сигналы + TP/SL уведомления — автоматически 📍"
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
                    f"   Вход: {fmt_price(entry)}  |  TP: {taken}/{total_tp}  |  ⏱ {time_s}\n\n"
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


    async def cmd_clearpos(self, args, reply_chat_id: str):
        """
        /clearpos — очистить active-сигналы из Redis.
        Нужно когда бот пишет "Max positions reached" хотя реальных позиций нет.
        Опционально: /clearpos BTCUSDT — только одну пару
                     /clearpos all    — все сигналы бота
        """
        try:
            target = args[0].upper() if args else "ALL"

            if target != "ALL":
                # Очищаем один символ
                symbol = target
                key = f"{self.bot_type}:signals:{symbol}"
                import json as _json
                signals = self.redis.client.lrange(key, 0, -1)
                if not signals:
                    await self._reply(reply_chat_id, f"❓ Сигналов по {symbol} не найдено")
                    return
                for i, s_json in enumerate(signals):
                    sig = _json.loads(s_json)
                    if sig.get("status") == "active":
                        sig["status"] = "expired"
                        sig["cleared_at"] = "manual"
                        self.redis.client.lset(key, i, _json.dumps(sig))
                await self._reply(reply_chat_id,
                    f"✅ Сигнал {symbol} помечен как expired\n"
                    f"Бот возобновит поиск по этой паре.")
            else:
                # Очищаем все active сигналы
                import json as _json
                pattern = f"{self.bot_type}:signals:*"
                keys = self.redis.client.keys(pattern)
                cleared = 0
                for key in keys:
                    signals = self.redis.client.lrange(key, 0, -1)
                    for i, s_json in enumerate(signals):
                        try:
                            sig = _json.loads(s_json)
                            if sig.get("status") == "active":
                                sig["status"] = "expired"
                                sig["cleared_at"] = "manual"
                                self.redis.client.lset(key, i, _json.dumps(sig))
                                cleared += 1
                        except Exception:
                            pass

                if self.state:
                    self.state.active_signals = 0

                await self._reply(reply_chat_id,
                    f"✅ <b>Очищено {cleared} активных сигналов</b>\n\n"
                    f"Бот снова начнёт открывать новые позиции.\n"
                    f"Используй /scan для немедленного скана.\n\n"
                    f"<i>Подсказка: /clearpos BTCUSDT — очистить одну пару</i>")
        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка: {e}")

    async def cmd_closeall(self, args, reply_chat_id: str):
        """
        /closeall — закрыть ВСЕ открытые позиции на BingX (рыночными ордерами).
        Используй когда бот показывает "Max positions" но позиции уже устарели.
        """
        try:
            # Проверяем есть ли auto_trader с bingx клиентом
            if not self.state or not hasattr(self.state, 'auto_trader') or not self.state.auto_trader:
                await self._reply(reply_chat_id, 
                    "❌ AutoTrader не инициализирован.\n"
                    "Убедись что BINGX_API_KEY и BINGX_API_SECRET настроены.")
                return
            
            bingx = self.state.auto_trader.bingx
            
            # Получаем все открытые позиции
            positions = await bingx.get_positions()
            
            if not positions:
                await self._reply(reply_chat_id, 
                    "✅ Нет открытых позиций на BingX.")
                return
            
            closed = 0
            failed = 0
            total_pnl = 0.0
            
            for pos in positions:
                try:
                    symbol = pos.symbol
                    side = "SELL" if pos.side == "LONG" else "BUY"
                    qty = abs(pos.size)
                    
                    # Рыночный ордер на закрытие
                    await bingx.place_market_order(
                        symbol=symbol,
                        side=side,
                        quantity=qty,
                        position_side=pos.side
                    )
                    
                    closed += 1
                    total_pnl += pos.unrealized_pnl
                    print(f"Closed position: {symbol} {pos.side} | PnL: {pos.unrealized_pnl:+.2f}")
                    
                except Exception as e:
                    failed += 1
                    print(f"Failed to close {pos.symbol}: {e}")
            
            # Сбрасываем счётчик позиций
            if self.state:
                self.state.active_signals = 0
            
            # Очищаем Redis сигналы
            pattern = f"{self.bot_type}:signals:*"
            keys = self.redis.client.keys(pattern)
            for key in keys:
                self.redis.client.delete(key)
            
            emoji = "✅" if closed > 0 else "⚠️"
            mode = "DEMO" if bingx.demo else "REAL"
            
            await self._reply(reply_chat_id,
                f"{emoji} <b>Закрыто {closed} позиций ({mode})</b>\n\n"
                f"💰 Total P&L: <code>{total_pnl:+.2f} USDT</code>\n"
                f"❌ Failed: {failed}\n\n"
                f"🤖 Бот снова может открывать новые позиции.\n"
                f"Используй /scan для немедленного скана.")
                
        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка закрытия позиций: {e}")

    async def cmd_balance(self, args, reply_chat_id: str):
        """/balance — баланс BingX аккаунта."""
        try:
            auto_trader = getattr(self.state, "auto_trader", None) if self.state else None
            if not auto_trader:
                await self._reply(reply_chat_id,
                    "❌ AutoTrader не подключён\n"
                    "Установи <code>AUTO_TRADING_ENABLED=true</code> в Render → Environment")
                return

            summary = await auto_trader.get_account_summary()
            balance = summary.get("balance", {})
            avail   = float(balance.get("availableMargin", 0))
            equity  = float(balance.get("equity", avail))
            pnl     = summary.get("unrealized_pnl", 0)
            mode    = summary.get("mode", "DEMO")
            n_pos   = summary.get("open_positions", 0)

            mode_str = "🟡 DEMO (торговля не реальная)" if mode == "DEMO" else "🟢 REAL"

            await self._reply(reply_chat_id,
                f"💰 <b>BingX Аккаунт</b>  [{mode_str}]\n\n"
                f"Equity:     <b>${equity:,.2f}</b>\n"
                f"Свободно:  <b>${avail:,.2f}</b>\n"
                f"Unrealized PnL: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USDT</b>\n\n"
                f"Открытых позиций: {n_pos}\n"
                f"Сделок сегодня: {summary.get('daily_trades', 0)}\n"
                f"P&L сегодня: {summary.get('daily_pnl', 0):.2f}%\n"
                f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
            )
        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка получения баланса: {e}")

    async def cmd_positions(self, args, reply_chat_id: str):
        """/positions — открытые позиции на BingX."""
        try:
            auto_trader = getattr(self.state, "auto_trader", None) if self.state else None
            if not auto_trader:
                await self._reply(reply_chat_id, "❌ AutoTrader не подключён")
                return

            positions = await auto_trader.bingx.get_positions()
            if not positions:
                await self._reply(reply_chat_id, "📭 Нет открытых позиций на BingX")
                return

            msg = f"📊 <b>Открытые позиции BingX ({len(positions)}):</b>\n\n"
            for p in positions:
                d_emoji = "🟢" if p.side == "LONG" else "🔴"
                pnl_str = f"+{p.unrealized_pnl:.2f}" if p.unrealized_pnl >= 0 else f"{p.unrealized_pnl:.2f}"
                msg += (
                    f"{d_emoji} <code>{p.symbol}</code>  {p.side}\n"
                    f"   Вход: <b>${p.entry_price:,.4f}</b>  |  Плечо: {p.leverage}x\n"
                    f"   Размер: {p.size:.4f}  |  PnL: <b>{pnl_str} USDT</b>\n\n"
                )

            await self._reply(reply_chat_id, msg)
        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка: {e}")

    async def cmd_emergency_stop(self, args, reply_chat_id: str):
        """
        /emergency_stop — экстренная остановка всех новых сигналов.
        Алиас для /pause с более чётким сообщением.
        """
        try:
            if not self.state:
                await self._reply(reply_chat_id, "❌ Бот не инициализирован")
                return

            self.state.is_paused = True

            bot_emoji = "🔴" if self.bot_type == "short" else "🟢"
            bot_name  = "SHORT" if self.bot_type == "short" else "LONG"

            await self._reply(reply_chat_id,
                f"🛑 <b>ЭКСТРЕННАЯ ОСТАНОВКА {bot_emoji} {bot_name} Bot</b>\n\n"
                "⏸️ Новые сигналы ОСТАНОВЛЕНЫ\n"
                "📊 Позиции продолжают отслеживаться\n"
                "🔄 Сканирование приостановлено\n\n"
                "Используй /resume для возобновления")

        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка: {e}")

    async def cmd_reset_stats(self, args, reply_chat_id: str):
        """
        /reset_stats — сбросить статистику (daily_trades, daily_pnl).
        """
        try:
            if not self.state or not hasattr(self.state, 'auto_trader') or not self.state.auto_trader:
                await self._reply(reply_chat_id, "❌ AutoTrader не инициализирован")
                return

            # Сбрасываем статистику
            self.state.auto_trader.daily_trades = 0
            self.state.auto_trader.daily_pnl = 0.0
            self.state.auto_trader.total_pnl = 0.0
            self.state.auto_trader.win_count = 0
            self.state.auto_trader.loss_count = 0

            # Сбрасываем в Redis
            if self.redis:
                self.redis.client.delete(f"{self.bot_type}:daily_trades")
                self.redis.client.delete(f"{self.bot_type}:daily_pnl")

            await self._reply(reply_chat_id,
                "🔄 <b>Статистика сброшена</b>\n\n"
                "📊 Daily trades: 0\n"
                "📈 Daily P&L: 0.00%\n"
                "🎯 Win/Loss: 0/0\n\n"
                "Счётчики обнулены ✅")

        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка сброса: {e}")

    async def cmd_cleanup(self, args, reply_chat_id: str):
        """
        /cleanup — удалить зависшие сделки из Redis.
        Очистка signals и проверка синхронизации с биржей.
        """
        try:
            if not self.state:
                await self._reply(reply_chat_id, "❌ Бот не инициализирован")
                return

            # Получаем реальные позиции с биржи
            real_positions = []
            if self.state.auto_trader:
                real_positions = await self.state.auto_trader.bingx.get_positions()
            real_symbols = {p.symbol for p in real_positions}

            # Чистим Redis
            cleaned = 0
            pattern = f"{self.bot_type}:signals:*"
            keys = self.redis.client.keys(pattern) if self.redis else []

            for key in keys:
                try:
                    # Проверяем есть ли эта позиция на бирже
                    symbol = key.decode().split(":")[-1] if isinstance(key, bytes) else key.split(":")[-1]
                    if symbol not in real_symbols:
                        self.redis.client.delete(key)
                        cleaned += 1
                except:
                    pass

            # Сбрасываем счётчик
            self.state.active_signals = len(real_positions)

            await self._reply(reply_chat_id,
                f"🧹 <b>Cleanup завершён</b>\n\n"
                f"📊 Реальных позиций на бирже: {len(real_positions)}\n"
                f"🗑 Очищено записей из Redis: {cleaned}\n"
                f"✅ Счётчик активных сигналов: {self.state.active_signals}\n\n"
                "Бот синхронизирован с биржей ✅")

        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка cleanup: {e}")

    async def cmd_clean(self, args, reply_chat_id: str):
        """
        /clean — полная очистка: trades + positions + Redis.
        Комбинация /reset_stats + /cleanup + /clearpos.
        """
        try:
            if not self.state:
                await self._reply(reply_chat_id, "❌ Бот не инициализирован")
                return

            # 1. Сброс статистики
            if self.state.auto_trader:
                self.state.auto_trader.daily_trades = 0
                self.state.auto_trader.daily_pnl = 0.0

            # 2. Очистка Redis signals
            pattern = f"{self.bot_type}:signals:*"
            keys = self.redis.client.keys(pattern) if self.redis else []
            cleaned_signals = len(keys)
            for key in keys:
                self.redis.client.delete(key)

            # 3. Очистка других ключей
            if self.redis:
                self.redis.client.delete(f"{self.bot_type}:daily_trades")
                self.redis.client.delete(f"{self.bot_type}:daily_pnl")
                self.redis.client.delete(f"{self.bot_type}:last_scan")

            # 4. Сброс счётчиков
            self.state.active_signals = 0
            self.state.is_paused = False

            await self._reply(reply_chat_id,
                "🧼 <b>Полная очистка завершена</b>\n\n"
                f"🗑 Очищено сигналов: {cleaned_signals}\n"
                "📊 Статистика сброшена\n"
                "⏸️ Пауза снята\n"
                "🔄 Сканирование активно\n\n"
                "Бот готов к работе! ✅")

        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка очистки: {e}")

    async def cmd_logs(self, args, reply_chat_id: str):
        """
        /logs — показать последние строки лога (для Render).
        """
        try:
            import subprocess
            import os

            # Пытаемся получить логи (работает если есть доступ к файловой системе)
            lines = 20
            if args and args[0].isdigit():
                lines = min(int(args[0]), 50)

            # Читаем из файла лога если он есть
            log_lines = []
            log_files = ["/var/log/render.log", "/app/logs/app.log", "app.log", "bot.log"]

            for log_file in log_files:
                if os.path.exists(log_file):
                    try:
                        with open(log_file, 'r') as f:
                            log_lines = f.readlines()[-lines:]
                        break
                    except:
                        pass

            if not log_lines:
                # Если нет файла — показываем информацию о боте
                uptime = getattr(self.state, 'start_time', None) if self.state else None
                uptime_str = "N/A"
                if uptime:
                    from datetime import datetime
                    delta = datetime.utcnow() - uptime
                    uptime_str = f"{delta.days}d {delta.seconds//3600}h"

                await self._reply(reply_chat_id,
                    f"📋 <b>Статус бота</b>\n\n"
                    f"🤖 Тип: {self.bot_type.upper()}\n"
                    f"⏸️ Пауза: {'Да' if self.state and getattr(self.state, 'is_paused', False) else 'Нет'}\n"
                    f"📊 Активных сигналов: {getattr(self.state, 'active_signals', 0)}\n"
                    f"🔄 AutoTrader: {'✅' if self.state and getattr(self.state, 'auto_trader', None) else '❌'}\n\n"
                    "💡 Подробные логи в Render Dashboard → Logs")
                return

            # Формируем сообщение с логами
            msg = f"📜 <b>Последние {len(log_lines)} строк лога:</b>\n\n<code>"
            for line in log_lines:
                msg += line.replace('<', '&lt;').replace('>', '&gt;')[:200] + "\n"
            msg += "</code>"

            await self._reply(reply_chat_id, msg)

        except Exception as e:
            await self._reply(reply_chat_id, f"❌ Ошибка получения логов: {e}")


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
