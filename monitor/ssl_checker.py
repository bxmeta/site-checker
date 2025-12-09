"""
Модуль проверки SSL-сертификатов.
"""
import asyncio
import ssl
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtensionOID


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


def _get_certificate_binary(hostname: str, port: int = 443) -> bytes:
    """
    Получает SSL-сертификат в бинарном формате (DER).

    Args:
        hostname: Имя хоста (поддерживает IDN/кириллицу)
        port: Порт (по умолчанию 443)

    Returns:
        Сертификат в DER формате
    """
    ascii_hostname = _to_punycode(hostname)

    # Используем контекст без проверки цепочки,
    # чтобы получить сертификат даже если цепочка неполная
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((ascii_hostname, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=ascii_hostname) as ssock:
            cert_der = ssock.getpeercert(binary_form=True)
            return cert_der


def _parse_certificate(cert_der: bytes) -> dict:
    """
    Парсит сертификат из DER формата с помощью cryptography.

    Args:
        cert_der: Сертификат в DER формате

    Returns:
        Словарь с информацией о сертификате
    """
    cert = x509.load_der_x509_certificate(cert_der)

    # Извлекаем CN из subject
    cn = None
    try:
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn_attrs:
            cn = cn_attrs[0].value
    except Exception:
        pass

    # Извлекаем SAN (Subject Alternative Names)
    san_list = []
    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san_list = [name.value for name in san_ext.value if isinstance(name, x509.DNSName)]
    except x509.ExtensionNotFound:
        pass

    # Даты
    not_after = cert.not_valid_after_utc if hasattr(cert, 'not_valid_after_utc') else cert.not_valid_after.replace(tzinfo=timezone.utc)

    return {
        'cn': cn,
        'san_list': san_list,
        'not_after': not_after
    }


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
    hostnames_to_check = {hostname.lower(), ascii_hostname.lower()}

    for name in all_names:
        name_lower = name.lower()
        if name_lower.startswith("*."):
            wildcard_domain = name_lower[2:]
            for h in hostnames_to_check:
                parts = h.split(".", 1)
                if len(parts) == 2 and parts[1] == wildcard_domain:
                    return True
        else:
            if name_lower in hostnames_to_check:
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
        cert_der = await loop.run_in_executor(
            None,
            _get_certificate_binary,
            hostname,
            port
        )

        cert_info = _parse_certificate(cert_der)

        expiry_date = cert_info['not_after']
        now = datetime.now(timezone.utc)
        days_until_expiry = (expiry_date - now).days

        cn = cert_info['cn']
        san_list = cert_info['san_list']

        if days_until_expiry < 0:
            return SSLCheckResult(
                valid=False,
                error=f"SSL-сертификат истёк {abs(days_until_expiry)} дней назад",
                error_type="ssl_expired",
                expiry_date=expiry_date,
                days_until_expiry=days_until_expiry,
                subject_cn=cn,
                san_list=san_list
            )

        if not _hostname_matches_cert(hostname, cn, san_list):
            return SSLCheckResult(
                valid=False,
                error=f"Сертификат выдан для другого домена. Хост: {hostname}, CN: {cn}, SAN: {san_list}",
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

    except ssl.SSLError as e:
        return SSLCheckResult(
            valid=False,
            error=f"SSL ошибка: {str(e)}",
            error_type="ssl_expired"
        )

    except socket.timeout:
        return SSLCheckResult(
            valid=False,
            error="SSL соединение: таймаут",
            error_type="ssl_expired"
        )

    except socket.gaierror as e:
        return SSLCheckResult(
            valid=False,
            error=f"Ошибка DNS: {str(e)}",
            error_type="ssl_mismatch"
        )

    except Exception as e:
        return SSLCheckResult(
            valid=False,
            error=f"Ошибка проверки SSL: {str(e)}",
            error_type="ssl_expired"
        )
