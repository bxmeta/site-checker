"""
Общие тексты уведомлений для всех каналов (Telegram, Bitrix24).

Каналы отличаются только разметкой жирного текста, поэтому функции принимают
callable `bold`, а собирают текст одинаково.
"""
from typing import Callable, List, Optional

from .checker import CheckResult
from .config_loader import SiteConfig
from .ssl_checker import SSLCheckResult
from .time_utils import format_for_message

Bold = Callable[[str], str]

# Заголовок и короткое пояснение по типу ошибки
_FAILURE_TITLES = {
    "timeout": ("🚨", "Сайт не отвечает"),
    "dns": ("🌐", "Домен не разрешается"),
    "conn_refused": ("🚨", "Веб-сервер не отвечает"),
    "conn_reset": ("🚨", "Сервер обрывает соединение"),
    "no_response": ("🚨", "Сайт недоступен"),
    "wrong_code": ("⚠️", "Неверный код ответа"),
    "redirect_loop": ("⚠️", "Цикл переадресации"),
    "bad_response": ("⚠️", "Некорректный ответ сервера"),
    "keyword_missing": ("⚠️", "Контент изменился"),
    "ssl_expired": ("🔐", "SSL-сертификат истёк"),
    "ssl_not_yet_valid": ("🔐", "SSL-сертификат ещё не действует"),
    "ssl_mismatch": ("🔐", "SSL-сертификат на другой домен"),
    "ssl_untrusted": ("🔐", "SSL-сертификат не доверенный"),
    "ssl_handshake": ("🔐", "Ошибка TLS-соединения"),
}

# Подсказка, что обычно означает ошибка
_FAILURE_HINTS = {
    "timeout": "Сервер принимает соединение или молчит дольше лимита. Смотрите диагностику ниже.",
    "dns": "Проверьте, не истекла ли регистрация домена и жива ли DNS-зона.",
    "conn_refused": "Порт закрыт: nginx/apache не запущен или слушает другой порт.",
    "conn_reset": "Сервер принимает соединение и сразу сбрасывает: падает веб-сервер или фильтрует трафик.",
    "redirect_loop": "Редиректы ведут по кругу: проверьте правила http->https и www.",
    "keyword_missing": "Страница отвечает, но без ожидаемого текста: заглушка хостинга, ошибка CMS или дефейс.",
    "ssl_expired": "Нужно перевыпустить и установить сертификат.",
    "ssl_not_yet_valid": "Скорее всего, неверное время на сервере.",
    "ssl_mismatch": "На этом домене установлен чужой сертификат (часто дефолтный сертификат хостинга).",
    "ssl_untrusted": "Браузеры показывают предупреждение. Чаще всего не установлен промежуточный сертификат.",
    "ssl_handshake": "Сервер не поднимает TLS: отключён HTTPS, упал прокси или блокировка.",
}


def failure_title(result: CheckResult) -> str:
    emoji, title = _FAILURE_TITLES.get(result.error_type or "", ("🚨", "Сайт недоступен"))
    return f"{emoji} {title}"


def failure_description(site: SiteConfig, result: CheckResult) -> str:
    """Одна строка с сутью проблемы."""
    error_type = result.error_type or ""
    if error_type == "wrong_code":
        code = result.status_code if result.status_code is not None else "нет"
        return f"ожидался код {site.expected_code}, получен {code}"
    return result.error or "причина не определена"


def _ssl_lines(ssl_result: Optional[SSLCheckResult]) -> List[str]:
    if not ssl_result or not ssl_result.expiry_date:
        return []
    lines = [f"Издатель сертификата: {ssl_result.issuer_str}"]
    if ssl_result.days_until_expiry is not None:
        if ssl_result.days_until_expiry >= 0:
            lines.append(f"Срок действия: до {ssl_result.expiry_str} ({ssl_result.days_until_expiry} дн.)")
        else:
            lines.append(f"Срок действия: истёк {ssl_result.expiry_str}")
    return lines


def format_down_message(site: SiteConfig, result: CheckResult, bold: Bold) -> str:
    """Уведомление о падении сайта с диагностикой."""
    status_code_str = str(result.status_code) if result.status_code is not None else "нет ответа"
    lines = [
        bold(failure_title(result)),
        "",
        f"Название: {site.name}",
        f"URL: {site.url}",
        f"Поддержка: {site.support_level}",
        f"Проблема: {failure_description(site, result)}",
    ]
    hint = _FAILURE_HINTS.get(result.error_type or "")
    if hint:
        lines.append(f"Что это значит: {hint}")
    lines.append(f"Код: {status_code_str}")
    if result.response_time_ms is not None:
        lines.append(f"Время ответа: {result.response_time_ms} мс")
    lines.extend(_ssl_lines(result.ssl))
    if result.diagnostics:
        lines.append("")
        lines.append(bold("Диагностика:"))
        lines.extend(f"• {line}" for line in result.diagnostics)
    lines.append("")
    lines.append(f"Время: {format_for_message()}")
    return "\n".join(lines)


def format_ssl_expiring_message(site: SiteConfig, ssl_result: SSLCheckResult, bold: Bold) -> str:
    """Предупреждение об истечении платного сертификата."""
    days = ssl_result.days_until_expiry if ssl_result.days_until_expiry is not None else 0
    if days <= 0:
        when = "сегодня"
    elif days == 1:
        when = "завтра"
    else:
        when = f"через {days} дн."
    return "\n".join([
        bold(f"🔐 SSL-сертификат истекает {when}"),
        "",
        f"Название: {site.name}",
        f"URL: {site.url}",
        f"Поддержка: {site.support_level}",
        f"Истекает: {ssl_result.expiry_str}",
        f"Издатель: {ssl_result.issuer_str}",
        f"Выдан на: {ssl_result.subject_cn or '-'}",
        "",
        "Сертификат не из автопродляемых (не Let's Encrypt): нужно купить/перевыпустить "
        "и установить новый вручную до истечения срока.",
        "",
        f"Время: {format_for_message()}",
    ])


def format_check_line(site: SiteConfig, result: CheckResult) -> str:
    """Строка для отчёта /check: ✅ site.ru (200) 230 мс / ❌ site.ru - причина."""
    if result.success:
        code = f" ({result.status_code})" if result.status_code else ""
        rt = f" {result.response_time_ms} мс" if result.response_time_ms is not None else ""
        return f"✅ {site.name}{code}{rt}"
    code = f" ({result.status_code})" if result.status_code else ""
    return f"❌ {site.name}{code} - {failure_description(site, result)}"
