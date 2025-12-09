#!/usr/bin/env python3
"""
Скрипт сброса всех сайтов в статус UP.
Запуск: python3 reset_sites.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor.database import Database
from monitor.time_utils import format_datetime


def main():
    db_path = os.path.join(os.path.dirname(__file__), "monitor.db")
    db = Database(db_path)

    print("Сброс всех сайтов в статус UP...")

    now = format_datetime()

    with db._get_connection() as conn:
        # Получаем все сайты со статусом DOWN
        rows = conn.execute(
            "SELECT site_id, status FROM sites_state WHERE status = 'DOWN'"
        ).fetchall()

        if not rows:
            print("Нет сайтов в статусе DOWN")
            return

        for row in rows:
            site_id = row["site_id"]
            print(f"  {site_id}: DOWN -> UP")

        # Сбрасываем все сайты в UP
        conn.execute("""
            UPDATE sites_state SET
                status = 'UP',
                fail_streak = 0,
                reminder_count = 0,
                next_reminder_at = NULL,
                current_incident_id = NULL,
                updated_at = ?
            WHERE status = 'DOWN'
        """, (now,))

        # Очищаем заглушки
        conn.execute("DELETE FROM user_mutes")

    print(f"\nГотово! Сброшено {len(rows)} сайтов.")
    print("Теперь при следующей неудачной проверке придёт уведомление.")


if __name__ == "__main__":
    main()
