"""
Пошаговая диагностика недоступного сайта.

Запускается один раз, когда сайт переходит в DOWN, и отвечает на вопрос
"на каком уровне сломалось": DNS, сеть (ping), TCP-порт, TLS, HTTP.
Результат - список коротких строк для уведомления.
"""
import asyncio
import logging
import ssl
import time
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp

from .ssl_checker import check_ssl, _to_punycode

logger = logging.getLogger("site_monitor")

STEP_TIMEOUT = 5


async def _resolve(hostname: str) -> List[str]:
    loop = asyncio.get_event_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(_to_punycode(hostname), None, proto=6), timeout=STEP_TIMEOUT
    )
    ips: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


async def _ping(ip: str) -> Optional[float]:
    """Возвращает время ответа в мс или None, если хост не отвечает на ICMP."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=STEP_TIMEOUT)
        if proc.returncode != 0:
            return None
        for part in out.decode(errors="ignore").split():
            if part.startswith("time="):
                return float(part[5:])
        return 0.0
    except Exception:
        return None


async def _tcp_connect(ip: str, port: int) -> str:
    start = time.time()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=STEP_TIMEOUT)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return f"TCP {port}: ok ({int((time.time() - start) * 1000)} мс)"
    except asyncio.TimeoutError:
        return f"TCP {port}: нет ответа за {STEP_TIMEOUT} с (пакеты отбрасываются: файрвол, блокировка по IP или сервер выключен)"
    except ConnectionRefusedError:
        return f"TCP {port}: соединение отклонено (порт закрыт, веб-сервер не запущен)"
    except OSError as e:
        return f"TCP {port}: ошибка ({e.strerror or e})"


async def _http_probe(url: str, timeout_seconds: int) -> str:
    ctx = None
    if url.startswith("https://"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    start = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as session:
            async with session.get(url, ssl=ctx if ctx else False, allow_redirects=False) as resp:
                elapsed = int((time.time() - start) * 1000)
                server = resp.headers.get("Server", "")
                location = resp.headers.get("Location", "")
                extra = f", сервер {server}" if server else ""
                if location:
                    extra += f", редирект на {location}"
                return f"HTTP: {resp.status} {resp.reason} за {elapsed} мс{extra}"
    except asyncio.TimeoutError:
        return (
            f"HTTP: соединение есть, но ответа нет {timeout_seconds} с "
            f"(похоже, зависло приложение: PHP, база данных, очередь воркеров)"
        )
    except aiohttp.ServerDisconnectedError:
        return "HTTP: сервер разорвал соединение, не ответив"
    except aiohttp.ClientConnectorError as e:
        return f"HTTP: соединение не установлено ({e.os_error or e})"
    except aiohttp.ClientError as e:
        return f"HTTP: {type(e).__name__}: {e}"
    except Exception as e:
        return f"HTTP: {type(e).__name__}: {e}"


async def diagnose(url: str, timeout_seconds: int = 10) -> List[str]:
    """
    Пошаговая диагностика URL. Никогда не бросает исключений.

    Returns:
        Список строк вида "DNS: ok (1.2.3.4)", "TCP 443: ...", "TLS: ...", "HTTP: ..."
    """
    lines: List[str] = []
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        is_https = parsed.scheme == "https"
        port = parsed.port or (443 if is_https else 80)

        # 1. DNS
        try:
            ips = await _resolve(hostname)
        except asyncio.TimeoutError:
            lines.append("DNS: резолвер не ответил за 5 с")
            return lines
        except OSError as e:
            lines.append(f"DNS: имя {hostname} не разрешается ({e.strerror or e}). Домен удалён, не продлён или сломана DNS-зона")
            return lines
        if not ips:
            lines.append(f"DNS: для {hostname} нет A/AAAA-записей")
            return lines
        lines.append(f"DNS: ok ({', '.join(ips[:3])})")
        ip = ips[0]

        # 2. ICMP
        ping_ms = await _ping(ip)
        if ping_ms is None:
            lines.append("Ping: нет ответа (сервер выключен или ICMP закрыт)")
        else:
            lines.append(f"Ping: ok ({ping_ms:.0f} мс)")

        # 3. TCP
        tcp_line = await _tcp_connect(ip, port)
        lines.append(tcp_line)
        if "ok" not in tcp_line:
            return lines

        # 4. TLS
        if is_https:
            ssl_result = await check_ssl(hostname, port, timeout=STEP_TIMEOUT)
            if ssl_result.valid:
                lines.append(
                    f"TLS: ok (сертификат до {ssl_result.expiry_str}, {ssl_result.days_until_expiry} дн., "
                    f"издатель {ssl_result.issuer_str})"
                )
            else:
                lines.append(f"TLS: {ssl_result.error}")
                if ssl_result.error_type in ("timeout", "ssl_handshake", "conn_refused", "no_response"):
                    return lines

        # 5. HTTP
        lines.append(await _http_probe(url, timeout_seconds))
    except Exception as e:
        logger.error(f"Диагностика {url}: {type(e).__name__}: {e}")
        lines.append(f"Диагностика прервана: {type(e).__name__}: {e}")
    return lines
