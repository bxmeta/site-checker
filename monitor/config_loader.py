"""
Модуль загрузки конфигурации из YAML-файла.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml


@dataclass
class TelegramConfig:
    """Конфигурация Telegram."""
    bot_token: str
    admin_ids: List[int]
    use_webhook: bool = False
    webhook_url: str = ""  # https://yourdomain.com/webhook
    webhook_path: str = "/webhook"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080


@dataclass
class SchedulerConfig:
    """Конфигурация планировщика."""
    interval_minutes: int


@dataclass
class DefaultConfig:
    """Настройки по умолчанию для проверок."""
    retry_count: int
    retry_interval_minutes: int
    timeout_seconds: int


@dataclass
class SiteConfig:
    """Конфигурация отдельного сайта."""
    id: str
    name: str
    url: str
    support_level: str = "none"
    check_ssl: bool = True
    check_http_code: bool = True
    expected_code: int = 200
    keywords: List[str] = field(default_factory=list)
    notify_users: List[int] = field(default_factory=list)


@dataclass
class Config:
    """Полная конфигурация приложения."""
    telegram: TelegramConfig
    scheduler: SchedulerConfig
    default: DefaultConfig
    sites: List[SiteConfig]


def load_config(config_path: str = "config.yaml") -> Config:
    """
    Загружает конфигурацию из YAML-файла.

    Args:
        config_path: Путь к файлу конфигурации

    Returns:
        Объект Config с настройками

    Raises:
        FileNotFoundError: Если файл конфигурации не найден
        ValueError: Если конфигурация некорректна
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError("Пустой файл конфигурации")

    telegram_data = data.get("telegram", {})
    telegram = TelegramConfig(
        bot_token=telegram_data.get("bot_token", ""),
        admin_ids=telegram_data.get("admin_ids", []),
        use_webhook=telegram_data.get("use_webhook", False),
        webhook_url=telegram_data.get("webhook_url", ""),
        webhook_path=telegram_data.get("webhook_path", "/webhook"),
        webhook_host=telegram_data.get("webhook_host", "0.0.0.0"),
        webhook_port=telegram_data.get("webhook_port", 8080)
    )

    scheduler_data = data.get("scheduler", {})
    scheduler = SchedulerConfig(
        interval_minutes=scheduler_data.get("interval_minutes", 3)
    )

    default_data = data.get("default", {})
    default = DefaultConfig(
        retry_count=default_data.get("retry_count", 3),
        retry_interval_minutes=default_data.get("retry_interval_minutes", 5),
        timeout_seconds=default_data.get("timeout_seconds", 10)
    )

    sites_data = data.get("sites", [])
    sites = []
    for site_data in sites_data:
        site = SiteConfig(
            id=site_data.get("id", ""),
            name=site_data.get("name", ""),
            url=site_data.get("url", ""),
            support_level=site_data.get("support_level", "none"),
            check_ssl=site_data.get("check_ssl", True),
            check_http_code=site_data.get("check_http_code", True),
            expected_code=site_data.get("expected_code", 200),
            keywords=site_data.get("keywords", []),
            notify_users=site_data.get("notify_users", [])
        )
        sites.append(site)

    return Config(
        telegram=telegram,
        scheduler=scheduler,
        default=default,
        sites=sites
    )


def get_site_by_id(config: Config, site_id: str) -> Optional[SiteConfig]:
    """
    Возвращает конфигурацию сайта по его ID.

    Args:
        config: Объект конфигурации
        site_id: Идентификатор сайта

    Returns:
        SiteConfig или None, если сайт не найден
    """
    for site in config.sites:
        if site.id == site_id:
            return site
    return None


def get_sites_for_user(config: Config, user_id: int) -> List[SiteConfig]:
    """
    Возвращает список сайтов, за которыми следит пользователь.

    Args:
        config: Объект конфигурации
        user_id: Telegram ID пользователя

    Returns:
        Список SiteConfig
    """
    return [site for site in config.sites if user_id in site.notify_users]


