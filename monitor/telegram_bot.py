"""
Telegram-бот управления мониторингом.

Принципы интерфейса:
- один экран = одно сообщение, переходы редактируют его (без простыней из сообщений);
- главный экран - сводка: сколько в порядке, что лежит и почему, кнопки по группам;
- списки короткие и постраничные, карточка сайта - вся суть в 6-8 строках;
- добавление сайта одним сообщением: ссылка (или несколько) и, по желанию, группы;
- группы переключаются кнопками, подписаться на сайт может сам пользователь.

Навигация кодируется в callback_data: "site:<id>:<kind>:<page>", где kind - вид списка,
из которого открыли карточку (all, down, my, muted, g_<группа>, q_<запрос>).
"""
import asyncio
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .changelog import set_actor, reset_actor, tg_actor, read_changes
from .config_loader import (
    Config, SiteConfig, get_site_by_id,
    add_site, remove_site, update_site, add_notify_user, remove_notify_user,
    add_admin, remove_admin
)
from .database import Database, SiteState
from .groups import KNOWN_GROUPS, group_label, group_emoji, group_title, groups_label, normalize_groups, DEFAULT_GROUP
from .notifier import TelegramNotifier, format_duration
from .scheduler import MonitorScheduler
from .time_utils import parse_datetime, now_izhevsk

logger = logging.getLogger("site_monitor")

router = Router()

_config: Optional[Config] = None
_database: Optional[Database] = None
_notifier: Optional[TelegramNotifier] = None
_scheduler: Optional[MonitorScheduler] = None
_config_path: str = "config.yaml"
_check_all_lock = asyncio.Lock()

PAGE_SIZE = 10
MAX_DOWN_ON_DASHBOARD = 8


# ==================== Служебное ====================

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


class AddStates(StatesGroup):
    waiting_urls = State()


class EditStates(StatesGroup):
    waiting_value = State()


class SearchStates(StatesGroup):
    waiting_query = State()


class SubStates(StatesGroup):
    waiting_user_id = State()


class AdminStates(StatesGroup):
    waiting_user_id = State()


