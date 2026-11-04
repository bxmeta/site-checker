"""
Модуль проверки SSL-сертификатов.

Делает две вещи:
1. Забирает сертификат без проверки цепочки и разбирает его: срок, CN, SAN, издатель.
2. Отдельно выполняет "честное" TLS-рукопожатие с проверкой цепочки доверия,
   как это делает браузер. Так ловятся истёкший сертификат, неполная цепочка,
   самоподписанный сертификат, несовпадение имени.

Типы ошибок (error_type):
- ssl_expired       - сертификат истёк
- ssl_not_yet_valid - сертификат ещё не начал действовать (часы на сервере)
- ssl_mismatch      - сертификат выдан на другой домен
- ssl_untrusted     - цепочка не доверенная (неполная цепочка, самоподписанный, неизвестный CA)
- ssl_handshake     - TLS-рукопожатие не удалось (сервер не говорит TLS, сброс соединения)
- timeout           - сервер не ответил
- dns               - имя не разрешается
"""
import asyncio
import ssl
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtensionOID
from cryptography.x509 import DNSName

# Издатели, чьи сертификаты продлеваются автоматически (ACME).
# Переопределяется в config.yaml: ssl.auto_renew_issuers
DEFAULT_AUTO_RENEW_ISSUERS = ["Let's Encrypt", "ZeroSSL"]

# Коды ошибок верификации OpenSSL (X509_V_ERR_*) -> описание на русском
_VERIFY_ERRORS = {
    2: ("ssl_untrusted", "не удалось получить сертификат издателя"),
    9: ("ssl_not_yet_valid", "сертификат ещё не начал действовать (проверьте время на сервере)"),
    10: ("ssl_expired", "сертификат истёк"),
    18: ("ssl_untrusted", "самоподписанный сертификат"),
    19: ("ssl_untrusted", "самоподписанный сертификат в цепочке (неизвестный корневой CA)"),
    20: ("ssl_untrusted", "не удалось построить цепочку доверия: сервер не отдаёт промежуточный сертификат или корень издателя неизвестен"),
    21: ("ssl_untrusted", "не удалось проверить первый сертификат цепочки"),
    23: ("ssl_untrusted", "сертификат отозван"),
    26: ("ssl_untrusted", "недопустимое назначение сертификата"),
    27: ("ssl_untrusted", "сертификат не доверенный"),
    62: ("ssl_mismatch", "имя хоста не совпадает с сертификатом"),
}


def _to_punycode(hostname: str) -> str:
    """Конвертирует IDN-домен (например, кириллический) в Punycode."""
    try:
        return hostname.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeDecodeError):
        return hostname


@dataclass
class SSLCheckResult:
    """Результат проверки SSL-сертификата."""
    valid: bool
    error: Optional[str] = None
    error_type: Optional[str] = None
    expiry_date: Optional[datetime] = None
    not_before: Optional[datetime] = None
    days_until_expiry: Optional[int] = None
    subject_cn: Optional[str] = None
    san_list: Optional[List[str]] = None
    issuer: Optional[str] = None          # "Let's Encrypt", "GlobalSign nv-sa"
    issuer_cn: Optional[str] = None       # "R11", "GlobalSign GCC R6 AlphaSSL CA 2025"
    auto_renew: bool = False              # издатель из списка автопродления (Let's Encrypt и т.п.)
    chain_trusted: Optional[bool] = None  # None - проверка цепочки не выполнялась

    @property
    def expiry_str(self) -> str:
        if not self.expiry_date:
            return "неизвестно"
        return self.expiry_date.strftime("%d.%m.%Y %H:%M UTC")

    @property
    def issuer_str(self) -> str:
        if self.issuer and self.issuer_cn and self.issuer != self.issuer_cn:
            return f"{self.issuer} ({self.issuer_cn})"
        return self.issuer or self.issuer_cn or "неизвестен"


