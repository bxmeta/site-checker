"""
Журнал изменений конфигурации: кто, когда и что поменял в списке сайтов и настройках.

Записи пишутся в changes.log рядом с config.yaml и дублируются в основной лог.
Автор действия задаётся через set_actor() (контекстная переменная), поэтому
функции изменения конфига не нуждаются в дополнительных параметрах.
"""
import contextvars
import logging
import os
from typing import List

from .time_utils import format_for_log, now_izhevsk

logger = logging.getLogger("site_monitor")

CHANGELOG_FILE = "changes.log"

_actor: contextvars.ContextVar = contextvars.ContextVar("changelog_actor", default="система")


def set_actor(actor: str):
    """Запоминает автора текущего действия. Возвращает токен для reset_actor()."""
    return _actor.set(actor or "неизвестно")


def reset_actor(token) -> None:
    """Сбрасывает автора, установленного set_actor()."""
    try:
        _actor.reset(token)
    except Exception:
        pass


def tg_actor(user) -> str:
    """Строка автора для пользователя Telegram (aiogram User)."""
    if user is None:
        return "Telegram (неизвестный)"
    parts = [f"Telegram {user.id}"]
    if getattr(user, "username", None):
        parts.append(f"@{user.username}")
    full_name = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x)
    if full_name:
        parts.append(f"({full_name})")
    return " ".join(parts)


def b24_actor(user_id) -> str:
    """Строка автора для пользователя Битрикс24."""
    return f"Битрикс24 пользователь {user_id}"


def _changelog_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), CHANGELOG_FILE)


def log_change(action: str, target: str, details: str = "", config_path: str = "config.yaml") -> None:
    """
    Записывает изменение в журнал.

    Args:
        action: что сделано ("добавлен сайт", "удалён сайт", "изменён сайт", ...)
        target: объект изменения (id сайта и URL, id пользователя)
        details: подробности (какие поля, старое и новое значение)
        config_path: путь к config.yaml, рядом с ним лежит changes.log
    """
    actor = _actor.get()
    line = f"[{format_for_log(now_izhevsk())}] {actor} | {action} | {target}"
    if details:
        line += f" | {details}"
    logger.info(f"ИЗМЕНЕНИЕ КОНФИГА: {actor} | {action} | {target}" + (f" | {details}" if details else ""))
    try:
        with open(_changelog_path(config_path), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.error(f"Не удалось записать changes.log: {e}")


def read_changes(limit: int = 20, config_path: str = "config.yaml") -> List[str]:
    """Возвращает последние limit записей журнала (старые выше, новые ниже)."""
    path = _changelog_path(config_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception as e:
        logger.error(f"Не удалось прочитать changes.log: {e}")
        return []
    return lines[-limit:]
