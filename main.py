"""
Главная точка входа приложения мониторинга сайтов.
"""
import asyncio
import os
import sys
import signal
from typing import Optional

from monitor.config_loader import load_config
from monitor.database import Database
from monitor.notifier import TelegramNotifier
from monitor.scheduler import MonitorScheduler
from monitor.telegram_bot import setup_bot, start_bot, start_bot_webhook, run_webhook_server
from monitor.logger import setup_logger

# Базовая директория приложения (где лежит main.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


async def main() -> None:
    """Главная асинхронная функция приложения."""
    logger = setup_logger(os.path.join(BASE_DIR, "monitor.log"))
    logger.info("=" * 50)
    logger.info("Запуск системы мониторинга сайтов")
    logger.info("=" * 50)

    config_path = os.path.join(BASE_DIR, "config.yaml")
    db_path = os.path.join(BASE_DIR, "monitor.db")
    state_path = os.path.join(BASE_DIR, "state.json")
    users_path = os.path.join(BASE_DIR, "users.json")

    try:
        config = load_config(config_path)
        logger.info(f"Конфигурация загружена: {len(config.sites)} сайтов")
    except FileNotFoundError as e:
        logger.error(f"Файл конфигурации не найден: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Ошибка в конфигурации: {e}")
        sys.exit(1)

    database = Database(db_path)
    logger.info("База данных инициализирована")

    # Миграция из старых JSON-файлов (если есть)
    migrated_sites, migrated_users = database.migrate_from_json(state_path, users_path)
    if migrated_sites or migrated_users:
        logger.info(f"Мигрировано: {migrated_sites} сайтов, {migrated_users} пользователей")

    notifier = TelegramNotifier(config.telegram)
    logger.info("Telegram-нотификатор инициализирован")

    scheduler = MonitorScheduler(config, database, notifier)

    bot, dp = setup_bot(config, database, notifier, scheduler, config_path)
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

        # Запускаем первоначальную проверку в фоне, не блокируя бота
        asyncio.create_task(scheduler.check_all_sites())

        # Выбираем режим работы бота
        if config.telegram.use_webhook:
            logger.info("Запускаю Telegram-бота в режиме webhook...")
            app = await start_bot_webhook(
                bot, dp,
                webhook_url=config.telegram.webhook_url,
                webhook_path=config.telegram.webhook_path,
                host=config.telegram.webhook_host,
                port=config.telegram.webhook_port
            )
            await run_webhook_server(app, config.telegram.webhook_host, config.telegram.webhook_port)
        else:
            logger.info("Запускаю Telegram-бота в режиме polling...")
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
