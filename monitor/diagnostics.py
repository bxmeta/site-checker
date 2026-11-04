"""
Пошаговая диагностика недоступного сайта.

Запускается один раз, когда сайт переходит в DOWN, и отвечает на вопрос
"на каком уровне сломалось": DNS, сеть (ping), TCP-порт, TLS, HTTP.
Результат: список коротких строк для уведомления и вердикт одной фразой.

Отдельно проверяется случай "сайт жив, проблема в DNS": если домен не резолвится
или стал резолвиться на другой адрес, пробуем достучаться до сервера по IP из
прошлой удачной проверки с правильным Host/SNI.
"""
import asyncio
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp

from .ssl_checker import check_ssl, _to_punycode

logger = logging.getLogger("site_monitor")

STEP_TIMEOUT = 5


@dataclass
class Diagnosis:
    lines: List[str] = field(default_factory=list)
    verdict: str = ""          # где сломалось, одной фразой
    stage: str = ""            # dns / network / tcp / tls / http / app / content / ok
    ip: Optional[str] = None   # на что резолвится домен сейчас


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


async def _tcp_connect(ip: str, port: int) -> (bool, str):
    start = time.time()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=STEP_TIMEOUT)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, f"TCP {port}: ok ({int((time.time() - start) * 1000)} мс)"
    except asyncio.TimeoutError:
        return False, f"TCP {port}: нет ответа за {STEP_TIMEOUT} с (пакеты отбрасываются: файрвол, блокировка по IP или сервер выключен)"
    except ConnectionRefusedError:
        return False, f"TCP {port}: соединение отклонено (порт закрыт, веб-сервер не запущен)"
    except OSError as e:
        return False, f"TCP {port}: ошибка ({e.strerror or e})"


@dataclass
class _HttpProbe:
    line: str
    status: Optional[int] = None
    stage: str = "http"        # http / app


