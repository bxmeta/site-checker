"""
Модуль планировщика проверок сайтов.

Сайты проверяются параллельно (scheduler.concurrency одновременно): ожидание
между повторными попытками одного сайта не задерживает проверку остальных.
"""
import asyncio
import logging
from typing import List, Optional

from .checker import CheckResult
from .config_loader import Config, SiteConfig
from .database import Database
from .diagnostics import diagnose
from .logger import log_check_result
from .messages import format_check_line
from .retry_logic import check_with_retry

logger = logging.getLogger("site_monitor")


class MonitorScheduler:
    """Планировщик мониторинга сайтов."""

    def __init__(self, config: Config, database: Database, notifier):
        """
        Args:
            config: Конфигурация приложения
            database: База данных
            notifier: Нотификатор (MultiNotifier: Telegram + Bitrix24)
        """
        self.config = config
        self.database = database
        self.notifier = notifier
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._semaphore = asyncio.Semaphore(max(1, config.scheduler.concurrency))

    # ==================== Обработка результата ====================

    async def attach_diagnostics(self, site: SiteConfig, result: CheckResult) -> None:
        """Дополняет результат пошаговой диагностикой (DNS, ping, TCP, TLS, HTTP)."""
        try:
            result.diagnostics = await diagnose(site.url, self.config.default.timeout_seconds)
            logger.info(f"[{site.id}] Диагностика: " + " | ".join(result.diagnostics))
        except Exception as e:
            logger.error(f"[{site.id}] Ошибка диагностики: {e}")

    async def _handle_result(self, site: SiteConfig, result: CheckResult) -> None:
        """Записывает результат в БД, шлёт уведомления о смене статуса и SSL."""
        log_check_result(
            logger, site.id, result.success,
            result.status_code, result.response_time_ms, result.error
        )

        if result.success:
            status_changed, downtime_seconds = self.database.update_on_success(site.id)
            if status_changed:
                await self.notifier.notify_site_up(site, result, downtime_seconds=downtime_seconds)
            await self._check_ssl_expiry(site, result)
        else:
            status_changed = self.database.update_on_failure(
                site.id,
                self.config.default.retry_count,
                error_type=result.error_type,
                error_message=result.error
            )
            if status_changed:
                await self.attach_diagnostics(site, result)
                await self.notifier.notify_site_down(site, result)

    async def _check_ssl_expiry(self, site: SiteConfig, result: CheckResult) -> None:
        """
        Предупреждает об истечении платного сертификата за ssl.expiry_warn_days.
        Для издателей с автопродлением (Let's Encrypt и т.п.) не предупреждает.
        """
        ssl_result = result.ssl
        if not ssl_result or not ssl_result.valid or ssl_result.days_until_expiry is None:
            return
        if ssl_result.auto_renew:
            return

        ssl_cfg = self.config.ssl
        if ssl_result.days_until_expiry > ssl_cfg.expiry_warn_days:
            return

        expiry_key = ssl_result.expiry_date.isoformat()
        if not self.database.should_warn_ssl_expiry(site.id, expiry_key, ssl_cfg.remind_every_hours):
            return

        try:
            await self.notifier.notify_ssl_expiring(site, ssl_result)
            self.database.mark_ssl_warned(site.id, expiry_key)
            logger.info(
                f"[{site.id}] Предупреждение: SSL ({ssl_result.issuer_str}) истекает "
                f"{ssl_result.expiry_str}, осталось {ssl_result.days_until_expiry} дн."
            )
        except Exception as e:
            logger.error(f"[{site.id}] Ошибка отправки предупреждения SSL: {e}")

    # ==================== Проверки ====================

    async def _check_site(self, site: SiteConfig) -> CheckResult:
        """Проверяет один сайт (с повторами) под семафором и обрабатывает результат."""
        async with self._semaphore:
            result = await check_with_retry(site, self.config.default, self.config.ssl)
        await self._handle_result(site, result)
        return result

    async def _check_sites(self, sites: List[SiteConfig]) -> List[Optional[CheckResult]]:
        """Параллельно проверяет список сайтов, ошибки одного не ломают остальные."""
        results = await asyncio.gather(
            *(self._check_site(site) for site in sites), return_exceptions=True
        )
        out: List[Optional[CheckResult]] = []
        for site, res in zip(sites, results):
            if isinstance(res, BaseException):
                logger.error(f"[{site.id}] Ошибка при проверке: {type(res).__name__}: {res}")
                out.append(None)
            else:
                out.append(res)
        return out

    async def check_all_sites(self) -> None:
        """Выполняет проверку всех сайтов."""
        logger.info(f"Запуск проверки всех сайтов ({len(self.config.sites)})")
        await self._check_sites(list(self.config.sites))
        logger.info("Проверка всех сайтов завершена")

    async def check_single_site(self, site_id: str) -> Optional[str]:
        """
        Проверяет один сайт по ID.

        Returns:
            Строка результата или None, если сайт не найден
        """
        site = next((s for s in self.config.sites if s.id == site_id), None)
        if not site:
            return None
        result = await self._check_site(site)
        return format_check_line(site, result)

    async def run_immediate_check(self) -> str:
        """Проверяет все сайты и возвращает отчёт построчно."""
        sites = list(self.config.sites)
        results = await self._check_sites(sites)
        lines = []
        for site, result in zip(sites, results):
            if result is None:
                lines.append(f"❌ {site.name} - внутренняя ошибка проверки")
            else:
                lines.append(format_check_line(site, result))
        return "\n".join(lines)

    # ==================== Напоминания ====================

    async def _check_pending_reminders(self) -> None:
        """Проверяет и отправляет просроченные напоминания."""
        sites_needing_reminder = self.database.get_sites_needing_reminder()

        for site_id, reminder_count in sites_needing_reminder:
            site = next((s for s in self.config.sites if s.id == site_id), None)
            if not site:
                logger.warning(f"Сайт {site_id} не найден в конфигурации")
                continue

            try:
                downtime_seconds = self.database.get_downtime_seconds(site_id)
                muted_users = self.database.get_muted_users(site_id)
                next_interval = self.database.mark_reminder_sent(site_id)
                reminder_number = reminder_count + 1

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

    # ==================== Цикл ====================

    async def _run_loop(self) -> None:
        """Основной цикл планировщика."""
        interval_seconds = self.config.scheduler.interval_minutes * 60

        while self._running:
            try:
                await self.check_all_sites()
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки: {e}")

            try:
                await self._check_pending_reminders()
            except Exception as e:
                logger.error(f"Ошибка в цикле напоминаний: {e}")

            await asyncio.sleep(interval_seconds)

    def start(self) -> asyncio.Task:
        """Запускает планировщик."""
        if self._running:
            logger.warning("Планировщик уже запущен")
            return self._task

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Планировщик запущен. Интервал: {self.config.scheduler.interval_minutes} мин., "
            f"параллельно: {self.config.scheduler.concurrency}"
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
        return self._running


async def run_immediate_check(config: Config, database: Database, notifier) -> str:
    """
    Совместимая обёртка: немедленная проверка всех сайтов с отчётом.
    Используется командой /check в Telegram-боте.
    """
    scheduler = MonitorScheduler(config, database, notifier)
    return await scheduler.run_immediate_check()
