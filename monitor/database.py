"""
Модуль для работы с SQLite базой данных.
Потокобезопасное хранение состояния сайтов, пользователей и инцидентов.
"""
import json
import os
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager

from .time_utils import now_izhevsk, format_datetime, parse_datetime

logger = logging.getLogger("site_monitor")


@dataclass
class SiteState:
    """Состояние отдельного сайта."""
    status: str  # UP или DOWN
    fail_streak: int
    last_status_change: str  # ISO 8601
    last_notify_at: Optional[str] = None
    reminder_count: int = 0
    next_reminder_at: Optional[str] = None
    current_incident_id: Optional[int] = None


@dataclass
class Incident:
    """Инцидент (период недоступности сайта)."""
    id: int
    site_id: str
    started_at: str
    ended_at: Optional[str] = None
    duration_seconds: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class User:
    """Зарегистрированный пользователь."""
    user_id: int
    username: Optional[str]
    full_name: Optional[str]
    registered_at: str


@dataclass
class SiteStats:
    """Статистика сайта."""
    site_id: str
    uptime_7d: float  # Процент
    uptime_30d: float
    incidents_30d: int
    avg_downtime_seconds: int
    last_incident_at: Optional[str]


# Интервалы напоминаний в минутах: 15, 30, 60, 120, 240
REMINDER_INTERVALS = [15, 30, 60, 120, 240]


def get_next_reminder_interval(reminder_count: int) -> int:
    """Возвращает интервал до следующего напоминания в минутах."""
    # 15 * 2^reminder_count, максимум 240 минут
    interval = 15 * (2 ** reminder_count)
    return min(interval, 240)


