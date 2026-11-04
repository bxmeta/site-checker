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
from monitor.multi_notifier import MultiNotifier
from monitor.b24 import create_b24
from monitor.scheduler import MonitorScheduler
from monitor.logger import setup_logger
# monitor.telegram_bot (aiogram) импортируется лениво: один импорт aiogram стоит ~150 МБ памяти,
# без токена Telegram он не нужен.

# Базовая директория приложения (где лежит main.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Каталог данных: config.yaml, monitor.db, monitor.log, b24_auth.json, changes.log.
# По умолчанию совпадает с каталогом кода; на сервере задаётся переменной SITE_CHECKER_DATA.
DATA_DIR = os.environ.get("SITE_CHECKER_DATA") or BASE_DIR


async def main() -> None:
    """Главная асинхронная функция приложения."""
    logger = setup_logger(os.path.join(DATA_DIR, "monitor.log"))
    logger.info("=" * 50)
    logger.info("Запуск системы мониторинга сайтов")
    logger.info("=" * 50)

    config_path = os.path.join(DATA_DIR, "config.yaml")
    db_path = os.path.join(DATA_DIR, "monitor.db")
    state_path = os.path.join(DATA_DIR, "state.json")
    users_path = os.path.join(DATA_DIR, "users.json")
    logger.info(f"Каталог данных: {DATA_DIR}")

    try:
        config = load_config(config_path)
        logger.info(f"Конфигурация загружена: {len(config.sites)} сайтов")
        if config.quiet:
            logger.info("ТИХИЙ РЕЖИМ: уведомления отключены (quiet: true в config.yaml)")
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

    tg_notifier = TelegramNotifier(config.telegram)
    logger.info("Telegram-нотификатор инициализирован")

    b24_app = create_b24(config, database, DATA_DIR, config_path)
    if b24_app:
        logger.info("Bitrix24-интеграция включена")

    notifier = MultiNotifier(tg_notifier, b24_app.notifier if b24_app else None, quiet=config.quiet)

    scheduler = MonitorScheduler(config, database, notifier)
    if b24_app:
        b24_app.set_scheduler(scheduler)

    bot = dp = None
    if config.telegram.bot_token:
        from monitor.telegram_bot import setup_bot
        bot, dp = setup_bot(config, database, notifier, scheduler, config_path)
        logger.info("Telegram-бот настроен")
    else:
        logger.warning("Токен Telegram-бота не задан: Telegram-бот не запускается, уведомления только в Bitrix24")

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
        # Первая проверка всех сайтов выполняется сразу внутри цикла планировщика
        scheduler_task = scheduler.start()

        # Выбираем режим работы бота
        if bot is not None and config.telegram.use_webhook:
            logger.info("Запускаю Telegram-бота в режиме webhook...")
            from monitor.telegram_bot import start_bot_webhook, run_webhook_server
            app = await start_bot_webhook(
                bot, dp,
                webhook_url=config.telegram.webhook_url,
                webhook_path=config.telegram.webhook_path,
                host=config.telegram.webhook_host,
                port=config.telegram.webhook_port
            )
            if b24_app:
                b24_app.register_routes(app)
                logger.info("Маршруты Bitrix24 (/b24/*) подключены")
                asyncio.create_task(b24_app.startup())
            await run_webhook_server(app, config.telegram.webhook_host, config.telegram.webhook_port)
        elif bot is None:
            # Токен Telegram не задан: работаем без TG-бота (проверки + канал Bitrix24)
            from aiohttp import web
            b24_web = web.Application()
            if b24_app:
                b24_app.register_routes(b24_web)
            runner = web.AppRunner(b24_web)
            await runner.setup()
            site = web.TCPSite(runner, config.telegram.webhook_host, config.telegram.webhook_port)
            await site.start()
            logger.info(
                f"HTTP-сервер запущен на {config.telegram.webhook_host}:{config.telegram.webhook_port}"
            )
            if b24_app:
                asyncio.create_task(b24_app.startup())
            while True:
                await asyncio.sleep(3600)
        else:
            logger.info("Запускаю Telegram-бота в режиме polling...")
            from monitor.telegram_bot import start_bot
            if b24_app:
                # В режиме polling поднимаем отдельный HTTP-сервер для событий Б24
                from aiohttp import web
                b24_web = web.Application()
                b24_app.register_routes(b24_web)
                runner = web.AppRunner(b24_web)
                await runner.setup()
                site = web.TCPSite(runner, config.telegram.webhook_host, config.telegram.webhook_port)
                await site.start()
                logger.info(
                    f"HTTP-сервер Bitrix24 запущен на "
                    f"{config.telegram.webhook_host}:{config.telegram.webhook_port}"
                )
                asyncio.create_task(b24_app.startup())
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
