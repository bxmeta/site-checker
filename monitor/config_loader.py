"""
Модуль загрузки конфигурации из YAML-файла.
"""
import os
import stat
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

from .changelog import log_change


@dataclass
class TelegramConfig:
    """Конфигурация Telegram."""
    bot_token: str
    admin_ids: List[int]
    use_webhook: bool = False
    webhook_url: str = ""  # https://yourdomain.com/webhook
    webhook_path: str = "/webhook"
    webhook_host: str = "0.0.0.0"   # в режиме polling на этом адресе/порту живёт HTTP-сервер Bitrix24
    webhook_port: int = 8080
    proxy: str = ""  # http://host:port (HTTP CONNECT), пусто - напрямую


@dataclass
class Bitrix24Config:
    """Канал Bitrix24: бот через входящий вебхук со scope imbot."""
    enabled: bool = False
    domain: str = ""                      # https://victory-corp.ru
    webhook_url: str = ""                 # входящий вебхук (scope imbot), секрет
    client_id: str = "site_monitor_bot"   # CLIENT_ID обязателен для imbot.* через вебхук
    client_secret: str = ""               # не используется в режиме вебхука, оставлено для совместимости
    handler_base: str = ""                # https://monitor-bot.victory-dev.ru:2053 - база для EVENT_HANDLER
    bot_code: str = "site_monitor"
    bot_name: str = "Мониторинг сайтов"
    chat_title: str = "🌐 Мониторинг сайтов"
    chat_dialog_id: str = ""              # chat8493; пусто - чат создаётся при первом запуске
    admin_users: List[int] = field(default_factory=list)
    application_token: str = ""           # auth[application_token] из событий портала; пусто - запомнить из первого события
    allowed_ips: List[str] = field(default_factory=list)  # откуда принимать события (IP портала); пусто - отовсюду


@dataclass
class SchedulerConfig:
    """Конфигурация планировщика."""
    interval_minutes: int
    concurrency: int = 10  # сколько сайтов проверять одновременно


@dataclass
class DefaultConfig:
    """Настройки по умолчанию для проверок."""
    retry_count: int
    retry_interval_minutes: int
    timeout_seconds: int


@dataclass
class SslConfig:
    """Настройки SSL-проверок."""
    expiry_warn_days: int = 3            # предупреждать об истечении платного сертификата за N дней
    remind_every_hours: int = 24         # повторять предупреждение не чаще, чем раз в N часов
    # Издатели с автопродлением (ACME): про срок действия не предупреждаем
    auto_renew_issuers: List[str] = field(default_factory=lambda: ["Let's Encrypt", "ZeroSSL"])


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
    notify_users: List[int] = field(default_factory=list)       # Telegram ID
    notify_b24_users: List[int] = field(default_factory=list)   # ID пользователей Bitrix24 (личные сообщения от бота)


@dataclass
class Config:
    """Полная конфигурация приложения."""
    telegram: TelegramConfig
    scheduler: SchedulerConfig
    default: DefaultConfig
    sites: List[SiteConfig]
    bitrix24: Bitrix24Config = field(default_factory=Bitrix24Config)
    ssl: SslConfig = field(default_factory=SslConfig)
    quiet: bool = False  # тихий режим: уведомления не отправляются, только лог


