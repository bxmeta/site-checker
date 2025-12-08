"""
Модуль отправки уведомлений в Telegram.
"""
import logging
from typing import List, Optional

import aiohttp

from .config_loader import SiteConfig, TelegramConfig
from .checker import CheckResult
from .time_utils import format_for_message

logger = logging.getLogger("site_monitor")


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

    async def send_message(self, chat_id: int, text: str) -> bool:
        """
        Отправляет сообщение в Telegram.

        Args:
            chat_id: ID чата
            text: Текст сообщения

        Returns:
            True, если сообщение отправлено успешно
        """
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }

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
            user_ids: Список ID пользователей для уведомления (если None, используются notify_users сайта)
        """
        recipients = user_ids or site.notify_users

        message = self._format_down_message(site, check_result)

        for user_id in recipients:
            success = await self.send_message(user_id, message)
            if success:
                logger.info(f"[{site.id}] Уведомление о падении отправлено пользователю {user_id}")
            else:
                logger.error(f"[{site.id}] Не удалось отправить уведомление пользователю {user_id}")

    async def notify_site_up(
        self,
        site: SiteConfig,
        check_result: CheckResult,
        user_ids: Optional[List[int]] = None
    ) -> None:
        """
        Отправляет уведомление о восстановлении сайта.

        Args:
            site: Конфигурация сайта
            check_result: Результат проверки
            user_ids: Список ID пользователей для уведомления (если None, используются notify_users сайта)
        """
        recipients = user_ids or site.notify_users

        message = self._format_up_message(site, check_result)

        for user_id in recipients:
            success = await self.send_message(user_id, message)
            if success:
                logger.info(f"[{site.id}] Уведомление о восстановлении отправлено пользователю {user_id}")
            else:
                logger.error(f"[{site.id}] Не удалось отправить уведомление пользователю {user_id}")

    def _format_down_message(self, site: SiteConfig, check_result: CheckResult) -> str:
        """Форматирует сообщение о падении сайта."""
        status_code_str = str(check_result.status_code) if check_result.status_code else "N/A"

        return (
            f"🚨 <b>Сайт недоступен</b>\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Статус поддержки: {site.support_level}\n"
            f"Ошибка: {check_result.error}\n"
            f"Код: {status_code_str}\n"
            f"Время: {format_for_message()}"
        )

    def _format_up_message(self, site: SiteConfig, check_result: CheckResult) -> str:
        """Форматирует сообщение о восстановлении сайта."""
        status_code_str = str(check_result.status_code) if check_result.status_code else "N/A"

        return (
            f"✅ <b>Сайт восстановлен</b>\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Статус поддержки: {site.support_level}\n"
            f"Код: {status_code_str}\n"
            f"Время: {format_for_message()}"
        )

    async def notify_admins(self, message: str) -> None:
        """
        Отправляет сообщение всем администраторам.

        Args:
            message: Текст сообщения
        """
        for admin_id in self.admin_ids:
            await self.send_message(admin_id, message)
