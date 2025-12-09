#!/usr/bin/env python3
"""
Скрипт отладки мониторинга.
Запуск: python3 debug.py
"""
import asyncio
import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor.config_loader import load_config, SiteConfig
from monitor.database import Database
from monitor.notifier import TelegramNotifier
from monitor.checker import CheckResult, check_site


async def main():
    print("=" * 50)
    print("ОТЛАДКА МОНИТОРИНГА")
    print("=" * 50)

    # Загрузка конфига
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    db_path = os.path.join(os.path.dirname(__file__), "monitor.db")

    try:
        config = load_config(config_path)
        print(f"\n✅ Конфиг загружен")
        print(f"   Сайтов: {len(config.sites)}")
        print(f"   Админы: {config.telegram.admin_ids}")
    except Exception as e:
        print(f"\n❌ Ошибка загрузки конфига: {e}")
        return

    # База данных
    try:
        db = Database(db_path)
        print(f"\n✅ База данных подключена")
    except Exception as e:
        print(f"\n❌ Ошибка базы данных: {e}")
        return

    notifier = TelegramNotifier(config.telegram)

    # Состояние сайтов в БД
    print(f"\n📊 СОСТОЯНИЕ САЙТОВ В БД:")
    print("-" * 50)
    for site in config.sites:
        state = db.get_state(site.id)
        status_emoji = "🟢" if state.status == "UP" else "🔴"
        print(f"   {status_emoji} {site.name}: {state.status} (ошибок: {state.fail_streak})")

    # Тест отправки уведомлений
    print(f"\n📨 ТЕСТ ПРОСТОГО СООБЩЕНИЯ:")
    print("-" * 50)
    admin_id = config.telegram.admin_ids[0]
    result = await notifier.send_message(admin_id, "🧪 Тест 1: простое сообщение")
    print(f"   Результат: {'✅' if result else '❌'}")

    # Тест notify_site_down
    print(f"\n📨 ТЕСТ NOTIFY_SITE_DOWN:")
    print("-" * 50)
    test_site = config.sites[0]
    fake_result = CheckResult(
        success=False,
        status_code=None,
        response_time_ms=100,
        error="ТЕСТОВАЯ ОШИБКА от debug.py",
        error_type="timeout"
    )
    print(f"   Сайт: {test_site.name}")
    print(f"   notify_users: {test_site.notify_users}")
    print(f"   admin_ids: {config.telegram.admin_ids}")
    recipients = notifier._get_all_recipients(test_site)
    print(f"   Получатели (all_recipients): {recipients}")

    print(f"\n   Отправляю notify_site_down...")
    await notifier.notify_site_down(test_site, fake_result)
    print(f"   ✅ notify_site_down вызван")

    # Проверка сайтов (без retry, быстро)
    print(f"\n🔍 ПРОВЕРКА САЙТОВ (таймаут {config.default.timeout_seconds}с):")
    print("-" * 50)
    for site in config.sites:
        print(f"   {site.name}...", end=" ", flush=True)
        try:
            result = await check_site(site, config.default)
            if result.success:
                print(f"✅ {result.status_code}")
            else:
                print(f"❌ {result.error}")
        except Exception as e:
            print(f"💥 {e}")

    print("\n" + "=" * 50)
    print("ОТЛАДКА ЗАВЕРШЕНА")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