def _is_admin(user_id: int) -> bool:
    return _config is not None and user_id in _config.telegram.admin_ids


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _kb(rows: List[List[Tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Строит клавиатуру из списка строк [(текст, callback_data), ...]."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t[:60], callback_data=c) for t, c in row] for row in rows if row
    ])


async def _show(event, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    """Показывает экран: для callback редактирует сообщение, для команды отвечает новым."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await event.message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        try:
            await event.answer()
        except Exception:
            pass
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


def _ago(iso: Optional[str]) -> str:
    """"5 мин", "2 ч 10 мин", "3 дн" с момента iso до сейчас."""
    if not iso:
        return "-"
    try:
        seconds = int((now_izhevsk() - parse_datetime(iso)).total_seconds())
    except Exception:
        return "-"
    return format_duration(max(0, seconds))


def _hhmm(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        dt = parse_datetime(iso)
        today = now_izhevsk().date()
        return dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%d.%m %H:%M")
    except Exception:
        return "-"


def _state(site_id: str) -> Optional[SiteState]:
    return _database.get_state(site_id) if _database else None


def _is_down(state: Optional[SiteState]) -> bool:
    return bool(state and state.status == "DOWN")


_SHORT_ERRORS = {
    "timeout": "не отвечает", "dns": "DNS", "conn_refused": "порт закрыт", "conn_reset": "сброс соединения",
    "no_response": "недоступен", "wrong_code": "неверный код", "redirect_loop": "цикл редиректов",
    "bad_response": "битый ответ", "keyword_missing": "контент", "ssl_expired": "SSL истёк",
    "ssl_not_yet_valid": "SSL не действует", "ssl_mismatch": "чужой SSL", "ssl_untrusted": "SSL не доверенный",
    "ssl_handshake": "TLS",
}


def _short_error(state: Optional[SiteState]) -> str:
    if not state:
        return ""
    short = _SHORT_ERRORS.get(state.last_error_type or "", "")
    if state.last_error_type == "wrong_code" and state.last_status_code:
        short = f"код {state.last_status_code}"
    return short or (state.last_error_type or "ошибка")


def _can_view(user_id: int, site: SiteConfig) -> bool:
    """Карточка и действия с сайтом доступны админам и подписчикам сайта."""
    return _is_admin(user_id) or user_id in site.notify_users


def _visible_sites(user_id: int) -> List[SiteConfig]:
    if _config is None:
        return []
    if _is_admin(user_id):
        return list(_config.sites)
    return [s for s in _config.sites if user_id in s.notify_users]


def _resolve_list(kind: str, user_id: int) -> Tuple[str, List[SiteConfig]]:
    """Возвращает (заголовок, сайты) для вида списка."""
    sites = _visible_sites(user_id)
    states = _database.get_all_states() if _database else {}
    if kind == "all":
        return ("📋 Все сайты" if _is_admin(user_id) else "📋 Мои сайты"), sites
    if kind == "my":
        return "📋 Мои сайты", [s for s in (_config.sites if _config else []) if user_id in s.notify_users]
    if kind == "down":
        return "🔴 Проблемы", [s for s in sites if _is_down(states.get(s.id))]
    if kind == "muted":
        muted = set(_database.get_user_mutes(user_id)) if _database else set()
        return "🔇 Заглушенные", [s for s in sites if s.id in muted]
    if kind.startswith("g_"):
        g = kind[2:]
        return group_label(g), [s for s in sites if g in s.groups]
    if kind.startswith("q_"):
        q = kind[2:].lower()
        return f"🔎 Поиск: {q}", [s for s in sites if q in s.name.lower() or q in s.url.lower() or q in s.id.lower()]
    return "📋 Сайты", sites


def _site_button_text(site: SiteConfig, state: Optional[SiteState]) -> str:
    if _is_down(state):
        return f"🔴 {site.name} · {_ago(state.last_status_change)} · {_short_error(state)}"
    return f"🟢 {site.name}"


def _back_cb(kind: str, page: int) -> str:
    return f"list:{kind}:{page}"


def _parse_back(parts: List[str], start: int) -> Tuple[str, int]:
    kind = parts[start] if len(parts) > start and parts[start] else "all"
    try:
        page = int(parts[start + 1]) if len(parts) > start + 1 else 0
    except ValueError:
        page = 0
    return kind, page


def _plural_sites(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} сайт"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} сайта"
    return f"{n} сайтов"


# ==================== Экраны ====================

def dashboard_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    sites = _visible_sites(user_id)
    states = _database.get_all_states() if _database else {}
    down = [s for s in sites if _is_down(states.get(s.id))]
    up_count = len(sites) - len(down)
    muted = _database.get_user_mutes(user_id) if _database else []
    last_check = max((st.last_check_at or "" for st in states.values()), default="")

    lines = ["🌐 <b>Мониторинг сайтов</b>"]
    if not sites:
        lines.append("")
        lines.append("Вы пока не подписаны ни на один сайт. Сообщите администратору свой ID (кнопка «Мой ID»).")
    else:
        summary = f"🟢 {up_count} в порядке · 🔴 {len(down)} недоступн{'ы' if len(down) == 1 else 'о'}"
        if muted:
            summary += f" · 🔇 {len(muted)} заглуш."
        lines.append(summary)
        interval = _config.scheduler.interval_minutes if _config else 5
        lines.append(f"Проверка каждые {interval} мин, последняя: {_hhmm(last_check)}")
        if down:
            lines.append("")
            for s in sorted(down, key=lambda x: states[x.id].last_status_change or "")[:MAX_DOWN_ON_DASHBOARD]:
                st = states[s.id]
                lines.append(f"🔴 <b>{_esc(s.name)}</b> · {_ago(st.last_status_change)} · {_esc(_short_error(st))}")
            if len(down) > MAX_DOWN_ON_DASHBOARD:
                lines.append(f"… и ещё {len(down) - MAX_DOWN_ON_DASHBOARD}")

    rows: List[List[Tuple[str, str]]] = []
    first_row = []
    if down:
        first_row.append((f"🔴 Проблемы ({len(down)})", _back_cb("down", 0)))
    first_row.append((f"📋 {'Все' if _is_admin(user_id) else 'Мои'} сайты ({len(sites)})", _back_cb("all", 0)))
    rows.append(first_row)

    if _is_admin(user_id) and sites:
        group_row = []
        for g in KNOWN_GROUPS:
            n = sum(1 for s in sites if g in s.groups)
            if n:
                group_row.append((f"{group_emoji(g)} {group_title(g)} {n}", _back_cb(f"g_{g}", 0)))
            if len(group_row) == 2:
                rows.append(group_row)
                group_row = []
        if group_row:
            rows.append(group_row)

    rows.append([("🔄 Проверить всё", "checkall"), ("🔎 Поиск", "search")])
    if muted:
        rows.append([(f"🔇 Заглушенные ({len(muted)})", _back_cb("muted", 0))])
    if _is_admin(user_id):
        rows.append([("➕ Добавить сайт", "add"), ("📓 Журнал", "changes"), ("👑 Админы", "admins")])
    rows.append([("🆔 Мой ID", "myid"), ("ℹ️ Справка", "help"), ("↻ Обновить", "dash")])
    return "\n".join(lines), _kb(rows)


def list_screen(user_id: int, kind: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    title, sites = _resolve_list(kind, user_id)
    states = _database.get_all_states() if _database else {}
    sites = sorted(sites, key=lambda s: (not _is_down(states.get(s.id)), s.name.lower()))
    total = len(sites)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = sites[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    down_n = sum(1 for s in sites if _is_down(states.get(s.id)))
    head = f"<b>{_esc(title)}</b> · {_plural_sites(total)}"
    if down_n:
        head += f" · 🔴 {down_n}"
    if pages > 1:
        head += f" · стр. {page + 1}/{pages}"
    text = head if chunk else head + "\n\nПусто."

    rows: List[List[Tuple[str, str]]] = []
    for s in chunk:
        rows.append([(_site_button_text(s, states.get(s.id)), f"site:{s.id}:{kind}:{page}")])
    nav = []
    if page > 0:
        nav.append(("◀️", _back_cb(kind, page - 1)))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶️", _back_cb(kind, page + 1)))
    if nav:
        rows.append(nav)
    rows.append([("🏠 Меню", "dash")])
    return text, _kb(rows)


def site_screen(user_id: int, site: SiteConfig, kind: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = _state(site.id)
    is_admin = _is_admin(user_id)
    muted = _database.is_muted(user_id, site.id) if _database else False
    subscribed = user_id in site.notify_users

    if _is_down(st):
        head = f"🔴 <b>{_esc(site.name)}</b>"
        status_line = f"Недоступен {_ago(st.last_status_change)}, с {_hhmm(st.last_status_change)}"
        if st.last_error:
            status_line += f"\n⚠️ {_esc(st.last_error)}"
    else:
        head = f"🟢 <b>{_esc(site.name)}</b>"
        since = f" уже {_ago(st.last_status_change)}" if st and st.last_status_change else ""
        status_line = f"В порядке{since}"
    lines = [head, _esc(site.url), f"Группы: {_esc(groups_label(site.groups))}", status_line]

    if st and st.last_check_at:
        check = f"Последняя проверка: {_hhmm(st.last_check_at)}"
        if st.last_status_code is not None:
            check += f" · код {st.last_status_code}"
        if st.last_response_ms is not None:
            check += f" · {st.last_response_ms} мс"
        if st.last_ip:
            check += f" · {st.last_ip}"
        lines.append(check)
    if site.check_ssl and st and st.ssl_expiry:
        try:
            exp = parse_datetime(st.ssl_expiry)
            days = (exp - now_izhevsk()).days
            ssl_line = f"SSL: {_esc(st.ssl_issuer or '')}, до {exp.strftime('%d.%m.%Y')} ({days} дн.)"
            if st.ssl_auto_renew:
                ssl_line += ", автопродление"
            if days <= 3:
                ssl_line = "🔐 " + ssl_line
            lines.append(ssl_line)
        except Exception:
            pass
    elif not site.check_ssl:
        lines.append("SSL: не проверяется")
    if _database:
        stats = _database.get_site_stats(site.id)
        lines.append(f"Аптайм 30 дн: {stats.uptime_30d:g}% · инцидентов: {stats.incidents_30d}")
    extra = []
    if not site.check_http_code:
        extra.append("код не проверяется")
    elif site.expected_code != 200:
        extra.append(f"ожидаемый код {site.expected_code}")
    if site.keywords:
        extra.append(f"слова: {', '.join(site.keywords)}")
    if extra:
        lines.append("Настройки: " + _esc("; ".join(extra)))
    subs = f"Подписчики TG: {len(site.notify_users)}"
    if subscribed:
        subs += " (вы подписаны)"
    if muted:
        subs += " · 🔇 вы заглушили"
    lines.append(subs)

    nav = f"{kind}:{page}"
    rows: List[List[Tuple[str, str]]] = [[("🔄 Проверить", f"chk:{site.id}:{nav}"), ("📈 История", f"hist:{site.id}:{nav}")]]
    row2 = []
    if _is_down(st):
        row2.append(("🔔 Включить напоминания", f"unmute:{site.id}:{nav}") if muted else ("🔇 Заглушить", f"mute:{site.id}:{nav}"))
    row2.append(("🔕 Отписаться", f"unsub:{site.id}:{nav}") if subscribed else ("🔔 Подписаться", f"sub:{site.id}:{nav}"))
    rows.append(row2)
    if is_admin:
        rows.append([("🏷 Группы", f"grps:{site.id}:{nav}"), ("✏️ Изменить", f"edit:{site.id}:{nav}")])
        rows.append([("👥 Подписчики", f"subs:{site.id}:{nav}"), ("🗑 Удалить", f"del:{site.id}:{nav}")])
    rows.append([("◀️ К списку", _back_cb(kind, page)), ("🏠 Меню", "dash")])
    return "\n".join(lines), _kb(rows)


def history_screen(site: SiteConfig, kind: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    stats = _database.get_site_stats(site.id)
    incidents = _database.get_site_incidents(site.id, days=30, limit=10)
    lines = [
        f"📈 <b>{_esc(site.name)}</b>: история за 30 дней",
        f"Аптайм: 7 дн {stats.uptime_7d:g}% · 30 дн {stats.uptime_30d:g}%",
        f"Инцидентов: {stats.incidents_30d}"
        + (f" · средний простой {format_duration(stats.avg_downtime_seconds)}" if stats.avg_downtime_seconds else ""),
    ]
    if incidents:
        lines.append("")
        for inc in incidents:
            dur = format_duration(inc.duration_seconds) if inc.duration_seconds else "идёт"
            reason = _SHORT_ERRORS.get(inc.error_type or "", inc.error_type or "")
            lines.append(f"• {_hhmm(inc.started_at)} · {dur} · {_esc(reason)}")
    else:
        lines.append("\nИнцидентов не было.")
    return "\n".join(lines), _kb([[("◀️ К сайту", f"site:{site.id}:{kind}:{page}"), ("🏠 Меню", "dash")]])


def groups_screen(site: SiteConfig, kind: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    text = (f"🏷 <b>{_esc(site.name)}</b>: группы\n\nСейчас: {_esc(groups_label(site.groups))}\n"
            f"Нажмите, чтобы включить или выключить.")
    all_groups = list(KNOWN_GROUPS) + [g for g in site.groups if g not in KNOWN_GROUPS]
    rows = []
    for g in all_groups:
        mark = "✅" if g in site.groups else "▫️"
        rows.append([(f"{mark} {group_label(g)}", f"grp:{site.id}:{g}:{kind}:{page}")])
    rows.append([("✔️ Готово", f"site:{site.id}:{kind}:{page}")])
    return text, _kb(rows)


def edit_screen(site: SiteConfig, kind: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    nav = f"{kind}:{page}"
    text = f"✏️ <b>{_esc(site.name)}</b>: настройки\nВыберите, что изменить."
    rows = [
        [(f"Название: {site.name}", f"ef:{site.id}:name:{nav}")],
        [(f"URL: {site.url}", f"ef:{site.id}:url:{nav}")],
        [(f"Ожидаемый код: {site.expected_code}", f"ef:{site.id}:expected_code:{nav}"),
         (f"Проверка кода: {'вкл' if site.check_http_code else 'выкл'}", f"tgl:{site.id}:check_http_code:{nav}")],
        [(f"Ключевые слова: {', '.join(site.keywords) if site.keywords else 'нет'}", f"ef:{site.id}:keywords:{nav}")],
        [(f"Проверка SSL: {'вкл' if site.check_ssl else 'выкл'}", f"tgl:{site.id}:check_ssl:{nav}")],
        [("◀️ К сайту", f"site:{site.id}:{nav}")],
    ]
    return text, _kb(rows)


def subs_screen(site: SiteConfig, kind: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    nav = f"{kind}:{page}"
    lines = [f"👥 <b>{_esc(site.name)}</b>: подписчики Telegram",
             "Админы получают уведомления по всем сайтам всегда."]
    rows = []
    for uid in site.notify_users:
        user = _database.get_user(uid) if _database else None
        name = (user.full_name or user.username) if user else None
        label = f"{name} ({uid})" if name else str(uid)
        rows.append([(f"➖ {label}", f"unsubu:{site.id}:{uid}:{nav}")])
    if not site.notify_users:
        lines.append("\nПодписчиков нет.")
    rows.append([("➕ Добавить по ID", f"subadd:{site.id}:{nav}")])
    rows.append([("◀️ К сайту", f"site:{site.id}:{nav}")])
    return "\n".join(lines), _kb(rows)


def admins_screen() -> Tuple[str, InlineKeyboardMarkup]:
    lines = ["👑 <b>Администраторы</b>", "Видят все сайты, получают все уведомления, управляют списком."]
    rows = []
    for uid in _config.telegram.admin_ids:
        user = _database.get_user(uid) if _database else None
        name = (user.full_name or user.username) if user else None
        label = f"{name} ({uid})" if name else str(uid)
        rows.append([(f"➖ {label}", f"admdel:{uid}")])
    rows.append([("➕ Добавить по ID", "admadd")])
    rows.append([("🏠 Меню", "dash")])
    return "\n".join(lines), _kb(rows)


HELP_TEXT = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "/start - главный экран со сводкой\n"
    "/down - что сейчас лежит\n"
    "/check [сайт] - проверить все сайты или один\n"
    "/find текст - найти сайт по имени или адресу\n"
    "/add ссылка [группы] - добавить сайт (админ), например: <code>/add shop.ru hosting vip</code>\n"
    "/mute сайт, /unmute сайт - напоминания о падении для себя\n"
    "/changes - журнал изменений (админ)\n"
    "/myid - ваш Telegram ID\n\n"
    "Любой текст без команды - поиск по сайтам.\n"
    "Уведомления о падении и восстановлении приходят подписчикам сайта и админам. "
    "Подписчиков добавляет администратор в карточке сайта (нужен ваш ID: /myid)."
)

ADD_PROMPT = (
    "➕ <b>Добавление сайта</b>\n\nПришлите ссылку (можно несколько, каждую с новой строки). "
    "Рядом можно указать группы: <code>shop.ru hosting vip</code>.\n"
    "Без группы сайт попадёт в «Стандарт»."
)


# ==================== Команды ====================

@router.message(Command("start", "menu", "status", "status_all", "my_sites", "sites"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if _database:
        if _database.register_user(user.id, user.username, user.full_name):
            logger.info(f"Новый пользователь: {user.id} (@{user.username})")
    text, kb = dashboard_screen(user.id)
    await _show(message, text, kb)


@router.message(Command("down"))
async def cmd_down(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = list_screen(message.from_user.id, "down", 0)
    await _show(message, text, kb)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _show(message, HELP_TEXT, _kb([[("🏠 Меню", "dash")]]))


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    query = (command.args or "").strip()
    if not query:
        await state.set_state(SearchStates.waiting_query)
        await message.answer("🔎 Введите часть названия или адреса сайта:")
        return
    text, kb = list_screen(message.from_user.id, f"q_{_clean_query(query)}", 0)
    await _show(message, text, kb)


@router.message(Command("check", "check_now"))
async def cmd_check(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if arg:
        site = _find_site(arg, message.from_user.id)
        if not site:
            await message.answer(f"Сайт «{_esc(arg)}» не найден. Попробуйте /find {_esc(arg)}", parse_mode="HTML")
            return
        line = await _scheduler.check_single_site(site.id) if _scheduler else "планировщик не запущен"
        await message.answer(_esc(line), reply_markup=_kb([[("📄 Карточка", f"site:{site.id}:all:0")]]))
        return
    if not _is_admin(message.from_user.id):
        await message.answer("Проверить все сайты может только администратор. Укажите сайт: /check имя")
        return
    await _run_check_all(message)


@router.message(Command("add", "add_site"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Добавлять сайты может только администратор")
        return
    args = (command.args or "").strip()
    if not args:
        await state.set_state(AddStates.waiting_urls)
        await message.answer(ADD_PROMPT, parse_mode="HTML", reply_markup=_kb([[("✖️ Отмена", "dash")]]))
        return
    await _add_sites_from_text(message, args)


@router.message(Command("mute", "unmute"))
async def cmd_mute(message: Message, command: CommandObject) -> None:
    mute = (message.text or "").lstrip("/").split()[0].lower().startswith("mute")
    arg = (command.args or "").strip()
    site = _find_site(arg, message.from_user.id) if arg else None
    if not site:
        await message.answer(f"Укажите сайт: /{'mute' if mute else 'unmute'} имя")
        return
    await message.answer(_toggle_mute(message.from_user.id, site, mute))


@router.message(Command("changes"))
async def cmd_changes(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов")
        return
    await _show(message, _changes_text(), _kb([[("🏠 Меню", "dash")]]))


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов")
        return
    text, kb = admins_screen()
    await _show(message, text, kb)


@router.message(Command("add_admin", "remove_admin"))
async def cmd_admin_change(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов")
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        await message.answer("Укажите Telegram ID числом")
        return
    if (message.text or "").startswith("/add_admin"):
        ok = add_admin(_config, uid, _config_path)
        await message.answer("✅ Администратор добавлен" if ok else "Уже администратор")
    else:
        ok = remove_admin(_config, uid, _config_path)
        await message.answer("✅ Администратор удалён" if ok else "Не удалось: не админ или последний админ")


# ==================== Callback: навигация ====================

@router.callback_query(F.data == "dash")
async def cb_dash(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = dashboard_screen(callback.from_user.id)
    await _show(callback, text, kb)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery) -> None:
    await _show(callback, HELP_TEXT, _kb([[("🏠 Меню", "dash")]]))


@router.callback_query(F.data == "myid")
async def cb_myid(callback: CallbackQuery) -> None:
    await _show(callback, f"🆔 Ваш Telegram ID: <code>{callback.from_user.id}</code>\n\n"
                          "Сообщите его администратору, чтобы вас добавили в подписчики сайтов.", _kb([[("🏠 Меню", "dash")]]))


@router.callback_query(F.data.startswith("list:"))
async def cb_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    parts = callback.data.split(":")
    kind, page = _parse_back(parts, 1)
    text, kb = list_screen(callback.from_user.id, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("site:"))
async def cb_site(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site or not _can_view(callback.from_user.id, site):
        await callback.answer("Сайт не найден (удалён?)", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    text, kb = site_screen(callback.from_user.id, site, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("hist:"))
async def cb_hist(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site or not _can_view(callback.from_user.id, site):
        await callback.answer("Сайт не найден", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    text, kb = history_screen(site, kind, page)
    await _show(callback, text, kb)


# ==================== Callback: действия с сайтом ====================

@router.callback_query(F.data.startswith("chk:") | F.data.startswith("check_now:") | F.data.startswith("check_site:"))
async def cb_check_site(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site or not _can_view(callback.from_user.id, site):
        await callback.answer("Сайт не найден", show_alert=True)
        return
    await callback.answer("⏳ Проверяю...")
    line = await _scheduler.check_single_site(site.id) if _scheduler else "планировщик не запущен"
    kind, page = _parse_back(parts, 2)
    if callback.data.startswith("chk:"):
        text, kb = site_screen(callback.from_user.id, site, kind, page)
        text += f"\n\n🔍 {_esc(line)}"
        await _show(callback, text, kb)
    else:
        await callback.message.answer(_esc(line), reply_markup=_kb([[("📄 Карточка", f"site:{site.id}:all:0")]]))


@router.callback_query(F.data.startswith("mute:") | F.data.startswith("unmute:")
                       | F.data.startswith("mute_site:") | F.data.startswith("unmute_site:"))
async def cb_mute(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    mute = parts[0] in ("mute", "mute_site")
    site = get_site_by_id(_config, parts[1])
    if not site or not _can_view(callback.from_user.id, site):
        await callback.answer("Сайт не найден", show_alert=True)
        return
    result = _toggle_mute(callback.from_user.id, site, mute)
    if parts[0] in ("mute", "unmute"):
        kind, page = _parse_back(parts, 2)
        text, kb = site_screen(callback.from_user.id, site, kind, page)
        await _show(callback, text, kb)
        try:
            await callback.answer(result)
        except Exception:
            pass
    else:
        await callback.answer(result, show_alert=True)


@router.callback_query(F.data.startswith("sub:") | F.data.startswith("unsub:"))
async def cb_subscribe(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    uid = callback.from_user.id
    if parts[0] == "sub":
        # Подписать себя может только администратор; остальных добавляет админ в карточке сайта
        if not _is_admin(uid):
            await callback.answer("Подписку оформляет администратор: сообщите ему свой ID (/myid)", show_alert=True)
            return
        add_notify_user(_config, site.id, uid, _config_path)
        note = "🔔 Вы подписаны на уведомления"
    else:
        remove_notify_user(_config, site.id, uid, _config_path)
        note = "🔕 Подписка снята"
    kind, page = _parse_back(parts, 2)
    text, kb = site_screen(uid, site, kind, page)
    await _show(callback, text, kb)
    try:
        await callback.answer(note)
    except Exception:
        pass


@router.callback_query(F.data.startswith("grps:"))
async def cb_groups(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    text, kb = groups_screen(site, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("grp:"))
async def cb_group_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    g = parts[2]
    groups = [x for x in site.groups if x != g] if g in site.groups else site.groups + [g]
    update_site(_config, site.id, _config_path, groups=groups)
    kind, page = _parse_back(parts, 3)
    text, kb = groups_screen(site, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    text, kb = edit_screen(site, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("tgl:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    field = parts[2]
    if not site or field not in ("check_ssl", "check_http_code"):
        await callback.answer("Ошибка", show_alert=True)
        return
    update_site(_config, site.id, _config_path, **{field: not getattr(site, field)})
    kind, page = _parse_back(parts, 3)
    text, kb = edit_screen(site, kind, page)
    await _show(callback, text, kb)


_FIELD_PROMPTS = {
    "name": "Введите новое название:",
    "url": "Введите новый адрес (с http:// или https://):",
    "expected_code": "Введите ожидаемый HTTP-код (например, 200 или 401):",
    "keywords": "Введите ключевые слова через запятую (пусто или «нет» - убрать проверку слов):",
}


@router.callback_query(F.data.startswith("ef:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    field = parts[2]
    if not site or field not in _FIELD_PROMPTS:
        await callback.answer("Ошибка", show_alert=True)
        return
    kind, page = _parse_back(parts, 3)
    await state.set_state(EditStates.waiting_value)
    await state.update_data(site_id=site.id, field=field, kind=kind, page=page)
    current = getattr(site, field)
    if field == "keywords":
        current = ", ".join(current) if current else "нет"
    await _show(callback, f"✏️ <b>{_esc(site.name)}</b>\nСейчас: {_esc(current)}\n\n{_FIELD_PROMPTS[field]}",
                _kb([[("✖️ Отмена", f"edit:{site.id}:{kind}:{page}")]]))


@router.message(StateFilter(EditStates.waiting_value))
async def msg_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    site = get_site_by_id(_config, data.get("site_id", ""))
    field = data.get("field")
    if not site or not field:
        await state.clear()
        await message.answer("Сайт не найден")
        return
    raw = (message.text or "").strip()
    value = raw
    if field == "expected_code":
        try:
            value = int(raw)
        except ValueError:
            await message.answer("Нужно число, например 200")
            return
    elif field == "url":
        if not raw.startswith(("http://", "https://")):
            value = "https://" + raw
        if not urlparse(value).hostname:
            await message.answer("Не похоже на адрес сайта")
            return
    elif field == "keywords":
        value = [k.strip() for k in raw.split(",") if k.strip()] if raw.lower() not in ("", "нет", "-") else []
    update_site(_config, site.id, _config_path, **{field: value})
    await state.clear()
    text, kb = edit_screen(site, data.get("kind", "all"), int(data.get("page", 0)))
    await message.answer("✅ Сохранено\n\n" + text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("subs:"))
async def cb_subs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    text, kb = subs_screen(site, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("unsubu:"))
async def cb_unsub_user(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    try:
        remove_notify_user(_config, site.id, int(parts[2]), _config_path)
    except ValueError:
        pass
    kind, page = _parse_back(parts, 3)
    text, kb = subs_screen(site, kind, page)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("subadd:"))
async def cb_sub_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    await state.set_state(SubStates.waiting_user_id)
    await state.update_data(site_id=site.id, kind=kind, page=page)
    await _show(callback, f"👥 <b>{_esc(site.name)}</b>\nВведите Telegram ID подписчика (узнать: /myid):",
                _kb([[("✖️ Отмена", f"subs:{site.id}:{kind}:{page}")]]))


@router.message(StateFilter(SubStates.waiting_user_id))
async def msg_sub_add(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    site = get_site_by_id(_config, data.get("site_id", ""))
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число, например 123456789")
        return
    if not site:
        await state.clear()
        await message.answer("Сайт не найден")
        return
    add_notify_user(_config, site.id, uid, _config_path)
    await state.clear()
    text, kb = subs_screen(site, data.get("kind", "all"), int(data.get("page", 0)))
    await message.answer("✅ Подписчик добавлен\n\n" + text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if not site:
        await callback.answer("Сайт не найден", show_alert=True)
        return
    kind, page = _parse_back(parts, 2)
    await _show(callback, f"🗑 Удалить <b>{_esc(site.name)}</b> из мониторинга?\n{_esc(site.url)}",
                _kb([[("🗑 Да, удалить", f"delc:{site.id}:{kind}:{page}"), ("✖️ Нет", f"site:{site.id}:{kind}:{page}")]]))


@router.callback_query(F.data.startswith("delc:"))
async def cb_delete_confirm(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    parts = callback.data.split(":")
    site = get_site_by_id(_config, parts[1])
    if site:
        remove_site(_config, site.id, _config_path)
        if _database:
            _database.delete_site_state(site.id)
    kind, page = _parse_back(parts, 2)
    text, kb = list_screen(callback.from_user.id, kind, page)
    await _show(callback, ("🗑 Сайт удалён\n\n" if site else "") + text, kb)


# ==================== Callback: добавление, поиск, проверка всех, журнал, админы ====================

@router.callback_query(F.data == "add")
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await state.set_state(AddStates.waiting_urls)
    await _show(callback, ADD_PROMPT, _kb([[("✖️ Отмена", "dash")]]))


@router.message(StateFilter(AddStates.waiting_urls))
async def msg_add_urls(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _is_admin(message.from_user.id):
        return
    await _add_sites_from_text(message, message.text or "")


@router.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_query)
    await _show(callback, "🔎 Введите часть названия или адреса сайта:", _kb([[("✖️ Отмена", "dash")]]))


@router.message(StateFilter(SearchStates.waiting_query))
async def msg_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = list_screen(message.from_user.id, f"q_{_clean_query(message.text or '')}", 0)
    await _show(message, text, kb)


@router.callback_query(F.data == "checkall")
async def cb_check_all(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Проверить все сайты может только администратор", show_alert=True)
        return
    if _check_all_lock.locked():
        await callback.answer("Проверка уже идёт", show_alert=True)
        return
    await callback.answer("⏳ Проверяю все сайты, это займёт до минуты")
    await _run_check_all(callback.message)


@router.callback_query(F.data == "changes")
async def cb_changes(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await _show(callback, _changes_text(), _kb([[("🏠 Меню", "dash")]]))


@router.callback_query(F.data == "admins")
async def cb_admins(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    text, kb = admins_screen()
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("admdel:"))
async def cb_admin_del(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    try:
        uid = int(callback.data.split(":")[1])
    except ValueError:
        return
    if uid == callback.from_user.id:
        await callback.answer("Себя удалить нельзя", show_alert=True)
        return
    if not remove_admin(_config, uid, _config_path):
        await callback.answer("Нельзя удалить последнего администратора", show_alert=True)
    text, kb = admins_screen()
    await _show(callback, text, kb)


@router.callback_query(F.data == "admadd")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_user_id)
    await _show(callback, "👑 Введите Telegram ID нового администратора:", _kb([[("✖️ Отмена", "admins")]]))


@router.message(StateFilter(AdminStates.waiting_user_id))
async def msg_admin_add(message: Message, state: FSMContext) -> None:
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число, например 123456789")
        return
    await state.clear()
    add_admin(_config, uid, _config_path)
    text, kb = admins_screen()
    await message.answer("✅ Администратор добавлен\n\n" + text, parse_mode="HTML", reply_markup=kb)


# Любой текст без состояния = поиск
@router.message(F.text & ~F.text.startswith("/"))
async def msg_free_text(message: Message) -> None:
    if message.chat.type != "private":
        return
    query = _clean_query(message.text or "")
    if not query:
        return
    text, kb = list_screen(message.from_user.id, f"q_{query}", 0)
    await _show(message, text, kb)


# ==================== Логика действий ====================

def _clean_query(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "", text.strip().lower())[:24]


def _find_site(key: str, user_id: int) -> Optional[SiteConfig]:
    key = key.strip().lower()
    site = get_site_by_id(_config, key)
    if site:
        return site if _can_view(user_id, site) else None
    visible = _visible_sites(user_id)
    for s in visible:
        host = (urlparse(s.url).hostname or "").lower()
        if key in (s.name.lower(), host, host.replace("www.", "")):
            return s
    matches = [s for s in visible if key in s.name.lower() or key in s.url.lower()]
    return matches[0] if len(matches) == 1 else None


def _toggle_mute(user_id: int, site: SiteConfig, mute: bool) -> str:
    if not _database:
        return "База недоступна"
    if mute:
        st = _database.get_state(site.id)
        if st.status != "DOWN":
            return f"{site.name} сейчас доступен, глушить нечего"
        _database.mute_for_user(user_id, site.id)
        return f"🔇 Напоминания по {site.name} отключены до восстановления"
    _database.unmute_for_user(user_id, site.id)
    return f"🔔 Напоминания по {site.name} включены"


def _changes_text() -> str:
    entries = read_changes(20, _config_path)
    if not entries:
        return "📓 Журнал изменений пуст"
    body = "\n".join(_esc(e) for e in entries)
    text = f"📓 <b>Журнал изменений</b> (последние {len(entries)})\n\n<code>{body}</code>"
    if len(text) > 4000:
        text = text[:3990] + "…</code>"
    return text


def _domain_to_site_id(domain: str) -> str:
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_") or "site"


async def _add_sites_from_text(message: Message, text: str) -> None:
    """Разбирает текст: ссылки + названия групп. Добавляет сайты и сразу проверяет."""
    tokens = [t for t in re.split(r"[\s,;]+", text.strip()) if t]
    groups = [t.lower() for t in tokens if t.lower() in KNOWN_GROUPS]
    urls = [t for t in tokens if t.lower() not in KNOWN_GROUPS]
    if not urls:
        await message.answer("Не вижу ссылок. Пример: <code>/add shop.ru hosting</code>", parse_mode="HTML")
        return
    groups = normalize_groups(groups) or [DEFAULT_GROUP]
    added: List[SiteConfig] = []
    skipped: List[str] = []
    for raw in urls:
        url = raw if raw.startswith(("http://", "https://")) else "https://" + raw
        host = urlparse(url).hostname
        if not host:
            skipped.append(f"{raw}: не похоже на адрес")
            continue
        site_id = _domain_to_site_id(host)
        if get_site_by_id(_config, site_id):
            skipped.append(f"{host}: уже в мониторинге")
            continue
        site = SiteConfig(id=site_id, name=host, url=url, support_level=groups[0], groups=list(groups))
        if add_site(_config, site, _config_path):
            added.append(site)
    lines = []
    if added:
        lines.append(f"➕ Добавлено: {len(added)} · группы: {_esc(groups_label(groups))}")
        if _scheduler:
            results = await asyncio.gather(*(_scheduler.check_single_site(s.id) for s in added), return_exceptions=True)
            for s, r in zip(added, results):
                lines.append(_esc(r if isinstance(r, str) else f"❌ {s.name} - ошибка проверки: {r}"))
    for sk in skipped:
        lines.append(f"⚠️ {_esc(sk)}")
    rows = [[("📄 " + s.name, f"site:{s.id}:all:0")] for s in added[:5]]
    if len(added) == 1:
        rows.append([("🏷 Группы", f"grps:{added[0].id}:all:0")])
    rows.append([("🏠 Меню", "dash")])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_kb(rows))


async def _run_check_all(message: Message) -> None:
    if _scheduler is None:
        await message.answer("Планировщик не запущен")
        return
    if _check_all_lock.locked():
        await message.answer("⏳ Проверка уже идёт, подождите")
        return
    async with _check_all_lock:
        status = await message.answer(f"⏳ Проверяю {len(_config.sites)} сайтов...")
        report = await _scheduler.run_immediate_check()
    lines = [l for l in report.split("\n") if l.strip()]
    bad = [l for l in lines if not l.startswith("✅")]
    ok_n = len(lines) - len(bad)
    text = f"🔍 <b>Проверка завершена</b>\n✅ {ok_n} в порядке"
    if bad:
        text += f" · ❌ {len(bad)} с ошибками:\n\n" + "\n".join(_esc(l) for l in bad[:40])
        if len(bad) > 40:
            text += f"\n… и ещё {len(bad) - 40}"
    try:
        await status.edit_text(text[:4000], parse_mode="HTML", reply_markup=_kb([[("🏠 Меню", "dash")]]))
    except Exception:
        await message.answer(text[:4000], parse_mode="HTML", reply_markup=_kb([[("🏠 Меню", "dash")]]))


# ==================== Запуск ====================

def setup_bot(
    config: Config,
    database: Database,
    notifier: TelegramNotifier,
    scheduler: MonitorScheduler = None,
    config_path: str = "config.yaml"
) -> tuple:
    """Настраивает и возвращает (Bot, Dispatcher)."""
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
    commands = [
        BotCommand(command="start", description="Главный экран"),
        BotCommand(command="down", description="Что сейчас лежит"),
        BotCommand(command="check", description="Проверить все сайты или один"),
        BotCommand(command="find", description="Найти сайт"),
        BotCommand(command="add", description="Добавить сайт (админ)"),
        BotCommand(command="changes", description="Журнал изменений (админ)"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="myid", description="Мой Telegram ID"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Меню команд установлено")


async def start_bot(bot: Bot, dp: Dispatcher) -> None:
    """Запускает бота в режиме polling."""
    await _setup_bot_commands(bot)
    # Если раньше бот работал через webhook (другой сервер), polling конфликтует с ним
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Telegram-бот запущен (polling)")
    await dp.start_polling(bot)


async def start_bot_webhook(bot: Bot, dp: Dispatcher, webhook_url: str, webhook_path: str,
                            host: str, port: int) -> web.Application:
    """Запускает бота в режиме webhook, возвращает aiohttp Application."""
    await _setup_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook установлен: {webhook_url}")
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)
    logger.info(f"Telegram-бот запущен (webhook) на {host}:{port}")
    return app


async def run_webhook_server(app: web.Application, host: str, port: int) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    while True:
        await asyncio.sleep(3600)
