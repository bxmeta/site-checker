"""
Telegram-бот для управления мониторингом.
"""
import asyncio
import logging
import re
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config_loader import (
    Config, SiteConfig, get_sites_for_user, get_site_by_id,
    add_site, remove_site, update_site, add_notify_user, remove_notify_user,
    add_admin, remove_admin
)
from .changelog import set_actor, reset_actor, tg_actor, read_changes
from .database import Database
from .notifier import TelegramNotifier, format_duration
from .scheduler import run_immediate_check, MonitorScheduler
from .time_utils import parse_datetime, now_izhevsk
from .retry_logic import check_with_retry

logger = logging.getLogger("site_monitor")

router = Router()


async def _actor_middleware(handler, event, data):
    """Запоминает автора действия для журнала изменений (changes.log)."""
    user = getattr(event, "from_user", None)
    token = set_actor(tg_actor(user)) if user else None
    try:
        return await handler(event, data)
    finally:
        if token is not None:
            reset_actor(token)


router.message.outer_middleware(_actor_middleware)
router.callback_query.outer_middleware(_actor_middleware)

_config: Optional[Config] = None
_database: Optional[Database] = None
_notifier: Optional[TelegramNotifier] = None
_scheduler: Optional[MonitorScheduler] = None
_config_path: str = "config.yaml"


class AddSiteStates(StatesGroup):
    """Состояния для добавления сайта."""
    waiting_for_url = State()
    waiting_for_support_level = State()
    waiting_for_keywords = State()
    waiting_for_notify_users = State()


class EditSiteStates(StatesGroup):
    """Состояния для редактирования сайта."""
    waiting_for_field = State()
    waiting_for_value = State()


def _is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return _config is not None and user_id in _config.telegram.admin_ids


async def _check_new_site(site: SiteConfig) -> str:
    """
    Немедленно проверяет новый сайт и возвращает результат.
    Если сайт недоступен — сразу уведомляет всех получателей.
    """
    result = await check_with_retry(site, _config.default, _config.ssl)

    if result.success:
        # Всё ок - записываем успешное состояние
        _database.update_on_success(site.id)
        return f"✅ Проверка пройдена (код {result.status_code})"
    else:
        # Сайт недоступен - сразу помечаем DOWN и уведомляем
        # Используем retry_count=1 чтобы сразу перейти в DOWN
        status_changed = _database.update_on_failure(
            site.id,
            retry_count=1,  # Сразу DOWN без ожидания retry
            error_type=result.error_type,
            error_message=result.error
        )
        if status_changed:
            if _scheduler is not None:
                await _scheduler.attach_diagnostics(site, result)
            await _notifier.notify_site_down(site, result)
        return f"❌ Проблема: {result.error}"


def _extract_domain(url: str) -> str:
    """Извлекает домен из URL."""
    # Убираем протокол
    domain = url.replace("https://", "").replace("http://", "")
    # Убираем путь и параметры
    domain = domain.split("/")[0].split("?")[0].split("#")[0]
    # Убираем www.
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _generate_site_id(domain: str) -> str:
    """Генерирует ID сайта из домена (кириллические домены через Punycode)."""
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    # Заменяем точки и дефисы на подчёркивания
    site_id = domain.replace(".", "_").replace("-", "_")
    # Убираем лишние символы
    site_id = re.sub(r"[^a-zA-Z0-9_]", "", site_id)
    return site_id


