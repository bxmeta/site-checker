"""
Модуль логирования с таймзоной UTC+4.
"""
import logging
import os
from datetime import datetime
from typing import Optional

from .time_utils import IZHEVSK_TZ, format_for_log


class IzhevskFormatter(logging.Formatter):
    """Форматер логов с временем в таймзоне Ижевска (UTC+4)."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=IZHEVSK_TZ)
        return format_for_log(dt)


def setup_logger(log_file: str = "monitor.log") -> logging.Logger:
    """
    Настраивает и возвращает логгер.

    Args:
        log_file: Путь к файлу логов

    Returns:
        Настроенный логгер
    """
    logger = logging.getLogger("site_monitor")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = IzhevskFormatter("[%(asctime)s] %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def log_check_result(
    logger: logging.Logger,
    site_id: str,
    success: bool,
    status_code: Optional[int] = None,
    response_time_ms: Optional[int] = None,
    error: Optional[str] = None
) -> None:
    """
    Логирует результат проверки сайта.

    Args:
        logger: Экземпляр логгера
        site_id: Идентификатор сайта
        success: Успешность проверки
        status_code: HTTP-код ответа
        response_time_ms: Время ответа в миллисекундах
        error: Описание ошибки (если есть)
    """
    if success:
        code_str = f"({status_code})" if status_code else ""
        time_str = f" {response_time_ms}ms" if response_time_ms else ""
        logger.info(f"[{site_id}] OK {code_str}{time_str}")
    else:
        error_str = error or "Unknown error"
        logger.info(f"[{site_id}] ERROR {error_str}")