async def _http_probe(url: str, timeout_seconds: int, ip: Optional[str] = None) -> _HttpProbe:
    """HTTP-запрос без редиректов. С ip - к конкретному серверу, минуя DNS (Host/SNI остаются)."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    is_https = parsed.scheme == "https"
    port = parsed.port or (443 if is_https else 80)
    ctx = None
    if is_https:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    connector = None
    if ip:
        class _Resolver(aiohttp.abc.AbstractResolver):
            async def resolve(self, host, port=0, family=0):
                return [{"hostname": host, "host": ip, "port": port, "family": 2, "proto": 0, "flags": 0}]

            async def close(self):
                pass
        connector = aiohttp.TCPConnector(resolver=_Resolver(), ssl=ctx if ctx else False)
    start = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds), connector=connector) as session:
            async with session.get(url, ssl=ctx if ctx else False, allow_redirects=False) as resp:
                elapsed = int((time.time() - start) * 1000)
                server = resp.headers.get("Server", "")
                location = resp.headers.get("Location", "")
                extra = f", сервер {server}" if server else ""
                if location:
                    extra += f", редирект на {location}"
                where = f" (по IP {ip})" if ip else ""
                return _HttpProbe(f"HTTP{where}: {resp.status} {resp.reason} за {elapsed} мс{extra}", resp.status)
    except asyncio.TimeoutError:
        return _HttpProbe(
            f"HTTP: соединение есть, но ответа нет {timeout_seconds} с "
            f"(похоже, зависло приложение: PHP, база данных, очередь воркеров)", stage="app")
    except aiohttp.ServerDisconnectedError:
        return _HttpProbe("HTTP: сервер разорвал соединение, не ответив", stage="app")
    except aiohttp.ClientConnectorError as e:
        return _HttpProbe(f"HTTP: соединение не установлено ({e.os_error or e})")
    except aiohttp.ClientError as e:
        return _HttpProbe(f"HTTP: {type(e).__name__}: {e}")
    except Exception as e:
        return _HttpProbe(f"HTTP: {type(e).__name__}: {e}")


async def _alive_by_ip(url: str, last_ip: str, timeout_seconds: int) -> Optional[str]:
    """Отвечает ли сайт по старому IP (минуя DNS). Строка результата или None."""
    try:
        ok, _ = await _tcp_connect(last_ip, urlparse(url).port or (443 if url.startswith("https") else 80))
        if not ok:
            return None
        probe = await _http_probe(url, timeout_seconds, ip=last_ip)
        if probe.status is not None:
            return f"Сайт отвечает по прежнему IP {last_ip}: {probe.status}"
    except Exception as e:
        logger.debug(f"alive_by_ip {url}: {e}")
    return None


async def diagnose(url: str, timeout_seconds: int = 10, last_ip: Optional[str] = None,
                   error_type: Optional[str] = None) -> Diagnosis:
    """
    Пошаговая диагностика URL. Никогда не бросает исключений.

    Args:
        url: адрес сайта
        timeout_seconds: таймаут HTTP-шага
        last_ip: IP из последней удачной проверки (для теста "сайт жив, проблема в DNS")
        error_type: тип ошибки основной проверки (уточняет вердикт)
    """
    d = Diagnosis()
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        is_https = parsed.scheme == "https"
        port = parsed.port or (443 if is_https else 80)

        # 1. DNS
        try:
            ips = await _resolve(hostname)
        except asyncio.TimeoutError:
            ips = None
            d.lines.append("DNS: резолвер не ответил за 5 с")
        except OSError as e:
            ips = None
            d.lines.append(f"DNS: имя {hostname} не разрешается ({e.strerror or e})")
        if not ips:
            d.stage = "dns"
            d.verdict = "Домен не разрешается в IP: удалён, не продлён или сломана DNS-зона"
            if last_ip:
                alive = await _alive_by_ip(url, last_ip, timeout_seconds)
                if alive:
                    d.lines.append(alive)
                    d.verdict = f"Сервер жив (отвечает по IP {last_ip}), проблема только в DNS или регистрации домена"
                else:
                    d.lines.append(f"По прежнему IP {last_ip} сайт тоже не отвечает")
            return d
        ip = ips[0]
        d.ip = ip
        d.lines.append(f"DNS: ok ({', '.join(ips[:3])})")
        if last_ip and last_ip not in ips:
            d.lines.append(f"DNS: IP изменился, раньше было {last_ip}")

        # 2. ICMP
        ping_ms = await _ping(ip)
        if ping_ms is None:
            d.lines.append("Ping: нет ответа (сервер выключен или ICMP закрыт)")
        else:
            d.lines.append(f"Ping: ok ({ping_ms:.0f} мс)")

        # 3. TCP
        tcp_ok, tcp_line = await _tcp_connect(ip, port)
        d.lines.append(tcp_line)
        if not tcp_ok:
            if ping_ms is None:
                d.stage = "network"
                d.verdict = "Сервер не в сети: выключен, недоступен по сети или блокирует наш IP"
            else:
                d.stage = "tcp"
                d.verdict = f"Сервер в сети (ping есть), но порт {port} не отвечает: веб-сервер не запущен или закрыт файрволом"
            if last_ip and last_ip != ip:
                alive = await _alive_by_ip(url, last_ip, timeout_seconds)
                if alive:
                    d.lines.append(alive)
                    d.verdict = f"DNS переведён на {ip}, который не отвечает; по прежнему IP {last_ip} сайт жив. Проблема в DNS"
                    d.stage = "dns"
            return d

        # 4. TLS
        if is_https:
            ssl_result = await check_ssl(hostname, port, timeout=STEP_TIMEOUT)
            if ssl_result.valid:
                d.lines.append(
                    f"TLS: ok (сертификат до {ssl_result.expiry_str}, {ssl_result.days_until_expiry} дн., "
                    f"издатель {ssl_result.issuer_str})"
                )
            else:
                d.lines.append(f"TLS: {ssl_result.error}")
                if ssl_result.error_type in ("timeout", "ssl_handshake", "conn_refused", "no_response"):
                    d.stage = "tls"
                    d.verdict = "Сервер и порт живы, но TLS не поднимается: HTTPS отключён или упал прокси/балансировщик"
                    return d
                d.stage = "tls"
                d.verdict = "Сервер и веб-сервер живы, проблема в SSL-сертификате"

        # 5. HTTP
        probe = await _http_probe(url, timeout_seconds)
        d.lines.append(probe.line)
        if d.verdict:
            return d
        if probe.stage == "app":
            d.stage = "app"
            d.verdict = "Веб-сервер принимает соединения, но приложение не отвечает: зависло или перегружено"
        elif probe.status is None:
            d.stage = "http"
            d.verdict = "Веб-сервер жив, но HTTP-запрос не проходит"
        elif probe.status >= 500:
            d.stage = "app"
            d.verdict = f"Веб-сервер жив, ошибка приложения (HTTP {probe.status}): PHP, база данных или конфигурация"
        elif probe.status in (401, 403):
            d.stage = "http"
            d.verdict = f"Сайт отвечает, но закрыт (HTTP {probe.status}): пароль, блокировка по IP или защита от ботов"
        elif probe.status == 404:
            d.stage = "http"
            d.verdict = "Сайт отвечает, но главной страницы нет (HTTP 404): сломан роутинг или удалён код"
        elif 300 <= probe.status < 400:
            d.stage = "http"
            d.verdict = "Сайт отвечает редиректом; проверьте цепочку редиректов или ожидаемый код" if error_type != "redirect_loop" \
                else "Редиректы ведут по кругу: правила http->https и www"
        elif error_type == "keyword_missing":
            d.stage = "content"
            d.verdict = "Сайт отвечает, но содержимое не то: заглушка хостинга, ошибка CMS или дефейс"
        else:
            d.stage = "ok"
            d.verdict = "Сейчас сайт отвечает нормально: сбой был кратковременным или зависит от условий (нагрузка, гео)"
    except Exception as e:
        logger.error(f"Диагностика {url}: {type(e).__name__}: {e}")
        d.lines.append(f"Диагностика прервана: {type(e).__name__}: {e}")
    return d
