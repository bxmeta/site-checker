"""
Канал Bitrix24: уведомления мониторинга в общий чат портала и команды бота.

Схема работы (коробка Bitrix24, OAuth-приложение не нужно):
- все вызовы идут через входящий вебхук портала со scope imbot (bitrix24.webhook_url);
- для методов imbot.* через вебхук обязателен строковый параметр CLIENT_ID;
- состояние (bot_id, chat_dialog_id, application_token, id команд) хранится в b24_auth.json;
- события бота (команды в чате) Bitrix24 присылает POST-ом на {handler_base}/b24/events;
- при каждом старте EVENT_HANDLER бота и команд обновляется, так что смена адреса
  сервера не требует перерегистрации бота.

Команды в чате: /status, /check [id], /mute id, /unmute id, /add url, /del id, /help.
"""
import asyncio
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from .changelog import set_actor, reset_actor, b24_actor
from .checker import CheckResult
from .config_loader import Config, SiteConfig, get_site_by_id, add_site, remove_site
from .database import Database
from .messages import format_down_message, format_ssl_expiring_message
from .notifier import format_duration
from .ssl_checker import SSLCheckResult
from .time_utils import format_for_message

logger = logging.getLogger("site_monitor")

# Команды бота: имя -> (описание, подсказка параметров)
COMMANDS = {
    "status": ("Статус всех сайтов", ""),
    "check": ("Проверить все сайты сейчас (или один: /check id)", "[id сайта]"),
    "mute": ("Не напоминать о падении сайта до восстановления", "id сайта"),
    "unmute": ("Снова напоминать о падении сайта", "id сайта"),
    "add": ("Добавить сайт в мониторинг (админ)", "https://site.ru"),
    "del": ("Удалить сайт из мониторинга (админ)", "id сайта"),
    "help": ("Справка по командам", ""),
}


def _bb_bold(text: str) -> str:
    return f"[B]{text}[/B]"


class B24Error(Exception):
    """Ошибка REST Bitrix24."""


# ==================== Хранилище состояния бота ====================

class B24AuthStorage:
    """b24_auth.json: bot_id, chat_dialog_id, application_token, id зарегистрированных команд."""

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f) or {}
        except FileNotFoundError:
            self.data = {}
        except Exception as e:
            logger.error(f"B24: не удалось прочитать {self.path}: {e}")
            self.data = {}

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"B24: не удалось записать {self.path}: {e}")

    @property
    def bot_id(self) -> Optional[int]:
        value = self.data.get("bot_id")
        return int(value) if value else None

    @bot_id.setter
    def bot_id(self, value: Optional[int]) -> None:
        self.data["bot_id"] = int(value) if value else None
        self.save()

    @property
    def chat_dialog_id(self) -> str:
        return str(self.data.get("chat_dialog_id") or "")

    @chat_dialog_id.setter
    def chat_dialog_id(self, value: str) -> None:
        self.data["chat_dialog_id"] = value or ""
        self.save()

    @property
    def application_token(self) -> str:
        return str(self.data.get("application_token") or "")

    @application_token.setter
    def application_token(self, value: str) -> None:
        self.data["application_token"] = value or ""
        self.save()

    @property
    def commands(self) -> Dict[str, int]:
        return dict(self.data.get("commands") or {})

    def set_command(self, name: str, command_id: int) -> None:
        cmds = self.commands
        cmds[name] = int(command_id)
        self.data["commands"] = cmds
        self.save()

    @property
    def installed(self) -> bool:
        return bool(self.bot_id)

    def reset(self) -> None:
        self.data = {}
        self.save()


# ==================== REST-клиент ====================

