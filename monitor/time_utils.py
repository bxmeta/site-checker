"""
Утилиты для работы с временем в таймзоне Europe/Samara (UTC+4).
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

IZHEVSK_TZ = timezone(timedelta(hours=4))


def now_izhevsk() -> datetime:
    """Возвращает текущее время в таймзоне Ижевска (UTC+4)."""
    return datetime.now(IZHEVSK_TZ)


def format_datetime(dt: Optional[datetime] = None) -> str:
    """
    Форматирует datetime в строку ISO 8601 с таймзоной.
    Если dt не указан, используется текущее время.
    """
    if dt is None:
        dt = now_izhevsk()
    return dt.isoformat()


def format_for_log(dt: Optional[datetime] = None) -> str:
    """
    Форматирует datetime для логов.
    Формат: YYYY-MM-DD HH:MM:SS+04:00
    """
    if dt is None:
        dt = now_izhevsk()
    return dt.strftime("%Y-%m-%d %H:%M:%S%z")


def format_for_message(dt: Optional[datetime] = None) -> str:
    """
    Форматирует datetime для уведомлений в Telegram.
    Формат: DD.MM.YYYY HH:MM:SS (UTC+4)
    """
    if dt is None:
        dt = now_izhevsk()
    return dt.strftime("%d.%m.%Y %H:%M:%S") + " (UTC+4)"


def parse_datetime(dt_str: str) -> datetime:
    """Парсит строку ISO 8601 в datetime объект."""
    return datetime.fromisoformat(dt_str)
