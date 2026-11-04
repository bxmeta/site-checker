"""
Композитный нотификатор: дублирует уведомления мониторинга в Telegram и Bitrix24.

Каналы независимы: ошибка одного не блокирует другой (смысл дублирования -
устойчивость к блокировкам Telegram). Специфичные для Telegram методы
(подтверждения mute из callback-хендлеров и т.п.) прозрачно проксируются
в TelegramNotifier через __getattr__.

Тихий режим (config.yaml: quiet: true): события мониторинга только пишутся в лог,
в каналы ничего не уходит. Нужен для первого запуска на новом сервере.
"""
import logging
from typing import Any, Optional

from .notifier import TelegramNotifier

logger = logging.getLogger("site_monitor")


class MultiNotifier:
    """Рассылает события мониторинга во все подключённые каналы."""

    def __init__(self, telegram: TelegramNotifier, bitrix24: Optional[Any] = None, quiet: bool = False):
        self.telegram = telegram
        self.bitrix24 = bitrix24  # Bitrix24Notifier или None
        self.quiet = quiet

    async def _fanout(self, method: str, *args, **kwargs) -> None:
        if self.quiet:
            site = args[0] if args else None
            target = getattr(site, "id", None) or (args[0] if args else "")
            logger.info(f"ТИХИЙ РЕЖИМ: {method} [{target}] не отправлено")
            return
        for name, channel in (("telegram", self.telegram), ("bitrix24", self.bitrix24)):
            if channel is None:
                continue
            try:
                await getattr(channel, method)(*args, **kwargs)
            except Exception as e:
                logger.error(f"Канал {name}: ошибка {method}: {e}")

    async def notify_site_down(self, *args, **kwargs) -> None:
        await self._fanout("notify_site_down", *args, **kwargs)

    async def notify_site_up(self, *args, **kwargs) -> None:
        await self._fanout("notify_site_up", *args, **kwargs)

    async def send_reminder(self, *args, **kwargs) -> None:
        await self._fanout("send_reminder", *args, **kwargs)

    async def notify_ssl_expiring(self, *args, **kwargs) -> None:
        await self._fanout("notify_ssl_expiring", *args, **kwargs)

    async def notify_admins(self, message: str) -> None:
        await self._fanout("notify_admins", message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.telegram, name)