class B24Client:
    """Вызовы REST через входящий вебхук. Для imbot.* автоматически подставляет CLIENT_ID."""

    def __init__(self, webhook_url: str, client_id: str, timeout_seconds: int = 20):
        self.base = webhook_url.rstrip("/") + "/"
        self.client_id = client_id
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def call(self, method: str, **params) -> Any:
        if method.startswith("imbot.") and "CLIENT_ID" not in params:
            params["CLIENT_ID"] = self.client_id
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(self.base + method, json=params) as response:
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        text = await response.text()
                        raise B24Error(f"{method}: HTTP {response.status}, не JSON: {text[:200]}")
        except B24Error:
            raise
        except Exception as e:
            raise B24Error(f"{method}: {type(e).__name__}: {e}") from e

        if isinstance(data, dict) and data.get("error"):
            raise B24Error(f"{method}: {data.get('error')}: {data.get('error_description', '')}")
        return data.get("result") if isinstance(data, dict) else data


# ==================== Нотификатор ====================

class Bitrix24Notifier:
    """
    Отправка уведомлений мониторинга в Bitrix24.

    Получатели: общий чат (chat_dialog_id) и личные диалоги пользователей из
    site.notify_b24_users. Заглушки (mute) для Bitrix24 хранятся в той же таблице
    user_mutes, что и Telegram, но с отрицательным user_id, чтобы не пересекаться.
    """

    def __init__(self, config: Config, database: Database, storage: B24AuthStorage, client: B24Client):
        self.config = config
        self.b24 = config.bitrix24
        self.database = database
        self.storage = storage
        self.client = client

    # --- Адресаты ---

    def _main_dialog(self) -> Optional[str]:
        return self.storage.chat_dialog_id or self.b24.chat_dialog_id or None

    @staticmethod
    def mute_user_id(b24_user_id: int) -> int:
        """ID пользователя Bitrix24 в таблице заглушек (отрицательный, чтобы не путать с Telegram)."""
        return -abs(int(b24_user_id))

    def _targets(self, site: SiteConfig) -> List[str]:
        targets: List[str] = []
        main = self._main_dialog()
        if main:
            targets.append(main)
        for user_id in site.notify_b24_users:
            targets.append(str(user_id))
        return list(dict.fromkeys(targets))

    def _is_muted_target(self, target: str, muted: List[int]) -> bool:
        b24_muted = [u for u in muted if u < 0]
        if not b24_muted:
            return False
        if target.startswith("chat"):
            # Общий чат: достаточно одной заглушки из Bitrix24
            return True
        try:
            return self.mute_user_id(int(target)) in b24_muted
        except ValueError:
            return False

    # --- Отправка ---

    def _down_keyboard(self, site_id: str) -> List[Dict[str, Any]]:
        return [
            {"TEXT": "🔇 Заглушить", "COMMAND": "mute", "COMMAND_PARAMS": site_id,
             "BG_COLOR": "#9aa3ad", "TEXT_COLOR": "#fff", "DISPLAY": "LINE"},
            {"TEXT": "🔄 Проверить", "COMMAND": "check", "COMMAND_PARAMS": site_id,
             "BG_COLOR": "#2fc6f6", "TEXT_COLOR": "#fff", "DISPLAY": "LINE"},
        ]

    async def send_message(self, dialog_id: str, text: str, keyboard: Optional[List[Dict[str, Any]]] = None) -> bool:
        if not self.storage.bot_id:
            logger.warning("B24: бот не зарегистрирован, сообщение не отправлено")
            return False
        params: Dict[str, Any] = {
            "BOT_ID": self.storage.bot_id,
            "DIALOG_ID": str(dialog_id),
            "MESSAGE": text,
        }
        if keyboard:
            params["KEYBOARD"] = keyboard
        try:
            await self.client.call("imbot.message.add", **params)
            return True
        except B24Error as e:
            logger.error(f"B24: ошибка отправки в {dialog_id}: {e}")
            return False

    async def _broadcast(self, site: SiteConfig, text: str,
                         keyboard: Optional[List[Dict[str, Any]]] = None,
                         skip_muted: bool = False) -> None:
        muted = self.database.get_muted_users(site.id) if skip_muted else []
        for target in self._targets(site):
            if skip_muted and self._is_muted_target(target, muted):
                logger.debug(f"[{site.id}] B24: пропуск напоминания для {target} (заглушено)")
                continue
            if await self.send_message(target, text, keyboard):
                logger.info(f"[{site.id}] B24: уведомление отправлено в {target}")

    async def notify_site_down(self, site: SiteConfig, check_result: CheckResult,
                               user_ids: Optional[List[int]] = None) -> None:
        text = format_down_message(site, check_result, _bb_bold)
        await self._broadcast(site, text, self._down_keyboard(site.id))

    async def notify_site_up(self, site: SiteConfig, check_result: CheckResult,
                             user_ids: Optional[List[int]] = None,
                             downtime_seconds: Optional[int] = None) -> None:
        text = self._format_up_message(site, check_result, downtime_seconds)
        await self._broadcast(site, text)

    async def notify_ssl_expiring(self, site: SiteConfig, ssl_result: SSLCheckResult,
                                  user_ids: Optional[List[int]] = None) -> None:
        text = format_ssl_expiring_message(site, ssl_result, _bb_bold)
        await self._broadcast(site, text)

    async def send_reminder(self, site: SiteConfig, reminder_number: int, downtime_seconds: int,
                            next_interval_minutes: int, user_ids: List[int], muted_users: List[int]) -> None:
        text = (
            f"🔔 [B]Напоминание #{reminder_number}[/B]\n\n"
            f"Сайт {site.name} всё ещё недоступен\n"
            f"URL: {site.url}\n"
            f"Время простоя: {format_duration(downtime_seconds)}\n"
            f"Следующее напоминание через {next_interval_minutes} мин"
        )
        await self._broadcast(site, text, self._down_keyboard(site.id), skip_muted=True)

    async def notify_admins(self, message: str) -> None:
        main = self._main_dialog()
        if main:
            await self.send_message(main, message)

    # --- Форматирование (BB-код Битрикса) ---

    def _format_up_message(self, site: SiteConfig, check_result: CheckResult,
                           downtime_seconds: Optional[int] = None) -> str:
        status_code_str = str(check_result.status_code) if check_result.status_code else "нет"
        lines = [
            "✅ [B]Сайт восстановлен[/B]\n",
            f"Название: {site.name}",
            f"URL: {site.url}",
            f"Поддержка: {site.support_level}",
            f"Код: {status_code_str}",
        ]
        if downtime_seconds is not None and downtime_seconds > 0:
            lines.append(f"Простой: {format_duration(downtime_seconds)}")
        lines.append(f"Время: {format_for_message()}")
        return "\n".join(lines)


