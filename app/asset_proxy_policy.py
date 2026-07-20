from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit


DEFAULT_ASSET_PROXY_ALLOWED_HOSTS = (
    "ozon.ru",
    "ozone.ru",
    "ozonusercontent.com",
    "cdn.ngenix.net",
    "o3static.com",
    "o3.ru",
)

OZON_CREDENTIAL_ORIGIN = ("https", "api-seller.ozon.ru", 443)


def _normalized_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def _normalized_allowed_host(value: str) -> str | None:
    host = _normalized_host(value)
    if not host or any(character in host for character in "/:@[]"):
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


def parse_allowed_asset_hosts(raw: str | None) -> tuple[str, ...]:
    values: Iterable[str]
    if raw is None:
        values = DEFAULT_ASSET_PROXY_ALLOWED_HOSTS
    else:
        values = raw.split(",")

    normalized: list[str] = []
    for value in values:
        host = _normalized_allowed_host(value)
        if host and host not in normalized:
            normalized.append(host)
    return tuple(normalized)


def _validated_origin(url: str) -> tuple[str, str, int] | None:
    if not isinstance(url, str) or not url or url != url.strip():
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None

    scheme = parsed.scheme.lower()
    host = _normalized_host(parsed.hostname)
    if scheme != "https" or not host:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None

    effective_port = 443 if port is None else port
    if effective_port != 443:
        return None
    return scheme, host, effective_port


def asset_url_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    origin = _validated_origin(url)
    if origin is None:
        return False
    _scheme, host, _port = origin
    for value in allowed_hosts:
        allowed_host = _normalized_allowed_host(value)
        if allowed_host and (host == allowed_host or host.endswith("." + allowed_host)):
            return True
    return False


def asset_url_requires_ozon_credentials(url: str) -> bool:
    return _validated_origin(url) == OZON_CREDENTIAL_ORIGIN


def resolve_asset_redirect(current_url: str, location: str | None) -> str | None:
    if not isinstance(location, str) or not location.strip():
        return None
    try:
        return urljoin(current_url, location.strip())
    except (TypeError, ValueError):
        return None
