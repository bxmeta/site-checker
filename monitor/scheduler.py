"""
Модуль планировщика проверок сайтов.
"""
import asyncio
import logging
from typing import Callable, Awaitable, Optional

from .config_loader import Config
from .state_manager import StateManager
from .retry_logic import check_with_retry
from .notifier import TelegramNotifier
from .logger import log_check_result

logger = logging.getLogger("site_monitor")


class MonitorScheduler:
    """Планировщик мониторинга сайтов."""

    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        notifier: TelegramNotifier
    ):
        """
        Инициализирует планировщик.

        Args:
            config: Конфигурация приложения
            state_manager: Менеджер состояния
            notifier: Telegram-нотификатор
        """
        self.config = config
        self.state_manager = state_manager
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

    async def _check_site(self, site) -> None:
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
            status_changed = self.state_manager.update_on_success(site.id)
            if status_changed:
                await self.notifier.notify_site_up(site, result)
        else:
            status_changed = self.state_manager.update_on_failure(
                site.id,
                self.config.default.retry_count
            )
            if status_changed:
                await self.notifier.notify_site_down(site, result)

    async def _run_loop(self) -> None:
        """Основной цикл планировщика."""
        interval_seconds = self.config.scheduler.interval_minutes * 60

        while self._running:
            try:
                await self.check_all_sites()
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки: {e}")

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
    state_manager: StateManager,
    notifier: TelegramNotifier
) -> str:
    """
    Выполняет немедленную проверку всех сайтов и возвращает отчёт.

    Args:
        config: Конфигурация приложения
        state_manager: Менеджер состояния
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
            status_changed = state_manager.update_on_success(site.id)
            if status_changed:
                await notifier.notify_site_up(site, result)
        else:
            status_changed = state_manager.update_on_failure(
                site.id,
                config.default.retry_count
            )
            if status_changed:
                await notifier.notify_site_down(site, result)

    return "\n".join(results)
