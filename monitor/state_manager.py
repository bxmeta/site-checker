"""
Модуль управления состоянием сайтов.
"""
import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from .time_utils import now_izhevsk, format_datetime, parse_datetime


@dataclass
class SiteState:
    """Состояние отдельного сайта."""
    status: str  # UP или DOWN
    fail_streak: int  # Количество последовательных неудачных проверок
    last_status_change: str  # ISO 8601 с таймзоной
    last_notify_at: Optional[str] = None  # ISO 8601 с таймзоной


class StateManager:
    """Менеджер состояния сайтов."""

    def __init__(self, state_file: str = "state.json"):
        """
        Инициализирует менеджер состояния.

        Args:
            state_file: Путь к файлу состояния
        """
        self.state_file = state_file
        self._states: Dict[str, SiteState] = {}
        self._load()

    def _load(self) -> None:
        """Загружает состояние из файла."""
        if not os.path.exists(self.state_file):
            self._states = {}
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            for site_id, state_data in data.items():
                self._states[site_id] = SiteState(
                    status=state_data.get("status", "UP"),
                    fail_streak=state_data.get("fail_streak", 0),
                    last_status_change=state_data.get("last_status_change", format_datetime()),
                    last_notify_at=state_data.get("last_notify_at")
                )
        except (json.JSONDecodeError, KeyError):
            self._states = {}

    def _save(self) -> None:
        """Сохраняет состояние в файл."""
        data = {}
        for site_id, state in self._states.items():
            data[site_id] = asdict(state)

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_state(self, site_id: str) -> SiteState:
        """
        Возвращает состояние сайта.

        Args:
            site_id: Идентификатор сайта

        Returns:
            SiteState (создаётся с дефолтными значениями, если не существует)
        """
        if site_id not in self._states:
            self._states[site_id] = SiteState(
                status="UP",
                fail_streak=0,
                last_status_change=format_datetime()
            )
            self._save()
        return self._states[site_id]

    def get_all_states(self) -> Dict[str, SiteState]:
        """Возвращает состояние всех сайтов."""
        return self._states.copy()

    def update_on_success(self, site_id: str) -> bool:
        """
        Обновляет состояние при успешной проверке.

        Args:
            site_id: Идентификатор сайта

        Returns:
            True, если статус изменился с DOWN на UP
        """
        state = self.get_state(site_id)
        old_status = state.status

        state.fail_streak = 0

        status_changed = old_status == "DOWN"
        if status_changed:
            state.status = "UP"
            state.last_status_change = format_datetime()
            state.last_notify_at = format_datetime()

        self._save()
        return status_changed

    def update_on_failure(self, site_id: str, retry_count: int) -> bool:
        """
        Обновляет состояние при неудачной проверке.

        Args:
            site_id: Идентификатор сайта
            retry_count: Максимальное количество повторных попыток

        Returns:
            True, если статус изменился с UP на DOWN
        """
        state = self.get_state(site_id)
        old_status = state.status

        state.fail_streak += 1

        status_changed = False
        if old_status == "UP" and state.fail_streak >= retry_count:
            state.status = "DOWN"
            state.last_status_change = format_datetime()
            state.last_notify_at = format_datetime()
            status_changed = True

        self._save()
        return status_changed

    def reset_fail_streak(self, site_id: str) -> None:
        """
        Сбрасывает счётчик неудачных проверок.

        Args:
            site_id: Идентификатор сайта
        """
        state = self.get_state(site_id)
        state.fail_streak = 0
        self._save()

    def should_notify(self, site_id: str) -> bool:
        """
        Проверяет, нужно ли отправлять уведомление.
        Уведомления отправляются только при смене статуса.

        Args:
            site_id: Идентификатор сайта

        Returns:
            True, если нужно отправить уведомление
        """
        state = self.get_state(site_id)
        return state.last_notify_at == state.last_status_change

    def mark_notified(self, site_id: str) -> None:
        """
        Отмечает, что уведомление было отправлено.

        Args:
            site_id: Идентификатор сайта
        """
        state = self.get_state(site_id)
        state.last_notify_at = format_datetime()
        self._save()