class Database:
    """Потокобезопасная база данных SQLite."""

    def __init__(self, db_path: str = "monitor.db"):
        """
        Инициализирует базу данных.

        Args:
            db_path: Путь к файлу базы данных
        """
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Контекстный менеджер для подключения к БД."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL mode для лучшей конкурентности
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Создаёт таблицы если их нет."""
        with self._get_connection() as conn:
            conn.executescript("""
                -- Состояние сайтов
                CREATE TABLE IF NOT EXISTS sites_state (
                    site_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'UP',
                    fail_streak INTEGER DEFAULT 0,
                    last_status_change TEXT NOT NULL,
                    last_notify_at TEXT,
                    reminder_count INTEGER DEFAULT 0,
                    next_reminder_at TEXT,
                    current_incident_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Пользователи
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    registered_at TEXT NOT NULL
                );

                -- Заглушки per-user
                CREATE TABLE IF NOT EXISTS user_mutes (
                    user_id INTEGER NOT NULL,
                    site_id TEXT NOT NULL,
                    muted_at TEXT NOT NULL,
                    incident_start TEXT NOT NULL,
                    PRIMARY KEY (user_id, site_id)
                );

                -- История инцидентов
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_seconds INTEGER,
                    error_type TEXT,
                    error_message TEXT
                );

                -- Индексы
                CREATE INDEX IF NOT EXISTS idx_incidents_site ON incidents(site_id);
                CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at);
                CREATE INDEX IF NOT EXISTS idx_mutes_site ON user_mutes(site_id);
                CREATE INDEX IF NOT EXISTS idx_sites_status ON sites_state(status);
                CREATE INDEX IF NOT EXISTS idx_sites_next_reminder ON sites_state(next_reminder_at);
            """)

    # ==================== Состояние сайтов ====================

    def get_state(self, site_id: str) -> SiteState:
        """Возвращает состояние сайта, создаёт если не существует."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sites_state WHERE site_id = ?",
                (site_id,)
            ).fetchone()

            if row:
                return SiteState(
                    status=row["status"],
                    fail_streak=row["fail_streak"],
                    last_status_change=row["last_status_change"],
                    last_notify_at=row["last_notify_at"],
                    reminder_count=row["reminder_count"],
                    next_reminder_at=row["next_reminder_at"],
                    current_incident_id=row["current_incident_id"]
                )

            # Создаём новую запись
            now = format_datetime()
            conn.execute("""
                INSERT INTO sites_state
                (site_id, status, fail_streak, last_status_change, created_at, updated_at)
                VALUES (?, 'UP', 0, ?, ?, ?)
            """, (site_id, now, now, now))

            return SiteState(
                status="UP",
                fail_streak=0,
                last_status_change=now
            )

    def get_all_states(self) -> Dict[str, SiteState]:
        """Возвращает состояние всех сайтов."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM sites_state").fetchall()

            return {
                row["site_id"]: SiteState(
                    status=row["status"],
                    fail_streak=row["fail_streak"],
                    last_status_change=row["last_status_change"],
                    last_notify_at=row["last_notify_at"],
                    reminder_count=row["reminder_count"],
                    next_reminder_at=row["next_reminder_at"],
                    current_incident_id=row["current_incident_id"]
                )
                for row in rows
            }

    def update_on_success(self, site_id: str) -> Tuple[bool, Optional[int]]:
        """
        Обновляет состояние при успешной проверке.

        Returns:
            (status_changed, downtime_seconds) - если статус изменился с DOWN на UP
        """
        state = self.get_state(site_id)
        old_status = state.status
        now = format_datetime()
        downtime_seconds = None

        with self._get_connection() as conn:
            if old_status == "DOWN":
                # Закрываем инцидент
                if state.current_incident_id:
                    downtime_seconds = self._close_incident(
                        conn, state.current_incident_id
                    )

                # Очищаем заглушки для этого сайта
                conn.execute(
                    "DELETE FROM user_mutes WHERE site_id = ?",
                    (site_id,)
                )

                # Обновляем состояние
                conn.execute("""
                    UPDATE sites_state SET
                        status = 'UP',
                        fail_streak = 0,
                        last_status_change = ?,
                        last_notify_at = ?,
                        reminder_count = 0,
                        next_reminder_at = NULL,
                        current_incident_id = NULL,
                        updated_at = ?
                    WHERE site_id = ?
                """, (now, now, now, site_id))

                return True, downtime_seconds
            else:
                # Просто сбрасываем счётчик ошибок
                conn.execute("""
                    UPDATE sites_state SET
                        fail_streak = 0,
                        updated_at = ?
                    WHERE site_id = ?
                """, (now, site_id))

                return False, None

    def update_on_failure(
        self,
        site_id: str,
        retry_count: int,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Обновляет состояние при неудачной проверке.

        Returns:
            True, если статус изменился с UP на DOWN
        """
        state = self.get_state(site_id)
        old_status = state.status
        now = format_datetime()
        new_fail_streak = state.fail_streak + 1

        with self._get_connection() as conn:
            # Если все retry провалились — сразу DOWN (retry_count=1 означает немедленно)
            if old_status == "UP" and new_fail_streak >= 1:
                # Создаём инцидент
                incident_id = self._create_incident(
                    conn, site_id, error_type, error_message
                )

                # Вычисляем время следующего напоминания
                next_reminder = self._calc_next_reminder(0)

                conn.execute("""
                    UPDATE sites_state SET
                        status = 'DOWN',
                        fail_streak = ?,
                        last_status_change = ?,
                        last_notify_at = ?,
                        reminder_count = 0,
                        next_reminder_at = ?,
                        current_incident_id = ?,
                        updated_at = ?
                    WHERE site_id = ?
                """, (new_fail_streak, now, now, next_reminder, incident_id, now, site_id))

                return True
            else:
                conn.execute("""
                    UPDATE sites_state SET
                        fail_streak = ?,
                        updated_at = ?
                    WHERE site_id = ?
                """, (new_fail_streak, now, site_id))

                return False

    # ==================== Напоминания ====================

    def _calc_next_reminder(self, reminder_count: int) -> str:
        """Вычисляет время следующего напоминания."""
        interval = get_next_reminder_interval(reminder_count)
        next_time = now_izhevsk() + timedelta(minutes=interval)
        return format_datetime(next_time)

    def get_sites_needing_reminder(self) -> List[Tuple[str, int]]:
        """
        Возвращает сайты, для которых пора отправить напоминание.

        Returns:
            Список кортежей (site_id, reminder_count)
        """
        now = format_datetime()
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT site_id, reminder_count
                FROM sites_state
                WHERE status = 'DOWN'
                  AND next_reminder_at IS NOT NULL
                  AND next_reminder_at <= ?
            """, (now,)).fetchall()

            return [(row["site_id"], row["reminder_count"]) for row in rows]

    def mark_reminder_sent(self, site_id: str) -> int:
        """
        Отмечает, что напоминание отправлено.

        Returns:
            Интервал до следующего напоминания в минутах
        """
        state = self.get_state(site_id)
        new_count = state.reminder_count + 1
        next_interval = get_next_reminder_interval(new_count)
        next_reminder = self._calc_next_reminder(new_count)
        now = format_datetime()

        with self._get_connection() as conn:
            conn.execute("""
                UPDATE sites_state SET
                    reminder_count = ?,
                    next_reminder_at = ?,
                    updated_at = ?
                WHERE site_id = ?
            """, (new_count, next_reminder, now, site_id))

        return next_interval

    def get_downtime_seconds(self, site_id: str) -> int:
        """Возвращает текущее время простоя в секундах."""
        state = self.get_state(site_id)
        if state.status != "DOWN":
            return 0

        start = parse_datetime(state.last_status_change)
        now = now_izhevsk()
        return int((now - start).total_seconds())

    # ==================== Mute/Unmute ====================

    def mute_for_user(self, user_id: int, site_id: str) -> bool:
        """
        Заглушает напоминания для пользователя.

        Returns:
            True если успешно, False если уже заглушено
        """
        state = self.get_state(site_id)
        now = format_datetime()

        with self._get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO user_mutes (user_id, site_id, muted_at, incident_start)
                    VALUES (?, ?, ?, ?)
                """, (user_id, site_id, now, state.last_status_change))
                return True
            except sqlite3.IntegrityError:
                return False  # Уже заглушено

    def unmute_for_user(self, user_id: int, site_id: str) -> bool:
        """
        Включает напоминания для пользователя.

        Returns:
            True если успешно, False если не было заглушено
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM user_mutes WHERE user_id = ? AND site_id = ?
            """, (user_id, site_id))
            return cursor.rowcount > 0

    def is_muted(self, user_id: int, site_id: str) -> bool:
        """Проверяет, заглушены ли напоминания для пользователя."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM user_mutes WHERE user_id = ? AND site_id = ?
            """, (user_id, site_id)).fetchone()
            return row is not None

    def get_muted_users(self, site_id: str) -> List[int]:
        """Возвращает список пользователей, заглушивших сайт."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT user_id FROM user_mutes WHERE site_id = ?
            """, (site_id,)).fetchall()
            return [row["user_id"] for row in rows]

    def get_user_mutes(self, user_id: int) -> List[str]:
        """Возвращает список сайтов, заглушенных пользователем."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT site_id FROM user_mutes WHERE user_id = ?
            """, (user_id,)).fetchall()
            return [row["site_id"] for row in rows]

    def clear_mutes_for_site(self, site_id: str) -> int:
        """Очищает все заглушки для сайта. Возвращает количество удалённых."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM user_mutes WHERE site_id = ?",
                (site_id,)
            )
            return cursor.rowcount

    # ==================== Пользователи ====================

    def register_user(
        self,
        user_id: int,
        username: Optional[str] = None,
        full_name: Optional[str] = None
    ) -> bool:
        """
        Регистрирует пользователя.

        Returns:
            True если новый пользователь, False если уже существует
        """
        now = format_datetime()
        with self._get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO users (user_id, username, full_name, registered_at)
                    VALUES (?, ?, ?, ?)
                """, (user_id, username, full_name, now))
                return True
            except sqlite3.IntegrityError:
                # Обновляем информацию
                conn.execute("""
                    UPDATE users SET username = ?, full_name = ?
                    WHERE user_id = ?
                """, (username, full_name, user_id))
                return False

    def get_user(self, user_id: int) -> Optional[User]:
        """Возвращает пользователя по ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            if row:
                return User(
                    user_id=row["user_id"],
                    username=row["username"],
                    full_name=row["full_name"],
                    registered_at=row["registered_at"]
                )
            return None

    def get_all_users(self) -> List[User]:
        """Возвращает всех пользователей."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM users").fetchall()
            return [
                User(
                    user_id=row["user_id"],
                    username=row["username"],
                    full_name=row["full_name"],
                    registered_at=row["registered_at"]
                )
                for row in rows
            ]

    # ==================== Инциденты ====================

    def _create_incident(
        self,
        conn: sqlite3.Connection,
        site_id: str,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> int:
        """Создаёт инцидент и возвращает его ID."""
        now = format_datetime()
        cursor = conn.execute("""
            INSERT INTO incidents (site_id, started_at, error_type, error_message)
            VALUES (?, ?, ?, ?)
        """, (site_id, now, error_type, error_message))
        return cursor.lastrowid

    def _close_incident(self, conn: sqlite3.Connection, incident_id: int) -> int:
        """Закрывает инцидент и возвращает длительность в секундах."""
        now = now_izhevsk()
        now_str = format_datetime(now)

        row = conn.execute(
            "SELECT started_at FROM incidents WHERE id = ?",
            (incident_id,)
        ).fetchone()

        if not row:
            return 0

        started_at = parse_datetime(row["started_at"])
        duration = int((now - started_at).total_seconds())

        conn.execute("""
            UPDATE incidents SET ended_at = ?, duration_seconds = ?
            WHERE id = ?
        """, (now_str, duration, incident_id))

        return duration

    def get_incident(self, incident_id: int) -> Optional[Incident]:
        """Возвращает инцидент по ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (incident_id,)
            ).fetchone()

            if row:
                return Incident(
                    id=row["id"],
                    site_id=row["site_id"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    duration_seconds=row["duration_seconds"],
                    error_type=row["error_type"],
                    error_message=row["error_message"]
                )
            return None

    def get_site_incidents(
        self,
        site_id: str,
        days: int = 30,
        limit: int = 100
    ) -> List[Incident]:
        """Возвращает инциденты сайта за последние N дней."""
        since = now_izhevsk() - timedelta(days=days)
        since_str = format_datetime(since)

        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM incidents
                WHERE site_id = ? AND started_at >= ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (site_id, since_str, limit)).fetchall()

            return [
                Incident(
                    id=row["id"],
                    site_id=row["site_id"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    duration_seconds=row["duration_seconds"],
                    error_type=row["error_type"],
                    error_message=row["error_message"]
                )
                for row in rows
            ]

    # ==================== Статистика ====================

    def get_site_stats(self, site_id: str) -> SiteStats:
        """Возвращает статистику сайта."""
        now = now_izhevsk()

        # Получаем инциденты за 7 и 30 дней
        incidents_7d = self.get_site_incidents(site_id, days=7)
        incidents_30d = self.get_site_incidents(site_id, days=30)

        # Считаем время простоя
        def calc_downtime(incidents: List[Incident], days: int) -> int:
            total = 0
            period_start = now - timedelta(days=days)

            for inc in incidents:
                started = parse_datetime(inc.started_at)
                if inc.ended_at:
                    ended = parse_datetime(inc.ended_at)
                else:
                    ended = now  # Текущий инцидент

                # Обрезаем по границам периода
                started = max(started, period_start)
                ended = min(ended, now)

                if ended > started:
                    total += int((ended - started).total_seconds())

            return total

        downtime_7d = calc_downtime(incidents_7d, 7)
        downtime_30d = calc_downtime(incidents_30d, 30)

        total_7d = 7 * 24 * 3600
        total_30d = 30 * 24 * 3600

        uptime_7d = 100 * (1 - downtime_7d / total_7d) if total_7d > 0 else 100
        uptime_30d = 100 * (1 - downtime_30d / total_30d) if total_30d > 0 else 100

        # Среднее время простоя
        closed_incidents = [i for i in incidents_30d if i.duration_seconds]
        if closed_incidents:
            avg_downtime = sum(i.duration_seconds for i in closed_incidents) // len(closed_incidents)
        else:
            avg_downtime = 0

        # Последний инцидент
        last_incident = incidents_30d[0] if incidents_30d else None

        return SiteStats(
            site_id=site_id,
            uptime_7d=round(uptime_7d, 2),
            uptime_30d=round(uptime_30d, 2),
            incidents_30d=len(incidents_30d),
            avg_downtime_seconds=avg_downtime,
            last_incident_at=last_incident.started_at if last_incident else None
        )

    # ==================== Миграция ====================

    def migrate_from_json(
        self,
        state_file: str = "state.json",
        users_file: str = "users.json"
    ) -> Tuple[int, int]:
        """
        Мигрирует данные из JSON файлов в SQLite.

        Returns:
            (migrated_sites, migrated_users)
        """
        migrated_sites = 0
        migrated_users = 0

        # Миграция состояний
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                now = format_datetime()
                with self._get_connection() as conn:
                    for site_id, state_data in data.items():
                        # Проверяем, нет ли уже записи
                        existing = conn.execute(
                            "SELECT 1 FROM sites_state WHERE site_id = ?",
                            (site_id,)
                        ).fetchone()

                        if not existing:
                            conn.execute("""
                                INSERT INTO sites_state
                                (site_id, status, fail_streak, last_status_change,
                                 last_notify_at, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (
                                site_id,
                                state_data.get("status", "UP"),
                                state_data.get("fail_streak", 0),
                                state_data.get("last_status_change", now),
                                state_data.get("last_notify_at"),
                                now,
                                now
                            ))
                            migrated_sites += 1

                logger.info(f"Мигрировано {migrated_sites} сайтов из {state_file}")

                # Переименовываем старый файл
                os.rename(state_file, state_file + ".bak")
                logger.info(f"Файл {state_file} переименован в {state_file}.bak")

            except Exception as e:
                logger.error(f"Ошибка миграции состояний: {e}")

        # Миграция пользователей
        if os.path.exists(users_file):
            try:
                with open(users_file, "r", encoding="utf-8") as f:
                    users = json.load(f)

                now = format_datetime()
                with self._get_connection() as conn:
                    for user_data in users:
                        user_id = user_data.get("id") or user_data.get("user_id")
                        if not user_id:
                            continue

                        existing = conn.execute(
                            "SELECT 1 FROM users WHERE user_id = ?",
                            (user_id,)
                        ).fetchone()

                        if not existing:
                            conn.execute("""
                                INSERT INTO users (user_id, username, full_name, registered_at)
                                VALUES (?, ?, ?, ?)
                            """, (
                                user_id,
                                user_data.get("username"),
                                user_data.get("full_name"),
                                user_data.get("registered_at", now)
                            ))
                            migrated_users += 1

                logger.info(f"Мигрировано {migrated_users} пользователей из {users_file}")

                # Переименовываем старый файл
                os.rename(users_file, users_file + ".bak")
                logger.info(f"Файл {users_file} переименован в {users_file}.bak")

            except Exception as e:
                logger.error(f"Ошибка миграции пользователей: {e}")

        return migrated_sites, migrated_users
