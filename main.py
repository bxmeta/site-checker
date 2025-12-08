"""
Главная точка входа приложения мониторинга сайтов.
"""
import asyncio
import sys
import signal
from typing import Optional

from monitor.config_loader import load_config
from monitor.state_manager import StateManager
from monitor.notifier import TelegramNotifier
from monitor.scheduler import MonitorScheduler
from monitor.telegram_bot import setup_bot, start_bot
from monitor.logger import setup_logger


async def main() -> None:
    """Главная асинхронная функция приложения."""
    logger = setup_logger("monitor.log")
    logger.info("=" * 50)
    logger.info("Запуск системы мониторинга сайтов")
    logger.info("=" * 50)

    try:
        config = load_config("config.yaml")
        logger.info(f"Конфигурация загружена: {len(config.sites)} сайтов")
    except FileNotFoundError as e:
        logger.error(f"Файл конфигурации не найден: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Ошибка в конфигурации: {e}")
        sys.exit(1)

    state_manager = StateManager("state.json")
    logger.info("Менеджер состояния инициализирован")

    notifier = TelegramNotifier(config.telegram)
    logger.info("Telegram-нотификатор инициализирован")

    scheduler = MonitorScheduler(config, state_manager, notifier)

    bot, dp = setup_bot(config, state_manager, notifier, "users.json")
    logger.info("Telegram-бот настроен")

    scheduler_task: Optional[asyncio.Task] = None

    def shutdown_handler(sig, frame):
        """Обработчик сигнала завершения."""
        logger.info(f"Получен сигнал {sig}, завершение работы...")
        scheduler.stop()
        if scheduler_task:
            scheduler_task.cancel()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        scheduler_task = scheduler.start()

        logger.info("Выполняю первоначальную проверку всех сайтов...")
        await scheduler.check_all_sites()

        logger.info("Запускаю Telegram-бота...")
        await start_bot(bot, dp)

    except asyncio.CancelledError:
        logger.info("Задачи отменены")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise
    finally:
        scheduler.stop()
        logger.info("Система мониторинга остановлена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрограмма прервана пользователем")
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        sys.exit(1)