def _get_certificate_binary(hostname: str, port: int = 443, timeout: int = 10) -> bytes:
    """Получает сертификат сервера в DER без проверки цепочки."""
    ascii_hostname = _to_punycode(hostname)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((ascii_hostname, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=ascii_hostname) as ssock:
            return ssock.getpeercert(binary_form=True)


def _verify_chain(hostname: str, port: int = 443, timeout: int = 10) -> Optional[ssl.SSLCertVerificationError]:
    """
    Выполняет TLS-рукопожатие с полной проверкой доверия (как браузер).

    Returns:
        None если всё в порядке, иначе исключение верификации.
    Raises:
        ssl.SSLError, socket.error - если рукопожатие не удалось по другой причине.
    """
    ascii_hostname = _to_punycode(hostname)
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ascii_hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=ascii_hostname):
                return None
    except ssl.SSLCertVerificationError as e:
        return e


def _name_attr(name: x509.Name, oid) -> Optional[str]:
    try:
        attrs = name.get_attributes_for_oid(oid)
        return attrs[0].value if attrs else None
    except Exception:
        return None


def _parse_certificate(cert_der: bytes) -> dict:
    """Разбирает DER-сертификат: CN, SAN, издатель, сроки."""
    cert = x509.load_der_x509_certificate(cert_der)

    san_list: List[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san_list = list(san_ext.value.get_values_for_type(DNSName))
    except x509.ExtensionNotFound:
        pass

    if hasattr(cert, 'not_valid_after_utc'):
        not_after = cert.not_valid_after_utc
        not_before = cert.not_valid_before_utc
    else:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)

    return {
        'cn': _name_attr(cert.subject, NameOID.COMMON_NAME),
        'san_list': san_list,
        'issuer_org': _name_attr(cert.issuer, NameOID.ORGANIZATION_NAME),
        'issuer_cn': _name_attr(cert.issuer, NameOID.COMMON_NAME),
        'not_after': not_after,
        'not_before': not_before,
    }


def _hostname_matches_cert(hostname: str, cn: Optional[str], san_list: List[str]) -> bool:
    """Проверяет соответствие имени хоста сертификату (с учётом wildcard и IDN)."""
    all_names = list(san_list)
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
        elif name_lower in hostnames_to_check:
            return True

    return False


def is_auto_renew_issuer(issuer_org: Optional[str], issuer_cn: Optional[str],
                         auto_renew_issuers: Optional[List[str]] = None) -> bool:
    """Издатель из списка автопродления (регистронезависимое вхождение подстроки)."""
    patterns = auto_renew_issuers if auto_renew_issuers is not None else DEFAULT_AUTO_RENEW_ISSUERS
    haystack = f"{issuer_org or ''} {issuer_cn or ''}".lower()
    return any(p.lower() in haystack for p in patterns if p)


def _describe_verify_error(e: ssl.SSLCertVerificationError) -> tuple:
    code = getattr(e, 'verify_code', None)
    if code in _VERIFY_ERRORS:
        return _VERIFY_ERRORS[code]
    msg = getattr(e, 'verify_message', None) or str(e)
    return "ssl_untrusted", f"цепочка не прошла проверку ({msg})"


