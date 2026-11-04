"""
Модуль HTTP-проверок сайтов.

Типы ошибок (error_type):
- timeout         - сервер не ответил за отведённое время
- dns             - имя не разрешается
- conn_refused    - порт закрыт
- conn_reset      - сервер сбросил соединение
- no_response     - прочие сетевые ошибки
- wrong_code      - неверный HTTP-код
- redirect_loop   - слишком много редиректов
- bad_response    - ответ оборван или некорректен
- keyword_missing - не найдено ключевое слово
- ssl_*           - см. ssl_checker
"""
import asyncio
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp

from .config_loader import SiteConfig, DefaultConfig, SslConfig
from .ssl_checker import check_ssl, SSLCheckResult
from .keyword_checker import check_keywords


def _create_ssl_context() -> ssl.SSLContext:
    """SSL контекст без проверки цепочки: страницу надо получить даже с битым сертификатом,
    доверие цепочки проверяется отдельно в ssl_checker."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass
class CheckResult:
    """Результат проверки сайта."""
    success: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[int] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    html_content: Optional[str] = None
    ssl: Optional[SSLCheckResult] = None       # результат SSL-проверки, если выполнялась
    diagnostics: List[str] = field(default_factory=list)  # пошаговая диагностика при падении


def _elapsed_ms(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


def _classify_connector_error(e: aiohttp.ClientConnectorError, hostname: str, port: int) -> tuple:
    """Разбирает ClientConnectorError в (error_type, человекочитаемое описание)."""
    if isinstance(e, aiohttp.ClientConnectorCertificateError):
        return "ssl_untrusted", f"TLS: сертификат не прошёл проверку ({e.certificate_error})"
    if isinstance(e, aiohttp.ClientConnectorSSLError):
        return "ssl_handshake", f"TLS-рукопожатие не удалось: {e.os_error or e}"

    os_err = e.os_error
    if isinstance(os_err, socket.gaierror):
        return "dns", f"DNS: имя {hostname} не разрешается ({os_err.strerror or os_err})"
    if isinstance(os_err, ConnectionRefusedError):
        return "conn_refused", f"TCP: порт {port} закрыт, соединение отклонено"
    if isinstance(os_err, ConnectionResetError):
        return "conn_reset", "TCP: сервер сбросил соединение"
    if isinstance(os_err, (socket.timeout, asyncio.TimeoutError)) or (
        os_err is not None and getattr(os_err, "errno", None) in (110, 60)  # ETIMEDOUT linux/mac
    ):
        return "timeout", f"TCP: нет ответа от {hostname}:{port} (соединение не устанавливается)"
    if os_err is not None and getattr(os_err, "errno", None) in (101, 113):  # ENETUNREACH, EHOSTUNREACH
        return "no_response", f"Сеть: хост {hostname} недостижим ({os_err.strerror})"

    detail = os_err.strerror if os_err is not None and getattr(os_err, "strerror", None) else str(os_err or e)
    return "no_response", f"Не удалось подключиться к {hostname}:{port} ({detail})"


async def check_site(
    site: SiteConfig,
    defaults: DefaultConfig,
    ssl_config: Optional[SslConfig] = None,
) -> CheckResult:
    """
    Выполняет проверку сайта: HTTP-код, SSL, ключевые слова.

    Args:
        site: Конфигурация сайта
        defaults: Настройки по умолчанию
        ssl_config: Настройки SSL (список издателей с автопродлением)

    Returns:
        CheckResult с результатами проверки. error никогда не пустой при success=False.
    """
    timeout = aiohttp.ClientTimeout(total=defaults.timeout_seconds)
    start_time = time.time()
    parsed = urlparse(site.url)
    hostname = parsed.hostname or site.url
    is_https = parsed.scheme == "https"
    port = parsed.port or (443 if is_https else 80)
    auto_renew_issuers = ssl_config.auto_renew_issuers if ssl_config else None

    try:
        ssl_context = _create_ssl_context() if is_https else False
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(site.url, ssl=ssl_context) as response:
                response_time_ms = _elapsed_ms(start_time)
                status_code = response.status
                html_content = await response.text(errors="replace")

                if site.check_http_code and status_code != site.expected_code:
                    return CheckResult(
                        success=False,
                        status_code=status_code,
                        response_time_ms=response_time_ms,
                        error=f"HTTP {status_code} {response.reason or ''}".strip()
                              + f", ожидался {site.expected_code}",
                        error_type="wrong_code",
                        html_content=html_content,
                    )

                ssl_result: Optional[SSLCheckResult] = None
                if site.check_ssl and is_https:
                    ssl_result = await check_ssl(
                        hostname, port,
                        timeout=defaults.timeout_seconds,
                        auto_renew_issuers=auto_renew_issuers,
                    )
                    if not ssl_result.valid:
                        return CheckResult(
                            success=False,
                            status_code=status_code,
                            response_time_ms=response_time_ms,
                            error=ssl_result.error,
                            error_type=ssl_result.error_type,
                            html_content=html_content,
                            ssl=ssl_result,
                        )

                if site.keywords:
                    keyword_result = check_keywords(html_content, site.keywords)
                    if not keyword_result.found:
                        return CheckResult(
                            success=False,
                            status_code=status_code,
                            response_time_ms=response_time_ms,
                            error=f"На странице нет ключевого слова \"{keyword_result.missing_keyword}\"",
                            error_type="keyword_missing",
                            html_content=html_content,
                            ssl=ssl_result,
                        )

                return CheckResult(
                    success=True,
                    status_code=status_code,
                    response_time_ms=response_time_ms,
                    html_content=html_content,
                    ssl=ssl_result,
                )

    except aiohttp.ClientConnectorError as e:
        error_type, description = _classify_connector_error(e, hostname, port)
        return CheckResult(success=False, response_time_ms=_elapsed_ms(start_time),
                           error=description, error_type=error_type)

    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
        # asyncio.TimeoutError имеет пустой str(): раньше это давало "Unexpected error: "
        return CheckResult(
            success=False,
            response_time_ms=_elapsed_ms(start_time),
            error=f"Таймаут: сервер не ответил за {defaults.timeout_seconds} с",
            error_type="timeout",
        )

    except aiohttp.ServerDisconnectedError:
        return CheckResult(success=False, response_time_ms=_elapsed_ms(start_time),
                           error="Сервер разорвал соединение, не прислав ответ", error_type="conn_reset")

    except aiohttp.TooManyRedirects:
        return CheckResult(success=False, response_time_ms=_elapsed_ms(start_time),
                           error="Слишком много редиректов (цикл переадресации)", error_type="redirect_loop")

    except aiohttp.ClientPayloadError as e:
        return CheckResult(success=False, response_time_ms=_elapsed_ms(start_time),
                           error=f"Ответ сервера оборван: {e}", error_type="bad_response")

    except aiohttp.ClientResponseError as e:
        return CheckResult(success=False, status_code=e.status, response_time_ms=_elapsed_ms(start_time),
                           error=f"Некорректный HTTP-ответ: {e.message}", error_type="bad_response")

    except aiohttp.ClientError as e:
        return CheckResult(success=False, response_time_ms=_elapsed_ms(start_time),
                           error=f"Сетевая ошибка: {type(e).__name__}: {e}", error_type="no_response")

    except ConnectionResetError:
        return CheckResult(success=False, response_time_ms=_elapsed_ms(start_time),
                           error="TCP: сервер сбросил соединение", error_type="conn_reset")

    except Exception as e:
        return CheckResult(
            success=False,
            response_time_ms=_elapsed_ms(start_time),
            error=f"Непредвиденная ошибка: {type(e).__name__}: {e or 'без описания'}",
            error_type="no_response",
        )
