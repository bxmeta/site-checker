"""
Модуль отправки уведомлений в Telegram.
"""
import logging
from typing import List, Optional, Tuple

import aiohttp

from .config_loader import SiteConfig, TelegramConfig
from .checker import CheckResult
from .ssl_checker import SSLCheckResult
from .time_utils import format_for_message
from .messages import (
    format_down_message, format_ssl_expiring_message, format_ssl_auto_renew_failed_message, format_up_message,
)


def _html_bold(text: str) -> str:
    return f"<b>{text}</b>"

logger = logging.getLogger("site_monitor")


def format_duration(seconds: int) -> str:
    """Форматирует длительность в человекочитаемый вид."""
    if seconds < 60:
        return f"{seconds} сек"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} мин"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes > 0:
            return f"{hours} ч {minutes} мин"
        return f"{hours} ч"


class TelegramNotifier:
    """Класс для отправки уведомлений в Telegram."""

    def __init__(self, config: TelegramConfig):
        """
        Инициализирует Telegram-нотификатор.

        Args:
            config: Конфигурация Telegram
        """
        self.bot_token = config.bot_token
        self.admin_ids = config.admin_ids
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.proxy = config.proxy or None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[dict] = None
    ) -> bool:
        """
        Отправляет сообщение в Telegram.

        Args:
            chat_id: ID чата
            text: Текст сообщения
            reply_markup: Inline-клавиатура в формате Bot API (dict) или aiogram InlineKeyboardMarkup

        Returns:
            True, если сообщение отправлено успешно
        """
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }

        if reply_markup:
            if hasattr(reply_markup, "model_dump"):
                reply_markup = reply_markup.model_dump(exclude_none=True)
            payload["reply_markup"] = reply_markup

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, proxy=self.proxy) as response:
                    if response.status == 200:
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка отправки в Telegram: {error_text}")
                        return False
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False

    # Клавиатуры собираются как dict Bot API, а не aiogram-модели: модуль не тянет aiogram
    # (его импорт стоит ~150 МБ памяти), а нотификатор работает и без Telegram-бота.

    def _create_down_keyboard(self, site_id: str) -> dict:
        """Создаёт клавиатуру для уведомления о падении."""
        return {"inline_keyboard": [[
            {"text": "🔇 Заглушить", "callback_data": f"mute_site:{site_id}"},
            {"text": "🔄 Проверить", "callback_data": f"check_now:{site_id}"},
            {"text": "📄 Карточка", "callback_data": f"site:{site_id}"},
        ]]}

    def _create_unmute_keyboard(self, site_id: str) -> dict:
        """Создаёт клавиатуру для подтверждения заглушки."""
        return {"inline_keyboard": [[
            {"text": "🔔 Включить обратно", "callback_data": f"unmute_site:{site_id}"},
        ]]}

    def _get_all_recipients(self, site: SiteConfig, user_ids: Optional[List[int]] = None) -> List[int]:
        """Возвращает список всех получателей: подписчики + админы (без дубликатов)."""
        base_users = user_ids if user_ids is not None else site.notify_users
        all_recipients = set(base_users)
        all_recipients.update(self.admin_ids)
        return list(all_recipients)

    async def notify_site_down(
        self,
        site: SiteConfig,
        check_result: CheckResult,
        user_ids: Optional[List[int]] = None
    ) -> None:
        """
        Отправляет уведомление о падении сайта.

        Args:
            site: Конфигурация сайта
            check_result: Результат проверки
            user_ids: Список ID пользователей для уведомления
        """
        recipients = self._get_all_recipients(site, user_ids)
        message = self._format_down_message(site, check_result)
        keyboard = self._create_down_keyboard(site.id)

        for user_id in recipients:
            success = await self.send_message(user_id, message, keyboard)
            if success:
                logger.info(f"[{site.id}] Уведомление о падении отправлено пользователю {user_id}")
            else:
                logger.error(f"[{site.id}] Не удалось отправить уведомление пользователю {user_id}")

    async def notify_site_up(
        self,
        site: SiteConfig,
        check_result: CheckResult,
        user_ids: Optional[List[int]] = None,
        downtime_seconds: Optional[int] = None
    ) -> None:
        """
        Отправляет уведомление о восстановлении сайта.

        Args:
            site: Конфигурация сайта
            check_result: Результат проверки
            user_ids: Список ID пользователей для уведомления
            downtime_seconds: Время простоя в секундах
        """
        recipients = self._get_all_recipients(site, user_ids)
        message = self._format_up_message(site, check_result, downtime_seconds)

        for user_id in recipients:
            success = await self.send_message(user_id, message)
            if success:
                logger.info(f"[{site.id}] Уведомление о восстановлении отправлено пользователю {user_id}")
            else:
                logger.error(f"[{site.id}] Не удалось отправить уведомление пользователю {user_id}")

    async def notify_ssl_expiring(
        self,
        site: SiteConfig,
        ssl_result: SSLCheckResult,
        user_ids: Optional[List[int]] = None
    ) -> None:
        """Предупреждает об истечении платного SSL-сертификата."""
        recipients = self._get_all_recipients(site, user_ids)
        if ssl_result.auto_renew:
            message = format_ssl_auto_renew_failed_message(site, ssl_result, _html_bold)
        else:
            message = format_ssl_expiring_message(site, ssl_result, _html_bold)
        for user_id in recipients:
            success = await self.send_message(user_id, message)
            if success:
                logger.info(f"[{site.id}] Предупреждение об истечении SSL отправлено пользователю {user_id}")
            else:
                logger.error(f"[{site.id}] Не удалось отправить предупреждение SSL пользователю {user_id}")

    async def send_reminder(
        self,
        site: SiteConfig,
        reminder_number: int,
        downtime_seconds: int,
        next_interval_minutes: int,
        user_ids: List[int],
        muted_users: List[int]
    ) -> None:
        """
        Отправляет напоминание о недоступности сайта.

        Args:
            site: Конфигурация сайта
            reminder_number: Номер напоминания
            downtime_seconds: Время простоя в секундах
            next_interval_minutes: Интервал до следующего напоминания в минутах
            user_ids: Список ID пользователей для уведомления
            muted_users: Список пользователей с заглушенными напоминаниями
        """
        message = self._format_reminder_message(
            site, reminder_number, downtime_seconds, next_interval_minutes
        )
        keyboard = self._create_down_keyboard(site.id)

        # Объединяем подписчиков и админов
        all_recipients = set(user_ids)
        all_recipients.update(self.admin_ids)

        for user_id in all_recipients:
            if user_id in muted_users:
                logger.debug(f"[{site.id}] Пропуск напоминания для {user_id} (заглушено)")
                continue

            success = await self.send_message(user_id, message, keyboard)
            if success:
                logger.info(f"[{site.id}] Напоминание #{reminder_number} отправлено пользователю {user_id}")
            else:
                logger.error(f"[{site.id}] Не удалось отправить напоминание пользователю {user_id}")

    def _format_down_message(self, site: SiteConfig, check_result: CheckResult) -> str:
        """Форматирует сообщение о падении сайта (общий текст с диагностикой)."""
        return format_down_message(site, check_result, _html_bold)

    def _format_up_message(
        self,
        site: SiteConfig,
        check_result: CheckResult,
        downtime_seconds: Optional[int] = None
    ) -> str:
        """Форматирует сообщение о восстановлении сайта (общий текст)."""
        return format_up_message(site, check_result, _html_bold, downtime_seconds)

    def _format_reminder_message(
        self,
        site: SiteConfig,
        reminder_number: int,
        downtime_seconds: int,
        next_interval_minutes: int
    ) -> str:
        """Форматирует сообщение-напоминание."""
        return (
            f"🔔 <b>Напоминание #{reminder_number}</b>\n\n"
            f"Сайт {site.name} всё ещё недоступен\n"
            f"URL: {site.url}\n"
            f"Время простоя: {format_duration(downtime_seconds)}\n"
            f"Следующее напоминание через {next_interval_minutes} мин"
        )

    async def notify_admins(self, message: str) -> None:
        """
        Отправляет сообщение всем администраторам.

        Args:
            message: Текст сообщения
        """
        for admin_id in self.admin_ids:
            await self.send_message(admin_id, message)

    async def send_mute_confirmation(
        self,
        chat_id: int,
        site_name: str,
        site_id: str
    ) -> bool:
        """
        Отправляет подтверждение о заглушке.

        Args:
            chat_id: ID чата
            site_name: Название сайта
            site_id: ID сайта

        Returns:
            True если успешно
        """
        message = (
            f"🔇 <b>Напоминания отключены</b>\n\n"
            f"Сайт: {site_name}\n"
            f"Вы не будете получать напоминания до восстановления сайта."
        )
        keyboard = self._create_unmute_keyboard(site_id)
        return await self.send_message(chat_id, message, keyboard)

    async def send_unmute_confirmation(
        self,
        chat_id: int,
        site_name: str
    ) -> bool:
        """
        Отправляет подтверждение о включении напоминаний.

        Args:
            chat_id: ID чата
            site_name: Название сайта

        Returns:
            True если успешно
        """
        message = (
            f"🔔 <b>Напоминания включены</b>\n\n"
            f"Сайт: {site_name}\n"
            f"Вы снова будете получать напоминания о недоступности."
        )
        return await self.send_message(chat_id, message)