# ==================== Разбор событий ====================

_KEY_RE = re.compile(r"\[([^\]]*)\]")


def parse_form(items) -> Dict[str, Any]:
    """
    Превращает плоские ключи формы Bitrix24 (data[PARAMS][DIALOG_ID]=chat1)
    во вложенный словарь {"data": {"PARAMS": {"DIALOG_ID": "chat1"}}}.
    """
    result: Dict[str, Any] = {}
    for key, value in items:
        head = key.split("[", 1)[0]
        parts = [head] + _KEY_RE.findall(key)
        node = result
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if part == "":
                part = str(len(node))
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    break
    return result


def _domain_to_site_id(domain: str) -> str:
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    site_id = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_")
    return site_id or "site"


# ==================== HTTP-обработчики и команды ====================

class B24BotApp:
    """HTTP-обработчики событий Bitrix24, регистрация бота и команды чата."""

    def __init__(self, config: Config, database: Database, storage: B24AuthStorage,
                 client: B24Client, notifier: Bitrix24Notifier, config_path: str = "config.yaml"):
        self.config = config
        self.b24 = config.bitrix24
        self.database = database
        self.storage = storage
        self.client = client
        self.notifier = notifier
        self.config_path = config_path
        self.scheduler = None  # проставляется из main после создания
        self._check_lock = asyncio.Lock()

    def set_scheduler(self, scheduler) -> None:
        self.scheduler = scheduler

    @property
    def handler_url(self) -> str:
        return self.b24.handler_base.rstrip("/") + "/b24/events"

    # --- Регистрация ---

    async def startup(self) -> None:
        """Регистрация/обновление бота при старте. Ошибки не валят приложение."""
        if not self.b24.webhook_url:
            logger.warning("B24: не задан bitrix24.webhook_url, канал выключен")
            return
        try:
            await self._setup_bot()
        except Exception as e:
            logger.error(f"B24: ошибка регистрации бота при старте: {type(e).__name__}: {e}")

    async def _setup_bot(self) -> None:
        handler = self.handler_url
        bot_fields = {
            "EVENT_MESSAGE_ADD": handler,
            "EVENT_WELCOME_MESSAGE": handler,
            "EVENT_BOT_DELETE": handler,
            "PROPERTIES": {
                "NAME": self.b24.bot_name,
                "COLOR": "GREEN",
                "WORK_POSITION": "Мониторинг доступности сайтов",
            },
        }

        if not self.storage.bot_id:
            bot_id = await self.client.call(
                "imbot.register",
                CODE=self.b24.bot_code,
                TYPE="B",
                **bot_fields,
            )
            self.storage.bot_id = int(bot_id)
            logger.info(f"B24: бот зарегистрирован, BOT_ID={bot_id}")
        else:
            try:
                await self.client.call("imbot.update", BOT_ID=self.storage.bot_id, FIELDS=bot_fields)
                logger.info(f"B24: бот {self.storage.bot_id} обновлён, обработчик событий {handler}")
            except B24Error as e:
                logger.error(f"B24: не удалось обновить бота {self.storage.bot_id}: {e}")

        await self._setup_commands(handler)

        if not self._dialog():
            chat_id = await self.client.call(
                "imbot.chat.add",
                BOT_ID=self.storage.bot_id,
                TITLE=self.b24.chat_title,
                TYPE="CHAT",
                USERS=self.b24.admin_users,
            )
            self.storage.chat_dialog_id = f"chat{chat_id}"
            logger.info(f"B24: создан чат {self.storage.chat_dialog_id}")

        if not self.config.quiet:
            await self.notifier.send_message(
                self._dialog(),
                f"🟢 [B]Мониторинг сайтов запущен[/B]\n"
                f"Сайтов под наблюдением: {len(self.config.sites)}. Команды: /help"
            )

    async def _setup_commands(self, handler: str) -> None:
        registered = self.storage.commands
        for name, (title, params) in COMMANDS.items():
            if name in registered:
                try:
                    await self.client.call(
                        "imbot.command.update",
                        COMMAND_ID=registered[name],
                        FIELDS={"EVENT_COMMAND_ADD": handler},
                    )
                except B24Error as e:
                    logger.warning(f"B24: команда /{name} ({registered[name]}) не обновлена: {e}")
                continue
            try:
                command_id = await self.client.call(
                    "imbot.command.register",
                    BOT_ID=self.storage.bot_id,
                    COMMAND=name,
                    COMMON="Y",
                    HIDDEN="N",
                    EXTRANET_SUPPORT="N",
                    LANG=[{"LANGUAGE_ID": "ru", "TITLE": title, "PARAMS": params}],
                    EVENT_COMMAND_ADD=handler,
                )
                self.storage.set_command(name, int(command_id))
                logger.info(f"B24: команда /{name} зарегистрирована (id {command_id})")
            except B24Error as e:
                logger.warning(f"B24: команда /{name} не зарегистрирована: {e}")

    def _dialog(self) -> Optional[str]:
        return self.notifier._main_dialog()

    # --- HTTP ---

    def register_routes(self, app: web.Application) -> None:
        app.router.add_post("/b24/install", self.handle_install)
        app.router.add_post("/b24/events", self.handle_event)
        app.router.add_get("/b24/health", self.handle_health)

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "installed": self.storage.installed,
            "bot_id": self.storage.bot_id,
            "chat_dialog_id": self._dialog(),
            "sites": len(self.config.sites),
            "quiet": self.config.quiet,
        })

    async def handle_install(self, request: web.Request) -> web.Response:
        # В режиме вебхука установка приложения не используется
        return web.Response(text="ok")

    async def handle_event(self, request: web.Request) -> web.Response:
        try:
            form = await request.post()
            payload = parse_form(form.items())
        except Exception as e:
            logger.error(f"B24: не удалось разобрать событие: {e}")
            return web.Response(status=400, text="bad request")

        event = str(payload.get("event") or "").upper()
        auth = payload.get("auth") or {}
        token = str(auth.get("application_token") or "") if isinstance(auth, dict) else ""

        client_ip = self._client_ip(request)
        if self.b24.allowed_ips and client_ip not in self.b24.allowed_ips:
            logger.warning(f"B24: событие {event} с адреса {client_ip} отклонено (не в allowed_ips)")
            return web.Response(status=403, text="forbidden")

        if not self._check_token(token):
            logger.warning(f"B24: событие {event} с адреса {client_ip} с неверным application_token отклонено")
            return web.Response(status=403, text="forbidden")

        data = payload.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        # Отвечаем сразу, обработку делаем в фоне: /check может идти десятки секунд
        asyncio.create_task(self._dispatch(event, data))
        return web.Response(text="ok")

    _LOOPBACK = ("127.0.0.1", "::1")

    def _client_ip(self, request: web.Request) -> str:
        """
        IP отправителя события. Заголовку X-Real-IP верим только если TCP-пир - локальный nginx
        (loopback) или адрес из bitrix24.trusted_proxies; иначе заголовок подделываем снаружи.
        """
        peer = request.remote or ""
        if peer in self._LOOPBACK or peer in self.b24.trusted_proxies:
            return request.headers.get("X-Real-IP") or peer
        return peer

    def _check_token(self, token: str) -> bool:
        """
        Проверка auth[application_token] события.

        Эталон берётся из config.yaml (bitrix24.application_token), иначе из b24_auth.json.
        Если эталона нет вообще, токен запоминается из первого события: при регистрации бота
        через вебхук портал его не возвращает. Окно доверия закрывается списком allowed_ips
        (события принимаются только с IP портала) и первым же принятым событием.
        """
        stored = self.b24.application_token or self.storage.application_token
        if stored:
            return bool(token) and hmac.compare_digest(token, stored)
        if token:
            self.storage.application_token = token
            logger.warning(
                "B24: эталонного application_token не было, запомнен токен из первого события. "
                "Для надёжности скопируйте его из b24_auth.json в config.yaml (bitrix24.application_token)"
            )
            return True
        return False

    # --- Диспетчер событий ---

    async def _dispatch(self, event: str, data: Dict[str, Any]) -> None:
        try:
            params = data.get("PARAMS") or {}
            dialog_id = str(params.get("DIALOG_ID") or "")
            from_user = int(params.get("FROM_USER_ID") or 0)

            if event == "ONIMCOMMANDADD":
                commands = data.get("COMMAND") or {}
                for _, cmd in sorted(commands.items(), key=lambda kv: str(kv[0])):
                    if not isinstance(cmd, dict):
                        continue
                    name = str(cmd.get("COMMAND") or "").lower().lstrip("/")
                    args = str(cmd.get("COMMAND_PARAMS") or "").strip()
                    await self._run_command(name, args, dialog_id, from_user)
            elif event == "ONIMBOTMESSAGEADD":
                message = str(params.get("MESSAGE") or "").strip()
                if message.startswith("/"):
                    parts = message[1:].split(None, 1)
                    name = parts[0].lower()
                    args = parts[1].strip() if len(parts) > 1 else ""
                    await self._run_command(name, args, dialog_id, from_user)
                elif dialog_id and not dialog_id.startswith("chat"):
                    await self._cmd_help(dialog_id, from_user)
            elif event == "ONIMBOTJOINCHAT":
                if dialog_id:
                    await self._cmd_help(dialog_id, from_user)
            elif event == "ONIMBOTDELETE":
                logger.warning("B24: бот удалён на портале, состояние сброшено")
                self.storage.reset()
            else:
                logger.info(f"B24: событие {event} пропущено")
        except Exception as e:
            logger.error(f"B24: ошибка обработки события {event}: {type(e).__name__}: {e}")

    async def _run_command(self, name: str, args: str, dialog_id: str, from_user: int) -> None:
        if not dialog_id:
            return
        logger.info(f"B24: команда /{name} {args} от {from_user} в {dialog_id}")
        if name == "status":
            await self._cmd_status(dialog_id)
        elif name == "check":
            await self._cmd_check(dialog_id, args)
        elif name == "mute":
            await self._cmd_mute(dialog_id, args, from_user, mute=True)
        elif name == "unmute":
            await self._cmd_mute(dialog_id, args, from_user, mute=False)
        elif name == "add":
            token = set_actor(b24_actor(from_user))
            try:
                await self._cmd_add(dialog_id, args, from_user)
            finally:
                reset_actor(token)
        elif name == "del":
            token = set_actor(b24_actor(from_user))
            try:
                await self._cmd_del(dialog_id, args, from_user)
            finally:
                reset_actor(token)
        elif name in ("help", "start"):
            await self._cmd_help(dialog_id, from_user)
        else:
            await self.notifier.send_message(dialog_id, f"Неизвестная команда /{name}. Справка: /help")

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.b24.admin_users

    def _find_site(self, key: str) -> Optional[SiteConfig]:
        """Ищет сайт по id, имени или домену."""
        key = key.strip().lower()
        if not key:
            return None
        site = get_site_by_id(self.config, key)
        if site:
            return site
        for s in self.config.sites:
            host = (urlparse(s.url).hostname or "").lower()
            if key in (s.name.lower(), host, host.replace("www.", "")):
                return s
        return None

    # --- Команды ---

    async def _cmd_status(self, dialog_id: str) -> None:
        if not self.config.sites:
            await self.notifier.send_message(dialog_id, "Список сайтов пуст")
            return
        states = self.database.get_all_states()
        up: List[str] = []
        down: List[str] = []
        for site in self.config.sites:
            state = states.get(site.id)
            if state and state.status == "DOWN":
                downtime = format_duration(self.database.get_downtime_seconds(site.id))
                down.append(f"🔴 {site.name} ({site.id}) - недоступен {downtime}")
            else:
                up.append(f"🟢 {site.name}")
        lines = [f"[B]Статус сайтов[/B] ({len(self.config.sites)}): 🟢 {len(up)}, 🔴 {len(down)}", ""]
        lines.extend(down)
        if down and up:
            lines.append("")
        lines.extend(up)
        lines.append("")
        lines.append(f"Время: {format_for_message()}")
        await self.notifier.send_message(dialog_id, "\n".join(lines))

    async def _cmd_check(self, dialog_id: str, args: str) -> None:
        if self.scheduler is None:
            await self.notifier.send_message(dialog_id, "Планировщик ещё не запущен")
            return
        if args:
            site = self._find_site(args)
            if not site:
                await self.notifier.send_message(dialog_id, f"Сайт \"{args}\" не найден. Список: /status")
                return
            line = await self.scheduler.check_single_site(site.id)
            await self.notifier.send_message(dialog_id, f"[B]Результат проверки[/B]\n{line}")
            return
        if self._check_lock.locked():
            await self.notifier.send_message(dialog_id, "⏳ Проверка уже идёт, подождите")
            return
        async with self._check_lock:
            await self.notifier.send_message(dialog_id, f"⏳ Проверяю {len(self.config.sites)} сайтов...")
            report = await self.scheduler.run_immediate_check()
        await self.notifier.send_message(dialog_id, f"[B]Результаты проверки[/B]\n\n{report}")

    async def _cmd_mute(self, dialog_id: str, args: str, from_user: int, mute: bool) -> None:
        site = self._find_site(args)
        if not site:
            await self.notifier.send_message(dialog_id, f"Укажите id сайта: /{'mute' if mute else 'unmute'} id. Список: /status")
            return
        mute_id = Bitrix24Notifier.mute_user_id(from_user)
        if mute:
            state = self.database.get_state(site.id)
            if state.status != "DOWN":
                await self.notifier.send_message(dialog_id, f"Сайт {site.name} сейчас доступен, глушить нечего")
                return
            self.database.mute_for_user(mute_id, site.id)
            await self.notifier.send_message(
                dialog_id,
                f"🔇 Напоминания по {site.name} отключены до восстановления сайта (/unmute {site.id} - включить)"
            )
        else:
            self.database.unmute_for_user(mute_id, site.id)
            await self.notifier.send_message(dialog_id, f"🔔 Напоминания по {site.name} включены")

    async def _cmd_add(self, dialog_id: str, args: str, from_user: int) -> None:
        if not self._is_admin(from_user):
            await self.notifier.send_message(dialog_id, "❌ Команда доступна только администраторам мониторинга")
            return
        url = args.split()[0] if args else ""
        if not url:
            await self.notifier.send_message(dialog_id, "Укажите адрес: /add https://site.ru")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        if not parsed.hostname:
            await self.notifier.send_message(dialog_id, f"Не похоже на адрес сайта: {args}")
            return
        domain = parsed.hostname
        site_id = _domain_to_site_id(domain)
        if get_site_by_id(self.config, site_id):
            await self.notifier.send_message(dialog_id, f"Сайт {domain} уже в мониторинге ({site_id})")
            return
        site = SiteConfig(id=site_id, name=domain, url=url, support_level="standard")
        add_site(self.config, site, self.config_path)
        line = ""
        if self.scheduler is not None:
            line = await self.scheduler.check_single_site(site.id) or ""
        await self.notifier.send_message(
            dialog_id, f"➕ Сайт добавлен: {site.name} ({site.id})\n{line}".rstrip()
        )

    async def _cmd_del(self, dialog_id: str, args: str, from_user: int) -> None:
        if not self._is_admin(from_user):
            await self.notifier.send_message(dialog_id, "❌ Команда доступна только администраторам мониторинга")
            return
        site = self._find_site(args)
        if not site:
            await self.notifier.send_message(dialog_id, "Укажите id сайта: /del id. Список: /status")
            return
        remove_site(self.config, site.id, self.config_path)
        self.database.clear_mutes_for_site(site.id)
        await self.notifier.send_message(dialog_id, f"➖ Сайт удалён из мониторинга: {site.name} ({site.id})")

    async def _cmd_help(self, dialog_id: str, from_user: int) -> None:
        lines = ["[B]Мониторинг сайтов: команды[/B]", ""]
        for name, (title, params) in COMMANDS.items():
            if name in ("add", "del") and not self._is_admin(from_user):
                continue
            usage = f"/{name} {params}".strip()
            lines.append(f"{usage} - {title}")
        lines.append("")
        lines.append("Уведомления о падении и восстановлении приходят в этот чат автоматически.")
        await self.notifier.send_message(dialog_id, "\n".join(lines))


# ==================== Фабрика ====================

def create_b24(config: Config, database: Database, data_dir: str, config_path: str) -> Optional[B24BotApp]:
    """Создаёт канал Bitrix24, если он включён в конфиге. Иначе None."""
    b24 = config.bitrix24
    if not b24.enabled:
        return None
    if not b24.webhook_url:
        logger.warning("B24: bitrix24.enabled=true, но webhook_url пуст - канал выключен")
        return None
    storage = B24AuthStorage(os.path.join(data_dir, "b24_auth.json"))
    client = B24Client(b24.webhook_url, b24.client_id)
    notifier = Bitrix24Notifier(config, database, storage, client)
    return B24BotApp(config, database, storage, client, notifier, config_path)