async def check_ssl(
    hostname: str,
    port: int = 443,
    timeout: int = 10,
    auto_renew_issuers: Optional[List[str]] = None,
) -> SSLCheckResult:
    """
    Проверяет SSL-сертификат сайта: срок, домен, доверие цепочки.

    Args:
        hostname: Имя хоста (поддерживает IDN)
        port: Порт
        timeout: Таймаут соединения, сек
        auto_renew_issuers: Подстроки издателей с автопродлением

    Returns:
        SSLCheckResult. valid=False только при реальной проблеме, из-за которой
        браузер покажет ошибку (или сервер вообще не отвечает по TLS).
    """
    loop = asyncio.get_event_loop()

    # Шаг 1: забираем сертификат без верификации
    try:
        cert_der = await loop.run_in_executor(None, _get_certificate_binary, hostname, port, timeout)
    except socket.timeout:
        return SSLCheckResult(valid=False, error_type="timeout",
                              error=f"TLS: сервер не ответил на порту {port} за {timeout} с")
    except socket.gaierror as e:
        return SSLCheckResult(valid=False, error_type="dns",
                              error=f"DNS: имя {hostname} не разрешается ({e.strerror or e})")
    except ConnectionRefusedError:
        return SSLCheckResult(valid=False, error_type="conn_refused",
                              error=f"TCP: порт {port} закрыт, соединение отклонено")
    except ConnectionResetError:
        return SSLCheckResult(valid=False, error_type="ssl_handshake",
                              error="TLS: сервер сбросил соединение во время рукопожатия")
    except ssl.SSLError as e:
        reason = getattr(e, 'reason', None) or str(e)
        return SSLCheckResult(valid=False, error_type="ssl_handshake",
                              error=f"TLS-рукопожатие не удалось: {reason}")
    except OSError as e:
        return SSLCheckResult(valid=False, error_type="no_response",
                              error=f"TLS: ошибка соединения ({e.strerror or e})")
    except Exception as e:
        return SSLCheckResult(valid=False, error_type="ssl_handshake",
                              error=f"TLS: {type(e).__name__}: {e}")

    try:
        info = _parse_certificate(cert_der)
    except Exception as e:
        return SSLCheckResult(valid=False, error_type="ssl_handshake",
                              error=f"TLS: сервер отдал нечитаемый сертификат ({type(e).__name__}: {e})")

    now = datetime.now(timezone.utc)
    expiry_date = info['not_after']
    days_until_expiry = (expiry_date - now).days

    result = SSLCheckResult(
        valid=True,
        expiry_date=expiry_date,
        not_before=info['not_before'],
        days_until_expiry=days_until_expiry,
        subject_cn=info['cn'],
        san_list=info['san_list'],
        issuer=info['issuer_org'],
        issuer_cn=info['issuer_cn'],
        auto_renew=is_auto_renew_issuer(info['issuer_org'], info['issuer_cn'], auto_renew_issuers),
    )

    # Шаг 2: проверки по содержимому сертификата
    if days_until_expiry < 0:
        days_ago = abs(days_until_expiry)
        result.valid = False
        result.error_type = "ssl_expired"
        result.error = (
            f"SSL-сертификат истёк {result.expiry_str} "
            f"({days_ago} дн. назад), издатель: {result.issuer_str}"
        )
        return result

    if info['not_before'] > now:
        result.valid = False
        result.error_type = "ssl_not_yet_valid"
        result.error = (
            f"SSL-сертификат ещё не действует (начало {info['not_before'].strftime('%d.%m.%Y %H:%M UTC')}), "
            f"проверьте время на сервере"
        )
        return result

    if not _hostname_matches_cert(hostname, info['cn'], info['san_list']):
        names = ", ".join(info['san_list'][:5]) or info['cn'] or "-"
        result.valid = False
        result.error_type = "ssl_mismatch"
        result.error = f"SSL-сертификат выдан на другой домен: {names} (хост {hostname})"
        return result

    # Шаг 3: доверие цепочки, как в браузере
    try:
        verify_err = await loop.run_in_executor(None, _verify_chain, hostname, port, timeout)
    except Exception as e:
        # Сертификат мы уже получили, значит проблема транзитная: не считаем ошибкой SSL
        result.chain_trusted = None
        result.error = f"проверка цепочки не выполнена: {type(e).__name__}: {e}"
        return result

    if verify_err is None:
        result.chain_trusted = True
        return result

    error_type, description = _describe_verify_error(verify_err)
    result.chain_trusted = False
    result.valid = False
    result.error_type = error_type
    result.error = f"SSL: {description}. Браузеры покажут ошибку. Издатель: {result.issuer_str}"
    return result