def save_config(config: Config, config_path: str = "config.yaml") -> None:
    """
    Сохраняет конфигурацию в YAML-файл.

    Args:
        config: Объект конфигурации
        config_path: Путь к файлу конфигурации
    """
    data = {
        "telegram": {
            "bot_token": config.telegram.bot_token,
            "admin_ids": config.telegram.admin_ids,
            "use_webhook": config.telegram.use_webhook,
            "webhook_url": config.telegram.webhook_url,
            "webhook_path": config.telegram.webhook_path,
            "webhook_host": config.telegram.webhook_host,
            "webhook_port": config.telegram.webhook_port
        },
        "scheduler": {
            "interval_minutes": config.scheduler.interval_minutes
        },
        "default": {
            "retry_count": config.default.retry_count,
            "retry_interval_minutes": config.default.retry_interval_minutes,
            "timeout_seconds": config.default.timeout_seconds
        },
        "sites": []
    }

    for site in config.sites:
        site_data = {
            "id": site.id,
            "name": site.name,
            "url": site.url,
            "support_level": site.support_level,
            "check_ssl": site.check_ssl,
            "check_http_code": site.check_http_code,
            "expected_code": site.expected_code,
            "keywords": site.keywords,
            "notify_users": site.notify_users
        }
        data["sites"].append(site_data)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def add_site(config: Config, site: SiteConfig, config_path: str = "config.yaml") -> bool:
    """
    Добавляет новый сайт в конфигурацию.

    Args:
        config: Объект конфигурации
        site: Конфигурация нового сайта
        config_path: Путь к файлу конфигурации

    Returns:
        True, если сайт добавлен успешно
    """
    if get_site_by_id(config, site.id):
        return False

    config.sites.append(site)
    save_config(config, config_path)
    return True


def remove_site(config: Config, site_id: str, config_path: str = "config.yaml") -> bool:
    """
    Удаляет сайт из конфигурации.

    Args:
        config: Объект конфигурации
        site_id: Идентификатор сайта
        config_path: Путь к файлу конфигурации

    Returns:
        True, если сайт удалён успешно
    """
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    config.sites.remove(site)
    save_config(config, config_path)
    return True


def update_site(
    config: Config,
    site_id: str,
    config_path: str = "config.yaml",
    **kwargs
) -> bool:
    """
    Обновляет настройки сайта.

    Args:
        config: Объект конфигурации
        site_id: Идентификатор сайта
        config_path: Путь к файлу конфигурации
        **kwargs: Поля для обновления (name, url, support_level, check_ssl, etc.)

    Returns:
        True, если сайт обновлён успешно
    """
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    allowed_fields = {
        "name", "url", "support_level", "check_ssl", "check_http_code",
        "expected_code", "keywords", "notify_users"
    }

    for key, value in kwargs.items():
        if key in allowed_fields and hasattr(site, key):
            setattr(site, key, value)

    save_config(config, config_path)
    return True


def add_notify_user(
    config: Config,
    site_id: str,
    user_id: int,
    config_path: str = "config.yaml"
) -> bool:
    """
    Добавляет пользователя в список уведомлений сайта.

    Args:
        config: Объект конфигурации
        site_id: Идентификатор сайта
        user_id: Telegram ID пользователя
        config_path: Путь к файлу конфигурации

    Returns:
        True, если пользователь добавлен
    """
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    if user_id not in site.notify_users:
        site.notify_users.append(user_id)
        save_config(config, config_path)
    return True


def remove_notify_user(
    config: Config,
    site_id: str,
    user_id: int,
    config_path: str = "config.yaml"
) -> bool:
    """
    Удаляет пользователя из списка уведомлений сайта.

    Args:
        config: Объект конфигурации
        site_id: Идентификатор сайта
        user_id: Telegram ID пользователя
        config_path: Путь к файлу конфигурации

    Returns:
        True, если пользователь удалён
    """
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    if user_id in site.notify_users:
        site.notify_users.remove(user_id)
        save_config(config, config_path)
    return True
