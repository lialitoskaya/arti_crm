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
    """Safe internal marker for OAuth flow failures."""


@dataclass(frozen=True)
class YandexOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    user_map: dict[str, str]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "да"}


def _normalize_mapping_key(value: str) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return ""
    if ":" not in key:
        return f"login:{key}"
    prefix, raw_value = key.split(":", 1)
    if prefix not in {"id", "login", "email"} or not raw_value.strip():
        return ""
    return f"{prefix}:{raw_value.strip()}"


def _load_user_map(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise YandexOAuthError("Yandex OAuth user mapping is invalid") from exc
    if not isinstance(parsed, dict):
        raise YandexOAuthError("Yandex OAuth user mapping is invalid")

    result: dict[str, str] = {}
    for source, target in parsed.items():
        key = _normalize_mapping_key(str(source))
        username = str(target or "").strip()
        if not key or not username:
            raise YandexOAuthError("Yandex OAuth user mapping is invalid")
        result[key] = username
    if not result:
        raise YandexOAuthError("Yandex OAuth user mapping is empty")
    return result


def _valid_redirect_uri(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def get_config() -> YandexOAuthConfig | None:
    enabled_raw = os.getenv("YANDEX_OAUTH_ENABLED")
    if enabled_raw is not None and not _truthy(enabled_raw):
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


def create_flow(config: YandexOAuthConfig) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    cookie = _sign_flow_cookie(config, state=state, verifier=verifier, issued_at=int(time.time()))
    return state, challenge, cookie


def build_authorization_url(config: YandexOAuthConfig, *, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _sign_flow_cookie(config: YandexOAuthConfig, *, state: str, verifier: str, issued_at: int) -> str:
    payload = _b64encode(
        json.dumps(
            {"v": 1, "state": state, "verifier": verifier, "iat": issued_at},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )
    signature = hmac.new(config.client_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_flow_cookie(
    config: YandexOAuthConfig,
    cookie_value: str,
    *,
    expected_state: str,
    now: int | None = None,
) -> str:
    try:
        payload, signature = str(cookie_value or "").split(".", 1)
        expected_signature = hmac.new(
            config.client_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise YandexOAuthError("OAuth state is invalid")
        data = json.loads(_b64decode(payload).decode("utf-8"))
        issued_at = int(data["iat"])
        state = str(data["state"])
        verifier = str(data["verifier"])
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise YandexOAuthError("OAuth state is invalid") from exc

    current_time = int(time.time()) if now is None else int(now)
    if data.get("v") != 1 or not state or not verifier:
        raise YandexOAuthError("OAuth state is invalid")
    if not hmac.compare_digest(state, str(expected_state or "")):
        raise YandexOAuthError("OAuth state is invalid")
    if issued_at > current_time + 30 or current_time - issued_at > FLOW_TTL_SECONDS:
        raise YandexOAuthError("OAuth state is expired")
    return verifier


def exchange_code(config: YandexOAuthConfig, *, code: str, code_verifier: str) -> str:
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        access_token = str(payload.get("access_token") or "")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise YandexOAuthError("OAuth token exchange failed") from exc
    if not access_token:
        raise YandexOAuthError("OAuth token exchange failed")
    return access_token


def fetch_profile(access_token: str) -> dict[str, Any]:
    try:
        response = httpx.get(
            PROFILE_URL,
            params={"format": "json"},
            headers={"Authorization": f"OAuth {access_token}", "Accept": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise YandexOAuthError("Yandex profile request failed") from exc
    if not isinstance(payload, dict) or not str(payload.get("id") or "").strip():
        raise YandexOAuthError("Yandex profile request failed")
    return payload


def resolve_crm_username(config: YandexOAuthConfig, profile: dict[str, Any]) -> str | None:
    yandex_id = str(profile.get("id") or "").strip().lower()
    login = str(profile.get("login") or "").strip().lower()
    email = str(profile.get("default_email") or "").strip().lower()
    for key in (f"id:{yandex_id}", f"login:{login}", f"email:{email}"):
        if key.endswith(":"):
            continue
        username = config.user_map.get(key)
        if username:
            return username
    return None
