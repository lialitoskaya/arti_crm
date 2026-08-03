from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx


AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
PROFILE_URL = "https://login.yandex.ru/info"
FLOW_TTL_SECONDS = 600


class YandexOAuthError(RuntimeError):
    """Safe marker for a rejected or failed Yandex OAuth flow."""


_PROVIDER_FAILURE_STAGES = frozenset(
    {"token_exchange", "profile_request", "profile_validation"}
)


class YandexOAuthCallbackError(YandexOAuthError):
    """Provider failure classified by a fixed, non-sensitive callback stage."""

    def __init__(self, stage: str):
        if stage not in _PROVIDER_FAILURE_STAGES:
            raise ValueError("Unsupported Yandex OAuth callback failure stage")
        self.stage = stage
        super().__init__("Yandex OAuth callback failed")


@dataclass(frozen=True)
class YandexOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    user_map: dict[str, str]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _normalize_mapping_key(value: str) -> str:
    prefix, separator, raw_identity = str(value or "").strip().partition(":")
    if not separator:
        return ""
    prefix = prefix.strip().casefold()
    identity = raw_identity.strip()
    if prefix not in {"id", "login", "email"} or not identity:
        return ""
    if prefix in {"login", "email"}:
        identity = identity.casefold()
    return f"{prefix}:{identity}"


def _load_user_map(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise YandexOAuthError("Yandex OAuth user mapping is invalid") from exc
    if not isinstance(parsed, dict):
        raise YandexOAuthError("Yandex OAuth user mapping is invalid")

    normalized: dict[str, str] = {}
    for source, target in parsed.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise YandexOAuthError("Yandex OAuth user mapping is invalid")
        key = _normalize_mapping_key(source)
        username = target.strip()
        if not key or not username or key in normalized:
            raise YandexOAuthError("Yandex OAuth user mapping is invalid")
        normalized[key] = username
    if not normalized:
        raise YandexOAuthError("Yandex OAuth user mapping is empty")
    return normalized


def _valid_redirect_uri(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def get_config() -> YandexOAuthConfig | None:
    """Read and validate OAuth configuration without affecting application startup."""
    if not _truthy(os.getenv("YANDEX_OAUTH_ENABLED")):
        return None

    client_id = os.getenv("YANDEX_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("YANDEX_OAUTH_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("YANDEX_OAUTH_REDIRECT_URI", "").strip()
    raw_user_map = os.getenv("YANDEX_OAUTH_USER_MAP", "").strip()
    if not client_id or not client_secret or not redirect_uri or not raw_user_map:
        return None
    if not _valid_redirect_uri(redirect_uri):
        return None
    try:
        user_map = _load_user_map(raw_user_map)
    except YandexOAuthError:
        return None
    return YandexOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        user_map=user_map,
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _flow_signature(config: YandexOAuthConfig, payload: str) -> str:
    digest = hmac.new(
        config.client_secret.encode("utf-8"),
        f"yandex-oauth-flow:v1:{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _sign_flow_cookie(
    config: YandexOAuthConfig,
    *,
    state: str,
    verifier: str,
    issued_at: int,
) -> str:
    payload = _b64encode(
        json.dumps(
            {"state": state, "verifier": verifier, "issued_at": issued_at},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )
    return f"{payload}.{_flow_signature(config, payload)}"


def create_flow(config: YandexOAuthConfig) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    cookie = _sign_flow_cookie(config, state=state, verifier=verifier, issued_at=int(time.time()))
    return state, challenge, cookie


def verify_flow_cookie(
    config: YandexOAuthConfig,
    cookie_value: str,
    *,
    expected_state: str,
    now: int | None = None,
) -> str:
    try:
        if not cookie_value or len(cookie_value) > 4096:
            raise ValueError("invalid cookie")
        payload, signature = cookie_value.split(".", 1)
        if not hmac.compare_digest(signature, _flow_signature(config, payload)):
            raise ValueError("invalid signature")
        data = json.loads(_b64decode(payload).decode("utf-8"))
        state = data["state"]
        verifier = data["verifier"]
        issued_at = int(data["issued_at"])
        current_time = int(time.time()) if now is None else int(now)
        if not isinstance(state, str) or not isinstance(verifier, str):
            raise ValueError("invalid flow data")
        if not hmac.compare_digest(state, expected_state):
            raise ValueError("state mismatch")
        if issued_at > current_time + 30 or current_time - issued_at > FLOW_TTL_SECONDS:
            raise ValueError("expired flow")
        if len(verifier) < 43 or len(verifier) > 128:
            raise ValueError("invalid verifier")
        return verifier
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise YandexOAuthError("Yandex OAuth flow is invalid") from exc


def build_authorization_url(config: YandexOAuthConfig, *, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "force_confirm": "yes",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _provider_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


async def fetch_profile_for_code(
    config: YandexOAuthConfig,
    *,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    try:
        async with _provider_client() as client:
            try:
                token_response = await client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": config.client_id,
                        "client_secret": config.client_secret,
                        "redirect_uri": config.redirect_uri,
                        "code_verifier": code_verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                if not isinstance(token_payload, dict):
                    raise ValueError("invalid token response")
                access_token = token_payload.get("access_token")
                if not isinstance(access_token, str) or not access_token.strip():
                    raise ValueError("missing access token")
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                raise YandexOAuthCallbackError("token_exchange") from exc

            try:
                profile_response = await client.get(
                    PROFILE_URL,
                    params={"format": "json"},
                    headers={"Accept": "application/json", "Authorization": f"OAuth {access_token}"},
                )
                profile_response.raise_for_status()
            except httpx.HTTPError as exc:
                raise YandexOAuthCallbackError("profile_request") from exc

            try:
                profile = profile_response.json()
                if not isinstance(profile, dict):
                    raise ValueError("invalid profile response")
                return profile
            except (TypeError, ValueError) as exc:
                raise YandexOAuthCallbackError("profile_validation") from exc
    except YandexOAuthCallbackError:
        raise
    except Exception as exc:
        raise YandexOAuthCallbackError("token_exchange") from exc


def get_yandex_user_id(profile: dict[str, Any]) -> str:
    return str(profile.get("id") or "").strip()


def resolve_crm_username(config: YandexOAuthConfig, profile: dict[str, Any]) -> str | None:
    yandex_id = get_yandex_user_id(profile)
    if yandex_id:
        username = config.user_map.get(f"id:{yandex_id}")
        if username:
            return username

    login = str(profile.get("login") or "").strip().casefold()
    if login:
        username = config.user_map.get(f"login:{login}")
        if username:
            return username

    emails: list[str] = []
    default_email = profile.get("default_email")
    if isinstance(default_email, str):
        emails.append(default_email)
    profile_emails = profile.get("emails")
    if isinstance(profile_emails, list):
        emails.extend(value for value in profile_emails if isinstance(value, str))
    for email in emails:
        normalized_email = email.strip().casefold()
        if normalized_email:
            username = config.user_map.get(f"email:{normalized_email}")
            if username:
                return username
    return None