def _int_list(values) -> List[int]:
    out: List[int] = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def load_config(config_path: str = "config.yaml") -> Config:
    """
    Загружает конфигурацию из YAML-файла.

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

    telegram_data = data.get("telegram", {}) or {}
    telegram = TelegramConfig(
        bot_token=str(telegram_data.get("bot_token", "") or ""),
        admin_ids=_int_list(telegram_data.get("admin_ids", [])),
        use_webhook=bool(telegram_data.get("use_webhook", False)),
        webhook_url=telegram_data.get("webhook_url", "") or "",
        webhook_path=telegram_data.get("webhook_path", "/webhook") or "/webhook",
        webhook_host=telegram_data.get("webhook_host", "0.0.0.0") or "0.0.0.0",
        webhook_port=int(telegram_data.get("webhook_port", 8080)),
        proxy=str(telegram_data.get("proxy", "") or "").strip(),
    )

    scheduler_data = data.get("scheduler", {}) or {}
    scheduler = SchedulerConfig(
        interval_minutes=int(scheduler_data.get("interval_minutes", 3)),
        concurrency=max(1, int(scheduler_data.get("concurrency", 10))),
    )

    default_data = data.get("default", {}) or {}
    default = DefaultConfig(
        retry_count=int(default_data.get("retry_count", 3)),
        retry_interval_minutes=int(default_data.get("retry_interval_minutes", 5)),
        timeout_seconds=int(default_data.get("timeout_seconds", 10)),
    )

    b24_data = data.get("bitrix24", {}) or {}
    b24_defaults = Bitrix24Config()
    bitrix24 = Bitrix24Config(
        enabled=bool(b24_data.get("enabled", False)),
        domain=str(b24_data.get("domain", "") or "").rstrip("/"),
        webhook_url=str(b24_data.get("webhook_url", "") or "").strip(),
        client_id=str(b24_data.get("client_id") or b24_defaults.client_id),
        client_secret=str(b24_data.get("client_secret", "") or ""),
        handler_base=str(b24_data.get("handler_base", "") or "").rstrip("/"),
        bot_code=str(b24_data.get("bot_code") or b24_defaults.bot_code),
        bot_name=str(b24_data.get("bot_name") or b24_defaults.bot_name),
        chat_title=str(b24_data.get("chat_title") or b24_defaults.chat_title),
        chat_dialog_id=str(b24_data.get("chat_dialog_id", "") or ""),
        admin_users=_int_list(b24_data.get("admin_users", [])),
        application_token=str(b24_data.get("application_token", "") or "").strip(),
        allowed_ips=[str(x).strip() for x in (b24_data.get("allowed_ips", []) or []) if str(x).strip()],
    )

    ssl_data = data.get("ssl", {}) or {}
    ssl_defaults = SslConfig()
    ssl_cfg = SslConfig(
        expiry_warn_days=int(ssl_data.get("expiry_warn_days", ssl_defaults.expiry_warn_days)),
        remind_every_hours=int(ssl_data.get("remind_every_hours", ssl_defaults.remind_every_hours)),
        auto_renew_issuers=list(ssl_data.get("auto_renew_issuers", ssl_defaults.auto_renew_issuers) or []),
    )

    sites: List[SiteConfig] = []
    seen = set()
    for site_data in data.get("sites", []) or []:
        site = SiteConfig(
            id=str(site_data.get("id", "") or ""),
            name=str(site_data.get("name", "") or ""),
            url=str(site_data.get("url", "") or ""),
            support_level=str(site_data.get("support_level", "none") or "none"),
            check_ssl=bool(site_data.get("check_ssl", True)),
            check_http_code=bool(site_data.get("check_http_code", True)),
            expected_code=int(site_data.get("expected_code", 200)),
            keywords=list(site_data.get("keywords", []) or []),
            notify_users=_int_list(site_data.get("notify_users", [])),
            notify_b24_users=_int_list(site_data.get("notify_b24_users", [])),
        )
        if not site.id or not site.url:
            raise ValueError(f"У сайта нет id или url: {site_data}")
        if site.id in seen:
            raise ValueError(f"Дублируется id сайта: {site.id}")
        seen.add(site.id)
        sites.append(site)

    return Config(
        telegram=telegram,
        scheduler=scheduler,
        default=default,
        sites=sites,
        bitrix24=bitrix24,
        ssl=ssl_cfg,
        quiet=bool(data.get("quiet", False)),
    )


def get_site_by_id(config: Config, site_id: str) -> Optional[SiteConfig]:
    """Возвращает конфигурацию сайта по его ID или None."""
    for site in config.sites:
        if site.id == site_id:
            return site
    return None


def get_sites_for_user(config: Config, user_id: int) -> List[SiteConfig]:
    """Возвращает список сайтов, за которыми следит пользователь Telegram."""
    return [site for site in config.sites if user_id in site.notify_users]


def save_config(config: Config, config_path: str = "config.yaml") -> None:
    """
    Сохраняет конфигурацию в YAML-файл.

    Внимание: секции сериализуются явно. При добавлении нового поля в dataclass
    его нужно вписать и сюда, иначе первое же сохранение через бота сотрёт ключ.
    """
    data = {
        "telegram": {
            "bot_token": config.telegram.bot_token,
            "admin_ids": config.telegram.admin_ids,
            "use_webhook": config.telegram.use_webhook,
            "webhook_url": config.telegram.webhook_url,
            "webhook_path": config.telegram.webhook_path,
            "webhook_host": config.telegram.webhook_host,
            "webhook_port": config.telegram.webhook_port,
            "proxy": config.telegram.proxy,
        },
        "scheduler": {
            "interval_minutes": config.scheduler.interval_minutes,
            "concurrency": config.scheduler.concurrency,
        },
        "default": {
            "retry_count": config.default.retry_count,
            "retry_interval_minutes": config.default.retry_interval_minutes,
            "timeout_seconds": config.default.timeout_seconds,
        },
        "bitrix24": {
            "enabled": config.bitrix24.enabled,
            "domain": config.bitrix24.domain,
            "webhook_url": config.bitrix24.webhook_url,
            "client_id": config.bitrix24.client_id,
            "client_secret": config.bitrix24.client_secret,
            "handler_base": config.bitrix24.handler_base,
            "bot_code": config.bitrix24.bot_code,
            "bot_name": config.bitrix24.bot_name,
            "chat_title": config.bitrix24.chat_title,
            "chat_dialog_id": config.bitrix24.chat_dialog_id,
            "admin_users": config.bitrix24.admin_users,
            "application_token": config.bitrix24.application_token,
            "allowed_ips": config.bitrix24.allowed_ips,
        },
        "ssl": {
            "expiry_warn_days": config.ssl.expiry_warn_days,
            "remind_every_hours": config.ssl.remind_every_hours,
            "auto_renew_issuers": config.ssl.auto_renew_issuers,
        },
        "quiet": config.quiet,
        "sites": [],
    }

    for site in config.sites:
        data["sites"].append({
            "id": site.id,
            "name": site.name,
            "url": site.url,
            "support_level": site.support_level,
            "check_ssl": site.check_ssl,
            "check_http_code": site.check_http_code,
            "expected_code": site.expected_code,
            "keywords": site.keywords,
            "notify_users": site.notify_users,
            "notify_b24_users": site.notify_b24_users,
        })

    # Конфиг содержит секреты: временный файл создаётся с правами 0600, режим исходного файла сохраняется
    tmp_path = config_path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(tmp_path, stat.S_IMODE(os.stat(config_path).st_mode))
    except OSError:
        pass
    os.replace(tmp_path, config_path)


def add_site(config: Config, site: SiteConfig, config_path: str = "config.yaml") -> bool:
    """Добавляет новый сайт в конфигурацию. False, если id занят."""
    if get_site_by_id(config, site.id):
        return False

    config.sites.append(site)
    save_config(config, config_path)
    log_change(
        "добавлен сайт", f"{site.id} ({site.url})",
        f"название: {site.name}; уровень: {site.support_level}; ssl: {'да' if site.check_ssl else 'нет'}; "
        f"код: {site.expected_code if site.check_http_code else 'не проверяется'}; ключевые слова: {site.keywords or 'нет'}",
        config_path
    )
    return True


def remove_site(config: Config, site_id: str, config_path: str = "config.yaml") -> bool:
    """Удаляет сайт из конфигурации. False, если сайт не найден."""
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    config.sites.remove(site)
    save_config(config, config_path)
    log_change("удалён сайт", f"{site.id} ({site.url})", f"название: {site.name}", config_path)
    return True


def update_site(config: Config, site_id: str, config_path: str = "config.yaml", **kwargs) -> bool:
    """Обновляет поля сайта (name, url, support_level, check_ssl, ...). False, если сайт не найден."""
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    allowed_fields = {
        "name", "url", "support_level", "check_ssl", "check_http_code",
        "expected_code", "keywords", "notify_users", "notify_b24_users"
    }

    changes = []
    for key, value in kwargs.items():
        if key in allowed_fields and hasattr(site, key):
            old = getattr(site, key)
            if old != value:
                changes.append(f"{key}: было {old!r}, стало {value!r}")
            setattr(site, key, value)

    save_config(config, config_path)
    if changes:
        log_change("изменён сайт", f"{site.id} ({site.url})", "; ".join(changes), config_path)
    return True


def add_notify_user(config: Config, site_id: str, user_id: int, config_path: str = "config.yaml") -> bool:
    """Добавляет пользователя Telegram в список уведомлений сайта."""
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    if user_id not in site.notify_users:
        site.notify_users.append(user_id)
        save_config(config, config_path)
        log_change("добавлен получатель уведомлений", f"{site.id} ({site.url})", f"Telegram {user_id}", config_path)
    return True


def remove_notify_user(config: Config, site_id: str, user_id: int, config_path: str = "config.yaml") -> bool:
    """Удаляет пользователя Telegram из списка уведомлений сайта."""
    site = get_site_by_id(config, site_id)
    if not site:
        return False

    if user_id in site.notify_users:
        site.notify_users.remove(user_id)
        save_config(config, config_path)
        log_change("удалён получатель уведомлений", f"{site.id} ({site.url})", f"Telegram {user_id}", config_path)
    return True


def add_admin(config: Config, user_id: int, config_path: str = "config.yaml") -> bool:
    """Добавляет администратора Telegram."""
    if user_id in config.telegram.admin_ids:
        return False

    config.telegram.admin_ids.append(user_id)
    save_config(config, config_path)
    log_change("добавлен администратор", f"Telegram {user_id}", config_path=config_path)
    return True


def remove_admin(config: Config, user_id: int, config_path: str = "config.yaml") -> bool:
    """Удаляет администратора Telegram (последнего удалить нельзя)."""
    if user_id not in config.telegram.admin_ids:
        return False

    if len(config.telegram.admin_ids) <= 1:
        return False

    config.telegram.admin_ids.remove(user_id)
    save_config(config, config_path)
    log_change("удалён администратор", f"Telegram {user_id}", config_path=config_path)
    return True
