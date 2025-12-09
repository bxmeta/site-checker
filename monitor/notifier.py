"""
Модуль отправки уведомлений в Telegram.
"""
import logging
from typing import List, Optional, Tuple

import aiohttp
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from .config_loader import SiteConfig, TelegramConfig
from .checker import CheckResult
from .time_utils import format_for_message

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

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None
    ) -> bool:
        """
        Отправляет сообщение в Telegram.

        Args:
            chat_id: ID чата
            text: Текст сообщения
            reply_markup: Inline-клавиатура (опционально)

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
            payload["reply_markup"] = reply_markup.model_dump(exclude_none=True)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка отправки в Telegram: {error_text}")
                        return False
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False

    def _create_down_keyboard(self, site_id: str) -> InlineKeyboardMarkup:
        """Создаёт клавиатуру для уведомления о падении."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔇 Заглушить",
                    callback_data=f"mute_site:{site_id}"
                ),
                InlineKeyboardButton(
                    text="🔄 Проверить",
                    callback_data=f"check_now:{site_id}"
                )
            ]
        ])

    def _create_unmute_keyboard(self, site_id: str) -> InlineKeyboardMarkup:
        """Создаёт клавиатуру для подтверждения заглушки."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔔 Включить обратно",
                    callback_data=f"unmute_site:{site_id}"
                )
            ]
        ])

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
        """Форматирует сообщение о падении сайта."""
        status_code_str = str(check_result.status_code) if check_result.status_code else "N/A"

        # Разные заголовки для разных типов ошибок
        error_type = check_result.error_type or "unknown"

        if error_type == "keyword_missing":
            title = "⚠️ <b>Контент изменился</b>"
            description = "Ключевое слово не найдено на странице"
        elif error_type == "wrong_code":
            title = "⚠️ <b>Неверный код ответа</b>"
            description = f"Ожидался {site.expected_code}, получен {status_code_str}"
        elif error_type in ("ssl_expired", "ssl_mismatch"):
            title = "🔐 <b>Проблема с SSL</b>"
            description = check_result.error
        elif error_type == "timeout":
            title = "🚨 <b>Сайт недоступен</b>"
            description = "Превышено время ожидания"
        else:
            title = "🚨 <b>Сайт недоступен</b>"
            description = check_result.error

        return (
            f"{title}\n\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Поддержка: {site.support_level}\n"
            f"Проблема: {description}\n"
            f"Код: {status_code_str}\n"
            f"Время: {format_for_message()}"
        )

    def _format_up_message(
        self,
        site: SiteConfig,
        check_result: CheckResult,
        downtime_seconds: Optional[int] = None
    ) -> str:
        """Форматирует сообщение о восстановлении сайта."""
        status_code_str = str(check_result.status_code) if check_result.status_code else "N/A"

        lines = [
            "✅ <b>Сайт восстановлен</b>\n",
            f"Название: {site.name}",
            f"URL: {site.url}",
            f"Статус поддержки: {site.support_level}",
            f"Код: {status_code_str}",
        ]

        if downtime_seconds is not None and downtime_seconds > 0:
            lines.append(f"Простой: {format_duration(downtime_seconds)}")

        lines.append(f"Время: {format_for_message()}")

        return "\n".join(lines)

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
