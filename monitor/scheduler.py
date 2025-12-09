"""
Модуль планировщика проверок сайтов.
"""
import asyncio
import logging
from typing import Optional

from .config_loader import Config, SiteConfig
from .database import Database
from .retry_logic import check_with_retry
from .notifier import TelegramNotifier
from .logger import log_check_result

logger = logging.getLogger("site_monitor")


class MonitorScheduler:
    """Планировщик мониторинга сайтов."""

    def __init__(
        self,
        config: Config,
        database: Database,
        notifier: TelegramNotifier
    ):
        """
        Инициализирует планировщик.

        Args:
            config: Конфигурация приложения
            database: База данных
            notifier: Telegram-нотификатор
        """
        self.config = config
        self.database = database
        self.notifier = notifier
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def check_all_sites(self) -> None:
        """Выполняет проверку всех сайтов."""
        logger.info("Запуск проверки всех сайтов")

        for site in self.config.sites:
            try:
                await self._check_site(site)
            except Exception as e:
                logger.error(f"[{site.id}] Ошибка при проверке: {e}")

        logger.info("Проверка всех сайтов завершена")

    async def _check_site(self, site: SiteConfig) -> None:
        """
        Выполняет проверку одного сайта.

        Args:
            site: Конфигурация сайта
        """
        result = await check_with_retry(site, self.config.default)

        log_check_result(
            logger,
            site.id,
            result.success,
            result.status_code,
            result.response_time_ms,
            result.error
        )

        if result.success:
            status_changed, downtime_seconds = self.database.update_on_success(site.id)
            if status_changed:
                await self.notifier.notify_site_up(
                    site, result,
                    downtime_seconds=downtime_seconds
                )
        else:
            status_changed = self.database.update_on_failure(
                site.id,
                self.config.default.retry_count,
                error_type=result.error_type,
                error_message=result.error
            )
            if status_changed:
                await self.notifier.notify_site_down(site, result)

    async def check_single_site(self, site_id: str) -> Optional[str]:
        """
        Выполняет проверку одного сайта по ID.

        Args:
            site_id: Идентификатор сайта

        Returns:
            Результат проверки или None если сайт не найден
        """
        site = next((s for s in self.config.sites if s.id == site_id), None)
        if not site:
            return None

        result = await check_with_retry(site, self.config.default)

        log_check_result(
            logger,
            site.id,
            result.success,
            result.status_code,
            result.response_time_ms,
            result.error
        )

        if result.success:
            status_changed, downtime_seconds = self.database.update_on_success(site.id)
            if status_changed:
                await self.notifier.notify_site_up(
                    site, result,
                    downtime_seconds=downtime_seconds
                )
            status = "✅"
        else:
            status_changed = self.database.update_on_failure(
                site.id,
                self.config.default.retry_count,
                error_type=result.error_type,
                error_message=result.error
            )
            if status_changed:
                await self.notifier.notify_site_down(site, result)
            status = "❌"

        code_str = f" ({result.status_code})" if result.status_code else ""
        error_str = f" - {result.error}" if result.error else ""
        return f"{status} {site.name}{code_str}{error_str}"

    async def _check_pending_reminders(self) -> None:
        """Проверяет и отправляет просроченные напоминания."""
        sites_needing_reminder = self.database.get_sites_needing_reminder()

        for site_id, reminder_count in sites_needing_reminder:
            site = next((s for s in self.config.sites if s.id == site_id), None)
            if not site:
                logger.warning(f"Сайт {site_id} не найден в конфигурации")
                continue

            try:
                # Получаем время простоя
                downtime_seconds = self.database.get_downtime_seconds(site_id)

                # Получаем список заглушенных пользователей
                muted_users = self.database.get_muted_users(site_id)

                # Отмечаем напоминание как отправленное и получаем следующий интервал
                next_interval = self.database.mark_reminder_sent(site_id)

                # Номер напоминания (начинаем с 1)
                reminder_number = reminder_count + 1

                # Отправляем напоминание
                await self.notifier.send_reminder(
                    site=site,
                    reminder_number=reminder_number,
                    downtime_seconds=downtime_seconds,
                    next_interval_minutes=next_interval,
                    user_ids=site.notify_users,
                    muted_users=muted_users
                )

                logger.info(
                    f"[{site_id}] Напоминание #{reminder_number} отправлено. "
                    f"Следующее через {next_interval} мин."
                )

            except Exception as e:
                logger.error(f"[{site_id}] Ошибка отправки напоминания: {e}")

    async def _run_loop(self) -> None:
        """Основной цикл планировщика."""
        interval_seconds = self.config.scheduler.interval_minutes * 60

        while self._running:
            try:
                await self.check_all_sites()
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки: {e}")

            # Проверяем напоминания после проверки сайтов
            try:
                await self._check_pending_reminders()
            except Exception as e:
                logger.error(f"Ошибка в цикле напоминаний: {e}")

            await asyncio.sleep(interval_seconds)

    def start(self) -> asyncio.Task:
        """
        Запускает планировщик.

        Returns:
            asyncio.Task с основным циклом
        """
        if self._running:
            logger.warning("Планировщик уже запущен")
            return self._task

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Планировщик запущен. Интервал: {self.config.scheduler.interval_minutes} мин."
        )
        return self._task

    def stop(self) -> None:
        """Останавливает планировщик."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Планировщик остановлен")

    @property
    def is_running(self) -> bool:
        """Возвращает True, если планировщик запущен."""
        return self._running


async def run_immediate_check(
    config: Config,
    database: Database,
    notifier: TelegramNotifier
) -> str:
    """
    Выполняет немедленную проверку всех сайтов и возвращает отчёт.

    Args:
        config: Конфигурация приложения
        database: База данных
        notifier: Telegram-нотификатор

    Returns:
        Строка с отчётом о проверке
    """
    results = []
    for site in config.sites:
        result = await check_with_retry(site, config.default)

        status = "✅" if result.success else "❌"
        code_str = f" ({result.status_code})" if result.status_code else ""
        error_str = f" - {result.error}" if result.error else ""

        results.append(f"{status} {site.name}{code_str}{error_str}")

        log_check_result(
            logger,
            site.id,
            result.success,
            result.status_code,
            result.response_time_ms,
            result.error
        )

        if result.success:
            status_changed, downtime_seconds = database.update_on_success(site.id)
            if status_changed:
                await notifier.notify_site_up(
                    site, result,
                    downtime_seconds=downtime_seconds
                )
        else:
            status_changed = database.update_on_failure(
                site.id,
                config.default.retry_count,
                error_type=result.error_type,
                error_message=result.error
            )
            if status_changed:
                await notifier.notify_site_down(site, result)

    return "\n".join(results)
