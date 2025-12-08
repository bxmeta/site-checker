"""
Telegram-бот для управления мониторингом.
"""
import json
import logging
import os
import re
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
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
    add_site, remove_site, update_site, add_notify_user, remove_notify_user
)
from .state_manager import StateManager
from .notifier import TelegramNotifier
from .scheduler import run_immediate_check

logger = logging.getLogger("site_monitor")

router = Router()

_config: Optional[Config] = None
_state_manager: Optional[StateManager] = None
_notifier: Optional[TelegramNotifier] = None
_users_file: str = "users.json"
_config_path: str = "config.yaml"


class AddSiteStates(StatesGroup):
    """Состояния для добавления сайта."""
    waiting_for_id = State()
    waiting_for_name = State()
    waiting_for_url = State()
    waiting_for_support_level = State()
    waiting_for_expected_code = State()
    waiting_for_keywords = State()
    waiting_for_notify_users = State()


class EditSiteStates(StatesGroup):
    """Состояния для редактирования сайта."""
    waiting_for_field = State()
    waiting_for_value = State()


def _load_users() -> dict:
    """Загружает список зарегистрированных пользователей."""
    if not os.path.exists(_users_file):
        return {}
    try:
        with open(_users_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_users(users: dict) -> None:
    """Сохраняет список зарегистрированных пользователей."""
    with open(_users_file, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return _config is not None and user_id in _config.telegram.admin_ids


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
        state = _state_manager.get_state(site.id) if _state_manager else None
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


def _site_info_keyboard(site_id: str) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для управления сайтом."""
    buttons = [
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_site:{site_id}"),
            InlineKeyboardButton(text="🔍 Проверить", callback_data=f"check_site:{site_id}")
        ],
        [
            InlineKeyboardButton(text="👥 Подписчики", callback_data=f"site_users:{site_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_site:{site_id}")
        ],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="sites_list")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _edit_site_keyboard(site_id: str) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для редактирования полей сайта."""
    buttons = [
        [
            InlineKeyboardButton(text="📝 Название", callback_data=f"edit_field:{site_id}:name"),
            InlineKeyboardButton(text="🔗 URL", callback_data=f"edit_field:{site_id}:url")
        ],
        [
            InlineKeyboardButton(text="⭐ Уровень поддержки", callback_data=f"edit_field:{site_id}:support_level"),
            InlineKeyboardButton(text="📊 Ожидаемый код", callback_data=f"edit_field:{site_id}:expected_code")
        ],
        [
            InlineKeyboardButton(text="🔐 Проверка SSL", callback_data=f"toggle_ssl:{site_id}"),
            InlineKeyboardButton(text="📡 Проверка HTTP", callback_data=f"toggle_http:{site_id}")
        ],
        [
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data=f"edit_field:{site_id}:keywords")
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"site_info:{site_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _support_level_keyboard(site_id: str = "") -> InlineKeyboardMarkup:
    """Создаёт клавиатуру выбора уровня поддержки."""
    prefix = f"set_support:{site_id}:" if site_id else "new_support:"
    buttons = [
        [
            InlineKeyboardButton(text="⚪ None", callback_data=f"{prefix}none"),
            InlineKeyboardButton(text="🟡 Standard", callback_data=f"{prefix}standard"),
            InlineKeyboardButton(text="🟢 Premium", callback_data=f"{prefix}premium")
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

    buttons.append([InlineKeyboardButton(text="➕ Добавить подписчика", callback_data=f"add_user:{site_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"site_info:{site_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаёт главное меню с кнопками."""
    buttons = [
        [InlineKeyboardButton(text="🆔 Мой ID", callback_data="menu_myid")],
        [InlineKeyboardButton(text="📋 Мои сайты", callback_data="menu_my_sites")],
    ]

    if _is_admin(user_id):
        buttons.extend([
            [InlineKeyboardButton(text="📊 Статус всех сайтов", callback_data="menu_status_all")],
            [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="menu_check_now")],
            [InlineKeyboardButton(text="⚙️ Управление сайтами", callback_data="sites_list")],
            [InlineKeyboardButton(text="➕ Добавить сайт", callback_data="add_site")],
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Обработчик команды /start."""
    user_id = message.from_user.id
    username = message.from_user.username or "Неизвестно"
    full_name = message.from_user.full_name or "Неизвестно"

    users = _load_users()
    if str(user_id) not in users:
        users[str(user_id)] = {
            "username": username,
            "full_name": full_name,
            "registered_at": str(message.date)
        }
        _save_users(users)
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


@router.callback_query(F.data == "menu_main")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в главное меню."""
    await state.clear()  # Сбрасываем любое ожидание ввода

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
    sites = get_sites_for_user(_config, user_id)

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

    lines = ["📋 <b>Мои сайты:</b>\n"]
    for site in sites:
        state = _state_manager.get_state(site.id) if _state_manager else None
        status_emoji = "🟢" if (state and state.status == "UP") else "🔴"
        status_text = state.status if state else "N/A"

        lines.append(
            f"{status_emoji} <b>{site.name}</b>\n"
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


@router.callback_query(F.data == "menu_status_all")
async def callback_menu_status_all(callback: CallbackQuery, state: FSMContext) -> None:
    """Статус всех сайтов через меню."""
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов", show_alert=True)
        return

    if _config is None or _state_manager is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    lines = ["📊 <b>Статус всех сайтов:</b>\n"]

    for site in _config.sites:
        state = _state_manager.get_state(site.id)
        status_emoji = "🟢" if state.status == "UP" else "🔴"

        lines.append(
            f"{status_emoji} <b>{site.name}</b>\n"
            f"   Статус: {state.status} | Ошибок подряд: {state.fail_streak}\n"
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

    if _config is None or _state_manager is None or _notifier is None:
        await callback.answer("❌ Система не инициализирована", show_alert=True)
        return

    await callback.answer("⏳ Запускаю проверку...")

    try:
        report = await run_immediate_check(_config, _state_manager, _notifier)
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
    sites = get_sites_for_user(_config, user_id)

    if not sites:
        await message.answer(
            "📋 Вы не подписаны на уведомления ни одного сайта.\n\n"
            "Попросите администратора добавить ваш ID в notify_users для нужных сайтов."
        )
        return

    lines = ["📋 <b>Ваши сайты:</b>\n"]
    for site in sites:
        state = _state_manager.get_state(site.id) if _state_manager else None
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

    if _config is None or _state_manager is None:
        await message.answer("❌ Конфигурация или состояние не загружены")
        return

    lines = ["📊 <b>Статус всех сайтов:</b>\n"]

    for site in _config.sites:
        state = _state_manager.get_state(site.id)
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

    if _config is None or _state_manager is None or _notifier is None:
        await message.answer("❌ Система не полностью инициализирована")
        return

    await message.answer("⏳ Запускаю проверку всех сайтов...")

    try:
        report = await run_immediate_check(_config, _state_manager, _notifier)
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

    state = _state_manager.get_state(site_id) if _state_manager else None
    status_emoji = "🟢" if (state and state.status == "UP") else "🔴"
    status_text = state.status if state else "N/A"

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
        reply_markup=_site_info_keyboard(site_id)
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
    result = await check_site_single(site, _config.default)

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
    elif field == "keywords":
        value = [k.strip() for k in value.split(",") if k.strip()]

    success = update_site(_config, site_id, _config_path, **{field: value})

    if success:
        await message.answer(
            f"✅ Поле успешно обновлено!",
            reply_markup=_site_info_keyboard(site_id)
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
        site = get_site_by_id(_config, site_id)
        await callback.message.edit_text(
            f"✅ Уровень поддержки обновлён: {level}",
            reply_markup=_site_info_keyboard(site_id)
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

    await state.set_state(AddSiteStates.waiting_for_id)
    await state.update_data(new_site={})

    text = (
        "➕ <b>Добавление нового сайта</b>\n\n"
        "Шаг 1/7: Введите уникальный ID сайта\n"
        "(латинские буквы, цифры, подчёркивание)\n\n"
        "Пример: <code>my_site_1</code>"
    )

    if isinstance(event, CallbackQuery):
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")


@router.message(StateFilter(AddSiteStates.waiting_for_id))
async def process_site_id(message: Message, state: FSMContext) -> None:
    """Обработчик ввода ID сайта."""
    site_id = message.text.strip()

    if not re.match(r"^[a-zA-Z0-9_]+$", site_id):
        await message.answer("❌ ID может содержать только латинские буквы, цифры и подчёркивание")
        return

    if get_site_by_id(_config, site_id):
        await message.answer("❌ Сайт с таким ID уже существует")
        return

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["id"] = site_id
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_name)

    await message.answer(
        "✅ ID принят!\n\n"
        "Шаг 2/7: Введите название сайта\n\n"
        "Пример: <code>Мой сайт</code>",
        parse_mode="HTML"
    )


@router.message(StateFilter(AddSiteStates.waiting_for_name))
async def process_site_name(message: Message, state: FSMContext) -> None:
    """Обработчик ввода названия сайта."""
    name = message.text.strip()

    if len(name) < 1 or len(name) > 100:
        await message.answer("❌ Название должно быть от 1 до 100 символов")
        return

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["name"] = name
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_url)

    await message.answer(
        "✅ Название принято!\n\n"
        "Шаг 3/7: Введите URL сайта\n\n"
        "Пример: <code>https://example.com</code>",
        parse_mode="HTML"
    )


@router.message(StateFilter(AddSiteStates.waiting_for_url))
async def process_site_url(message: Message, state: FSMContext) -> None:
    """Обработчик ввода URL сайта."""
    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("❌ URL должен начинаться с http:// или https://")
        return

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["url"] = url
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_support_level)

    await message.answer(
        "✅ URL принят!\n\n"
        "Шаг 4/7: Выберите уровень поддержки",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⚪ None", callback_data="new_support:none"),
                InlineKeyboardButton(text="🟡 Standard", callback_data="new_support:standard"),
                InlineKeyboardButton(text="🟢 Premium", callback_data="new_support:premium")
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
    await state.set_state(AddSiteStates.waiting_for_expected_code)

    await callback.message.edit_text(
        f"✅ Уровень поддержки: {level}\n\n"
        "Шаг 5/7: Введите ожидаемый HTTP код\n\n"
        "Пример: <code>200</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(AddSiteStates.waiting_for_expected_code))
async def process_expected_code(message: Message, state: FSMContext) -> None:
    """Обработчик ввода ожидаемого HTTP кода."""
    try:
        code = int(message.text.strip())
        if code < 100 or code > 599:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректный HTTP код (100-599)")
        return

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["expected_code"] = code
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_keywords)

    await message.answer(
        f"✅ Ожидаемый код: {code}\n\n"
        "Шаг 6/7: Введите ключевые слова через запятую\n"
        "(или отправьте <code>-</code> чтобы пропустить)\n\n"
        "Пример: <code>Welcome, Home, Login</code>",
        parse_mode="HTML"
    )


@router.message(StateFilter(AddSiteStates.waiting_for_keywords))
async def process_keywords(message: Message, state: FSMContext) -> None:
    """Обработчик ввода ключевых слов."""
    text = message.text.strip()

    if text == "-":
        keywords = []
    else:
        keywords = [k.strip() for k in text.split(",") if k.strip()]

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["keywords"] = keywords
    await state.update_data(new_site=new_site)
    await state.set_state(AddSiteStates.waiting_for_notify_users)

    keywords_str = ", ".join(keywords) if keywords else "—"
    await message.answer(
        f"✅ Ключевые слова: {keywords_str}\n\n"
        "Шаг 7/7: Введите Telegram ID пользователей для уведомлений\n"
        "(через запятую или <code>-</code> чтобы пропустить)\n\n"
        "Пример: <code>123456789, 987654321</code>",
        parse_mode="HTML"
    )


@router.message(StateFilter(AddSiteStates.waiting_for_notify_users))
async def process_notify_users(message: Message, state: FSMContext) -> None:
    """Обработчик ввода пользователей для уведомлений."""
    text = message.text.strip()

    if text == "-":
        notify_users = []
    else:
        try:
            notify_users = [int(u.strip()) for u in text.split(",") if u.strip()]
        except ValueError:
            await message.answer("❌ Введите числовые Telegram ID через запятую")
            return

    data = await state.get_data()
    new_site = data.get("new_site", {})
    new_site["notify_users"] = notify_users

    site = SiteConfig(
        id=new_site["id"],
        name=new_site["name"],
        url=new_site["url"],
        support_level=new_site.get("support_level", "none"),
        check_ssl=True,
        check_http_code=True,
        expected_code=new_site.get("expected_code", 200),
        keywords=new_site.get("keywords", []),
        notify_users=notify_users
    )

    success = add_site(_config, site, _config_path)

    if success:
        await message.answer(
            f"✅ <b>Сайт успешно добавлен!</b>\n\n"
            f"🆔 ID: {site.id}\n"
            f"📝 Название: {site.name}\n"
            f"🔗 URL: {site.url}\n"
            f"⭐ Поддержка: {site.support_level}",
            parse_mode="HTML",
            reply_markup=_site_info_keyboard(site.id)
        )
    else:
        await message.answer("❌ Не удалось добавить сайт")

    await state.clear()


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery) -> None:
    """Пустой обработчик."""
    await callback.answer()


def setup_bot(
    config: Config,
    state_manager: StateManager,
    notifier: TelegramNotifier,
    users_file: str = "users.json",
    config_path: str = "config.yaml"
) -> tuple[Bot, Dispatcher]:
    """
    Настраивает и возвращает бота и диспетчер.

    Args:
        config: Конфигурация приложения
        state_manager: Менеджер состояния
        notifier: Telegram-нотификатор
        users_file: Путь к файлу пользователей
        config_path: Путь к файлу конфигурации

    Returns:
        Кортеж (Bot, Dispatcher)
    """
    global _config, _state_manager, _notifier, _users_file, _config_path

    _config = config
    _state_manager = state_manager
    _notifier = notifier
    _users_file = users_file
    _config_path = config_path

    bot = Bot(token=config.telegram.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    return bot, dp


async def _setup_bot_commands(bot: Bot) -> None:
    """Устанавливает меню команд бота."""
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="myid", description="Показать мой Telegram ID"),
        BotCommand(command="my_sites", description="Мои сайты"),
        BotCommand(command="status_all", description="Статус всех сайтов (админ)"),
        BotCommand(command="check_now", description="Проверить все сейчас (админ)"),
        BotCommand(command="sites", description="Управление сайтами (админ)"),
        BotCommand(command="add_site", description="Добавить сайт (админ)"),
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

    # Удаляем старый webhook и устанавливаем новый
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook установлен: {webhook_url}")

    # Создаём aiohttp приложение
    app = web.Application()

    # Настраиваем webhook handler
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    )
    webhook_requests_handler.register(app, path=webhook_path)

    # Настраиваем startup/shutdown
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

    # Держим сервер запущенным
    while True:
        await asyncio.sleep(3600)


# Нужен импорт asyncio для run_webhook_server
import asyncio
