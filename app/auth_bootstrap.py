from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on", "да"})
_AUTH_DISABLED_ENVIRONMENTS = frozenset({"development", "test"})
_FORWARDED_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)
_PREVIOUS_DEFAULT_USERNAMES = frozenset({"admin"})
_PREVIOUS_DEFAULT_PASSWORDS = frozenset({"admin123", "change_me_please"})


class AuthBootstrapError(RuntimeError):
    """Safe startup error for invalid authentication bootstrap settings."""


@dataclass(frozen=True)
class BootstrapAdminCredentials:
    username: str
    password: str
    display_name: str


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _TRUTHY_VALUES


def validate_auth_disabled_config(
    *,
    app_env: str | None,
    auth_disabled: str | None,
    allow_insecure_dev_auth: str | None,
) -> bool:
    """Return whether the explicit development/test authentication bypass is enabled."""
    if not _is_truthy(auth_disabled):
        return False
    normalized_env = str(app_env or "").strip().casefold()
    if normalized_env not in _AUTH_DISABLED_ENVIRONMENTS or not _is_truthy(allow_insecure_dev_auth):
        raise AuthBootstrapError("Insecure authentication bypass is not allowed in this environment")
    return True


def auth_disabled_request_allowed(client_host: str | None, header_names: Iterable[str]) -> bool:
    """Allow bypass traffic only from a direct loopback peer without proxy headers."""
    if str(client_host or "").strip() not in {"127.0.0.1", "::1"}:
        return False
    normalized_headers = {str(name).strip().casefold() for name in header_names}
    return not bool(normalized_headers & _FORWARDED_HEADERS)


def resolve_bootstrap_admin_credentials(
    username: str | None,
    password: str | None,
    display_name: str | None = None,
) -> BootstrapAdminCredentials:
    """Validate explicit credentials used only when the users table is empty."""
    raw_username = str(username or "")
    normalized_username = raw_username.strip()
    raw_password = str(password or "")
    normalized_display_name = str(display_name or "").strip() or normalized_username

    if not normalized_username or not raw_password.strip():
        raise AuthBootstrapError("Explicit bootstrap credentials are required")
    if not 2 <= len(normalized_username) <= 120:
        raise AuthBootstrapError("Bootstrap credentials are invalid")
    if any(character.isspace() or not character.isprintable() for character in normalized_username):
        raise AuthBootstrapError("Bootstrap credentials are invalid")
    if normalized_username.casefold() in _PREVIOUS_DEFAULT_USERNAMES:
        raise AuthBootstrapError("Bootstrap credentials are invalid")
    if not 12 <= len(raw_password) <= 500:
        raise AuthBootstrapError("Bootstrap credentials are invalid")
    if raw_password.strip().casefold() in _PREVIOUS_DEFAULT_PASSWORDS:
        raise AuthBootstrapError("Bootstrap credentials are invalid")
    if not normalized_display_name or len(normalized_display_name) > 160:
        raise AuthBootstrapError("Bootstrap credentials are invalid")

    return BootstrapAdminCredentials(
        username=normalized_username,
        password=raw_password,
        display_name=normalized_display_name,
    )
