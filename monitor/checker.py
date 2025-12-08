"""
Модуль HTTP-проверок сайтов.
"""
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import aiohttp

from .config_loader import SiteConfig, DefaultConfig
from .ssl_checker import check_ssl
from .keyword_checker import check_keywords


@dataclass
class CheckResult:
    """Результат проверки сайта."""
    success: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[int] = None
    error: Optional[str] = None
    error_type: Optional[str] = None  # timeout, no_response, wrong_code, ssl_expired, ssl_mismatch, keyword_missing
    html_content: Optional[str] = None


async def check_site(
    site: SiteConfig,
    defaults: DefaultConfig
) -> CheckResult:
    """
    Выполняет проверку сайта.

    Args:
        site: Конфигурация сайта
        defaults: Настройки по умолчанию

    Returns:
        CheckResult с результатами проверки
    """
    timeout = aiohttp.ClientTimeout(total=defaults.timeout_seconds)
    start_time = time.time()

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(site.url, ssl=True) as response:
                response_time_ms = int((time.time() - start_time) * 1000)
                status_code = response.status
                html_content = await response.text()

                if site.check_http_code and status_code != site.expected_code:
                    return CheckResult(
                        success=False,
                        status_code=status_code,
                        response_time_ms=response_time_ms,
                        error=f"Wrong HTTP code: {status_code}, expected: {site.expected_code}",
                        error_type="wrong_code"
                    )

                if site.check_ssl and site.url.startswith("https://"):
                    hostname = urlparse(site.url).hostname
                    ssl_result = await check_ssl(hostname)
                    if not ssl_result.valid:
                        return CheckResult(
                            success=False,
                            status_code=status_code,
                            response_time_ms=response_time_ms,
                            error=ssl_result.error,
                            error_type=ssl_result.error_type
                        )

                if site.keywords:
                    keyword_result = check_keywords(html_content, site.keywords)
                    if not keyword_result.found:
                        return CheckResult(
                            success=False,
                            status_code=status_code,
                            response_time_ms=response_time_ms,
                            error=f"Keyword missing: {keyword_result.missing_keyword}",
                            error_type="keyword_missing",
                            html_content=html_content
                        )

                return CheckResult(
                    success=True,
                    status_code=status_code,
                    response_time_ms=response_time_ms,
                    html_content=html_content
                )

    except aiohttp.ClientConnectorError as e:
        response_time_ms = int((time.time() - start_time) * 1000)
        return CheckResult(
            success=False,
            response_time_ms=response_time_ms,
            error=f"Connection error: {str(e)}",
            error_type="no_response"
        )

    except aiohttp.ServerTimeoutError:
        response_time_ms = int((time.time() - start_time) * 1000)
        return CheckResult(
            success=False,
            response_time_ms=response_time_ms,
            error="Timeout",
            error_type="timeout"
        )

    except aiohttp.ClientError as e:
        response_time_ms = int((time.time() - start_time) * 1000)
        return CheckResult(
            success=False,
            response_time_ms=response_time_ms,
            error=f"Client error: {str(e)}",
            error_type="no_response"
        )

    except Exception as e:
        response_time_ms = int((time.time() - start_time) * 1000)
        return CheckResult(
            success=False,
            response_time_ms=response_time_ms,
            error=f"Unexpected error: {str(e)}",
            error_type="no_response"
        )