def _sites_list_keyboard(page: int = 0, items_per_page: int = 5) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру со списком сайтов."""
    if not _config:
        return InlineKeyboardMarkup(inline_keyboard=[])

    sites = _config.sites
    total_pages = max(1, (len(sites) + items_per_page - 1) // items_per_page)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(sites))

    buttons = []
    for site in sites[start_idx:end_idx]:
        state = _database.get_state(site.id) if _database else None
        status_emoji = "🟢" if (state and state.status == "UP") else "🔴"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_emoji} {site.name}",
                callback_data=f"site_info:{site.id}"
            )
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"sites_page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"sites_page:{page + 1}"))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(text="➕ Добавить сайт", callback_data="add_site")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _site_info_keyboard(site_id: str, user_id: int = 0) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для управления сайтом."""
    buttons = [
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_site:{site_id}"),
            InlineKeyboardButton(text="🔍 Проверить", callback_data=f"check_site:{site_id}")
        ],
        [
            InlineKeyboardButton(text="👥 Подписчики", callback_data=f"site_users:{site_id}"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"site_stats:{site_id}")
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_site:{site_id}")
        ],
    ]

    # Добавляем кнопку mute/unmute если сайт DOWN
    if _database:
        state = _database.get_state(site_id)
        if state.status == "DOWN" and user_id:
            is_muted = _database.is_muted(user_id, site_id)
            if is_muted:
                buttons.append([
                    InlineKeyboardButton(
                        text="🔔 Включить напоминания",
                        callback_data=f"unmute_site:{site_id}"
                    )
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(
                        text="🔇 Заглушить напоминания",
                        callback_data=f"mute_site:{site_id}"
                    )
                ])

    buttons.append([InlineKeyboardButton(text="◀️ Назад к списку", callback_data="sites_list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _edit_site_keyboard(site_id: str) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для редактирования полей сайта."""
    buttons = [
        [
            InlineKeyboardButton(text="📝 Имя", callback_data=f"edit_field:{site_id}:name"),
            InlineKeyboardButton(text="🔗 URL", callback_data=f"edit_field:{site_id}:url")
        ],
        [
            InlineKeyboardButton(text="⭐ Уровень", callback_data=f"edit_field:{site_id}:support_level"),
            InlineKeyboardButton(text="📊 HTTP код", callback_data=f"edit_field:{site_id}:expected_code")
        ],
        [
            InlineKeyboardButton(text="🔐 SSL", callback_data=f"toggle_ssl:{site_id}"),
            InlineKeyboardButton(text="📡 HTTP", callback_data=f"toggle_http:{site_id}")
        ],
        [
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data=f"edit_keywords:{site_id}")
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"site_info:{site_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _support_level_keyboard(site_id: str = "") -> InlineKeyboardMarkup:
    """Создаёт клавиатуру выбора уровня поддержки."""
    prefix = f"set_support:{site_id}:" if site_id else "new_support:"
    buttons = [
        [
            InlineKeyboardButton(text="🟡 Стандарт", callback_data=f"{prefix}standard"),
            InlineKeyboardButton(text="🟢 Премиум", callback_data=f"{prefix}premium")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _confirm_delete_keyboard(site_id: str) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру подтверждения удаления."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete:{site_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"site_info:{site_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _site_users_keyboard(site_id: str) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру управления подписчиками сайта."""
    site = get_site_by_id(_config, site_id) if _config else None
    buttons = []

    if site and site.notify_users:
        for user_id in site.notify_users:
            buttons.append([
                InlineKeyboardButton(
                    text=f"👤 {user_id}",
                    callback_data="noop"
                ),
                InlineKeyboardButton(
                    text="❌",
                    callback_data=f"remove_user:{site_id}:{user_id}"
                )
            ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"add_user:{site_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"site_info:{site_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _keywords_keyboard(site_id: str) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру управления ключевыми словами."""
    site = get_site_by_id(_config, site_id) if _config else None
    buttons = []

    if site and site.keywords:
        for kw in site.keywords:
            buttons.append([
                InlineKeyboardButton(text=f"🔑 {kw}", callback_data="noop"),
                InlineKeyboardButton(text="❌", callback_data=f"remove_kw:{site_id}:{kw}")
            ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"add_keyword:{site_id}")])
    if site and site.keywords:
        buttons.append([InlineKeyboardButton(text="🗑 Очистить все", callback_data=f"clear_keywords:{site_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"edit_site:{site_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаёт главное меню с кнопками."""
    buttons = [
        [InlineKeyboardButton(text="🆔 Мой ID", callback_data="menu_myid")],
        [InlineKeyboardButton(text="📋 Мои сайты", callback_data="menu_my_sites")],
    ]

    # Добавляем кнопку "Мои заглушки" если есть
    if _database:
        muted = _database.get_user_mutes(user_id)
        if muted:
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔇 Заглушенные ({len(muted)})",
                    callback_data="menu_muted"
                )
            ])

    if _is_admin(user_id):
        buttons.extend([
            [InlineKeyboardButton(text="📊 Статус всех сайтов", callback_data="menu_status_all")],
            [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="menu_check_now")],
            [InlineKeyboardButton(text="⚙️ Управление сайтами", callback_data="sites_list")],
            [InlineKeyboardButton(text="➕ Добавить сайт", callback_data="add_site")],
            [InlineKeyboardButton(text="👑 Администраторы", callback_data="menu_admins")],
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== Команды ====================

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Обработчик команды /start."""
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name or "Пользователь"

    # Регистрируем пользователя в базе
    if _database:
        is_new = _database.register_user(user_id, username, full_name)
        if is_new:
            logger.info(f"Новый пользователь зарегистрирован: {user_id} (@{username})")

    role = "👑 Администратор" if _is_admin(user_id) else "👤 Пользователь"

    await message.answer(
        f"👋 Привет, {full_name}!\n\n"
        f"🆔 Ваш ID: <code>{user_id}</code>\n"
        f"🔑 Роль: {role}\n\n"
        f"Выберите действие:",
        parse_mode="HTML",
        reply_markup=_main_menu_keyboard(user_id)
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """Обработчик команды /myid."""
    user_id = message.from_user.id
    await message.answer(
        f"🆔 Ваш Telegram ID: <code>{user_id}</code>",
        parse_mode="HTML"
    )


@router.message(Command("my_sites"))
async def cmd_my_sites(message: Message) -> None:
    """Обработчик команды /my_sites."""
    if _config is None:
        await message.answer("❌ Конфигурация не загружена")
        return

    user_id = message.from_user.id

    # Админы видят все сайты, остальные — только свои
    if _is_admin(user_id):
        sites = _config.sites
        title = "📋 <b>Все сайты:</b>\n"
    else:
        sites = get_sites_for_user(_config, user_id)
        title = "📋 <b>Ваши сайты:</b>\n"

    if not sites:
        await message.answer(
            "📋 Вы не подписаны на уведомления ни одного сайта.\n\n"
            "Попросите администратора добавить ваш ID в подписчики."
        )
        return

    lines = [title]
    for site in sites:
        state = _database.get_state(site.id) if _database else None
        status_emoji = "🟢" if (state and state.status == "UP") else "🔴"
        status_text = state.status if state else "N/A"

        lines.append(
            f"{status_emoji} <b>{site.name}</b>\n"
            f"   URL: {site.url}\n"
            f"   Статус: {status_text}\n"
            f"   Поддержка: {site.support_level}\n"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("status_all"))
async def cmd_status_all(message: Message) -> None:
    """Обработчик команды /status_all (только админы)."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None or _database is None:
        await message.answer("❌ Конфигурация или состояние не загружены")
        return

    lines = ["📊 <b>Статус всех сайтов:</b>\n"]

    for site in _config.sites:
        state = _database.get_state(site.id)
        status_emoji = "🟢" if state.status == "UP" else "🔴"

        lines.append(
            f"{status_emoji} <b>{site.name}</b> ({site.id})\n"
            f"   URL: {site.url}\n"
            f"   Статус: {state.status}\n"
            f"   Неудачных проверок подряд: {state.fail_streak}\n"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("check_now"))
async def cmd_check_now(message: Message) -> None:
    """Обработчик команды /check_now (только админы)."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None or _database is None or _notifier is None:
        await message.answer("❌ Система не полностью инициализирована")
        return

    await message.answer("⏳ Запускаю проверку всех сайтов...")

    try:
        report = await run_immediate_check(_config, _database, _notifier)
        await message.answer(
            f"📋 <b>Результаты проверки:</b>\n\n{report}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при мгновенной проверке: {e}")
        await message.answer(f"❌ Ошибка при проверке: {e}")


@router.message(Command("sites"))
async def cmd_sites(message: Message) -> None:
    """Обработчик команды /sites (только админы)."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None:
        await message.answer("❌ Конфигурация не загружена")
        return

    await message.answer(
        "📋 <b>Управление сайтами</b>\n\n"
        f"Всего сайтов: {len(_config.sites)}",
        parse_mode="HTML",
        reply_markup=_sites_list_keyboard()
    )


@router.message(Command("changes"))
async def cmd_changes(message: Message) -> None:
    """Обработчик команды /changes: журнал изменений конфигурации (только админы)."""
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    entries = read_changes(20, _config_path)
    if not entries:
        await message.answer("📓 Журнал изменений пуст")
        return

    body = "\n".join(
        e.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for e in entries
    )
    text = f"📓 <b>Журнал изменений</b> (последние {len(entries)})\n\n<code>{body}</code>"
    if len(text) > 4000:
        text = text[:3990] + "…</code>"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Обработчик команды /stats (только админы)."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None or _database is None:
        await message.answer("❌ Система не инициализирована")
        return

    # Показываем список сайтов для выбора
    buttons = []
    for site in _config.sites:
        buttons.append([
            InlineKeyboardButton(
                text=f"📊 {site.name}",
                callback_data=f"site_stats:{site.id}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")])

    await message.answer(
        "📊 <b>Статистика сайтов</b>\n\n"
        "Выберите сайт для просмотра статистики:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.message(Command("muted"))
async def cmd_muted(message: Message) -> None:
    """Обработчик команды /muted - список заглушенных сайтов."""
    user_id = message.from_user.id

    if _database is None or _config is None:
        await message.answer("❌ Система не инициализирована")
        return

    muted_sites = _database.get_user_mutes(user_id)

    if not muted_sites:
        await message.answer("🔔 У вас нет заглушенных сайтов.")
        return

    lines = ["🔇 <b>Заглушенные сайты:</b>\n"]
    buttons = []

    for site_id in muted_sites:
        site = get_site_by_id(_config, site_id)
        if site:
            lines.append(f"• {site.name}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔔 Включить {site.name}",
                    callback_data=f"unmute_site:{site_id}"
                )
            ])

    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    """Показывает список администраторов."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None:
        await message.answer("❌ Конфигурация не загружена")
        return

    lines = ["👑 <b>Администраторы:</b>\n"]
    for admin_id in _config.telegram.admin_ids:
        # Попробуем получить информацию из базы
        user_info = _database.get_user(admin_id) if _database else None
        if user_info:
            name = user_info.full_name or user_info.username or str(admin_id)
            lines.append(f"• {name} (<code>{admin_id}</code>)")
        else:
            lines.append(f"• <code>{admin_id}</code>")

    lines.append(f"\nВсего: {len(_config.telegram.admin_ids)}")
    lines.append("\n<i>Добавить: /add_admin ID\nУдалить: /remove_admin ID</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("add_admin"))
async def cmd_add_admin(message: Message) -> None:
    """Добавляет нового администратора."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None:
        await message.answer("❌ Конфигурация не загружена")
        return

    # Парсим аргументы
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Укажите Telegram ID пользователя\n\n"
            "Пример: <code>/add_admin 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        new_admin_id = int(args[1].strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return

    if new_admin_id in _config.telegram.admin_ids:
        await message.answer("ℹ️ Этот пользователь уже администратор")
        return

    if add_admin(_config, new_admin_id, _config_path):
        # Получаем имя если есть в базе
        user_info = _database.get_user(new_admin_id) if _database else None
        name = ""
        if user_info:
            name = f" ({user_info.full_name or user_info.username})"

        await message.answer(
            f"✅ Администратор добавлен: <code>{new_admin_id}</code>{name}",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Не удалось добавить администратора")


@router.message(Command("remove_admin"))
async def cmd_remove_admin(message: Message) -> None:
    """Удаляет администратора."""
    user_id = message.from_user.id

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    if _config is None:
        await message.answer("❌ Конфигурация не загружена")
        return

    # Парсим аргументы
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Укажите Telegram ID администратора\n\n"
            "Пример: <code>/remove_admin 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        admin_to_remove = int(args[1].strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return

    if admin_to_remove not in _config.telegram.admin_ids:
        await message.answer("ℹ️ Этот пользователь не является администратором")
        return

    if len(_config.telegram.admin_ids) <= 1:
        await message.answer("❌ Нельзя удалить последнего администратора")
        return

    if remove_admin(_config, admin_to_remove, _config_path):
        await message.answer(
            f"✅ Администратор удалён: <code>{admin_to_remove}</code>",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Не удалось удалить администратора")


# ==================== Callback handlers для меню ====================

@router.callback_query(F.data == "menu_main")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в главное меню."""
    await state.clear()

    user_id = callback.from_user.id
    full_name = callback.from_user.full_name or "Пользователь"
    role = "👑 Администратор" if _is_admin(user_id) else "👤 Пользователь"

    await callback.message.edit_text(
        f"👋 Привет, {full_name}!\n\n"
        f"🆔 Ваш ID: <code>{user_id}</code>\n"
        f"🔑 Роль: {role}\n\n"
        f"Выберите действие:",
        parse_mode="HTML",
        reply_markup=_main_menu_keyboard(user_id)
    )
    await callback.answer()


@router.callback_query(F.data == "menu_myid")
async def callback_menu_myid(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать ID через меню."""
    await state.clear()
    user_id = callback.from_user.id
    await callback.message.edit_text(
        f"🆔 Ваш Telegram ID:\n\n<code>{user_id}</code>\n\n"
        f"Используйте этот ID для добавления в подписчики сайтов.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "menu_my_sites")
async def callback_menu_my_sites(callback: CallbackQuery, state: FSMContext) -> None:
    """Мои сайты через меню."""
    await state.clear()
    if _config is None:
        await callback.answer("❌ Конфигурация не загружена", show_alert=True)
        return

    user_id = callback.from_user.id

    # Админы видят все сайты
    if _is_admin(user_id):
        sites = _config.sites
        title = "📋 <b>Все сайты:</b>\n"
    else:
        sites = get_sites_for_user(_config, user_id)
        title = "📋 <b>Мои сайты:</b>\n"

    if not sites:
        await callback.message.edit_text(
            "📋 <b>Мои сайты</b>\n\n"
            "Вы не подписаны на уведомления ни одного сайта.\n\n"
            "Попросите администратора добавить ваш ID в подписчики.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ])
        )
        await callback.answer()
        return

    lines = [title]
    for site in sites:
        site_state = _database.get_state(site.id) if _database else None
        status_emoji = "🟢" if (site_state and site_state.status == "UP") else "🔴"
        status_text = site_state.status if site_state else "N/A"

        # Проверяем, заглушен ли сайт
        muted_str = ""
        if _database and site_state and site_state.status == "DOWN":
            if _database.is_muted(user_id, site.id):
                muted_str = " 🔇"

        lines.append(
            f"{status_emoji} <b>{site.name}</b>{muted_str}\n"
            f"   Статус: {status_text} | Поддержка: {site.support_level}\n"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "menu_muted")
async def callback_menu_muted(callback: CallbackQuery, state: FSMContext) -> None:
    """Список заглушенных сайтов через меню."""
    await state.clear()
    user_id = callback.from_user.id

    if _database is None or _config is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    muted_sites = _database.get_user_mutes(user_id)

    if not muted_sites:
        await callback.message.edit_text(
            "🔔 У вас нет заглушенных сайтов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ])
        )
        await callback.answer()
        return

    lines = ["🔇 <b>Заглушенные сайты:</b>\n"]
    buttons = []

    for site_id in muted_sites:
        site = get_site_by_id(_config, site_id)
        if site:
            lines.append(f"• {site.name}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔔 Включить {site.name}",
                    callback_data=f"unmute_site:{site_id}"
                )
            ])

    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")])

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data == "menu_admins")
async def callback_menu_admins(callback: CallbackQuery, state: FSMContext) -> None:
    """Управление администраторами через меню."""
    await state.clear()
    user_id = callback.from_user.id

    if not _is_admin(user_id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    if _config is None:
        await callback.answer("❌ Конфигурация не загружена", show_alert=True)
        return

    lines = ["👑 <b>Администраторы:</b>\n"]
    buttons = []

    for admin_id in _config.telegram.admin_ids:
        # Получаем информацию из базы
        user_info = _database.get_user(admin_id) if _database else None
        if user_info:
            name = user_info.full_name or user_info.username or str(admin_id)
            lines.append(f"• {name} (<code>{admin_id}</code>)")
        else:
            lines.append(f"• <code>{admin_id}</code>")

        # Кнопка удаления (кроме себя и если админов больше 1)
        if admin_id != user_id and len(_config.telegram.admin_ids) > 1:
            btn_name = user_info.full_name[:15] if user_info and user_info.full_name else str(admin_id)
            buttons.append([
                InlineKeyboardButton(
                    text=f"❌ Удалить {btn_name}",
                    callback_data=f"remove_admin:{admin_id}"
                )
            ])

    lines.append(f"\nВсего: {len(_config.telegram.admin_ids)}")

    buttons.append([InlineKeyboardButton(text="➕ Добавить админа", callback_data="add_admin_start")])
    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")])

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("remove_admin:"))
async def callback_remove_admin(callback: CallbackQuery) -> None:
    """Удаление администратора через меню."""
    user_id = callback.from_user.id

    if not _is_admin(user_id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    admin_to_remove = int(callback.data.split(":")[1])

    if remove_admin(_config, admin_to_remove, _config_path):
        await callback.answer("✅ Администратор удалён", show_alert=True)
        # Обновляем список
        await callback_menu_admins(callback, None)
    else:
        await callback.answer("❌ Не удалось удалить", show_alert=True)


class AddAdminStates(StatesGroup):
    """Состояния для добавления админа."""
    waiting_for_id = State()


@router.callback_query(F.data == "add_admin_start")
async def callback_add_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления администратора."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    await state.set_state(AddAdminStates.waiting_for_id)

    await callback.message.edit_text(
        "👑 <b>Добавление администратора</b>\n\n"
        "Введите Telegram ID нового администратора:\n\n"
        "<i>Пользователь может узнать свой ID через /myid</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_admins")]
        ])
    )
    await callback.answer()


@router.message(StateFilter(AddAdminStates.waiting_for_id))
async def process_add_admin_id(message: Message, state: FSMContext) -> None:
    """Обработчик ввода ID нового админа."""
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов")
        return

    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте ещё раз:")
        return

    if new_admin_id in _config.telegram.admin_ids:
        await message.answer("ℹ️ Этот пользователь уже администратор")
        await state.clear()
        return

    if add_admin(_config, new_admin_id, _config_path):
        user_info = _database.get_user(new_admin_id) if _database else None
        name = ""
        if user_info:
            name = f" ({user_info.full_name or user_info.username})"

        await message.answer(
            f"✅ Администратор добавлен: <code>{new_admin_id}</code>{name}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👑 К списку админов", callback_data="menu_admins")],
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ])
        )
    else:
        await message.answer("❌ Не удалось добавить администратора")

    await state.clear()


@router.callback_query(F.data == "menu_status_all")
async def callback_menu_status_all(callback: CallbackQuery, state: FSMContext) -> None:
    """Статус всех сайтов через меню."""
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    if _config is None or _database is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    lines = ["📊 <b>Статус всех сайтов:</b>\n"]

    for site in _config.sites:
        site_state = _database.get_state(site.id)
        status_emoji = "🟢" if site_state.status == "UP" else "🔴"

        lines.append(
            f"{status_emoji} <b>{site.name}</b>\n"
            f"   Статус: {site_state.status} | Ошибок подряд: {site_state.fail_streak}\n"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_status_all")],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "menu_check_now")
async def callback_menu_check_now(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверка всех сайтов через меню."""
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    if _config is None or _database is None or _notifier is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    await callback.answer("⏳ Запускаю проверку...")

    try:
        report = await run_immediate_check(_config, _database, _notifier)
        await callback.message.edit_text(
            f"📋 <b>Результаты проверки:</b>\n\n{report}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="menu_check_now")],
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ])
        )
    except Exception as e:
        logger.error(f"Ошибка при мгновенной проверке: {e}")
        await callback.message.answer(f"❌ Ошибка: {e}")


# ==================== Mute/Unmute handlers ====================

@router.callback_query(F.data.startswith("mute_site:"))
async def callback_mute_site(callback: CallbackQuery) -> None:
    """Обработчик заглушения напоминаний."""
    user_id = callback.from_user.id
    site_id = callback.data.split(":")[1]

    if _database is None or _config is None or _notifier is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    site = get_site_by_id(_config, site_id)
    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    # Проверяем, что сайт действительно DOWN
    state = _database.get_state(site_id)
    if state.status != "DOWN":
        await callback.answer("ℹ️ Сайт уже восстановлен", show_alert=True)
        return

    success = _database.mute_for_user(user_id, site_id)

    if success:
        await _notifier.send_mute_confirmation(user_id, site.name, site_id)
        await callback.answer("🔇 Напоминания отключены")
    else:
        await callback.answer("ℹ️ Уже заглушено", show_alert=True)


@router.callback_query(F.data.startswith("unmute_site:"))
async def callback_unmute_site(callback: CallbackQuery) -> None:
    """Обработчик включения напоминаний."""
    user_id = callback.from_user.id
    site_id = callback.data.split(":")[1]

    if _database is None or _config is None or _notifier is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    site = get_site_by_id(_config, site_id)
    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    success = _database.unmute_for_user(user_id, site_id)

    if success:
        await _notifier.send_unmute_confirmation(user_id, site.name)
        await callback.answer("🔔 Напоминания включены")
    else:
        await callback.answer("ℹ️ Не было заглушено", show_alert=True)


@router.callback_query(F.data.startswith("check_now:"))
async def callback_check_now_single(callback: CallbackQuery) -> None:
    """Обработчик мгновенной проверки из уведомления."""
    site_id = callback.data.split(":")[1]

    if _scheduler is None or _config is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    site = get_site_by_id(_config, site_id)
    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    await callback.answer("⏳ Проверяю...")

    result = await _scheduler.check_single_site(site_id)

    if result:
        await callback.message.answer(
            f"🔍 <b>Результат проверки</b>\n\n{result}",
            parse_mode="HTML"
        )
    else:
        await callback.message.answer("❌ Не удалось выполнить проверку")


# ==================== Статистика ====================

@router.callback_query(F.data.startswith("site_stats:"))
async def callback_site_stats(callback: CallbackQuery) -> None:
    """Обработчик просмотра статистики сайта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]

    if _database is None or _config is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    site = get_site_by_id(_config, site_id)
    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    stats = _database.get_site_stats(site_id)

    # Форматируем последний инцидент
    last_incident_str = "—"
    if stats.last_incident_at:
        last_dt = parse_datetime(stats.last_incident_at)
        delta = now_izhevsk() - last_dt
        if delta.days > 0:
            last_incident_str = f"{delta.days} дн. назад"
        elif delta.seconds > 3600:
            last_incident_str = f"{delta.seconds // 3600} ч. назад"
        else:
            last_incident_str = f"{delta.seconds // 60} мин. назад"

    text = (
        f"📊 <b>Статистика сайта {site.name}</b>\n\n"
        f"Uptime за 7 дней: <b>{stats.uptime_7d}%</b>\n"
        f"Uptime за 30 дней: <b>{stats.uptime_30d}%</b>\n"
        f"Инцидентов за 30 дней: <b>{stats.incidents_30d}</b>\n"
        f"Среднее время простоя: <b>{format_duration(stats.avg_downtime_seconds)}</b>\n"
        f"Последний инцидент: <b>{last_incident_str}</b>"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"site_stats:{site_id}")],
            [InlineKeyboardButton(text="◀️ К сайту", callback_data=f"site_info:{site_id}")],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()


# ==================== Управление сайтами ====================

@router.callback_query(F.data == "sites_list")
async def callback_sites_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик возврата к списку сайтов."""
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    await callback.message.edit_text(
        "📋 <b>Управление сайтами</b>\n\n"
        f"Всего сайтов: {len(_config.sites) if _config else 0}",
        parse_mode="HTML",
        reply_markup=_sites_list_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sites_page:"))
async def callback_sites_page(callback: CallbackQuery) -> None:
    """Обработчик переключения страниц списка сайтов."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    page = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=_sites_list_keyboard(page))
    await callback.answer()


@router.callback_query(F.data.startswith("site_info:"))
async def callback_site_info(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик просмотра информации о сайте."""
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    site_state = _database.get_state(site_id) if _database else None
    status_emoji = "🟢" if (site_state and site_state.status == "UP") else "🔴"
    status_text = site_state.status if site_state else "N/A"

    ssl_status = "✅" if site.check_ssl else "❌"
    http_status = "✅" if site.check_http_code else "❌"
    keywords_str = ", ".join(site.keywords) if site.keywords else "—"
    users_str = ", ".join(str(u) for u in site.notify_users) if site.notify_users else "—"

    text = (
        f"{status_emoji} <b>{site.name}</b>\n\n"
        f"🆔 ID: <code>{site.id}</code>\n"
        f"🔗 URL: {site.url}\n"
        f"⭐ Поддержка: {site.support_level}\n"
        f"📊 Статус: {status_text}\n\n"
        f"<b>Настройки проверок:</b>\n"
        f"🔐 SSL: {ssl_status}\n"
        f"📡 HTTP код: {http_status} (ожидается {site.expected_code})\n"
        f"🔑 Ключевые слова: {keywords_str}\n\n"
        f"👥 Подписчики: {users_str}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_site_info_keyboard(site_id, callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("check_site:"))
async def callback_check_site(callback: CallbackQuery) -> None:
    """Обработчик проверки конкретного сайта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    await callback.answer("⏳ Проверяю сайт...")

    from .retry_logic import check_site_single
    result = await check_site_single(site, _config.default, _config.ssl)

    status = "✅ Доступен" if result.success else "❌ Недоступен"
    code_str = f" (код {result.status_code})" if result.status_code else ""
    time_str = f"\n⏱ Время ответа: {result.response_time_ms}ms" if result.response_time_ms else ""
    error_str = f"\n⚠️ Ошибка: {result.error}" if result.error else ""

    await callback.message.answer(
        f"🔍 <b>Результат проверки</b>\n\n"
        f"Сайт: {site.name}\n"
        f"Статус: {status}{code_str}{time_str}{error_str}",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("edit_site:"))
async def callback_edit_site(callback: CallbackQuery) -> None:
    """Обработчик редактирования сайта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"✏️ <b>Редактирование сайта</b>\n\n"
        f"Сайт: {site.name}\n"
        f"ID: {site.id}\n\n"
        f"Выберите поле для редактирования:",
        parse_mode="HTML",
        reply_markup=_edit_site_keyboard(site_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_field:"))
async def callback_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик выбора поля для редактирования."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    parts = callback.data.split(":")
    site_id = parts[1]
    field = parts[2]

    site = get_site_by_id(_config, site_id) if _config else None
    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    field_names = {
        "name": "название",
        "url": "URL",
        "support_level": "уровень поддержки",
        "expected_code": "ожидаемый HTTP код",
        "keywords": "ключевые слова"
    }

    if field == "support_level":
        await callback.message.edit_text(
            f"⭐ <b>Выберите уровень поддержки</b>\n\n"
            f"Текущий: {site.support_level}",
            parse_mode="HTML",
            reply_markup=_support_level_keyboard(site_id)
        )
        await callback.answer()
        return

    await state.update_data(edit_site_id=site_id, edit_field=field)
    await state.set_state(EditSiteStates.waiting_for_value)

    current_value = getattr(site, field, "")
    if field == "keywords":
        current_value = ", ".join(site.keywords) if site.keywords else "—"

    await callback.message.edit_text(
        f"✏️ <b>Редактирование: {field_names.get(field, field)}</b>\n\n"
        f"Текущее значение: {current_value}\n\n"
        f"Введите новое значение:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(EditSiteStates.waiting_for_value))
async def process_edit_value(message: Message, state: FSMContext) -> None:
    """Обработчик ввода нового значения поля."""
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов")
        await state.clear()
        return

    data = await state.get_data()

    # Проверяем, добавляем ли ключевые слова
    add_keyword_site_id = data.get("add_keyword_site_id")
    if add_keyword_site_id:
        site = get_site_by_id(_config, add_keyword_site_id) if _config else None
        if not site:
            await message.answer("❌ Сайт не найден")
            await state.clear()
            return

        new_keywords = [k.strip() for k in message.text.strip().split(",") if k.strip()]
        all_keywords = (site.keywords or []) + new_keywords
        success = update_site(_config, add_keyword_site_id, _config_path, keywords=all_keywords)

        if success:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(
                f"✅ Добавлено: {', '.join(new_keywords)}",
                reply_markup=_keywords_keyboard(add_keyword_site_id)
            )
        else:
            await message.answer("❌ Ошибка добавления")
        await state.clear()
        return

    site_id = data.get("edit_site_id")
    field = data.get("edit_field")

    if not site_id or not field:
        await message.answer("❌ Ошибка редактирования")
        await state.clear()
        return

    value = message.text.strip()

    if field == "expected_code":
        try:
            value = int(value)
        except ValueError:
            await message.answer("❌ Введите числовое значение HTTP кода")
            return

    success = update_site(_config, site_id, _config_path, **{field: value})

    if success:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            f"✅ Поле успешно обновлено!",
            reply_markup=_site_info_keyboard(site_id, message.from_user.id)
        )
    else:
        await message.answer("❌ Не удалось обновить поле")

    await state.clear()


@router.callback_query(F.data.startswith("set_support:"))
async def callback_set_support(callback: CallbackQuery) -> None:
    """Обработчик установки уровня поддержки."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    parts = callback.data.split(":")
    site_id = parts[1]
    level = parts[2]

    success = update_site(_config, site_id, _config_path, support_level=level)

    if success:
        await callback.answer(f"✅ Уровень поддержки: {level}")
        await callback.message.edit_text(
            f"✅ Уровень поддержки обновлён: {level}",
            reply_markup=_site_info_keyboard(site_id, callback.from_user.id)
        )
    else:
        await callback.answer("❌ Ошибка обновления", show_alert=True)


@router.callback_query(F.data.startswith("toggle_ssl:"))
async def callback_toggle_ssl(callback: CallbackQuery) -> None:
    """Обработчик переключения проверки SSL."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    new_value = not site.check_ssl
    success = update_site(_config, site_id, _config_path, check_ssl=new_value)

    if success:
        status = "включена" if new_value else "отключена"
        await callback.answer(f"✅ Проверка SSL {status}")
        await callback.message.edit_reply_markup(reply_markup=_edit_site_keyboard(site_id))
    else:
        await callback.answer("❌ Ошибка обновления", show_alert=True)


@router.callback_query(F.data.startswith("toggle_http:"))
async def callback_toggle_http(callback: CallbackQuery) -> None:
    """Обработчик переключения проверки HTTP кода."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    new_value = not site.check_http_code
    success = update_site(_config, site_id, _config_path, check_http_code=new_value)

    if success:
        status = "включена" if new_value else "отключена"
        await callback.answer(f"✅ Проверка HTTP кода {status}")
        await callback.message.edit_reply_markup(reply_markup=_edit_site_keyboard(site_id))
    else:
        await callback.answer("❌ Ошибка обновления", show_alert=True)


@router.callback_query(F.data.startswith("edit_keywords:"))
async def callback_edit_keywords(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик редактирования ключевых слов."""
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    count = len(site.keywords) if site.keywords else 0
    await callback.message.edit_text(
        f"🔑 <b>Ключевые слова</b>\n\n"
        f"Сайт: {site.name}\n"
        f"Количество: {count}",
        parse_mode="HTML",
        reply_markup=_keywords_keyboard(site_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("remove_kw:"))
async def callback_remove_keyword(callback: CallbackQuery) -> None:
    """Обработчик удаления ключевого слова."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    site_id = parts[1]
    keyword = parts[2]

    site = get_site_by_id(_config, site_id) if _config else None
    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    new_keywords = [kw for kw in site.keywords if kw != keyword]
    success = update_site(_config, site_id, _config_path, keywords=new_keywords)

    if success:
        await callback.answer(f"✅ Удалено: {keyword}")
        count = len(new_keywords)
        await callback.message.edit_text(
            f"🔑 <b>Ключевые слова</b>\n\n"
            f"Сайт: {site.name}\n"
            f"Количество: {count}",
            parse_mode="HTML",
            reply_markup=_keywords_keyboard(site_id)
        )
    else:
        await callback.answer("❌ Ошибка удаления", show_alert=True)


@router.callback_query(F.data.startswith("clear_keywords:"))
async def callback_clear_keywords(callback: CallbackQuery) -> None:
    """Обработчик очистки всех ключевых слов."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    success = update_site(_config, site_id, _config_path, keywords=[])

    if success:
        await callback.answer("✅ Все ключевые слова удалены")
        await callback.message.edit_text(
            f"🔑 <b>Ключевые слова</b>\n\n"
            f"Сайт: {site.name}\n"
            f"Количество: 0",
            parse_mode="HTML",
            reply_markup=_keywords_keyboard(site_id)
        )
    else:
        await callback.answer("❌ Ошибка очистки", show_alert=True)


@router.callback_query(F.data.startswith("add_keyword:"))
async def callback_add_keyword(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик добавления ключевого слова."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    await state.update_data(add_keyword_site_id=site_id)
    await state.set_state(EditSiteStates.waiting_for_value)

    await callback.message.edit_text(
        "🔑 <b>Добавление ключевого слова</b>\n\n"
        "Введите ключевое слово или несколько через запятую:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_keywords:{site_id}")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_site:"))
async def callback_delete_site(callback: CallbackQuery) -> None:
    """Обработчик запроса на удаление сайта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"🗑 <b>Удаление сайта</b>\n\n"
        f"Вы уверены, что хотите удалить сайт?\n\n"
        f"Название: {site.name}\n"
        f"URL: {site.url}",
        parse_mode="HTML",
        reply_markup=_confirm_delete_keyboard(site_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete:"))
async def callback_confirm_delete(callback: CallbackQuery) -> None:
    """Обработчик подтверждения удаления сайта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    success = remove_site(_config, site_id, _config_path)

    if success:
        await callback.answer("✅ Сайт удалён")
        await callback.message.edit_text(
            "✅ Сайт успешно удалён!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К списку сайтов", callback_data="sites_list")]
            ])
        )
    else:
        await callback.answer("❌ Ошибка удаления", show_alert=True)


@router.callback_query(F.data.startswith("site_users:"))
async def callback_site_users(callback: CallbackQuery) -> None:
    """Обработчик просмотра подписчиков сайта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    site = get_site_by_id(_config, site_id) if _config else None

    if not site:
        await callback.answer("❌ Сайт не найден", show_alert=True)
        return

    count = len(site.notify_users) if site.notify_users else 0
    await callback.message.edit_text(
        f"👥 <b>Подписчики сайта</b>\n\n"
        f"Сайт: {site.name}\n"
        f"Количество: {count}",
        parse_mode="HTML",
        reply_markup=_site_users_keyboard(site_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add_user:"))
async def callback_add_user(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик добавления подписчика."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    site_id = callback.data.split(":")[1]
    await state.update_data(add_user_site_id=site_id)
    await state.set_state(EditSiteStates.waiting_for_field)

    await callback.message.edit_text(
        "👤 <b>Добавление подписчика</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(EditSiteStates.waiting_for_field))
async def process_add_user(message: Message, state: FSMContext) -> None:
    """Обработчик ввода ID пользователя."""
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов")
        await state.clear()
        return

    data = await state.get_data()
    site_id = data.get("add_user_site_id")

    if not site_id:
        await message.answer("❌ Ошибка")
        await state.clear()
        return

    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID")
        return

    success = add_notify_user(_config, site_id, user_id, _config_path)

    if success:
        await message.answer(
            f"✅ Пользователь {user_id} добавлен в подписчики!",
            reply_markup=_site_users_keyboard(site_id)
        )
    else:
        await message.answer("❌ Не удалось добавить пользователя")

    await state.clear()


@router.callback_query(F.data.startswith("remove_user:"))
async def callback_remove_user(callback: CallbackQuery) -> None:
    """Обработчик удаления подписчика."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    parts = callback.data.split(":")
    site_id = parts[1]
    user_id = int(parts[2])

    success = remove_notify_user(_config, site_id, user_id, _config_path)

    if success:
        await callback.answer(f"✅ Пользователь {user_id} удалён")
        await callback.message.edit_reply_markup(reply_markup=_site_users_keyboard(site_id))
    else:
        await callback.answer("❌ Ошибка удаления", show_alert=True)


# ==================== Добавление сайта ====================

@router.callback_query(F.data == "add_site")
@router.message(Command("add_site"))
async def cmd_add_site(event, state: FSMContext) -> None:
    """Обработчик начала добавления сайта."""
    if isinstance(event, CallbackQuery):
        user_id = event.from_user.id
        message = event.message
        await event.answer()
    else:
        user_id = event.from_user.id
        message = event

    if not _is_admin(user_id):
        await message.answer("❌ Эта команда доступна только администраторам")
        return

    await state.set_state(AddSiteStates.waiting_for_url)
    await state.update_data(new_site={})

    text = (
        "➕ <b>Добавление нового сайта</b>\n\n"
        "Шаг 1/3: Введите URL сайта\n\n"
        "Пример: <code>example.com</code>"
    )

    if isinstance(event, CallbackQuery):
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")


@router.message(StateFilter(AddSiteStates.waiting_for_url))
async def process_site_url(message: Message, state: FSMContext) -> None:
    """Обработчик ввода URL сайта."""
    url = message.text.strip()

    # Добавляем https:// если не указан протокол
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Извлекаем домен и генерируем ID
    domain = _extract_domain(url)
    site_id = _generate_site_id(domain)

    # Проверяем, не существует ли уже такой сайт
    if get_site_by_id(_config, site_id):
        await message.answer(
            f"❌ Сайт с доменом {domain} уже существует",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["id"] = site_id
    new_site["name"] = domain
    new_site["url"] = url
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_support_level)

    await message.answer(
        f"✅ Сайт: <b>{domain}</b>\n\n"
        "Выберите уровень поддержки:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟡 Стандарт", callback_data="new_support:standard"),
                InlineKeyboardButton(text="🟢 Премиум", callback_data="new_support:premium")
            ]
        ])
    )


@router.callback_query(F.data.startswith("new_support:"), StateFilter(AddSiteStates.waiting_for_support_level))
async def process_new_support(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик выбора уровня поддержки для нового сайта."""
    level = callback.data.split(":")[1]

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["support_level"] = level
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_keywords)

    await callback.message.edit_text(
        f"✅ Уровень поддержки: {level}\n\n"
        "Шаг 2/3: Введите ключевые слова через запятую\n\n"
        "Пример: <code>Welcome, Главная</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="new_skip_keywords")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "new_skip_keywords", StateFilter(AddSiteStates.waiting_for_keywords))
async def callback_skip_keywords(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропуск ключевых слов."""
    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["keywords"] = []
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_notify_users)

    await callback.message.edit_text(
        "✅ Ключевые слова: —\n\n"
        "Шаг 3/3: Введите Telegram ID для уведомлений\n\n"
        "Пример: <code>123456789</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="new_skip_notify")]
        ])
    )
    await callback.answer()


@router.message(StateFilter(AddSiteStates.waiting_for_keywords))
async def process_keywords(message: Message, state: FSMContext) -> None:
    """Обработчик ввода ключевых слов."""
    text = message.text.strip()
    keywords = [k.strip() for k in text.split(",") if k.strip()]

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["keywords"] = keywords
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_notify_users)

    keywords_str = ", ".join(keywords)
    await message.answer(
        f"✅ Ключевые слова: {keywords_str}\n\n"
        "Шаг 3/3: Введите Telegram ID для уведомлений\n\n"
        "Пример: <code>123456789</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="new_skip_notify")]
        ])
    )


@router.callback_query(F.data == "new_skip_notify", StateFilter(AddSiteStates.waiting_for_notify_users))
async def callback_skip_notify(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропуск подписчиков — завершение добавления."""
    data = await state.get_data()
    new_site = data.get("new_site", {})

    site = SiteConfig(
        id=new_site["id"],
        name=new_site["name"],
        url=new_site["url"],
        support_level=new_site.get("support_level", "none"),
        check_ssl=True,
        check_http_code=True,
        expected_code=200,
        keywords=new_site.get("keywords", []),
        notify_users=[]
    )

    if add_site(_config, site, _config_path):
        await callback.message.edit_text(
            f"✅ <b>Сайт добавлен!</b>\n\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Поддержка: {site.support_level}\n"
            f"Ключевые слова: {', '.join(site.keywords) if site.keywords else '—'}\n"
            f"Подписчики: —\n\n"
            f"⏳ Проверяю...",
            parse_mode="HTML"
        )
        # Немедленная проверка
        check_result = await _check_new_site(site)
        await callback.message.edit_text(
            f"✅ <b>Сайт добавлен!</b>\n\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Поддержка: {site.support_level}\n"
            f"Ключевые слова: {', '.join(site.keywords) if site.keywords else '—'}\n"
            f"Подписчики: —\n\n"
            f"<b>Первая проверка:</b> {check_result}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ])
        )
    else:
        await callback.message.edit_text("❌ Ошибка при добавлении сайта")

    await state.clear()
    await callback.answer()


@router.message(StateFilter(AddSiteStates.waiting_for_notify_users))
async def process_notify_users(message: Message, state: FSMContext) -> None:
    """Обработчик ввода пользователей для уведомлений."""
    text = message.text.strip()

    try:
        notify_users = [int(u.strip()) for u in text.split(",") if u.strip()]
    except ValueError:
        await message.answer("❌ Введите числовые Telegram ID через запятую")
        return

    if not notify_users:
        await message.answer("❌ Введите хотя бы один Telegram ID")
        return

    data = await state.get_data()
    new_site = data.get("new_site", {})

    site = SiteConfig(
        id=new_site["id"],
        name=new_site["name"],
        url=new_site["url"],
        support_level=new_site.get("support_level", "none"),
        check_ssl=True,
        check_http_code=True,
        expected_code=200,
        keywords=new_site.get("keywords", []),
        notify_users=notify_users
    )

    success = add_site(_config, site, _config_path)

    if success:
        status_msg = await message.answer(
            f"✅ <b>Сайт добавлен!</b>\n\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Поддержка: {site.support_level}\n"
            f"Ключевые слова: {', '.join(site.keywords) if site.keywords else '—'}\n"
            f"Подписчики: {', '.join(str(u) for u in notify_users)}\n\n"
            f"⏳ Проверяю...",
            parse_mode="HTML"
        )
        # Немедленная проверка
        check_result = await _check_new_site(site)
        await status_msg.edit_text(
            f"✅ <b>Сайт добавлен!</b>\n\n"
            f"Название: {site.name}\n"
            f"URL: {site.url}\n"
            f"Поддержка: {site.support_level}\n"
            f"Ключевые слова: {', '.join(site.keywords) if site.keywords else '—'}\n"
            f"Подписчики: {', '.join(str(u) for u in notify_users)}\n\n"
            f"<b>Первая проверка:</b> {check_result}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ])
        )
    else:
        await message.answer("❌ Не удалось добавить сайт")

    await state.clear()


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery) -> None:
    """Пустой обработчик."""
    await callback.answer()


# ==================== Setup ====================

def setup_bot(
    config: Config,
    database: Database,
    notifier: TelegramNotifier,
    scheduler: MonitorScheduler = None,
    config_path: str = "config.yaml"
) -> tuple[Bot, Dispatcher]:
    """
    Настраивает и возвращает бота и диспетчер.

    Args:
        config: Конфигурация приложения
        database: База данных
        notifier: Telegram-нотификатор
        scheduler: Планировщик (опционально)
        config_path: Путь к файлу конфигурации

    Returns:
        Кортеж (Bot, Dispatcher)
    """
    global _config, _database, _notifier, _scheduler, _config_path

    _config = config
    _database = database
    _notifier = notifier
    _scheduler = scheduler
    _config_path = config_path

    session = AiohttpSession(proxy=config.telegram.proxy) if config.telegram.proxy else None
    bot = Bot(token=config.telegram.bot_token, session=session)
    dp = Dispatcher()
    dp.include_router(router)

    return bot, dp


async def _setup_bot_commands(bot: Bot) -> None:
    """Устанавливает меню команд бота."""
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="myid", description="Показать мой Telegram ID"),
        BotCommand(command="my_sites", description="Мои сайты"),
        BotCommand(command="muted", description="Заглушенные сайты"),
        BotCommand(command="status_all", description="Статус всех сайтов (админ)"),
        BotCommand(command="check_now", description="Проверить все сейчас (админ)"),
        BotCommand(command="stats", description="Статистика сайтов (админ)"),
        BotCommand(command="sites", description="Управление сайтами (админ)"),
        BotCommand(command="add_site", description="Добавить сайт (админ)"),
        BotCommand(command="changes", description="Журнал изменений (админ)"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Меню команд установлено")


async def start_bot(bot: Bot, dp: Dispatcher) -> None:
    """
    Запускает бота в режиме polling.

    Args:
        bot: Экземпляр бота
        dp: Диспетчер
    """
    await _setup_bot_commands(bot)
    logger.info("Telegram-бот запущен (polling)")
    await dp.start_polling(bot)


async def start_bot_webhook(
    bot: Bot,
    dp: Dispatcher,
    webhook_url: str,
    webhook_path: str,
    host: str,
    port: int
) -> web.Application:
    """
    Запускает бота в режиме webhook.

    Args:
        bot: Экземпляр бота
        dp: Диспетчер
        webhook_url: Полный URL вебхука (https://domain.com/webhook)
        webhook_path: Путь вебхука (/webhook)
        host: Хост для прослушивания
        port: Порт для прослушивания

    Returns:
        aiohttp Application
    """
    await _setup_bot_commands(bot)

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook установлен: {webhook_url}")

    app = web.Application()

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    )
    webhook_requests_handler.register(app, path=webhook_path)

    setup_application(app, dp, bot=bot)

    logger.info(f"Telegram-бот запущен (webhook) на {host}:{port}")

    return app


async def run_webhook_server(app: web.Application, host: str, port: int) -> None:
    """
    Запускает webhook сервер.

    Args:
        app: aiohttp Application
        host: Хост
        port: Порт
    """
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    while True:
        await asyncio.sleep(3600)
