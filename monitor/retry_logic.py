"""
Модуль retry-логики для проверки сайтов.
"""
import asyncio
import logging
from typing import Callable, Awaitable

from .checker import CheckResult, check_site
from .config_loader import SiteConfig, DefaultConfig

logger = logging.getLogger("site_monitor")


async def check_with_retry(
    site: SiteConfig,
    defaults: DefaultConfig
) -> CheckResult:
    """
    Выполняет проверку сайта с повторными попытками при неудаче.

    Args:
        site: Конфигурация сайта
        defaults: Настройки по умолчанию (содержат retry_count и retry_interval_minutes)

    Returns:
        CheckResult - результат проверки
        Если хотя бы одна попытка успешна - возвращается успешный результат.
        Если все попытки неуспешны - возвращается результат последней попытки.
    """
    retry_count = defaults.retry_count
    retry_interval_seconds = defaults.retry_interval_minutes * 60

    last_result = None

    for attempt in range(retry_count):
        result = await check_site(site, defaults)

        if result.success:
            if attempt > 0:
                logger.info(f"[{site.id}] Успешно с попытки {attempt + 1}/{retry_count}")
            return result

        last_result = result

        if attempt < retry_count - 1:
            logger.info(
                f"[{site.id}] Попытка {attempt + 1}/{retry_count} неудачна: {result.error}. "
                f"Повтор через {defaults.retry_interval_minutes} мин."
            )
            await asyncio.sleep(retry_interval_seconds)
        else:
            logger.info(
                f"[{site.id}] Все {retry_count} попытки неудачны. Последняя ошибка: {result.error}"
            )

    return last_result


async def check_site_single(
    site: SiteConfig,
    defaults: DefaultConfig
) -> CheckResult:
    """
    Выполняет одну проверку сайта без повторных попыток.

    Args:
        site: Конфигурация сайта
        defaults: Настройки по умолчанию

    Returns:
        CheckResult - результат проверки
    """
    return await check_site(site, defaults)
