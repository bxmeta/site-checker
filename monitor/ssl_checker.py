"""
Модуль проверки SSL-сертификатов.
"""
import asyncio
import ssl
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


def _to_punycode(hostname: str) -> str:
    """
    Конвертирует IDN-домен (например, кириллический) в Punycode.

    Args:
        hostname: Имя хоста (может быть IDN или ASCII)

    Returns:
        ASCII-совместимое имя хоста (Punycode)
    """
    try:
        return hostname.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeDecodeError):
        return hostname


@dataclass
class SSLCheckResult:
    """Результат проверки SSL-сертификата."""
    valid: bool
    error: Optional[str] = None
    error_type: Optional[str] = None  # ssl_expired, ssl_mismatch
    expiry_date: Optional[datetime] = None
    days_until_expiry: Optional[int] = None
    subject_cn: Optional[str] = None
    san_list: Optional[List[str]] = None


def _get_certificate_info(hostname: str, port: int = 443) -> dict:
    """
    Получает информацию о SSL-сертификате.

    Args:
        hostname: Имя хоста (поддерживает IDN/кириллицу)
        port: Порт (по умолчанию 443)

    Returns:
        Словарь с информацией о сертификате
    """
    ascii_hostname = _to_punycode(hostname)
    context = ssl.create_default_context()
    with socket.create_connection((ascii_hostname, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=ascii_hostname) as ssock:
            cert = ssock.getpeercert()
            return cert


def _parse_cert_date(date_str: str) -> datetime:
    """Парсит дату сертификата в datetime."""
    return datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")


def _get_cn_from_subject(subject: tuple) -> Optional[str]:
    """Извлекает Common Name из subject сертификата."""
    for item in subject:
        for key, value in item:
            if key == "commonName":
                return value
    return None


def _get_san_list(cert: dict) -> List[str]:
    """Извлекает список Subject Alternative Names из сертификата."""
    san_list = []
    for san_type, san_value in cert.get("subjectAltName", []):
        if san_type == "DNS":
            san_list.append(san_value)
    return san_list


def _hostname_matches_cert(hostname: str, cn: Optional[str], san_list: List[str]) -> bool:
    """
    Проверяет, соответствует ли имя хоста сертификату.

    Поддерживает IDN-домены: сравнивает как оригинальное имя,
    так и его Punycode-версию с именами в сертификате.

    Args:
        hostname: Имя хоста для проверки (может быть IDN)
        cn: Common Name из сертификата
        san_list: Список Subject Alternative Names

    Returns:
        True, если имя хоста соответствует сертификату
    """
    all_names = san_list.copy()
    if cn:
        all_names.append(cn)

    ascii_hostname = _to_punycode(hostname)
    hostnames_to_check = {hostname, ascii_hostname}

    for name in all_names:
        if name.startswith("*."):
            wildcard_domain = name[2:]
            for h in hostnames_to_check:
                parts = h.split(".", 1)
                if len(parts) == 2 and parts[1] == wildcard_domain:
                    return True
        else:
            if name in hostnames_to_check:
                return True

    return False


async def check_ssl(hostname: str, port: int = 443) -> SSLCheckResult:
    """
    Проверяет SSL-сертификат сайта.

    Args:
        hostname: Имя хоста
        port: Порт (по умолчанию 443)

    Returns:
        SSLCheckResult с результатами проверки
    """
    try:
        loop = asyncio.get_event_loop()
        cert = await loop.run_in_executor(
            None,
            _get_certificate_info,
            hostname,
            port
        )

        not_after = cert.get("notAfter")
        if not not_after:
            return SSLCheckResult(
                valid=False,
                error="SSL certificate has no expiry date",
                error_type="ssl_expired"
            )

        expiry_date = _parse_cert_date(not_after)
        now = datetime.utcnow()
        days_until_expiry = (expiry_date - now).days

        if days_until_expiry < 0:
            return SSLCheckResult(
                valid=False,
                error=f"SSL certificate expired {abs(days_until_expiry)} days ago",
                error_type="ssl_expired",
                expiry_date=expiry_date,
                days_until_expiry=days_until_expiry
            )

        subject = cert.get("subject", ())
        cn = _get_cn_from_subject(subject)
        san_list = _get_san_list(cert)

        if not _hostname_matches_cert(hostname, cn, san_list):
            return SSLCheckResult(
                valid=False,
                error=f"SSL certificate CN/SAN mismatch. Hostname: {hostname}, CN: {cn}, SAN: {san_list}",
                error_type="ssl_mismatch",
                expiry_date=expiry_date,
                days_until_expiry=days_until_expiry,
                subject_cn=cn,
                san_list=san_list
            )

        return SSLCheckResult(
            valid=True,
            expiry_date=expiry_date,
            days_until_expiry=days_until_expiry,
            subject_cn=cn,
            san_list=san_list
        )

    except ssl.SSLCertVerificationError as e:
        return SSLCheckResult(
            valid=False,
            error=f"SSL verification error: {str(e)}",
            error_type="ssl_mismatch"
        )

    except ssl.SSLError as e:
        return SSLCheckResult(
            valid=False,
            error=f"SSL error: {str(e)}",
            error_type="ssl_expired"
        )

    except socket.timeout:
        return SSLCheckResult(
            valid=False,
            error="SSL connection timeout",
            error_type="ssl_expired"
        )

    except socket.gaierror as e:
        return SSLCheckResult(
            valid=False,
            error=f"DNS resolution error: {str(e)}",
            error_type="ssl_mismatch"
        )

    except Exception as e:
        return SSLCheckResult(
            valid=False,
            error=f"SSL check error: {str(e)}",
            error_type="ssl_expired"
        )
