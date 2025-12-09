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

from monitor.config_loader import load_config
from monitor.database import Database
from monitor.notifier import TelegramNotifier
from monitor.retry_logic import check_with_retry


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

    # Состояние сайтов в БД
    print(f"\n📊 СОСТОЯНИЕ САЙТОВ В БД:")
    print("-" * 50)
    for site in config.sites:
        state = db.get_state(site.id)
        print(f"   {site.name}")
        print(f"      URL: {site.url}")
        print(f"      Статус: {state.status}")
        print(f"      Ошибок подряд: {state.fail_streak}")
        print()

    # Тест отправки уведомлений
    print(f"\n📨 ТЕСТ УВЕДОМЛЕНИЙ:")
    print("-" * 50)
    notifier = TelegramNotifier(config.telegram)

    for admin_id in config.telegram.admin_ids:
        print(f"   Отправка на {admin_id}...")
        try:
            result = await notifier.send_message(admin_id, "🧪 Тестовое сообщение от debug.py")
            if result:
                print(f"   ✅ Успешно отправлено на {admin_id}")
            else:
                print(f"   ❌ Ошибка отправки на {admin_id}")
        except Exception as e:
            print(f"   ❌ Исключение: {e}")

    # Проверка сайтов
    print(f"\n🔍 ПРОВЕРКА САЙТОВ:")
    print("-" * 50)
    for site in config.sites:
        print(f"\n   Проверяю: {site.name} ({site.url})")
        try:
            result = await check_with_retry(site, config.default)
            print(f"      Success: {result.success}")
            print(f"      Code: {result.status_code}")
            print(f"      Error: {result.error}")
            print(f"      Error type: {result.error_type}")
        except Exception as e:
            print(f"      ❌ Исключение: {e}")

    print("\n" + "=" * 50)
    print("ОТЛАДКА ЗАВЕРШЕНА")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
