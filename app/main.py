from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import repository as repo
from app import yandex_oauth
from app.asset_proxy_policy import (
    asset_url_resolves_globally,
    asset_url_allowed,
    asset_url_requires_ozon_credentials,
    parse_allowed_asset_hosts,
    resolve_asset_host_addresses,
    resolve_asset_redirect,
)
from app.auth_dependencies import (
    current_user as _shared_current_user,
    require_admin as _shared_require_admin,
    route_requires_admin as _shared_route_requires_admin,
)
from app.auth_bootstrap import (
    AuthBootstrapError,
    auth_disabled_request_allowed,
    resolve_bootstrap_admin_credentials,
    validate_auth_disabled_config,
)
from app.chat_settings_router import create_chat_settings_router
from app.marketplace_sender import (
    extract_sender_designations as _shared_extract_sender_designations,
    normalize_system_sender as _shared_normalize_system_sender,
    system_sender_matches as _shared_system_sender_matches,
)
from app.notifications_router import create_notifications_router
from app.reply_templates_router import create_reply_templates_router
from app.services.analytics import build_chat_analytics, build_chat_analytics_drilldown
from app.services.knowledge_images import (
    article_knowledge_image_url,
    knowledge_image_media_type,
    lexical_path_is_within,
    normalized_static_lookup_path,
    private_knowledge_image_reference,
    private_storage_root,
    resolved_path_is_within,
    resolve_article_image_reference,
    resolve_knowledge_image_path,
    validate_knowledge_image_upload,
)
from app.task_types_router import create_task_types_router
from app.connectors.mock import MockConnector
from app.connectors.ozon import OzonConnector
from app.connectors.wildberries import WildberriesConnector
from app.connectors.yandex_market import YandexMarketConnector
from app.db import get_connection, init_db
from app.schemas import AiReplyCreate, ChatCreate, ChatUpdate, InternalNoteCreate, InternalNoteUpdate, LoginCreate, MessageCreate, ReviewReplyCreate, QuestionAnswerCreate, TaskCreate, TaskUpdate, UserCreate, UserPasswordUpdate, UserUpdate, ProfileUpdate, KnowledgeCategoryCreate, KnowledgeArticleCreate, KnowledgeArticleUpdate, YandexOAuthManagedLinkCreate, YandexOAuthManagedLinkUpdate

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CHAT_ATTACHMENTS_DIR = Path(os.getenv("CRM_CHAT_ATTACHMENTS_DIR", str(Path.cwd() / "chat_attachments"))).resolve()
KNOWLEDGE_IMAGES_DIR = Path(os.getenv("CRM_KNOWLEDGE_IMAGES_DIR", str(Path.cwd() / "knowledge_images"))).resolve()
MAX_CHAT_IMAGE_BYTES = int(os.getenv("CRM_MAX_CHAT_IMAGE_MB", "12")) * 1024 * 1024
ALLOWED_CHAT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

CRM_BUILD_VERSION = "v80-login-bruteforce-lockout-20260708"


class KnowledgeSafeStaticFiles(StaticFiles):
    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        normalized_path = normalized_static_lookup_path(path)
        for directory in self.all_directories:
            lexical_path = Path(os.path.abspath(os.path.join(os.fspath(directory), normalized_path)))
            lexical_legacy_root = Path(os.path.abspath(os.path.join(os.fspath(directory), "uploads", "knowledge")))
            if lexical_path_is_within(lexical_path, lexical_legacy_root):
                return "", None
        full_path, stat_result = super().lookup_path(path)
        if stat_result is None:
            return full_path, stat_result
        for directory in self.all_directories:
            legacy_root = Path(os.fspath(directory)) / "uploads" / "knowledge"
            if resolved_path_is_within(full_path, legacy_root):
                return "", None
        return full_path, stat_result


app = FastAPI(title="Arti CRM", version="1.0.3")
app.mount("/static", KnowledgeSafeStaticFiles(directory=STATIC_DIR), name="static")

AUTH_COOKIE_NAME = "arti_crm_session"
YANDEX_OAUTH_STATE_COOKIE_NAME = "arti_crm_yandex_oauth"
_AUTH_DISABLED_CONFIGURATION_ERROR: AuthBootstrapError | None = None
try:
    AUTH_DISABLED = validate_auth_disabled_config(
        app_env=os.getenv("APP_ENV"),
        auth_disabled=os.getenv("CRM_AUTH_DISABLED"),
        allow_insecure_dev_auth=os.getenv("ALLOW_INSECURE_DEV_AUTH"),
    )
except AuthBootstrapError as exc:
    AUTH_DISABLED = False
    _AUTH_DISABLED_CONFIGURATION_ERROR = exc

# v75 security hardening: keep browser-visible state separate from server secrets.
CSRF_COOKIE_NAME = "arti_crm_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SENSITIVE_PUBLIC_PATH_RE = re.compile(
    r"(^|/)(\.env|\.git|__pycache__|secrets?|backup|backups)(/|$)|"
    r"\.(?:db|sqlite|sqlite3|log|bak|backup|zip|rar|7z|tar|gz|pem|key)$",
    re.IGNORECASE,
)
SENSITIVE_ENV_NAMES = (
    "OZON_API_KEY", "OZON_CLIENT_ID", "WB_ANALYTICS_TOKEN", "WB_STATISTICS_TOKEN", "WB_API_TOKEN",
    "YANDEX_MARKET_API_KEY", "YANDEX_API_KEY", "WEB_PUSH_VAPID_PRIVATE_KEY", "VAPID_PRIVATE_KEY",
    "CRM_BACKGROUND_TICK_TOKEN", "YANDEX_OAUTH_CLIENT_SECRET", "SECRET_KEY", "DATABASE_URL",
)


def _security_env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _security_env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _is_truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "да"}


def _request_is_https(request: Request | None = None) -> bool:
    if request is None:
        return False
    if str(request.url.scheme).lower() == "https":
        return True
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto == "https":
        return True
    if (request.headers.get("x-forwarded-ssl") or "").strip().lower() == "on":
        return True
    if (request.headers.get("x-forwarded-protocol") or "").strip().lower() == "https":
        return True
    if (request.headers.get("x-url-scheme") or "").strip().lower() == "https":
        return True
    return False


def _public_base_url_env() -> str:
    return (os.getenv("CRM_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def _public_site_is_https() -> bool:
    return _public_base_url_env().lower().startswith("https://")


def _force_https_enabled() -> bool:
    raw = os.getenv("CRM_FORCE_HTTPS")
    if raw is not None:
        return _is_truthy(raw)
    return _public_site_is_https()


def _cookie_secure_enabled(request: Request | None = None) -> bool:
    raw = os.getenv("CRM_COOKIE_SECURE")
    if raw is not None:
        return _is_truthy(raw)

    # Production-safe default: auth cookies should be Secure. Shared hosting/proxies can hide
    # HTTPS from the ASGI app, so relying only on request.url.scheme is not enough.
    # For local HTTP development explicitly set CRM_COOKIE_SECURE=0.
    if _request_is_https(request) or _public_site_is_https() or _force_https_enabled():
        return True
    if _is_truthy(os.getenv("CRM_ALLOW_INSECURE_COOKIES")):
        return False
    return True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _mask_sensitive(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    for name in SENSITIVE_ENV_NAMES:
        secret = os.getenv(name)
        if secret and len(secret) >= 6:
            text = text.replace(secret, "****")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\\\"']+", r"\1****", text)
    text = re.sub(r"(?i)((?:api[-_]?key|token|session|cookie|password|secret)\s*[:=]\s*)[^\s,;\\\"']+", r"\1****", text)
    text = re.sub(r"(?i)([?&](?:token|api_key|key|password|secret)=)[^&\s]+", r"\1****", text)
    return text


def _safe_error_detail(detail: Any) -> Any:
    if isinstance(detail, str):
        return _mask_sensitive(detail)[:1200]
    if isinstance(detail, dict):
        return {str(k): _safe_error_detail(v) for k, v in detail.items()}
    if isinstance(detail, list):
        return [_safe_error_detail(v) for v in detail[:20]]
    return detail


def _csrf_secret() -> bytes:
    # Prefer a dedicated secret, then the general app secret. The fallback keeps local dev working,
    # but production should set CRM_CSRF_SECRET or SECRET_KEY.
    raw = (
        os.getenv("CRM_CSRF_SECRET")
        or os.getenv("SECRET_KEY")
        or os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY")
        or os.getenv("VAPID_PRIVATE_KEY")
        or "arti-crm-dev-change-this-csrf-secret"
    )
    return raw.encode("utf-8")


def _csrf_token_for_session(session_token: str | None) -> str:
    if not session_token:
        return ""
    digest = hmac.new(_csrf_secret(), f"csrf:v2:{session_token}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v2.{digest}"


def _delete_cookie_secure(response: Response, key: str, request: Request | None = None, *, httponly: bool = True) -> None:
    response.delete_cookie(
        key,
        path="/",
        secure=_cookie_secure_enabled(request),
        httponly=httponly,
        samesite=os.getenv("CRM_COOKIE_SAMESITE", "lax"),
    )


def _set_auth_cookie(response: Response, token: str, request: Request, *, max_age: int) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        samesite=os.getenv("CRM_COOKIE_SAMESITE", "lax"),
        secure=_cookie_secure_enabled(request),
        max_age=max_age,
        path="/",
    )


def _set_yandex_oauth_state_cookie(response: Response, value: str, request: Request) -> None:
    response.set_cookie(
        YANDEX_OAUTH_STATE_COOKIE_NAME,
        value,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure_enabled(request),
        max_age=yandex_oauth.FLOW_TTL_SECONDS,
        path="/api/auth/yandex",
    )


def _delete_yandex_oauth_state_cookie(response: Response, request: Request | None = None) -> None:
    response.delete_cookie(
        YANDEX_OAUTH_STATE_COOKIE_NAME,
        path="/api/auth/yandex",
        secure=_cookie_secure_enabled(request),
        httponly=True,
        samesite="lax",
    )


def _delete_legacy_csrf_cookie(response: Response, request: Request | None = None) -> None:
    # v75-v77 used a browser-readable double-submit CSRF cookie.
    # v78+ switches to a session-bound token returned by /api/security/csrf.
    # Do not emit a Set-Cookie deletion header on every response: scanners treat any cookie
    # without Secure as a finding, and unnecessary Set-Cookie also hurts caching.
    if request is not None and CSRF_COOKIE_NAME not in request.cookies:
        return
    _delete_cookie_secure(response, CSRF_COOKIE_NAME, request, httponly=True)


def _csrf_exempt_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return normalized in {"/api/auth/login", "/api/auth/me", "/api/background/tick"}


def _validate_csrf(request: Request) -> None:
    if AUTH_DISABLED or not _security_env_bool("CRM_CSRF_ENABLED", True):
        return
    if request.method.upper() not in UNSAFE_HTTP_METHODS:
        return
    if not request.url.path.startswith("/api/") or _csrf_exempt_path(request.url.path):
        return
    session_token = request.cookies.get(AUTH_COOKIE_NAME) or ""
    header_token = request.headers.get(CSRF_HEADER_NAME) or request.headers.get("X-CSRF-Token") or ""
    expected_token = _csrf_token_for_session(session_token)
    if not expected_token or not header_token or not hmac.compare_digest(expected_token, header_token):
        raise HTTPException(status_code=403, detail="Запрос отклонён защитой CSRF. Обновите страницу и повторите действие.")


def _rate_limit_bucket() -> dict[str, list[float]]:
    bucket = getattr(app.state, "security_rate_limits", None)
    if not isinstance(bucket, dict):
        bucket = {}
        app.state.security_rate_limits = bucket
    return bucket


def _rate_limit(request: Request, key: str, *, limit: int, window_seconds: int) -> None:
    if not _security_env_bool("CRM_RATE_LIMIT_ENABLED", True):
        return
    now = time.time()
    client_key = f"{key}:{_client_ip(request)}"
    bucket = _rate_limit_bucket()
    hits = [ts for ts in bucket.get(client_key, []) if now - ts < window_seconds]
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Подождите и повторите.")
    hits.append(now)
    bucket[client_key] = hits


def _audit_action(request: Request, response: Response) -> None:
    if not _security_env_bool("CRM_AUDIT_LOG_ENABLED", True):
        return
    if request.method.upper() not in UNSAFE_HTTP_METHODS or not request.url.path.startswith("/api/"):
        return
    if response.status_code >= 500:
        return
    try:
        user = getattr(request.state, "user", None)
        repo.write_audit_log(
            user_id=int(user["id"]) if isinstance(user, dict) and user.get("id") not in (None, "") else None,
            username=(user.get("username") if isinstance(user, dict) else None),
            action=f"{request.method.upper()} {request.url.path}",
            path=request.url.path,
            method=request.method.upper(),
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            metadata={"status_code": response.status_code},
        )
    except Exception:
        pass


@app.exception_handler(HTTPException)
async def _security_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": _safe_error_detail(exc.detail)},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def _security_unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Keep raw tracebacks and secrets out of API responses. Server logs can still be inspected separately.
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})


@app.middleware("http")
async def security_guard_middleware(request: Request, call_next):
    if SENSITIVE_PUBLIC_PATH_RE.search(request.url.path):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    try:
        _validate_csrf(request)
    except HTTPException as exc:
        response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        _delete_legacy_csrf_cookie(response, request)
        _apply_security_headers(request, response)
        return response
    response = await call_next(request)
    _delete_legacy_csrf_cookie(response, request)
    _audit_action(request, response)
    _apply_security_headers(request, response)
    return response


def _auth_public_path(path: str) -> bool:
    normalized_path = path.rstrip("/") or "/"
    return (
        normalized_path == "/"
        or normalized_path == "/health"
        or normalized_path.startswith("/static/")
        or normalized_path in {
            "/api/auth/login",
            "/api/auth/me",
            "/api/auth/yandex/status",
            "/api/auth/yandex/start",
            "/api/auth/yandex/callback",
            "/api/background/tick",
        }
    )


@app.middleware("http")
async def require_auth_for_api(request: Request, call_next):
    if AUTH_DISABLED:
        client_host = request.client.host if request.client is not None else None
        if not auth_disabled_request_allowed(client_host, request.headers.keys()):
            response = JSONResponse(
                status_code=403,
                content={"detail": "Insecure development authentication is limited to direct loopback requests"},
            )
            _delete_legacy_csrf_cookie(response, request)
            _apply_security_headers(request, response)
            return response
        return await call_next(request)
    if not request.url.path.startswith("/api/") or _auth_public_path(request.url.path):
        return await call_next(request)
    token = request.cookies.get(AUTH_COOKIE_NAME)
    user = repo.get_user_by_session(token)
    if not user:
        response = Response(
            content=json.dumps({"detail": "Требуется авторизация"}, ensure_ascii=False),
            status_code=401,
            media_type="application/json",
        )
        _delete_legacy_csrf_cookie(response, request)
        _apply_security_headers(request, response)
        return response
    request.state.user = user
    if _shared_route_requires_admin(request.method, request.url.path):
        try:
            _shared_require_admin(request)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            _delete_legacy_csrf_cookie(response, request)
            _apply_security_headers(request, response)
            return response
    return await call_next(request)


def _apply_security_headers(request: Request, response: Response) -> None:
    # Use direct assignment instead of setdefault: hosting/proxy defaults must not remove
    # browser protections from the final response seen by scanners and users.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "script-src-elem 'self'; "
        "script-src-attr 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "connect-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "manifest-src 'self'; "
        "worker-src 'self'; "
        "upgrade-insecure-requests"
    )
    if _cookie_secure_enabled(request) or _force_https_enabled():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def _https_redirect_response(request: Request) -> RedirectResponse | None:
    if not _force_https_enabled() or _request_is_https(request):
        return None
    if request.url.path == "/health":
        return None
    public_base = _public_base_url_env()
    if public_base.lower().startswith("https://"):
        target = f"{public_base}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
    else:
        target = str(request.url.replace(scheme="https"))
    response = RedirectResponse(url=target, status_code=308)
    _apply_security_headers(request, response)
    return response


@app.middleware("http")
async def add_fastfox_cache_headers(request: Request, call_next):
    redirect = _https_redirect_response(request)
    if redirect is not None:
        return redirect

    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        # Fastfox serves the app through Python; browser caching avoids reloading
        # the large JS/CSS files on every navigation.
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    elif path.startswith("/api/chat-uploads/"):
        # Uploaded chat images are immutable filenames. Cache them privately so
        # opening chats with many images does not download the same files again.
        response.headers.setdefault("Cache-Control", "private, max-age=86400")

    _apply_security_headers(request, response)
    return response


def _current_user(request: Request) -> dict[str, Any]:
    return _shared_current_user(request, auth_disabled=AUTH_DISABLED)


def _require_admin(request: Request) -> dict[str, Any]:
    return _shared_require_admin(request, auth_disabled=AUTH_DISABLED)


def _web_push_public_key() -> str:
    return (os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY") or os.getenv("VAPID_PUBLIC_KEY") or "").strip()


def _web_push_private_key() -> str:
    return (os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY") or os.getenv("VAPID_PRIVATE_KEY") or "").strip()


def _web_push_subject() -> str:
    return (os.getenv("WEB_PUSH_VAPID_SUBJECT") or os.getenv("VAPID_SUBJECT") or "mailto:artitechno.official@gmail.com").strip()


def _web_push_enabled() -> bool:
    return _env_bool("WEB_PUSH_ENABLED", True)


def _web_push_configured() -> bool:
    return bool(_web_push_enabled() and _web_push_public_key() and _web_push_private_key())


def _public_base_url(request: Request | None = None) -> str:
    env_url = _public_base_url_env()
    if env_url:
        return env_url
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def _absolute_push_url(relative_or_absolute: str, *, base_url: str = "") -> str:
    raw = str(relative_or_absolute or "/").strip() or "/"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    base = base_url or (os.getenv("CRM_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return raw
    if raw.startswith("/#"):
        return base + raw[1:]
    if raw.startswith("/"):
        return base + raw
    return base + "/" + raw


async def _send_web_push_subscription(subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    if not _web_push_configured():
        raise RuntimeError("WEB_PUSH VAPID keys are not configured")
    try:
        from pywebpush import webpush  # type: ignore
    except Exception as exc:
        raise RuntimeError("pywebpush is not installed. Install: pip install pywebpush") from exc

    data = json.dumps(payload, ensure_ascii=False)
    await asyncio.to_thread(
        webpush,
        subscription_info=subscription,
        data=data,
        vapid_private_key=_web_push_private_key(),
        vapid_claims={"sub": _web_push_subject()},
        ttl=_env_int("WEB_PUSH_TTL_SECONDS", 3600, minimum=60, maximum=86400),
    )


async def _send_web_push_to_user(user_id: int, payload: dict[str, Any], *, base_url: str = "") -> dict[str, Any]:
    subscriptions = repo.list_push_subscriptions(int(user_id), active_only=True)
    if not subscriptions:
        return {"ok": False, "sent": 0, "subscriptions": 0, "error": "no active push subscriptions"}

    sent = 0
    errors: list[str] = []
    payload = dict(payload or {})
    payload.setdefault("title", "Arti CRM")
    payload.setdefault("body", "Новое уведомление")
    payload.setdefault("icon", "/static/icons/app-icon-192.png")
    payload.setdefault("badge", "/static/icons/app-icon-192.png")
    payload["url"] = _absolute_push_url(payload.get("url") or "/", base_url=base_url)

    for item in subscriptions:
        endpoint = item.get("endpoint") or ""
        subscription = item.get("subscription") or {}
        try:
            await _send_web_push_subscription(subscription, payload)
            repo.mark_push_subscription_result(endpoint, ok=True)
            sent += 1
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = int(getattr(response, "status_code", 0) or 0)
            deactivate = status_code in {404, 410}
            error = str(exc)
            repo.mark_push_subscription_result(endpoint, ok=False, error=error, deactivate=deactivate)
            errors.append(error[:300])

    return {"ok": sent > 0, "sent": sent, "subscriptions": len(subscriptions), "errors": errors[:3]}


async def _drain_push_outbox_once() -> dict[str, Any]:
    if not _web_push_configured():
        return {"ok": False, "configured": False, "sent": 0, "error": "VAPID keys are not configured"}

    items = repo.get_pending_push_outbox(_env_int("WEB_PUSH_OUTBOX_BATCH_SIZE", 50, minimum=1, maximum=200))
    sent_items = 0
    failed_items = 0
    skipped_items = 0
    for item in items:
        payload = item.get("payload") or {}
        result = await _send_web_push_to_user(int(item["user_id"]), payload)
        if result.get("sent", 0) > 0:
            repo.mark_push_outbox_sent(int(item["id"]))
            sent_items += 1
        elif result.get("error") == "no active push subscriptions":
            repo.mark_push_outbox_sent(int(item["id"]))
            skipped_items += 1
        else:
            repo.mark_push_outbox_failed(int(item["id"]), "; ".join(result.get("errors") or [result.get("error") or "push failed"]))
            failed_items += 1
    return {"ok": True, "configured": True, "total": len(items), "sent": sent_items, "failed": failed_items, "skipped": skipped_items}


async def _push_outbox_loop() -> None:
    if not _web_push_enabled():
        app.state.last_push_outbox = {"enabled": False}
        return
    await asyncio.sleep(5)
    interval = _env_int("WEB_PUSH_OUTBOX_INTERVAL_SECONDS", 3, minimum=1, maximum=300)
    while True:
        try:
            app.state.last_push_outbox = await _drain_push_outbox_once()
            app.state.last_push_outbox["interval_seconds"] = interval
            app.state.last_push_outbox["enabled"] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.last_push_outbox = {"enabled": True, "ok": False, "error": str(exc), "interval_seconds": interval}
        await asyncio.sleep(interval)


def _background_tick_token() -> str:
    return (
        os.getenv("CRM_BACKGROUND_TICK_TOKEN")
        or os.getenv("WEB_PUSH_BACKGROUND_TICK_TOKEN")
        or os.getenv("BACKGROUND_TICK_TOKEN")
        or ""
    ).strip()


def _require_background_tick_access(request: Request) -> None:
    expected = _background_tick_token()
    if not expected:
        raise HTTPException(status_code=503, detail="Background tick token is not configured")
    provided = (request.headers.get("x-background-token") or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid background tick token")


def _background_tick_lock() -> asyncio.Lock:
    lock = getattr(app.state, "background_tick_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        app.state.background_tick_lock = lock
    return lock


async def _run_background_tick_once(*, source: str = "manual") -> dict[str, Any]:
    lock = _background_tick_lock()
    if lock.locked():
        return {
            "ok": True,
            "status": "already_running",
            "source": source,
            "last": getattr(app.state, "last_external_background_tick", None),
        }

    started_at = time.time()
    result: dict[str, Any] = {
        "ok": True,
        "status": "finished",
        "source": source,
        "started_at": started_at,
        "marketplaces": {},
    }

    async with lock:
        try:
            if _env_bool("OZON_BACKGROUND_SYNC", True):
                try:
                    result["marketplaces"]["ozon"] = await _sync_ozon_fast_inbox_locked(background=True)
                except Exception as exc:
                    result["marketplaces"]["ozon"] = {"ok": False, "error": str(exc)}

            if _env_bool("OZON_QUESTIONS_BACKGROUND_SYNC", True):
                try:
                    result["marketplaces"]["ozon_questions"] = await _sync_ozon_questions_unlocked(background=True)
                except Exception as exc:
                    result["marketplaces"]["ozon_questions"] = {"ok": False, "error": str(exc)}

            # v51: after importing marketplace data, run an idempotent
            # notification catch-up. This covers cases where the message was
            # inserted before the chat unread metadata was updated, or when
            # existing local rows became waiting-for-reply during sync.
            try:
                result["notification_catchup"] = {
                    "messages": repo.enqueue_missing_message_notifications(
                        _env_int("CRM_NOTIFICATION_CATCHUP_MESSAGE_LIMIT", 200, minimum=1, maximum=1000)
                    ),
                    "questions": repo.enqueue_missing_question_notifications(
                        _env_int("CRM_NOTIFICATION_CATCHUP_QUESTION_LIMIT", 200, minimum=1, maximum=1000)
                    ),
                }
            except Exception as exc:
                result["notification_catchup"] = {"ok": False, "error": str(exc)}

            if _web_push_enabled():
                try:
                    result["push_outbox"] = await _drain_push_outbox_once()
                except Exception as exc:
                    result["push_outbox"] = {"ok": False, "error": str(exc)}

            result["duration_seconds"] = round(time.time() - started_at, 2)
            app.state.last_external_background_tick = result
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result["ok"] = False
            result["status"] = "error"
            result["error"] = str(exc)
            result["duration_seconds"] = round(time.time() - started_at, 2)
            app.state.last_external_background_tick = result
            return result


@app.middleware("http")
async def no_cache_frontend(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

connectors = {
    "mock": MockConnector(),
    "ozon": OzonConnector(),
    "yandex": YandexMarketConnector(),
    "wildberries": WildberriesConnector(),
}


def _marketplace_sync_lock(marketplace: str) -> asyncio.Lock:
    """Return a per-marketplace lock shared by background and frontend sync.

    The frontend operator endpoint and the background worker can run close to
    each other on shared hosting. For Ozon this used to create duplicate message
    insert races and crash the whole sync with UNIQUE constraint failed.
    """
    locks = getattr(app.state, "marketplace_sync_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        app.state.marketplace_sync_locks = locks
    lock = locks.get(marketplace)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        locks[marketplace] = lock
    return lock


_GENERIC_AUTHOR_NAMES = {
    "customer", "buyer", "client", "user", "покупатель", "клиент",
    "seller", "operator", "admin", "manager", "support", "продавец",
    "notificationuser", "notification_user", "systemuser", "system_user",
    "chatbot", "chat_bot", "chat bot",
}


def _is_real_customer_name(value: str | None) -> bool:
    if not value:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.lower() in _GENERIC_AUTHOR_NAMES:
        return False
    if text.isdigit():
        return False
    compact = text.replace("-", "")
    if len(compact) >= 24 and all(ch in "0123456789abcdefABCDEF" for ch in compact):
        return False
    return True


def _customer_info_from_messages(messages) -> tuple[str | None, str | None]:
    """Use inbound message author/raw data as fallback for customer name."""
    for message in messages:
        if getattr(message, "direction", None) != "inbound":
            continue
        author = getattr(message, "author", None)
        if _is_real_customer_name(author):
            raw = getattr(message, "raw", {}) or {}
            public_id = raw.get("_crm_author_public_id") if isinstance(raw, dict) else None
            return str(author), str(public_id) if public_id else None
    return None, None




def _ozon_system_dialog_markers() -> tuple[str, ...]:
    """Exact account markers for Ozon non-customer/system dialogs."""
    return tuple(
        token.strip().lower()
        for token in os.getenv(
            "OZON_SYSTEM_DIALOG_MARKERS",
            "notificationuser,notification_user,systemuser,system_user",
        ).split(",")
        if token.strip()
    )


def _ozon_chatbot_first_message_markers() -> tuple[str, ...]:
    return tuple(
        token.strip().lower()
        for token in os.getenv("OZON_FIRST_MESSAGE_SYSTEM_USER_MARKERS", os.getenv("OZON_CHATBOT_MARKERS", "chatbot")).split(",")
        if token.strip()
    )


def _ozon_chatbot_message_markers() -> tuple[str, ...]:
    return tuple(
        token.strip().lower()
        for token in os.getenv("OZON_CHATBOT_MARKERS", os.getenv("OZON_FIRST_MESSAGE_SYSTEM_USER_MARKERS", "chatbot")).split(",")
        if token.strip()
    )


def _normalize_system_sender(value: Any) -> str:
    return _shared_normalize_system_sender(value)


def _system_sender_matches(value: Any, markers: tuple[str, ...]) -> bool:
    return _shared_system_sender_matches(value, markers)


def _extract_sender_designations(value: Any, depth: int = 0) -> list[str]:
    """Extract sender/user names only; do not scan arbitrary raw text."""
    return _shared_extract_sender_designations(value, depth)


def _message_system_designations(message: Any) -> list[str]:
    indicators: list[str] = []
    author = getattr(message, "author", None)
    if author not in (None, ""):
        indicators.append(str(author))
    raw = getattr(message, "raw", None)
    indicators.extend(_extract_sender_designations(raw))
    return [item.strip().lower() for item in indicators if str(item or "").strip()]


def _message_sender_matches_markers(message: Any, markers: tuple[str, ...]) -> bool:
    indicators = _message_system_designations(message)
    return any(_system_sender_matches(indicator, markers) for indicator in indicators)


def _message_is_ozon_chatbot_message(message: Any) -> bool:
    if os.getenv("OZON_EXCLUDE_CHATBOT_MESSAGES", "1").strip().lower() in {"0", "false", "no", "off", "нет"}:
        return False
    return _message_sender_matches_markers(message, _ozon_chatbot_message_markers())


def _filter_ozon_chatbot_messages(messages: list[Any]) -> list[Any]:
    return [message for message in messages if not _message_is_ozon_chatbot_message(message)]


def _messages_are_ozon_system_dialog(messages: list[Any]) -> bool:
    """Return True for explicit Ozon non-customer/system dialogs.

    Rules:
    - notificationuser/systemuser are blocked on any message;
    - chatbot blocks the whole dialog when it is the first message sender;
    - dialogs made only of chatbot/system messages are hidden;
    - in mixed customer dialogs, chatbot messages are removed individually.
    """
    if os.getenv("OZON_EXCLUDE_SYSTEM_HISTORY_CHATS", "1").strip().lower() in {"0", "false", "no", "off", "нет"}:
        return False
    if not messages:
        return False

    technical_markers = _ozon_system_dialog_markers()
    chatbot_markers = _ozon_chatbot_message_markers()

    for message in messages:
        if _message_sender_matches_markers(message, technical_markers):
            return True

    first_indicators = _message_system_designations(messages[0])
    first_markers = _ozon_chatbot_first_message_markers()
    if any(_system_sender_matches(indicator, first_markers) for indicator in first_indicators):
        return True

    messages_with_sender = [message for message in messages if _message_system_designations(message)]
    if messages_with_sender and all(
        _message_sender_matches_markers(message, chatbot_markers) or _message_sender_matches_markers(message, technical_markers)
        for message in messages_with_sender
    ):
        return True

    return False


def _sync_hint(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    hint = metadata.get("_sync_hint")
    return hint if isinstance(hint, dict) else {}


def _hint_value(metadata: dict[str, Any] | None, *keys: str) -> str:
    hint = _sync_hint(metadata)
    for key in keys:
        value = hint.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _hint_int(metadata: dict[str, Any] | None, *keys: str) -> int:
    raw = _hint_value(metadata, *keys)
    try:
        return int(raw)
    except Exception:
        return 0





def _trusted_marketplace_message_id(raw_response: Any) -> str:
    """Extract only real message ids from marketplace send responses.

    A previous implementation used `result` as a fallback. For APIs that return
    `{"result": true}` or an object without the final message id, this created a
    fake external_message_id like "True" or "{'...'}". Later sync imported the
    real marketplace echo as another outbound message. Returning an empty string
    here lets repository-level echo matching upgrade the local row correctly.
    """
    if not isinstance(raw_response, dict):
        return ""
    for key in ("message_id", "messageId", "id", "uuid", "external_message_id", "externalMessageId"):
        value = raw_response.get(key)
        if value in (None, "") or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            return text
    result = raw_response.get("result")
    if isinstance(result, dict):
        for key in ("message_id", "messageId", "id", "uuid", "external_message_id", "externalMessageId"):
            value = result.get(key)
            if value in (None, "") or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def _mark_crm_sent_raw(raw_response: Any, *, author: str | None = None, user_id: int | None = None) -> dict[str, Any]:
    raw = dict(raw_response) if isinstance(raw_response, dict) else {"_crm_marketplace_response": raw_response}
    raw["_crm_sent_from_crm"] = True
    if author:
        raw["_crm_sent_by_label"] = author
    if user_id:
        raw["_crm_sent_by_user_id"] = user_id
    return raw

def _wb_last_message_payload_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return WB lastMessage saved in chat metadata, if present."""
    if not isinstance(metadata, dict):
        return None
    sync_hint = metadata.get("_sync_hint") if isinstance(metadata.get("_sync_hint"), dict) else {}
    candidates = [
        sync_hint.get("lastMessage") if isinstance(sync_hint, dict) else None,
        sync_hint.get("last_message") if isinstance(sync_hint, dict) else None,
        metadata.get("lastMessage"),
        metadata.get("last_message"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


def _normalize_wb_synced_message_for_local_outbound(chat_id: int, message: Any) -> tuple[str, str | None, dict[str, Any]]:
    """Correct WB echo of our own CRM reply when WB omits sender direction.

    WB `lastMessage` may not include a sender flag. The connector must default
    unknown messages to inbound, but if the same text/time was just saved locally
    as an outbound CRM reply, keep it outbound so SLA does not show
    "ждёт ответа" for our own answer.
    """
    direction = str(getattr(message, 'direction', None) or 'inbound')
    author = getattr(message, 'author', None)
    raw = getattr(message, 'raw', {}) or {}
    if not isinstance(raw, dict):
        raw = {'_crm_raw_value': raw}
    else:
        raw = dict(raw)

    if direction != 'inbound':
        return direction, author, raw

    try:
        match = repo.find_recent_matching_outbound_message(
            int(chat_id),
            getattr(message, 'text', '') or '',
            getattr(message, 'created_at', None),
            window_seconds=_env_int('WB_OUTBOUND_ECHO_MATCH_WINDOW_SECONDS', 900, minimum=30, maximum=86400),
        )
    except Exception:
        match = None

    if match:
        raw['_crm_direction_corrected_from_local_outbound'] = True
        raw['_crm_matched_outbound_message_id'] = match.get('id')
        direction = 'outbound'
        author = author if str(author or '').lower() in {'seller', 'manager', 'operator'} else (match.get('author') or 'seller')
    return direction, author, raw


def _import_wb_last_message_from_metadata(
    chat_id: int,
    external_chat_id: str,
    metadata: dict[str, Any] | None,
    *,
    fallback_created_at: str | None = None,
) -> dict[str, Any]:
    """Create/update one local message from WB chat-list lastMessage.

    This does not call WB API. It repairs old local WB chats that were created
    from /seller/chats but stayed empty because /seller/events was rate-limited
    or returned a shape the previous parser did not understand.
    """
    connector = connectors.get("wildberries")
    if not connector or not hasattr(connector, "_message_from_last_message"):
        return {"created": False, "reason": "wb_connector_unavailable"}

    last_message = _wb_last_message_payload_from_metadata(metadata)
    if not last_message:
        return {"created": False, "reason": "no_last_message_in_metadata"}

    try:
        message = connector._message_from_last_message(  # type: ignore[attr-defined]
            str(external_chat_id),
            {**last_message, "_chat_item": metadata or {}},
        )
    except Exception as exc:
        return {"created": False, "reason": f"parse_error: {exc}"}

    if not message:
        return {"created": False, "reason": "parser_returned_empty"}

    created_at = getattr(message, "created_at", None) or fallback_created_at
    try:
        direction, author, raw = _normalize_wb_synced_message_for_local_outbound(int(chat_id), message)
        message_id = repo.add_message(
            chat_id=int(chat_id),
            direction=direction,
            text=getattr(message, "text", "") or "[сообщение без текста / вложение]",
            author=author,
            external_message_id=getattr(message, "external_message_id", None),
            raw=raw,
            created_at=created_at,
        )
    except Exception as exc:
        return {"created": False, "reason": f"db_error: {exc}"}

    return {
        "created": True,
        "message_id": message_id,
        "direction": direction,
        "created_at": created_at,
        "text_preview": str(getattr(message, "text", "") or "")[:160],
    }


def repair_wb_local_messages_from_metadata(limit: int = 1000) -> dict[str, Any]:
    """Repair empty WB chats using lastMessage already saved in local metadata."""
    safe_limit = max(1, min(int(limit or 1000), 5000))
    repaired: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    scanned = 0

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                c.id,
                c.external_chat_id,
                c.customer_name,
                c.metadata_json,
                c.created_at,
                c.updated_at,
                c.last_message_at,
                c.last_message_preview,
                (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS messages_count
            FROM chats c
            WHERE c.marketplace='wildberries'
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    for row in rows:
        scanned += 1
        row_d = dict(row)
        try:
            metadata = json.loads(row_d.get("metadata_json") or "{}")
        except Exception:
            metadata = {}
        result = _import_wb_last_message_from_metadata(
            int(row_d["id"]),
            str(row_d["external_chat_id"]),
            metadata,
            fallback_created_at=row_d.get("updated_at") or row_d.get("created_at"),
        )
        if result.get("created"):
            repaired.append({
                "chat_id": row_d["id"],
                "external_chat_id": row_d["external_chat_id"],
                "customer_name": row_d.get("customer_name"),
                **result,
            })
        else:
            reason = str(result.get("reason") or "unknown")
            skipped[reason] = skipped.get(reason, 0) + 1

    repo.repair_chat_last_message_cache()
    return {
        "ok": True,
        "scanned": scanned,
        "repaired_count": len(repaired),
        "repaired_sample": repaired[:30],
        "skipped": skipped,
    }




def _recent_external_message_ids(chat_id: int, *, limit: int = 50) -> set[str]:
    """Return recent marketplace message IDs stored locally for one chat."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT external_message_id
                FROM messages
                WHERE chat_id = ?
                  AND external_message_id IS NOT NULL
                  AND external_message_id != ''
                  AND external_message_id != 'success'
                ORDER BY datetime(replace(replace(created_at, 'T', ' '), 'Z', '')) DESC, id DESC
                LIMIT ?
                """,
                (int(chat_id), int(limit)),
            ).fetchall()
    except Exception:
        return set()

    ids: set[str] = set()
    for row in rows:
        try:
            value = row["external_message_id"]
        except Exception:
            value = row[0] if row else None
        if value not in (None, ""):
            ids.add(str(value))
    return ids


def _ozon_last_message_is_missing_locally(existing_chat: dict[str, Any] | None, last_message_id: str | None) -> bool:
    """True when Ozon's last_message_id is not present in local messages."""
    if not existing_chat or not last_message_id:
        return False
    try:
        chat_id = int(existing_chat.get("id"))
    except Exception:
        return True
    return str(last_message_id) not in _recent_external_message_ids(chat_id)

def _should_fetch_messages(marketplace: str, existing_chat: dict[str, Any] | None, unified_chat: Any, *, background: bool) -> bool:
    """Decide whether this sync pass needs full message history for a chat.

    The slowest part of polling is /chat/history for many unchanged chats.
    In background mode we skip history when the marketplace list says that the
    last_message_id has not changed. New/unread chats are still fetched at once.
    Manual sync remains full.
    """
    if not background:
        return True

    # Ozon exposes last_message_id/first_unread_message_id in /v3/chat/list,
    # so we can safely do incremental background polling there. Other connectors
    # keep their previous behavior for now.
    if marketplace != "ozon":
        return True

    if not existing_chat:
        return True

    new_meta = getattr(unified_chat, "metadata", {}) or {}
    old_meta = existing_chat.get("metadata") or {}

    if _hint_int(new_meta, "unread_count") > 0:
        return True
    if _hint_value(new_meta, "first_unread_message_id"):
        return True

    new_last = _hint_value(new_meta, "last_message_id")
    old_last = _hint_value(old_meta, "last_message_id")

    # If this chat has no messages locally yet, fetch once even if Ozon does not
    # provide a useful last_message_id.
    try:
        if not repo.chat_has_messages(int(existing_chat.get("id"))):
            return True
    except Exception:
        return True

    # Ozon can update chat metadata with a new last_message_id before the
    # corresponding /v3/chat/history messages are saved locally. In that case
    # metadata-to-metadata comparison would skip the chat forever, while the CRM
    # still misses the latest messages inside the dialog.
    if new_last and _ozon_last_message_is_missing_locally(existing_chat, new_last):
        return True

    if new_last and old_last and new_last == old_last:
        return False
    if new_last and new_last != old_last:
        return True

    # Without a reliable marker, keep background lightweight and do not refetch
    # old unchanged chats on every pass. Manual sync can be used for deep repair.
    return False



def _shorten(value: str | None, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _message_for_ai(message: dict[str, Any]) -> str:
    direction = message.get("direction")
    if direction == "inbound":
        speaker = "Клиент"
    elif direction == "outbound":
        speaker = "Мы"
    else:
        speaker = "Внутренняя заметка CRM"
    time = message.get("created_at") or ""
    author = message.get("author") or ""
    text = _shorten(message.get("text") or "[вложение/нет текста]", 900)
    meta = ""
    if time:
        meta += f" {time}"
    if author:
        meta += f" · {author}"
    return f"{speaker}{meta}: {text}"


def _extract_response_text(data: dict[str, Any]) -> str:
    # Responses API often includes output_text, but we also support the nested output format.
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    parts: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts).strip()


def _openai_error_detail(response: httpx.Response) -> str:
    """Human-readable OpenAI error without exposing secrets."""
    body = response.text[:2000]
    message = body
    try:
        data = response.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("code") or body)
            code = err.get("code") or err.get("type")
            if code:
                message = f"{message} [{code}]"
    except Exception:
        pass
    hint = ""
    if response.status_code in {401, 403}:
        hint = " Проверьте OPENAI_API_KEY и доступ к API."
    elif response.status_code == 404:
        hint = " Проверьте OPENAI_MODEL: модель может быть недоступна вашему API-ключу."
    elif response.status_code == 429:
        hint = " Проверьте баланс, лимиты и квоты OpenAI API."
    elif response.status_code == 400:
        hint = " Проверьте OPENAI_MODEL и формат запроса."
    return f"OpenAI API error {response.status_code}: {message}{hint}"


async def _generate_ai_reply(chat: dict[str, Any], selected_message: dict[str, Any], extra_instruction: str | None = None) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY не указан в .env. Добавьте ключ и перезапустите CRM.")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    company_name = os.getenv("AI_COMPANY_NAME", "Arti")
    reply_style = os.getenv(
        "AI_REPLY_STYLE",
        "Вежливо, кратко, по делу, без лишних обещаний и без перевода клиента в сторонние мессенджеры.",
    )

    messages = chat.get("messages") or []
    selected_id = selected_message.get("id")
    selected_index = next((i for i, m in enumerate(messages) if m.get("id") == selected_id), len(messages) - 1)
    start = max(0, selected_index - 12)
    end = min(len(messages), selected_index + 6)
    context_messages = messages[start:end]

    task_lines = []
    for task in chat.get("tasks") or []:
        if task.get("status") not in {"done", "cancelled"}:
            title = _shorten(task.get("title"), 160)
            status = task.get("status") or "open"
            due = task.get("due_at") or "без срока"
            task_lines.append(f"- {title} · статус: {status} · срок: {due}")
    tasks_text = "\n".join(task_lines[:8]) if task_lines else "Нет открытых задач."

    system_prompt = f"""Ты помощник оператора маркетплейса для CRM {company_name}.
Твоя задача — подготовить черновик ответа клиенту на русском языке.
Стиль: {reply_style}
Правила:
- Ответь именно на выбранное сообщение клиента, учитывая контекст переписки.
- Не упоминай, что ты ИИ, модель или ассистент.
- Не обещай возврат, замену, компенсацию, скидку, сроки доставки или конкретное решение, если этого нет в данных.
- Не проси клиента перейти в WhatsApp, Telegram, на сайт или в сторонний канал.
- Не запрашивай паспортные данные, банковские реквизиты, телефон или email.
- Если информации не хватает, напиши безопасное уточнение или предложи менеджеру проверить данные.
- Не раскрывай внутренние заметки и задачи CRM клиенту; используй их только как контекст.
- Верни только готовый текст ответа клиенту, без заголовков, вариантов и пояснений.
"""

    context_text = "\n".join(_message_for_ai(m) for m in context_messages)
    selected_text = _message_for_ai(selected_message)
    user_prompt = f"""Маркетплейс: {chat.get('marketplace')}
Клиент: {chat.get('customer_name') or chat.get('customer_public_id') or 'неизвестно'}
Заказ: {chat.get('order_id') or 'не указан'}
Статус чата: {chat.get('status_label') or chat.get('status')}
SLA: {'чат ждёт ответа' if chat.get('sla_waiting_response') else 'последний ответ не требует срочного ответа'}

Выбранное сообщение, на которое нужно ответить:
{selected_text}

Контекст переписки вокруг выбранного сообщения:
{context_text}

Открытые задачи по чату:
{tasks_text}

Дополнительная инструкция менеджера:
{_shorten(extra_instruction, 800) if extra_instruction else 'нет'}

Подготовь один аккуратный ответ клиенту.
"""

    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "max_output_tokens": _env_int("OPENAI_MAX_OUTPUT_TOKENS", 700, minimum=100, maximum=3000),
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OpenAI API недоступен: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=_openai_error_detail(response))

    data = response.json()
    draft = _extract_response_text(data)
    if not draft:
        raise HTTPException(status_code=502, detail="OpenAI API вернул пустой ответ")
    return draft


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "нет"}


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except Exception:
        return default


def _asset_proxy_allowed(url: str) -> bool:
    """Conservative allow-list for server-side image previews.

    Needed because some Ozon image/file URLs do not render directly in the browser.
    The proxy only accepts https URLs, blocks local/private IPs, and limits hosts to
    marketplace/CDN-like domains.
    """
    raw_allowed = os.getenv("IMAGE_PROXY_ALLOWED_HOSTS")
    return asset_url_allowed(url, parse_allowed_asset_hosts(raw_allowed))


def _asset_proxy_headers(url: str) -> dict[str, str]:
    headers = {
        "Accept": "image/jpeg,image/png,image/webp,image/gif",
        "Accept-Encoding": "identity",
    }
    if asset_url_requires_ozon_credentials(url):
        connector = connectors.get("ozon")
        if connector and getattr(connector, "client_id", None) and getattr(connector, "api_key", None):
            headers.update({"Client-Id": connector.client_id, "Api-Key": connector.api_key})
    return headers


def _temporary_connector_overrides(connector: Any, overrides: dict[str, Any]):
    """Tiny async-friendly context manager for per-sync connector limits."""
    class _Ctx:
        def __enter__(self_inner):
            self_inner.old_values = {}
            for key, value in overrides.items():
                if value is None or not hasattr(connector, key):
                    continue
                self_inner.old_values[key] = getattr(connector, key)
                setattr(connector, key, value)
            return connector

        def __exit__(self_inner, exc_type, exc, tb):
            for key, value in self_inner.old_values.items():
                setattr(connector, key, value)
            return False

    return _Ctx()



async def _sync_ozon_fast_inbox_unlocked(*, background: bool = True) -> dict[str, Any]:
    """Fast Ozon inbox sync for new/recent chats.

    v83: deep Ozon backfill may scan thousands of chats and many history pages.
    That is correct for archive recovery but too slow for operator inbox polling.
    This function uses a fresh OzonConnector instance and a small/recent profile,
    so new chats are not delayed by backfill settings or a long deep import.
    """
    connector = OzonConnector()
    if not getattr(connector, "client_id", "") or not getattr(connector, "api_key", ""):
        return {"ok": False, "marketplace": "ozon", "configured": False, "count": 0}

    # Operator/background polling must be quick. Deep archive recovery should
    # use manual/debug sync with explicit env limits, not the opened CRM tab.
    default_max_chats = _env_int("OZON_OPERATOR_FAST_SYNC_MAX_CHATS", 50, minimum=20, maximum=1000) if background else 300
    default_pages_per_variant = _env_int("OZON_OPERATOR_FAST_SYNC_PAGES_PER_VARIANT", 1, minimum=1, maximum=20) if background else 3

    connector.sync_max_chats = _env_int("OZON_FAST_SYNC_MAX_CHATS", default_max_chats, minimum=20, maximum=1000)
    connector.sync_pages_per_variant = _env_int("OZON_FAST_SYNC_PAGES_PER_VARIANT", default_pages_per_variant, minimum=1, maximum=20)
    connector.sync_variant_mode = os.getenv("OZON_FAST_SYNC_VARIANT_MODE", "fast")
    connector.sync_include_closed = False
    connector.history_pages = _env_int("OZON_FAST_HISTORY_PAGES", 1, minimum=1, maximum=5)

    synced: list[int] = []
    errors: list[dict[str, Any]] = []
    messages_total = 0
    histories_skipped = 0
    reopened_count = 0

    try:
        unified_chats = await connector.list_chats()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    chat_refs: list[tuple[int, Any, dict[str, Any] | None]] = []
    for unified_chat in unified_chats:
        existing_chat = repo.get_chat_by_external(unified_chat.marketplace, unified_chat.external_chat_id)
        should_fetch = _should_fetch_messages("ozon", existing_chat, unified_chat, background=background)
        chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace=unified_chat.marketplace,  # type: ignore[arg-type]
                external_chat_id=unified_chat.external_chat_id,
                customer_name=unified_chat.customer_name,
                customer_public_id=unified_chat.customer_public_id,
                order_id=unified_chat.order_id,
                status=unified_chat.status,  # type: ignore[arg-type]
                metadata=unified_chat.metadata,
            )
        )
        synced.append(chat_id)
        if should_fetch:
            chat_refs.append((chat_id, unified_chat, existing_chat))
        else:
            histories_skipped += 1

    concurrency = _env_int("OZON_FAST_MESSAGE_FETCH_CONCURRENCY", 4, minimum=1, maximum=8)
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_messages(chat_id: int, unified_chat: Any, existing_chat: dict[str, Any] | None) -> tuple[int, Any, dict[str, Any] | None, list[Any] | None, str | None]:
        try:
            async with semaphore:
                messages = await connector.get_messages(unified_chat.external_chat_id)
            return chat_id, unified_chat, existing_chat, messages, None
        except Exception as exc:
            return chat_id, unified_chat, existing_chat, None, str(exc)

    fetch_results = await asyncio.gather(*(fetch_messages(chat_id, unified_chat, existing_chat) for chat_id, unified_chat, existing_chat in chat_refs))

    for chat_id, unified_chat, existing_chat, messages, error in fetch_results:
        if error:
            errors.append({"chat_id": chat_id, "external_chat_id": unified_chat.external_chat_id, "error": error})
            continue

        messages = messages or []

        if _messages_are_ozon_system_dialog(messages):
            repo.hide_ozon_system_chat_ids([chat_id], reason="fast_sync_system_or_chatbot_sender")
            if _env_bool("OZON_DELETE_SYSTEM_HISTORY_CHATS", False):
                repo.delete_chats_by_ids([chat_id])
            continue

        messages_to_store = _filter_ozon_chatbot_messages(messages)

        if not _is_real_customer_name(unified_chat.customer_name):
            fallback_name, fallback_public_id = _customer_info_from_messages(messages)
            if fallback_name:
                repo.update_chat_customer_info(chat_id, fallback_name, fallback_public_id)

        previous_last_at = (existing_chat or {}).get("last_message_at")
        for message in messages_to_store:
            repo.add_message(
                chat_id=chat_id,
                direction=message.direction,
                text=message.text,
                author=message.author,
                external_message_id=message.external_message_id,
                raw=message.raw,
                created_at=message.created_at,
            )
            messages_total += 1

        latest_local = repo.get_latest_message_for_chat(chat_id)
        latest_at = (latest_local or {}).get("created_at")
        if (existing_chat or {}).get("status") == "closed" and latest_at and latest_at != previous_last_at:
            if repo.reopen_closed_chat_for_new_activity(chat_id, (latest_local or {}).get("direction")):
                reopened_count += 1

    result = {
        "ok": not errors,
        "marketplace": "ozon",
        "mode": "fast_inbox",
        "configured": True,
        "count": len(synced),
        "messages_count": messages_total,
        "errors_count": len(errors),
        "errors": errors[:20],
        "chat_ids": synced,
        "background": background,
        "message_fetch_concurrency": concurrency,
        "histories_fetched": len(chat_refs),
        "histories_skipped": histories_skipped,
        "reopened_closed_chats": reopened_count,
        "connector_debug": getattr(connector, "last_sync_debug", {}),
        "fast_settings": {
            "sync_max_chats": connector.sync_max_chats,
            "sync_pages_per_variant": connector.sync_pages_per_variant,
            "sync_variant_mode": connector.sync_variant_mode,
            "history_pages": connector.history_pages,
        },
    }
    app.state.last_ozon_fast_sync = result
    return result


async def _sync_ozon_fast_inbox_locked(*, background: bool = True) -> dict[str, Any]:
    lock = _marketplace_sync_lock("ozon")
    async with lock:
        return await _sync_ozon_fast_inbox_unlocked(background=background)


@app.post("/api/debug/ozon/fast-sync")
async def debug_ozon_fast_sync() -> dict[str, Any]:
    """Run the lightweight Ozon new/recent chats sync once."""
    return await _sync_ozon_fast_inbox_locked(background=False)


def _background_overrides_for_marketplace(marketplace: str, connector: Any) -> dict[str, Any]:
    """Use a lighter sync profile for background polling.

    Full sync of every chat history is too slow for daily operator work. In the
    background we focus on unread/recent chats and keep history requests parallel.
    Manual /api/sync/<marketplace> still uses normal connector limits.
    """
    if marketplace == "ozon":
        return {
            "sync_max_chats": _env_int("OZON_BACKGROUND_SYNC_MAX_CHATS", _env_int("OZON_SYNC_MAX_CHATS", 100, minimum=1, maximum=1000), minimum=1, maximum=1000),
            "sync_pages_per_variant": _env_int("OZON_BACKGROUND_SYNC_PAGES_PER_VARIANT", 2, minimum=1, maximum=10),
            "sync_variant_mode": os.getenv("OZON_BACKGROUND_SYNC_VARIANT_MODE", "fast"),
        }
    if marketplace == "yandex":
        return {
            "max_chats": _env_int("YANDEX_BACKGROUND_SYNC_MAX_CHATS", _env_int("YANDEX_SYNC_MAX_CHATS", 30, minimum=1, maximum=200), minimum=1, maximum=200),
            "max_pages": _env_int("YANDEX_BACKGROUND_SYNC_MAX_PAGES", 1, minimum=1, maximum=20),
        }
    if marketplace == "wildberries":
        return {
            "max_events": _env_int("WB_BACKGROUND_SYNC_MAX_EVENTS", _env_int("WB_SYNC_MAX_EVENTS", 1000, minimum=1, maximum=10000), minimum=1, maximum=10000),
            "event_pages": _env_int("WB_BACKGROUND_SYNC_EVENT_PAGES", 3, minimum=1, maximum=100),
        }
    return {}


async def _sync_marketplace_unlocked(marketplace: str, *, background: bool = False) -> dict[str, Any]:
    """Sync one marketplace without taking the global sync lock.

    v17: message history requests are fetched concurrently. This makes new chats
    and new messages appear much faster than the older sequential loop.
    """
    if marketplace not in connectors or marketplace == "mock":
        raise HTTPException(status_code=400, detail="Unknown marketplace")

    connector = connectors[marketplace]
    synced: list[int] = []
    errors: list[dict[str, Any]] = []
    messages_total = 0
    histories_skipped = 0

    overrides = _background_overrides_for_marketplace(marketplace, connector) if background else {}
    try:
        with _temporary_connector_overrides(connector, overrides):
            unified_chats = await connector.list_chats()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    chat_refs: list[tuple[int, Any, dict[str, Any] | None]] = []
    for unified_chat in unified_chats:
        existing_chat = repo.get_chat_by_external(unified_chat.marketplace, unified_chat.external_chat_id)
        if repo.chat_is_excluded_as_system(existing_chat):
            histories_skipped += 1
            continue
        should_fetch = _should_fetch_messages(marketplace, existing_chat, unified_chat, background=background)
        chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace=unified_chat.marketplace,  # type: ignore[arg-type]
                external_chat_id=unified_chat.external_chat_id,
                customer_name=unified_chat.customer_name,
                customer_public_id=unified_chat.customer_public_id,
                order_id=unified_chat.order_id,
                status=unified_chat.status,  # type: ignore[arg-type]
                metadata=unified_chat.metadata,
            )
        )
        synced.append(chat_id)
        if marketplace == "wildberries":
            try:
                _import_wb_last_message_from_metadata(
                    chat_id,
                    unified_chat.external_chat_id,
                    getattr(unified_chat, "metadata", {}) or {},
                    fallback_created_at=(existing_chat or {}).get("updated_at") or (existing_chat or {}).get("created_at"),
                )
            except Exception:
                pass
        if should_fetch:
            chat_refs.append((chat_id, unified_chat, existing_chat))
        else:
            histories_skipped += 1

    if marketplace == "yandex":
        # Yandex Market returns 420 METHOD_FAILURE when more than 4 history
        # requests are made in parallel for one businessId. Keep a safe default
        # below the hard limit; it can still be overridden through .env.
        concurrency = _env_int("YANDEX_MESSAGE_FETCH_CONCURRENCY", 3, minimum=1, maximum=4)
    elif marketplace == "wildberries":
        concurrency = _env_int("WB_MESSAGE_FETCH_CONCURRENCY", 2, minimum=1, maximum=4)
    elif marketplace == "ozon":
        concurrency = _env_int("OZON_MESSAGE_FETCH_CONCURRENCY", 4, minimum=1, maximum=8)
    else:
        concurrency = _env_int(
            "MARKETPLACE_MESSAGE_FETCH_CONCURRENCY",
            4 if background else 3,
            minimum=1,
            maximum=8,
        )
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_messages(chat_id: int, unified_chat: Any, existing_chat: dict[str, Any] | None) -> tuple[int, Any, dict[str, Any] | None, list[Any] | None, str | None]:
        try:
            async with semaphore:
                messages = await connector.get_messages(unified_chat.external_chat_id)
            return chat_id, unified_chat, existing_chat, messages, None
        except Exception as exc:
            return chat_id, unified_chat, existing_chat, None, str(exc)

    fetch_results = await asyncio.gather(*(fetch_messages(chat_id, unified_chat, existing_chat) for chat_id, unified_chat, existing_chat in chat_refs))

    reopened_count = 0
    for chat_id, unified_chat, existing_chat, messages, error in fetch_results:
        if error:
            errors.append(
                {
                    "chat_id": chat_id,
                    "external_chat_id": unified_chat.external_chat_id,
                    "error": error,
                }
            )
            continue

        messages = messages or []
        if marketplace == "ozon" and _messages_are_ozon_system_dialog(messages):
            # Explicit system dialogs are not customer chats. Hide and remember them
            # instead of deleting by default; otherwise the next chat-list sync can
            # recreate them and show them again before history is fetched.
            repo.hide_ozon_system_chat_ids([chat_id], reason="history_system_or_chatbot_sender")
            if _env_bool("OZON_DELETE_SYSTEM_HISTORY_CHATS", False):
                repo.delete_chats_by_ids([chat_id])
            continue

        messages_to_store = _filter_ozon_chatbot_messages(messages) if marketplace == "ozon" else messages

        # Если список чатов не содержит имени покупателя, пробуем взять его из истории.
        if not _is_real_customer_name(unified_chat.customer_name):
            fallback_name, fallback_public_id = _customer_info_from_messages(messages)
            if fallback_name:
                repo.update_chat_customer_info(chat_id, fallback_name, fallback_public_id)

        previous_last_at = (existing_chat or {}).get("last_message_at")
        for message in messages_to_store:
            direction = message.direction
            author = message.author
            raw = message.raw
            if marketplace == "wildberries":
                direction, author, raw = _normalize_wb_synced_message_for_local_outbound(chat_id, message)
            repo.add_message(
                chat_id=chat_id,
                direction=direction,
                text=message.text,
                author=author,
                external_message_id=message.external_message_id,
                raw=raw,
                created_at=message.created_at,
            )
            messages_total += 1

        latest_local = repo.get_latest_message_for_chat(chat_id)
        latest_at = (latest_local or {}).get("created_at")
        if (existing_chat or {}).get("status") == "closed" and latest_at and latest_at != previous_last_at:
            if repo.reopen_closed_chat_for_new_activity(chat_id, (latest_local or {}).get("direction")):
                reopened_count += 1

    wb_lastmessage_direction_repairs = 0
    if marketplace == "wildberries":
        try:
            wb_lastmessage_direction_repairs = repo.repair_wb_lastmessage_directions()
        except Exception:
            wb_lastmessage_direction_repairs = 0

    return {
        "ok": not errors,
        "marketplace": marketplace,
        "count": len(synced),
        "messages_count": messages_total,
        "errors_count": len(errors),
        "errors": errors[:20],
        "chat_ids": synced,
        "background": background,
        "message_fetch_concurrency": concurrency,
        "histories_fetched": len(chat_refs),
        "histories_skipped": histories_skipped,
        "reopened_closed_chats": reopened_count,
        "wb_lastmessage_direction_repairs": wb_lastmessage_direction_repairs,
        "sync_overrides": overrides,
    }




async def _sync_ozon_reviews_unlocked(*, background: bool = False) -> dict[str, Any]:
    connector = connectors.get("ozon")
    if not connector or not getattr(connector, "client_id", "") or not getattr(connector, "api_key", ""):
        return {"ok": False, "marketplace": "ozon", "configured": False, "count": 0}
    if not hasattr(connector, "list_reviews"):
        return {"ok": False, "marketplace": "ozon", "error": "Ozon connector has no review API"}
    limit = _env_int("OZON_REVIEWS_BACKGROUND_LIMIT" if background else "OZON_REVIEWS_SYNC_LIMIT", 50, minimum=20, maximum=100)
    pages = _env_int("OZON_REVIEWS_BACKGROUND_PAGES" if background else "OZON_REVIEWS_SYNC_PAGES", 1 if background else 2, minimum=1, maximum=20)
    try:
        reviews = await connector.list_reviews(limit=limit, pages=pages)  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    ids: list[int] = []
    for review in reviews:
        try:
            ids.append(repo.upsert_review(review))
        except Exception as exc:
            # one malformed review must not break the whole sync
            print("review upsert failed", exc)
    result = {
        "ok": True,
        "marketplace": "ozon",
        "count": len(ids),
        "review_ids": ids[:50],
        "background": background,
        "limit": limit,
        "pages": pages,
        "debug": getattr(connector, "last_reviews_debug", {}),
    }
    if not background:
        app.state.last_reviews_sync = result
    return result


async def _sync_ozon_questions_unlocked(*, background: bool = False) -> dict[str, Any]:
    connector = connectors.get("ozon")
    if not connector or not getattr(connector, "client_id", "") or not getattr(connector, "api_key", ""):
        return {"ok": False, "marketplace": "ozon", "configured": False, "count": 0}
    if not hasattr(connector, "list_questions"):
        return {"ok": False, "marketplace": "ozon", "error": "Ozon connector has no questions API"}
    limit = _env_int("OZON_QUESTIONS_BACKGROUND_LIMIT" if background else "OZON_QUESTIONS_SYNC_LIMIT", 100 if background else 100, minimum=1, maximum=100)
    pages = _env_int("OZON_QUESTIONS_BACKGROUND_PAGES" if background else "OZON_QUESTIONS_SYNC_PAGES", 3 if background else 5, minimum=1, maximum=20)
    try:
        questions = await connector.list_questions(limit=limit, pages=pages)  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    ids: list[int] = []
    for question in questions:
        try:
            ids.append(repo.upsert_ozon_question(question))
        except Exception as exc:
            print("question upsert failed", exc)
    result = {
        "ok": True,
        "marketplace": "ozon",
        "count": len(ids),
        "question_ids": ids[:50],
        "background": background,
        "limit": limit,
        "pages": pages,
        "debug": getattr(connector, "last_questions_debug", {}),
    }
    if not background:
        app.state.last_questions_sync = result
    return result


async def _sync_marketplace_locked(marketplace: str) -> dict[str, Any]:
    # Keep the old global lock for manual /api/sync/{marketplace}, but also use
    # the marketplace-specific lock so manual sync cannot overlap with the
    # background/frontend worker for the same external API.
    lock: asyncio.Lock = app.state.sync_lock
    async with lock:
        async with _marketplace_sync_lock(marketplace):
            result = await _sync_marketplace_unlocked(marketplace)
        app.state.last_sync = result
        return result


def _connector_is_configured_for_sync(marketplace: str, connector: Any) -> bool:
    if marketplace == "ozon":
        return bool(getattr(connector, "client_id", "") and getattr(connector, "api_key", ""))
    if marketplace == "yandex":
        return bool(getattr(connector, "token", "") and getattr(connector, "business_id", ""))
    if marketplace == "wildberries":
        return bool(getattr(connector, "token", ""))
    return True


def _frontend_sync_enabled_for_marketplace(marketplace: str) -> bool:
    if marketplace == "ozon":
        return _env_bool("OZON_FRONTEND_SYNC", True)
    if marketplace == "yandex":
        return _env_bool("YANDEX_FRONTEND_SYNC", True)
    if marketplace == "wildberries":
        return _env_bool("WB_FRONTEND_SYNC", True)
    return _env_bool(f"{marketplace.upper()}_FRONTEND_SYNC", True)


async def _sync_operator_frontend_unlocked() -> dict[str, Any]:
    """Lightweight operator-triggered sync for shared hosting.

    Fastfox/Fox Start does not guarantee a permanently running background worker,
    so the opened CRM tab periodically calls this endpoint. Ozon uses the fast
    inbox sync; WB and Yandex use their background sync profiles with the same
    per-marketplace throttles as the server background loop to avoid API spam.
    """
    now = time.time()
    last_poll_at: dict[str, float] = getattr(app.state, "frontend_operator_sync_last_poll_at", {})
    if not isinstance(last_poll_at, dict):
        last_poll_at = {}
        app.state.frontend_operator_sync_last_poll_at = last_poll_at

    per_marketplace: dict[str, Any] = {}
    total_chats = 0
    total_messages = 0
    total_errors = 0

    for marketplace in ("ozon", "yandex", "wildberries"):
        connector = connectors.get(marketplace)
        if connector is None:
            per_marketplace[marketplace] = {"enabled": False, "status": "missing_connector"}
            continue

        if not _frontend_sync_enabled_for_marketplace(marketplace):
            per_marketplace[marketplace] = {"enabled": False}
            continue

        if not _connector_is_configured_for_sync(marketplace, connector):
            per_marketplace[marketplace] = {"enabled": True, "configured": False, "status": "skipped"}
            continue

        if marketplace == "wildberries":
            cooldown_remaining = 0
            if hasattr(connector, "_cooldown_remaining"):
                try:
                    cooldown_remaining = int(connector._cooldown_remaining())
                except Exception:
                    cooldown_remaining = 0
            if cooldown_remaining > 0:
                per_marketplace[marketplace] = {
                    "enabled": True,
                    "configured": True,
                    "status": "cooldown",
                    "retry_after_seconds": cooldown_remaining,
                    "reason": "WB 429 Too Many Requests",
                }
                continue

        min_interval = _background_min_interval_for_marketplace(marketplace)
        last_poll = float(last_poll_at.get(marketplace, 0.0) or 0.0)
        wait_seconds = int(max(0.0, min_interval - (now - last_poll)))
        if wait_seconds > 0:
            per_marketplace[marketplace] = {
                "enabled": True,
                "configured": True,
                "status": "throttled",
                "retry_after_seconds": wait_seconds,
                "min_interval_seconds": min_interval,
            }
            continue

        last_poll_at[marketplace] = now
        try:
            if marketplace == "ozon" and _env_bool("OZON_FAST_INBOX_SYNC_ENABLED", True):
                result = await _sync_ozon_fast_inbox_locked(background=True)
            else:
                async with _marketplace_sync_lock(marketplace):
                    result = await _sync_marketplace_unlocked(marketplace, background=True)

            total_chats += int(result.get("count") or 0)
            total_messages += int(result.get("messages_count") or 0)
            total_errors += int(result.get("errors_count") or 0)
            per_marketplace[marketplace] = {
                "enabled": True,
                "configured": result.get("configured", True),
                "status": "ok" if result.get("ok", True) else "partial_error",
                "result": result,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            total_errors += 1
            per_marketplace[marketplace] = {
                "enabled": True,
                "configured": True,
                "status": "error",
                "error": str(exc),
            }

    ok_statuses = {"ok", "skipped", "throttled", "cooldown", "missing_connector"}
    payload = {
        "ok": all((not value.get("enabled", True)) or value.get("status") in ok_statuses for value in per_marketplace.values()),
        "mode": "operator_frontend",
        "marketplaces": per_marketplace,
        "count": total_chats,
        "messages_count": total_messages,
        "errors_count": total_errors,
        "background": True,
    }
    app.state.last_frontend_operator_sync = payload
    return payload



def _background_min_interval_for_marketplace(marketplace: str) -> int:
    """Per-marketplace polling guard to avoid API 429 rate limits.

    WB Buyers Chat is especially sensitive to frequent /seller/chats and
    /seller/events polling, so it gets its own larger interval. The UI can
    still refresh local DB every few seconds; only external API polling is
    throttled.
    """
    if marketplace == "wildberries":
        return _env_int("WB_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS", 3700, minimum=30, maximum=7200)
    if marketplace == "yandex":
        return _env_int("YANDEX_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS", 30, minimum=10, maximum=1800)
    if marketplace == "ozon":
        return _env_int("OZON_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS", 8, minimum=3, maximum=3600)
    return _env_int("MARKETPLACE_BACKGROUND_SYNC_INTERVAL", 15, minimum=5, maximum=3600)


async def _background_sync_loop() -> None:
    """Background polling for all configured marketplaces.

    Ozon/WB/Yandex are synced without UI buttons. Each marketplace can be
    switched off independently through .env.
    """
    if not _env_bool("MARKETPLACE_BACKGROUND_SYNC", True):
        app.state.last_background_sync = {"enabled": False}
        return

    interval = _env_int("MARKETPLACE_BACKGROUND_SYNC_INTERVAL", 15, minimum=5, maximum=3600)
    app.state.last_background_sync = {
        "enabled": True,
        "interval_seconds": interval,
        "status": "waiting",
        "marketplaces": {},
    }

    await asyncio.sleep(3)
    last_marketplace_poll_at: dict[str, float] = {}
    last_reviews_poll_at = 0.0
    last_questions_poll_at = 0.0
    while True:
        loop_started_at = time.time()
        per_marketplace: dict[str, Any] = {}
        sync_jobs: list[tuple[str, Any]] = []
        for marketplace, connector in connectors.items():
            if marketplace == "mock":
                continue
            enabled = _env_bool(f"{marketplace.upper()}_BACKGROUND_SYNC", True)
            if marketplace == "ozon":
                enabled = _env_bool("OZON_BACKGROUND_SYNC", enabled)
            if marketplace == "yandex":
                enabled = _env_bool("YANDEX_BACKGROUND_SYNC", enabled)
            if marketplace == "wildberries":
                enabled = _env_bool("WB_BACKGROUND_SYNC", enabled)
            if not enabled:
                per_marketplace[marketplace] = {"enabled": False}
                continue

            # Skip unconfigured connectors silently; debug endpoints/README explain keys.
            configured = True
            if marketplace == "ozon":
                configured = bool(getattr(connector, "client_id", "") and getattr(connector, "api_key", ""))
            elif marketplace == "yandex":
                configured = bool(getattr(connector, "token", "") and getattr(connector, "business_id", ""))
            elif marketplace == "wildberries":
                configured = bool(getattr(connector, "token", ""))
            if not configured:
                per_marketplace[marketplace] = {"enabled": True, "configured": False, "status": "skipped"}
                continue

            # Prevent API spam and 429 rate-limit storms. The CRM UI can keep
            # refreshing local data often, but external marketplace polling must
            # respect per-API cadence.
            min_interval = _background_min_interval_for_marketplace(marketplace)
            last_poll = last_marketplace_poll_at.get(marketplace, 0.0)
            wait_seconds = int(max(0.0, min_interval - (loop_started_at - last_poll)))

            if marketplace == "wildberries":
                cooldown_remaining = 0
                if hasattr(connector, "_cooldown_remaining"):
                    try:
                        cooldown_remaining = int(connector._cooldown_remaining())
                    except Exception:
                        cooldown_remaining = 0
                if cooldown_remaining > 0:
                    per_marketplace[marketplace] = {
                        "enabled": True,
                        "configured": True,
                        "status": "cooldown",
                        "retry_after_seconds": cooldown_remaining,
                        "reason": "WB 429 Too Many Requests",
                    }
                    continue

            if wait_seconds > 0:
                per_marketplace[marketplace] = {
                    "enabled": True,
                    "configured": True,
                    "status": "throttled",
                    "retry_after_seconds": wait_seconds,
                    "min_interval_seconds": min_interval,
                }
                continue

            last_marketplace_poll_at[marketplace] = loop_started_at
            sync_jobs.append((marketplace, connector))

        async def run_marketplace_sync(marketplace: str) -> tuple[str, dict[str, Any]]:
            try:
                if marketplace == "ozon" and _env_bool("OZON_FAST_INBOX_SYNC_ENABLED", True):
                    result = await _sync_ozon_fast_inbox_locked(background=True)
                else:
                    async with _marketplace_sync_lock(marketplace):
                        result = await _sync_marketplace_unlocked(marketplace, background=True)
                return marketplace, {"enabled": True, "configured": True, "status": "ok", "result": result}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return marketplace, {"enabled": True, "configured": True, "status": "error", "error": str(exc)}

        # Run marketplace polling in parallel instead of Ozon -> Yandex -> WB sequentially.
        # This keeps new messages visible much sooner when multiple connectors are enabled.
        if sync_jobs:
            results = await asyncio.gather(*(run_marketplace_sync(marketplace) for marketplace, _ in sync_jobs))
            for marketplace, result in results:
                per_marketplace[marketplace] = result

        if _env_bool("OZON_REVIEWS_BACKGROUND_SYNC", True):
            reviews_min_interval = _env_int("OZON_REVIEWS_BACKGROUND_MIN_INTERVAL_SECONDS", 300, minimum=30, maximum=7200)
            reviews_wait_seconds = int(max(0.0, reviews_min_interval - (loop_started_at - last_reviews_poll_at)))
            if reviews_wait_seconds > 0:
                per_marketplace["ozon_reviews"] = {"enabled": True, "configured": True, "status": "throttled", "retry_after_seconds": reviews_wait_seconds, "min_interval_seconds": reviews_min_interval}
            else:
                last_reviews_poll_at = loop_started_at
                try:
                    reviews_result = await _sync_ozon_reviews_unlocked(background=True)
                    per_marketplace["ozon_reviews"] = {"enabled": True, "configured": reviews_result.get("configured", True), "status": "ok" if reviews_result.get("ok") else "skipped", "result": reviews_result}
                except Exception as exc:
                    per_marketplace["ozon_reviews"] = {"enabled": True, "configured": True, "status": "error", "error": str(exc)}

        if _env_bool("OZON_QUESTIONS_BACKGROUND_SYNC", True):
            questions_min_interval = _env_int("OZON_QUESTIONS_MIN_INTERVAL_SECONDS", 15, minimum=5, maximum=7200)
            questions_wait_seconds = int(max(0.0, questions_min_interval - (loop_started_at - last_questions_poll_at)))
            if questions_wait_seconds > 0:
                per_marketplace["ozon_questions"] = {"enabled": True, "configured": True, "status": "throttled", "retry_after_seconds": questions_wait_seconds, "min_interval_seconds": questions_min_interval}
            else:
                last_questions_poll_at = loop_started_at
                try:
                    questions_result = await _sync_ozon_questions_unlocked(background=True)
                    per_marketplace["ozon_questions"] = {"enabled": True, "configured": questions_result.get("configured", True), "status": "ok" if questions_result.get("ok") else "skipped", "result": questions_result}
                except Exception as exc:
                    per_marketplace["ozon_questions"] = {"enabled": True, "configured": True, "status": "error", "error": str(exc)}

        notification_catchup: dict[str, Any] | None = None
        try:
            notification_catchup = {
                "messages": repo.enqueue_missing_message_notifications(
                    _env_int("CRM_NOTIFICATION_CATCHUP_MESSAGE_LIMIT", 200, minimum=1, maximum=1000)
                ),
                "questions": repo.enqueue_missing_question_notifications(
                    _env_int("CRM_NOTIFICATION_CATCHUP_QUESTION_LIMIT", 200, minimum=1, maximum=1000)
                ),
            }
        except Exception as exc:
            notification_catchup = {"ok": False, "error": str(exc)}

        app.state.last_background_sync = {
            "enabled": True,
            "interval_seconds": interval,
            "status": "ok" if all(v.get("status") in {"ok", "skipped", "throttled", "cooldown"} or v.get("enabled") is False for v in per_marketplace.values()) else "partial_error",
            "marketplaces": per_marketplace,
            "notification_catchup": notification_catchup,
        }
        await asyncio.sleep(interval)


def _ensure_initial_admin() -> dict[str, Any] | None:
    if repo.users_exist():
        return None
    credentials = resolve_bootstrap_admin_credentials(
        os.getenv("BOOTSTRAP_ADMIN_USERNAME"),
        os.getenv("BOOTSTRAP_ADMIN_PASSWORD"),
        os.getenv("BOOTSTRAP_ADMIN_DISPLAY_NAME"),
    )
    created_admin = repo.ensure_initial_admin(
        credentials.username,
        credentials.password,
        credentials.display_name,
    )
    if created_admin:
        print("[Arti CRM] Created initial administrator from explicit bootstrap configuration.")
    return created_admin


@app.on_event("startup")
async def on_startup() -> None:
    if _AUTH_DISABLED_CONFIGURATION_ERROR is not None:
        raise _AUTH_DISABLED_CONFIGURATION_ERROR
    init_db()
    try:
        repo.ensure_security_tables()
        repo.ensure_login_security_tables()
    except Exception as exc:
        print(f"[Arti CRM] Security table migration failed: {_mask_sensitive(str(exc))}")
    _ensure_initial_admin()
    repo.cleanup_expired_sessions()
    repo.delete_mock_chats()
    repo.delete_ozon_support_chats()
    repo.normalize_legacy_outbound_timestamps()
    # Repair stale chat previews/order after previous versions updated cached
    # last_message_* fields while importing older history.
    repo.repair_chat_last_message_cache()
    try:
        app.state.last_wb_local_repair = repair_wb_local_messages_from_metadata(limit=2000)
    except Exception as exc:
        app.state.last_wb_local_repair = {"ok": False, "error": str(exc)}
    try:
        app.state.last_wb_lastmessage_direction_repair = repo.repair_wb_lastmessage_directions()
    except Exception as exc:
        app.state.last_wb_lastmessage_direction_repair = {"ok": False, "error": str(exc)}
    try:
        app.state.last_outbound_echo_repair = repo.repair_outbound_marketplace_echo_duplicates(limit=3000)
    except Exception as exc:
        app.state.last_outbound_echo_repair = {"ok": False, "error": str(exc)}
    app.state.sync_lock = asyncio.Lock()
    app.state.last_sync = {}
    app.state.last_background_sync = {}
    app.state.last_reviews_sync = {}
    app.state.last_questions_sync = {}
    app.state.frontend_operator_sync_lock = asyncio.Lock()
    app.state.background_tick_lock = asyncio.Lock()
    app.state.frontend_operator_sync_last_poll_at = {}
    app.state.last_frontend_operator_sync = {}
    app.state.marketplace_sync_locks = {
        "ozon": asyncio.Lock(),
        "yandex": asyncio.Lock(),
        "wildberries": asyncio.Lock(),
    }
    _ensure_wb_events_auto_plan_from_env()
    app.state.background_sync_task = asyncio.create_task(_background_sync_loop())
    app.state.wb_events_import_planner_task = asyncio.create_task(_wb_events_import_planner_loop())
    app.state.push_outbox_task = asyncio.create_task(_push_outbox_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task_name in ("background_sync_task", "wb_events_import_planner_task", "push_outbox_task"):
        task = getattr(app.state, task_name, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/yandex/status")
def auth_yandex_status() -> JSONResponse:
    return JSONResponse(
        {"enabled": yandex_oauth.get_config() is not None},
        headers={"Cache-Control": "no-store"},
    )


_YANDEX_OAUTH_FAILURE_STAGES = frozenset(
    {
        "token_exchange",
        "profile_request",
        "profile_validation",
        "user_mapping",
        "database_link",
        "session_creation",
    }
)
_YANDEX_OAUTH_LOGGER = logging.getLogger("arti_crm.yandex_oauth")


def _log_yandex_oauth_failure(stage: str) -> None:
    if stage in _YANDEX_OAUTH_FAILURE_STAGES:
        _YANDEX_OAUTH_LOGGER.warning("Yandex OAuth callback failed stage=%s", stage)


_YANDEX_OAUTH_ERROR_CODES = frozenset(
    {
        "account_inactive",
        "account_not_allowed",
        "cancelled",
        "failed",
        "flow_expired",
        "oauth_rate_limited",
        "provider_unavailable",
    }
)


def _yandex_oauth_error_response(request: Request, code: str) -> RedirectResponse:
    safe_code = code if code in _YANDEX_OAUTH_ERROR_CODES else "failed"
    response = RedirectResponse(url=f"/?yandex_oauth={safe_code}", status_code=303)
    _delete_yandex_oauth_state_cookie(response, request)
    return response


def _yandex_oauth_rate_limit_response(
    request: Request,
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> RedirectResponse | None:
    try:
        _rate_limit(request, key, limit=limit, window_seconds=window_seconds)
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        return _yandex_oauth_error_response(request, "oauth_rate_limited")
    return None


def _yandex_oauth_profile_emails(profile: dict[str, Any]) -> list[str]:
    emails: list[str] = []
    default_email = profile.get("default_email")
    if isinstance(default_email, str):
        emails.append(default_email)
    profile_emails = profile.get("emails")
    if isinstance(profile_emails, list):
        emails.extend(value for value in profile_emails if isinstance(value, str))
    normalized: list[str] = []
    for email in emails:
        value = email.strip().casefold()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


@app.get("/api/auth/yandex/start")
def auth_yandex_start(request: Request) -> RedirectResponse:
    config = yandex_oauth.get_config()
    if config is None:
        return _yandex_oauth_error_response(request, "provider_unavailable")
    rate_limit_response = _yandex_oauth_rate_limit_response(
        request,
        "auth-yandex-start",
        limit=30,
        window_seconds=600,
    )
    if rate_limit_response:
        return rate_limit_response
    state, code_challenge, cookie_value = yandex_oauth.create_flow(config)
    response = RedirectResponse(
        url=yandex_oauth.build_authorization_url(
            config,
            state=state,
            code_challenge=code_challenge,
        ),
        status_code=302,
    )
    _set_yandex_oauth_state_cookie(response, cookie_value, request)
    return response


@app.get("/api/auth/yandex/callback")
async def auth_yandex_callback(request: Request) -> RedirectResponse:
    config = yandex_oauth.get_config()
    if config is None:
        return _yandex_oauth_error_response(request, "provider_unavailable")
    rate_limit_response = _yandex_oauth_rate_limit_response(
        request,
        "auth-yandex-callback",
        limit=60,
        window_seconds=600,
    )
    if rate_limit_response:
        return rate_limit_response

    state = str(request.query_params.get("state") or "").strip()
    cookie_value = request.cookies.get(YANDEX_OAUTH_STATE_COOKIE_NAME) or ""
    if not state or not cookie_value:
        return _yandex_oauth_error_response(request, "flow_expired")
    try:
        verifier = yandex_oauth.verify_flow_cookie(
            config,
            cookie_value,
            expected_state=state,
        )
    except yandex_oauth.YandexOAuthError:
        return _yandex_oauth_error_response(request, "flow_expired")

    provider_error = str(request.query_params.get("error") or "").strip()
    if provider_error:
        error_code = "cancelled" if provider_error == "access_denied" else "provider_unavailable"
        if error_code != "cancelled":
            _log_yandex_oauth_failure("token_exchange")
        return _yandex_oauth_error_response(request, error_code)
    code = str(request.query_params.get("code") or "").strip()
    if not code:
        _log_yandex_oauth_failure("token_exchange")
        return _yandex_oauth_error_response(request, "failed")

    try:
        profile = await yandex_oauth.fetch_profile_for_code(
            config,
            code=code,
            code_verifier=verifier,
        )
    except yandex_oauth.YandexOAuthCallbackError as exc:
        _log_yandex_oauth_failure(exc.stage)
        return _yandex_oauth_error_response(request, "provider_unavailable")
    except yandex_oauth.YandexOAuthError:
        _log_yandex_oauth_failure("token_exchange")
        return _yandex_oauth_error_response(request, "provider_unavailable")
    except Exception:
        _log_yandex_oauth_failure("token_exchange")
        return _yandex_oauth_error_response(request, "failed")
    yandex_user_id = yandex_oauth.get_yandex_user_id(profile)
    if not yandex_user_id:
        _log_yandex_oauth_failure("profile_validation")
        return _yandex_oauth_error_response(request, "failed")
    provider_login = str(profile.get("login") or "").strip().casefold()
    provider_emails = _yandex_oauth_profile_emails(profile)
    try:
        managed_link = repo.find_yandex_oauth_managed_link(
            yandex_user_id,
            login=provider_login,
            emails=provider_emails,
        )
    except Exception:
        _log_yandex_oauth_failure("database_link")
        return _yandex_oauth_error_response(request, "failed")

    linked_crm_user_id: int | None = None
    if managed_link is None:
        try:
            linked_crm_user_id = repo.get_yandex_oauth_linked_user_id(yandex_user_id)
        except Exception:
            _log_yandex_oauth_failure("database_link")
            return _yandex_oauth_error_response(request, "failed")

    bootstrap_username: str | None = None
    if managed_link is None and linked_crm_user_id is None:
        try:
            bootstrap_username = yandex_oauth.resolve_crm_username(config, profile)
        except Exception:
            _log_yandex_oauth_failure("user_mapping")
            return _yandex_oauth_error_response(request, "failed")
    session_ttl_seconds = _security_env_int(
        "CRM_SESSION_TTL_SECONDS",
        14 * 24 * 60 * 60,
        minimum=1800,
        maximum=60 * 24 * 60 * 60,
    )
    try:
        authentication = repo.create_session_for_yandex_identity(
            yandex_user_id,
            bootstrap_username=bootstrap_username,
            normalized_login=provider_login,
            normalized_emails=provider_emails,
            user_agent=request.headers.get("user-agent"),
            ip=_client_ip(request),
            seconds=session_ttl_seconds,
        )
    except repo.InactiveUserAuthenticationError:
        _log_yandex_oauth_failure("user_mapping")
        return _yandex_oauth_error_response(request, "account_inactive")
    except repo.YandexOAuthDatabaseLinkError:
        _log_yandex_oauth_failure("database_link")
        return _yandex_oauth_error_response(request, "failed")
    except repo.YandexOAuthSessionCreationError:
        _log_yandex_oauth_failure("session_creation")
        return _yandex_oauth_error_response(request, "failed")
    except Exception:
        _log_yandex_oauth_failure("session_creation")
        return _yandex_oauth_error_response(request, "failed")
    if not authentication:
        failure_stage = "database_link" if linked_crm_user_id is not None else "user_mapping"
        _log_yandex_oauth_failure(failure_stage)
        return _yandex_oauth_error_response(request, "account_not_allowed")

    _user, token = authentication
    response = RedirectResponse(url="/", status_code=303)
    _set_auth_cookie(response, token, request, max_age=session_ttl_seconds)
    _delete_yandex_oauth_state_cookie(response, request)
    _delete_legacy_csrf_cookie(response, request)
    return response




def _auth_login_impl(payload: LoginCreate, request: Request, response: Response) -> dict[str, Any]:
    # v80: persistent brute-force protection. The previous guard was in-memory and
    # could be bypassed by restart/multiple workers or look like infinite guessing.
    username = (payload.username or "").strip()
    client_ip = _client_ip(request)
    user_agent = request.headers.get("user-agent")
    max_attempts = _security_env_int("CRM_LOGIN_MAX_ATTEMPTS", 5, minimum=1, maximum=50)
    window_seconds = _security_env_int("CRM_LOGIN_WINDOW_SECONDS", 600, minimum=60, maximum=86400)
    lockout_seconds = _security_env_int("CRM_LOGIN_LOCKOUT_SECONDS", 900, minimum=60, maximum=86400)

    lockout = repo.get_login_lockout(username, client_ip)
    if lockout.get("locked"):
        retry_after = int(lockout.get("retry_after_seconds") or lockout_seconds)
        raise HTTPException(
            status_code=429,
            detail={"code": "login_rate_limited"},
            headers={"Retry-After": str(retry_after)},
        )

    # A short in-memory IP throttle remains as an additional anti-spam layer.
    _rate_limit(
        request,
        "auth-login",
        limit=_security_env_int("CRM_LOGIN_RATE_LIMIT", 20, minimum=1, maximum=200),
        window_seconds=_security_env_int("CRM_LOGIN_RATE_WINDOW_SECONDS", 600, minimum=60, maximum=86400),
    )

    session_ttl_seconds = _security_env_int("CRM_SESSION_TTL_SECONDS", 14 * 24 * 60 * 60, minimum=1800, maximum=60 * 24 * 60 * 60)
    authentication = repo.authenticate_user_and_create_session(
        username,
        payload.password,
        user_agent=user_agent,
        ip=client_ip,
        seconds=session_ttl_seconds,
    )
    if not authentication:
        state = repo.record_login_failure(
            username,
            client_ip,
            user_agent,
            max_attempts=max_attempts,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )
        if state.get("locked"):
            retry_after = int(state.get("retry_after_seconds") or lockout_seconds)
            raise HTTPException(
                status_code=429,
                detail={"code": "login_rate_limited"},
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})

    user, token = authentication
    repo.record_login_success(username, client_ip, user_agent)
    _set_auth_cookie(response, token, request, max_age=session_ttl_seconds)
    _delete_legacy_csrf_cookie(response, request)
    return {"ok": True, "user": user}


@app.post("/api/auth/login")
def auth_login(payload: LoginCreate, request: Request, response: Response) -> dict[str, Any]:
    try:
        return _auth_login_impl(payload, request, response)
    except repo.InactiveUserAuthenticationError:
        raise HTTPException(status_code=403, detail={"code": "password_account_inactive"})
    except HTTPException as exc:
        if exc.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail={"code": "login_rate_limited"},
                headers=exc.headers,
            )
        raise
    except Exception:
        raise HTTPException(status_code=500, detail={"code": "login_failed"})


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    repo.revoke_session(request.cookies.get(AUTH_COOKIE_NAME))
    _delete_cookie_secure(response, AUTH_COOKIE_NAME, request, httponly=True)
    _delete_cookie_secure(response, CSRF_COOKIE_NAME, request, httponly=True)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    if AUTH_DISABLED:
        return {"authenticated": True, "user": {"id": 0, "username": "local", "display_name": "Local", "role": "admin"}, "auth_disabled": True}
    user = repo.get_user_by_session(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return {"authenticated": True, "user": user}



@app.get("/api/security/csrf")
def security_csrf(request: Request, response: Response) -> dict[str, Any]:
    # Safe same-origin endpoint: returns a session-bound CSRF token for app.js to keep in memory.
    # It does not create a browser-readable CSRF cookie, so cookie scanners should only see
    # the HttpOnly auth session cookie after login.
    session_token = request.cookies.get(AUTH_COOKIE_NAME) or ""
    token = _csrf_token_for_session(session_token)
    if not token:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    _delete_legacy_csrf_cookie(response, request)
    return {"ok": True, "csrf_token": token}


@app.patch("/api/auth/profile")
def auth_update_profile(payload: ProfileUpdate, request: Request, response: Response) -> dict[str, Any]:
    user = _current_user(request)
    user_id = int(user["id"])
    updated = None
    if payload.new_password:
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
        session_ttl_seconds = _security_env_int("CRM_SESSION_TTL_SECONDS", 14 * 24 * 60 * 60, minimum=1800, maximum=60 * 24 * 60 * 60)
        try:
            token = repo.change_user_password_and_rotate_session(
                user_id,
                current_password=payload.current_password,
                new_password=payload.new_password,
                username=payload.username,
                display_name=payload.display_name,
                user_agent=request.headers.get("user-agent"),
                ip=_client_ip(request),
                seconds=session_ttl_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not token:
            raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
        _set_auth_cookie(response, token, request, max_age=session_ttl_seconds)
    else:
        try:
            updated = repo.update_user_profile(user_id, username=payload.username, display_name=payload.display_name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": repo.get_user_by_id(user_id) or updated}


@app.get("/api/users")
def api_list_users(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    return repo.list_users()


@app.get("/api/admin/yandex-oauth-links")
def api_list_yandex_oauth_managed_links(request: Request) -> dict[str, Any]:
    _require_admin(request)
    users = [
        {
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name"),
            "is_active": bool(user.get("is_active")),
        }
        for user in repo.list_users()
    ]
    return {"links": repo.list_yandex_oauth_managed_links(), "users": users}


@app.post("/api/admin/yandex-oauth-links")
def api_create_yandex_oauth_managed_link(
    payload: YandexOAuthManagedLinkCreate,
    request: Request,
) -> dict[str, Any]:
    _require_admin(request)
    try:
        return repo.create_yandex_oauth_managed_link(
            identifier_type=payload.identifier_type,
            identifier=payload.identifier,
            crm_user_id=payload.crm_user_id,
        )
    except repo.YandexOAuthManagedLinkUserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Сотрудник не найден") from exc
    except repo.YandexOAuthManagedLinkConflictError as exc:
        raise HTTPException(status_code=409, detail="Такой логин или email уже добавлен") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный логин или email") from exc


@app.patch("/api/admin/yandex-oauth-links/{link_id}")
def api_update_yandex_oauth_managed_link(
    link_id: int,
    payload: YandexOAuthManagedLinkUpdate,
    request: Request,
) -> dict[str, Any]:
    _require_admin(request)
    updated = repo.update_yandex_oauth_managed_link(link_id, is_active=payload.is_active)
    if not updated:
        raise HTTPException(status_code=404, detail="Связь не найдена")
    return updated


@app.delete("/api/admin/yandex-oauth-links/{link_id}")
def api_delete_yandex_oauth_managed_link(link_id: int, request: Request) -> dict[str, bool]:
    _require_admin(request)
    if not repo.delete_yandex_oauth_managed_link(link_id):
        raise HTTPException(status_code=404, detail="Связь не найдена")
    return {"ok": True}




@app.get("/api/users/assignees")
def api_list_assignees(request: Request) -> list[dict[str, Any]]:
    _current_user(request)
    return repo.list_assignees()

@app.post("/api/users")
def api_create_user(payload: UserCreate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        return repo.create_user(payload.username, payload.password, payload.display_name, payload.role)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось создать сотрудника: {exc}") from exc


@app.patch("/api/users/{user_id}")
def api_update_user(user_id: int, payload: UserUpdate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    updated = repo.update_user(user_id, display_name=payload.display_name, role=payload.role, is_active=payload.is_active)
    if not updated:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return updated


@app.post("/api/users/{user_id}/password")
def api_update_user_password(user_id: int, payload: UserPasswordUpdate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    if not repo.update_user_password(user_id, payload.password):
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return {"ok": True}


@app.get("/api/debug/local")
def debug_local() -> dict[str, Any]:
    return {
        "build_version": CRM_BUILD_VERSION,
        "cwd": str(Path.cwd()),
        "app_file": str(Path(__file__).resolve()),
        "env_exists": (Path.cwd() / ".env").exists(),
        "db_exists": (Path.cwd() / "crm.sqlite3").exists(),
    }


@app.get("/api/debug/version")
def debug_version() -> dict[str, Any]:
    return {
        "ok": True,
        "build_version": CRM_BUILD_VERSION,
        "app_version": app.version,
        "questions_section": True,
        "wb_safe_debug_default": True,
    }


@app.get("/api/debug/ozon")
async def debug_ozon() -> dict[str, Any]:
    connector = connectors["ozon"]
    if not hasattr(connector, "diagnostics"):
        raise HTTPException(status_code=404, detail="Diagnostics not supported")
    try:
        return await connector.diagnostics()  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc












def _local_ozon_chat_stats() -> dict[str, Any]:
    init_db()
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE marketplace='ozon'").fetchone()["c"]
        minmax = conn.execute(
            """
            SELECT
                MIN(last_message_at) AS min_last_message_at,
                MAX(last_message_at) AS max_last_message_at,
                MIN(created_at) AS min_created_at,
                MAX(updated_at) AS max_updated_at
            FROM chats
            WHERE marketplace='ozon'
            """
        ).fetchone()
        messages_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE c.marketplace='ozon'
            """
        ).fetchone()["c"]
    return {
        "local_ozon_total_chats": total,
        "local_ozon_messages_count": messages_count,
        "local_range": dict(minmax) if minmax else {},
    }


@app.get("/api/debug/ozon/chats")
async def debug_ozon_chats() -> dict[str, Any]:
    """Show local Ozon chat coverage and last connector sync debug."""
    connector = connectors.get("ozon")
    if not connector:
        raise HTTPException(status_code=404, detail="Ozon connector not found")
    init_db()
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE marketplace='ozon'").fetchone()["c"]
        minmax = conn.execute(
            """
            SELECT
                MIN(last_message_at) AS min_last_message_at,
                MAX(last_message_at) AS max_last_message_at,
                MIN(created_at) AS min_created_at,
                MAX(updated_at) AS max_updated_at
            FROM chats
            WHERE marketplace='ozon'
            """
        ).fetchone()
        latest = [dict(row) for row in conn.execute(
            """
            SELECT
                c.id,
                c.external_chat_id,
                c.customer_name,
                c.status,
                c.last_message_at,
                c.last_message_preview,
                (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS messages_count
            FROM chats c
            WHERE c.marketplace='ozon'
            ORDER BY COALESCE(c.last_message_at, c.updated_at, c.created_at) DESC
            LIMIT 30
            """
        ).fetchall()]
    return {
        "ok": True,
        "local_ozon_total_chats": total,
        "local_range": dict(minmax) if minmax else {},
        "latest_local_chats": latest,
        "connector_settings": {
            "sync_max_chats": getattr(connector, "sync_max_chats", None),
            "sync_pages_per_variant": getattr(connector, "sync_pages_per_variant", None),
            "sync_variant_mode": getattr(connector, "sync_variant_mode", None),
            "sync_include_closed": getattr(connector, "sync_include_closed", None),
            "history_pages": getattr(connector, "history_pages", None),
        },
        "last_sync_debug": getattr(connector, "last_sync_debug", {}),
        "hint": "Если max_last_message_at старее ожидаемой даты, нужен backfill: /api/debug/ozon/backfill-chats?max_chats=2000&pages_per_variant=20&history_pages=5&include_closed=1",
    }


@app.post("/api/debug/ozon/backfill-chats")
async def debug_ozon_backfill_chats(
    max_chats: int = 5000,
    pages_per_variant: int = 50,
    history_pages: int = 5,
    include_closed: bool = True,
    include_service_chats: bool = True,
) -> dict[str, Any]:
    """Deep Ozon chats/history import for missed local history.

    Normal sync is intentionally light. This endpoint temporarily increases:
    - number of chat list pages,
    - total chats to scan,
    - chat history pages per chat.
    """
    connector = connectors.get("ozon")
    if not connector:
        raise HTTPException(status_code=404, detail="Ozon connector not found")
    if not getattr(connector, "client_id", "") or not getattr(connector, "api_key", ""):
        return {"ok": False, "marketplace": "ozon", "configured": False, "error": "OZON_CLIENT_ID/OZON_API_KEY are not configured"}

    safe_max_chats = max(1, min(int(max_chats or 5000), 20000))
    safe_pages_per_variant = max(1, min(int(pages_per_variant or 50), 200))
    safe_history_pages = max(1, min(int(history_pages or 5), 50))

    overrides = {
        "sync_max_chats": safe_max_chats,
        "sync_pages_per_variant": safe_pages_per_variant,
        "sync_variant_mode": "full",
        "sync_include_closed": bool(include_closed),
        "history_pages": safe_history_pages,
    }

    old_settings = {
        "sync_max_chats": getattr(connector, "sync_max_chats", None),
        "sync_pages_per_variant": getattr(connector, "sync_pages_per_variant", None),
        "sync_variant_mode": getattr(connector, "sync_variant_mode", None),
        "sync_include_closed": getattr(connector, "sync_include_closed", None),
        "history_pages": getattr(connector, "history_pages", None),
    }

    old_exclude_support = os.environ.get("OZON_EXCLUDE_SUPPORT_CHATS")
    old_delete_support = os.environ.get("OZON_DELETE_SUPPORT_CHATS")
    old_exclude_system_history = os.environ.get("OZON_EXCLUDE_SYSTEM_HISTORY_CHATS")
    old_delete_system_history = os.environ.get("OZON_DELETE_SYSTEM_HISTORY_CHATS")
    try:
        if include_service_chats:
            # Keep every Ozon chat that API returns. We can hide/mark service later,
            # but losing customer history during backfill is worse.
            os.environ["OZON_EXCLUDE_SUPPORT_CHATS"] = "0"
            os.environ["OZON_DELETE_SUPPORT_CHATS"] = "0"
            os.environ["OZON_EXCLUDE_SYSTEM_HISTORY_CHATS"] = "0"
            os.environ["OZON_DELETE_SYSTEM_HISTORY_CHATS"] = "0"
        with _temporary_connector_overrides(connector, overrides):
            result = await _sync_marketplace_unlocked("ozon", background=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if old_exclude_support is None:
            os.environ.pop("OZON_EXCLUDE_SUPPORT_CHATS", None)
        else:
            os.environ["OZON_EXCLUDE_SUPPORT_CHATS"] = old_exclude_support
        if old_delete_support is None:
            os.environ.pop("OZON_DELETE_SUPPORT_CHATS", None)
        else:
            os.environ["OZON_DELETE_SUPPORT_CHATS"] = old_delete_support
        if old_exclude_system_history is None:
            os.environ.pop("OZON_EXCLUDE_SYSTEM_HISTORY_CHATS", None)
        else:
            os.environ["OZON_EXCLUDE_SYSTEM_HISTORY_CHATS"] = old_exclude_system_history
        if old_delete_system_history is None:
            os.environ.pop("OZON_DELETE_SYSTEM_HISTORY_CHATS", None)
        else:
            os.environ["OZON_DELETE_SYSTEM_HISTORY_CHATS"] = old_delete_system_history

    result["backfill"] = True
    result["include_service_chats"] = include_service_chats
    result["backfill_overrides"] = overrides
    result["local_after_backfill"] = _local_ozon_chat_stats()
    result["previous_connector_settings"] = old_settings
    result["connector_debug"] = getattr(connector, "last_sync_debug", {})
    result["hint"] = (
        "Это глубокий импорт. В v81 include_service_chats=true по умолчанию: CRM сохраняет все Ozon-чаты, которые API отдаёт, "
        "чтобы не потерять клиентскую историю из-за ошибочной фильтрации. Если после этого min_last_message_at не уходит глубже, "
        "значит нужно увеличивать pages_per_variant/max_chats или Ozon API не отдаёт более старые страницы этим методом."
    )
    return result


@app.post("/api/debug/wb/import-events")
async def debug_wb_import_events(
    days: int = 30,
    pages: int = 1,
    max_events: int = 5000,
    safe: bool = True,
    reset: bool = False,
) -> dict[str, Any]:
    """Import one WB events page and save the cursor for the next run.

    v68: if events belong to chats that are not in the current /seller/chats
    local cache, create those chats instead of silently skipping them.
    """
    connector = connectors.get("wildberries")
    if not connector:
        raise HTTPException(status_code=404, detail="WB connector not found")

    cursor_file = Path(os.getenv("WB_EVENTS_CURSOR_STATE_FILE", ".wb_events_cursor.json"))
    if not cursor_file.is_absolute():
        cursor_file = Path.cwd() / cursor_file

    def load_cursor_state() -> dict[str, Any]:
        try:
            if cursor_file.exists():
                data = json.loads(cursor_file.read_text(encoding="utf-8") or "{}")
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def save_cursor_state(data: dict[str, Any]) -> None:
        try:
            cursor_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    if reset:
        try:
            cursor_file.unlink(missing_ok=True)
        except Exception:
            pass
        app.state.last_wb_events_import = {"ok": True, "reset": True, "cursor_file": str(cursor_file)}
        return {
            "ok": True,
            "reset": True,
            "cursor_file": str(cursor_file),
            "hint": "Cursor сброшен. Следующий /api/debug/wb/import-events начнёт с самой свежей страницы WB events.",
        }

    if hasattr(connector, "_cooldown_remaining") and connector._cooldown_remaining() > 0:  # type: ignore[attr-defined]
        return {
            "ok": False,
            "cooldown_remaining_seconds": int(connector._cooldown_remaining()),  # type: ignore[attr-defined]
            "cursor_state": load_cursor_state(),
            "last_wb_events_import": getattr(app.state, "last_wb_events_import", {}),
            "error": "WB cooldown active. Подождите окончания cooldown и запустите импорт событий снова.",
            "hint": "Не открывайте /api/debug/wb?live=1 и import-events до окончания cooldown — каждый лишний запрос снова продлевает паузу.",
        }

    cursor_state = load_cursor_state()
    saved_next = cursor_state.get("next")
    old_days = getattr(connector, "events_lookback_days", 30)
    old_pages = getattr(connector, "event_pages", 10)
    old_max = getattr(connector, "max_events", 2000)
    old_start = getattr(connector, "events_start_timestamp_ms", 0)

    try:
        connector.events_lookback_days = max(1, min(int(days or 30), 365))
        connector.event_pages = 1 if safe else max(1, min(int(pages or 1), 100))
        connector.max_events = max(1, min(int(max_events or 5000), 10000))
        try:
            connector.events_start_timestamp_ms = int(saved_next or 0)
        except Exception:
            connector.events_start_timestamp_ms = 0
        events = await connector._events()  # type: ignore[attr-defined]
        grouped = connector._group_events_by_chat(events)  # type: ignore[attr-defined]
    except Exception as exc:
        message = str(exc)
        cooldown = int(connector._cooldown_remaining()) if hasattr(connector, "_cooldown_remaining") else 0  # type: ignore[attr-defined]
        result = {
            "ok": False,
            "cooldown_remaining_seconds": cooldown,
            "cursor_state": cursor_state,
            "error": message,
            "connector_debug": getattr(connector, "last_debug", {}),
            "hint": "WB вернул 429. Это не ошибка парсера: история не может загрузиться до окончания X-Ratelimit-Retry.",
        }
        app.state.last_wb_events_import = result
        return result
    finally:
        connector.events_lookback_days = old_days
        connector.event_pages = old_pages
        connector.max_events = old_max
        connector.events_start_timestamp_ms = old_start

    def event_customer_name(event: dict[str, Any]) -> str | None:
        try:
            value = connector._first_value(event, "clientName", "buyerName", "customerName")  # type: ignore[attr-defined]
            if not value and isinstance(event.get("message"), dict):
                value = connector._first_value(event["message"], "clientName", "buyerName", "customerName")  # type: ignore[attr-defined]
            return str(value).strip() if value not in (None, "") else None
        except Exception:
            return None

    imported = 0
    chats_touched = 0
    chats_created = 0
    parser_skipped = 0
    empty_group_skipped = 0
    sample: list[dict[str, Any]] = []
    created_sample: list[dict[str, Any]] = []
    parser_skipped_sample: list[dict[str, Any]] = []

    for external_chat_id, chat_events in grouped.items():
        if not chat_events:
            empty_group_skipped += 1
            continue
        local = repo.get_chat_by_external("wildberries", external_chat_id)
        if not local:
            first_event = chat_events[0] if isinstance(chat_events[0], dict) else {}
            chat_id = repo.upsert_chat(
                ChatCreate(
                    marketplace="wildberries",  # type: ignore[arg-type]
                    external_chat_id=str(external_chat_id),
                    customer_name=event_customer_name(first_event),
                    customer_public_id=None,
                    order_id=None,
                    status="in_progress",  # type: ignore[arg-type]
                    metadata={
                        "_crm_created_from_wb_events": True,
                        "_events_import_count": len(chat_events),
                        "_first_event": first_event,
                    },
                )
            )
            local = repo.get_chat(chat_id)
            chats_created += 1
            if len(created_sample) < 20:
                created_sample.append({
                    "chat_id": chat_id,
                    "external_chat_id": external_chat_id,
                    "customer_name": (local or {}).get("customer_name") if local else None,
                    "events_count": len(chat_events),
                })
        if not local:
            continue
        chats_touched += 1
        chat_imported = 0
        for event in chat_events:
            try:
                message = connector._event_to_message(external_chat_id, event)  # type: ignore[attr-defined]
            except Exception:
                message = None
            if not message:
                parser_skipped += 1
                if len(parser_skipped_sample) < 10:
                    parser_skipped_sample.append({
                        "external_chat_id": external_chat_id,
                        "event_keys": list(event.keys())[:30] if isinstance(event, dict) else [],
                        "event_type": event.get("eventType") or event.get("event_type") or event.get("type") if isinstance(event, dict) else None,
                    })
                continue
            repo.add_message(
                chat_id=int(local["id"]),
                direction=getattr(message, "direction", "inbound"),
                text=getattr(message, "text", "") or "[сообщение без текста / вложение]",
                author=getattr(message, "author", None),
                external_message_id=getattr(message, "external_message_id", None),
                raw=getattr(message, "raw", {}) or {},
                created_at=getattr(message, "created_at", None),
            )
            imported += 1
            chat_imported += 1
        if chat_imported and len(sample) < 30:
            sample.append({
                "chat_id": local.get("id"),
                "external_chat_id": external_chat_id,
                "customer_name": local.get("customer_name"),
                "imported": chat_imported,
            })

    repo.repair_chat_last_message_cache()

    pages_debug = (getattr(connector, "last_debug", {}) or {}).get("events_pages_debug") or []
    response_next = None
    total_events = None
    if pages_debug:
        last_page = pages_debug[-1] if isinstance(pages_debug[-1], dict) else {}
        response_next = last_page.get("response_next")
        total_events = last_page.get("totalEvents")

    new_cursor_state = {
        "next": response_next,
        "done": not bool(response_next) or total_events == 0,
        "last_run_at": time.time(),
        "last_events_count": len(events),
        "last_imported": imported,
        "last_chats_touched": chats_touched,
        "last_chats_created": chats_created,
        "last_parser_skipped": parser_skipped,
        "runs_count": int(cursor_state.get("runs_count") or 0) + 1,
        "total_imported": int(cursor_state.get("total_imported") or 0) + imported,
        "total_chats_created": int(cursor_state.get("total_chats_created") or 0) + chats_created,
        "previous_next": saved_next,
    }
    save_cursor_state(new_cursor_state)

    result = {
        "ok": True,
        "safe_mode": safe,
        "cursor_file": str(cursor_file),
        "used_cursor_next": saved_next,
        "saved_next_for_next_run": response_next,
        "cursor_state": new_cursor_state,
        "pages_requested": 1 if safe else pages,
        "events_count": len(events),
        "events_grouped_chats_count": len(grouped),
        "local_chats_touched": chats_touched,
        "chats_created_from_events": chats_created,
        "messages_imported_or_updated": imported,
        "parser_skipped_events": parser_skipped,
        "empty_group_skipped": empty_group_skipped,
        "sample": sample,
        "created_sample": created_sample,
        "parser_skipped_sample": parser_skipped_sample,
        "connector_debug": getattr(connector, "last_debug", {}),
        "hint": "Если events_count > 0, но messages_imported_or_updated было 0 в старой версии, причина часто в том, что events пришли по chatID, которых не было в локальном списке. v68 создаёт такие чаты из events.",
    }
    app.state.last_wb_events_import = result
    return result



def _wb_events_plan_file() -> Path:
    plan_file = Path(os.getenv("WB_EVENTS_AUTO_IMPORT_PLAN_FILE", ".wb_events_auto_import.json"))
    if not plan_file.is_absolute():
        plan_file = Path.cwd() / plan_file
    return plan_file


def _load_wb_events_plan() -> dict[str, Any]:
    try:
        path = _wb_events_plan_file()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _save_wb_events_plan(plan: dict[str, Any]) -> None:
    try:
        _wb_events_plan_file().write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _decorate_wb_events_plan(plan: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    decorated = dict(plan or {})
    run_after = float(decorated.get("run_after") or 0)
    decorated["plan_file"] = str(_wb_events_plan_file())
    decorated["next_run_in_seconds"] = max(0, int(run_after - now)) if run_after else 0
    return decorated


def _wb_events_auto_enabled_by_env() -> bool:
    return os.getenv("WB_EVENTS_AUTO_IMPORT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on", "да"}


def _ensure_wb_events_auto_plan_from_env() -> None:
    """Enable safe WB events auto-import at startup unless disabled in .env."""
    if not _wb_events_auto_enabled_by_env():
        return
    plan = _load_wb_events_plan()
    if plan.get("next_action") == "stopped":
        return
    if plan.get("enabled"):
        return
    now = time.time()
    connector = connectors.get("wildberries")
    cooldown = 0
    try:
        cooldown = int(connector._cooldown_remaining()) if connector and hasattr(connector, "_cooldown_remaining") else 0  # type: ignore[attr-defined]
    except Exception:
        cooldown = 0
    plan.update({
        "enabled": True,
        "created_at": plan.get("created_at") or now,
        "updated_at": now,
        "run_after": now + cooldown + 5 if cooldown > 0 else now + 10,
        "cooldown_remaining_seconds": cooldown,
        "next_action": "waiting_cooldown" if cooldown > 0 else "ready_to_run",
        "auto_started_from_env": True,
    })
    _save_wb_events_plan(plan)


async def _wb_events_import_planner_loop() -> None:
    """Automatically import one safe WB events page after cooldown."""
    while True:
        try:
            await asyncio.sleep(30)
            _ensure_wb_events_auto_plan_from_env()
            plan = _load_wb_events_plan()
            if not plan.get("enabled"):
                continue

            connector = connectors.get("wildberries")
            if not connector:
                plan["last_error"] = "WB connector not found"
                plan["updated_at"] = time.time()
                _save_wb_events_plan(plan)
                app.state.last_wb_events_auto_import = _decorate_wb_events_plan(plan)
                continue

            now = time.time()
            cooldown = int(connector._cooldown_remaining()) if hasattr(connector, "_cooldown_remaining") else 0  # type: ignore[attr-defined]
            run_after = float(plan.get("run_after") or 0)

            if cooldown > 0:
                plan["cooldown_remaining_seconds"] = cooldown
                plan["run_after"] = max(run_after, now + cooldown + 5)
                plan["next_action"] = "waiting_cooldown"
                plan["updated_at"] = now
                _save_wb_events_plan(plan)
                app.state.last_wb_events_auto_import = _decorate_wb_events_plan(plan)
                continue

            if run_after and now < run_after:
                app.state.last_wb_events_auto_import = _decorate_wb_events_plan(plan)
                continue

            result = await debug_wb_import_events()
            now = time.time()
            plan["last_result"] = result
            plan["last_run_at"] = now
            plan["updated_at"] = now

            if result.get("ok"):
                cursor_state = result.get("cursor_state") if isinstance(result.get("cursor_state"), dict) else {}
                done = bool(cursor_state.get("done"))
                has_next = bool(result.get("saved_next_for_next_run") or cursor_state.get("next"))
                interval = int(os.getenv("WB_EVENTS_AUTO_IMPORT_INTERVAL_SECONDS", "3700") or "3700")
                keep_alive = os.getenv("WB_EVENTS_AUTO_IMPORT_KEEP_ALIVE", "true").strip().lower() in {"1", "true", "yes", "on", "да"}
                if done or not has_next:
                    plan["enabled"] = keep_alive
                    plan["run_after"] = now + max(3600, interval)
                    plan["next_action"] = "scheduled_check_for_new_events" if keep_alive else "done"
                else:
                    plan["enabled"] = True
                    plan["run_after"] = now + max(3600, interval)
                    plan["next_action"] = "scheduled_next_page"
            else:
                cooldown = int(result.get("cooldown_remaining_seconds") or 0)
                plan["enabled"] = True
                plan["run_after"] = now + max(60, cooldown) + 5
                plan["next_action"] = "retry_after_cooldown"

            _save_wb_events_plan(plan)
            app.state.last_wb_events_auto_import = _decorate_wb_events_plan(plan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            plan = _load_wb_events_plan()
            plan["last_error"] = str(exc)
            plan["updated_at"] = time.time()
            _save_wb_events_plan(plan)
            app.state.last_wb_events_auto_import = _decorate_wb_events_plan(plan)


@app.post("/api/debug/wb/import-events-auto")
async def debug_wb_import_events_auto(action: str = "status") -> dict[str, Any]:
    """Manage automatic WB events import. Actions: status/start/stop/reset."""
    connector = connectors.get("wildberries")
    if not connector:
        raise HTTPException(status_code=404, detail="WB connector not found")

    action_norm = str(action or "status").strip().lower()
    plan = _load_wb_events_plan()
    now = time.time()
    cooldown = int(connector._cooldown_remaining()) if hasattr(connector, "_cooldown_remaining") else 0  # type: ignore[attr-defined]

    if action_norm in {"start", "enable", "on"}:
        plan.update({
            "enabled": True,
            "created_at": plan.get("created_at") or now,
            "updated_at": now,
            "cooldown_remaining_seconds": cooldown,
            "run_after": now + cooldown + 5 if cooldown > 0 else now,
            "next_action": "waiting_cooldown" if cooldown > 0 else "ready_to_run",
            "manual_start": True,
        })
        _save_wb_events_plan(plan)
    elif action_norm in {"stop", "disable", "off"}:
        plan.update({
            "enabled": False,
            "updated_at": now,
            "next_action": "stopped",
        })
        _save_wb_events_plan(plan)
    elif action_norm == "reset":
        plan = {
            "enabled": _wb_events_auto_enabled_by_env(),
            "updated_at": now,
            "next_action": "ready_to_run" if _wb_events_auto_enabled_by_env() and cooldown == 0 else "waiting_cooldown",
            "run_after": now + cooldown + 5 if cooldown > 0 else now,
            "cooldown_remaining_seconds": cooldown,
            "reset": True,
        }
        try:
            cursor_file = Path(os.getenv("WB_EVENTS_CURSOR_STATE_FILE", ".wb_events_cursor.json"))
            if not cursor_file.is_absolute():
                cursor_file = Path.cwd() / cursor_file
            cursor_file.unlink(missing_ok=True)
            plan["cursor_reset"] = True
        except Exception as exc:
            plan["cursor_reset_error"] = str(exc)
        _save_wb_events_plan(plan)
    elif action_norm not in {"status", ""}:
        raise HTTPException(status_code=400, detail="action must be status/start/stop/reset")

    decorated = _decorate_wb_events_plan(plan)
    decorated["ok"] = True
    decorated["auto_enabled_by_env"] = _wb_events_auto_enabled_by_env()
    decorated["cooldown_remaining_seconds"] = cooldown
    decorated["hint"] = (
        "Если enabled=true, CRM сама дождётся cooldown=0 и выполнит один безопасный импорт WB events. "
        "Открывать import-events вручную больше не нужно."
    )
    app.state.last_wb_events_auto_import = decorated
    return decorated


@app.get("/api/debug/wb")
async def debug_wb(request: Request) -> dict[str, Any]:
    """Inspect WB local state.

    By default this endpoint DOES NOT call the live WB API, because every
    /seller/chats request can consume the hourly bucket on some WB tokens.
    Add ?live=1 only when you intentionally want one live API probe.
    """
    connector = connectors.get("wildberries")
    if not connector:
        raise HTTPException(status_code=404, detail="WB connector not found")
    init_db()
    live = str(request.query_params.get("live", "0")).strip().lower() in {"1", "true", "yes", "on", "да"}
    with get_connection() as conn:
        local_total = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE marketplace='wildberries'").fetchone()["c"]
        local_latest_raw = [dict(row) for row in conn.execute(
            """
            SELECT
                c.id,
                c.external_chat_id,
                c.customer_name,
                c.last_message_at,
                c.last_message_preview,
                c.status,
                c.metadata_json,
                (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS messages_count
            FROM chats c
            WHERE c.marketplace='wildberries'
            ORDER BY COALESCE(c.last_message_at, c.updated_at, c.created_at) DESC
            LIMIT 20
            """
        ).fetchall()]
        local_latest = []
        for row in local_latest_raw:
            try:
                metadata = json.loads(row.pop("metadata_json") or "{}")
            except Exception:
                metadata = {}
            row["has_last_message_in_metadata"] = bool(_wb_last_message_payload_from_metadata(metadata))
            local_latest.append(row)
    base = {
        "configured": bool(getattr(connector, "token", "")),
        "token_present": bool(getattr(connector, "token", "")),
        "live_probe": live,
        "local_total_in_db": local_total,
        "local_latest_sample": local_latest,
        "connector_debug": getattr(connector, "last_debug", {}),
        "last_wb_local_repair": getattr(app.state, "last_wb_local_repair", {}),
        "last_wb_events_import": getattr(app.state, "last_wb_events_import", {}),
        "last_wb_events_auto_import": getattr(app.state, "last_wb_events_auto_import", _decorate_wb_events_plan(_load_wb_events_plan())),
        "cooldown_remaining_seconds": int(connector._cooldown_remaining()) if hasattr(connector, "_cooldown_remaining") else 0,
        "hint": "По умолчанию live_probe выключен, чтобы не тратить лимит WB. Для одного живого запроса откройте /api/debug/wb?live=1 после паузы без WB-запросов. Для ремонта уже сохранённых пустых WB-чатов откройте /api/debug/wb/repair-local.",
    }
    if not base["token_present"]:
        return {**base, "ok": False, "error": "WB token is not configured. Используйте WB_BUYERS_CHAT_TOKEN или WB_API_TOKEN."}
    if not live:
        return {**base, "ok": True}
    try:
        chats = await connector.list_chats()
    except Exception as exc:
        return {**base, "ok": False, "error": str(exc), "connector_debug": getattr(connector, "last_debug", {})}
    live_samples = []
    for c in chats[:5]:
        try:
            msgs = await connector.get_messages(c.external_chat_id)
            live_samples.append({
                "external_chat_id": c.external_chat_id,
                "customer_name": c.customer_name,
                "messages_count": len(msgs),
                "sample_messages": [
                    {
                        "direction": getattr(m, "direction", None),
                        "created_at": getattr(m, "created_at", None),
                        "text": str(getattr(m, "text", "") or "")[:180],
                        "external_message_id": getattr(m, "external_message_id", None),
                    }
                    for m in msgs[-3:]
                ],
            })
        except Exception as exc:
            live_samples.append({"external_chat_id": c.external_chat_id, "error": str(exc)})
    return {
        **base,
        "ok": True,
        "api_chats_count": len(chats),
        "api_sample_chat_ids": [c.external_chat_id for c in chats[:20]],
        "live_message_samples": live_samples,
        "connector_debug": getattr(connector, "last_debug", {}),
        "hint": "Если api_chats_count > 0, но live_message_samples.messages_count=0 — пришлите этот debug. Если live_probe даёт 429, ждём cooldown и не нажимаем обновление WB повторно.",
    }


@app.get("/api/debug/ozon/coverage")
async def debug_ozon_coverage(limit: int = 80) -> dict[str, Any]:
    """Compare the current Ozon API inbox with local CRM rows.

    This helps find why an Ozon dialog is missing: not returned by API variant,
    saved locally but archived/closed, or present in DB but sorted/previewed wrong.
    """
    connector = connectors["ozon"]
    if not getattr(connector, "client_id", "") or not getattr(connector, "api_key", ""):
        return {"configured": False, "error": "OZON_CLIENT_ID/OZON_API_KEY are not configured"}
    limit = max(1, min(int(limit or 80), 200))
    old_max = getattr(connector, "sync_max_chats", 500)
    old_pages = getattr(connector, "sync_pages_per_variant", 5)
    old_mode = getattr(connector, "sync_variant_mode", "full")
    try:
        connector.sync_max_chats = limit
        connector.sync_pages_per_variant = 3
        connector.sync_variant_mode = "full"
        api_chats = await connector.list_chats()
    finally:
        connector.sync_max_chats = old_max
        connector.sync_pages_per_variant = old_pages
        connector.sync_variant_mode = old_mode

    rows = []
    with get_connection() as conn:
        for c in api_chats:
            row = conn.execute(
                """
                SELECT id, external_chat_id, customer_name, status, last_message_at, last_message_preview,
                       updated_at, created_at
                FROM chats
                WHERE marketplace='ozon' AND external_chat_id=?
                """,
                (c.external_chat_id,),
            ).fetchone()
            local = repo.row_to_dict(row) if row else None
            rows.append({
                "external_chat_id": c.external_chat_id,
                "api_customer_name": c.customer_name,
                "api_public_id": c.customer_public_id,
                "api_status": c.status,
                "api_unread_count": ((c.metadata or {}).get("_sync_hint") or {}).get("unread_count"),
                "api_last_message_id": ((c.metadata or {}).get("_sync_hint") or {}).get("last_message_id"),
                "local_exists": bool(local),
                "local_status": local.get("status") if local else None,
                "local_visible_in_active": bool(local and local.get("status") != "closed"),
                "local_last_message_at": local.get("last_message_at") if local else None,
                "local_preview": local.get("last_message_preview") if local else None,
            })
    missing = [r for r in rows if not r["local_exists"]]
    archived = [r for r in rows if r["local_status"] == "closed"]
    return {
        "configured": True,
        "api_checked_count": len(rows),
        "api_missing_in_local_count": len(missing),
        "api_archived_in_local_count": len(archived),
        "api_missing_in_local_sample": missing[:20],
        "api_archived_in_local_sample": archived[:20],
        "rows": rows[:limit],
        "connector_debug": getattr(connector, "last_sync_debug", {}),
    }


@app.get("/api/debug/ozon/reviews")
async def debug_ozon_reviews() -> dict[str, Any]:
    connector = connectors["ozon"]
    if not hasattr(connector, "reviews_diagnostics"):
        raise HTTPException(status_code=404, detail="Ozon reviews diagnostics not supported")
    try:
        return await connector.reviews_diagnostics()  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/debug/ozon/questions")
async def debug_ozon_questions() -> dict[str, Any]:
    connector = connectors["ozon"]
    if not hasattr(connector, "questions_diagnostics"):
        raise HTTPException(status_code=404, detail="Ozon questions diagnostics not supported")
    try:
        return await connector.questions_diagnostics()  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/debug/ozon/chat/{external_chat_id}")
async def debug_ozon_chat(external_chat_id: str) -> dict[str, Any]:
    """Inspect one Ozon chat without exposing API keys.

    Use this when Ozon returns chat_type=UNSPECIFIED and we need to know whether
    the dialog is a real buyer chat or a service notification dialog.
    """
    connector = connectors["ozon"]
    try:
        messages = await connector.get_messages(external_chat_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    markers = _ozon_system_dialog_markers()
    sample = []
    for message in messages[:10]:
        raw = getattr(message, "raw", {}) or {}
        sample.append({
            "direction": direction,
            "author": getattr(message, "author", None),
            "created_at": getattr(message, "created_at", None),
            "text_preview": str(getattr(message, "text", "") or "")[:160],
            "raw_keys": list(raw.keys())[:30] if isinstance(raw, dict) else [],
            "has_system_marker": _value_has_any_marker(raw, markers) or _value_has_any_marker(getattr(message, "author", None), markers),
        })
    return {
        "external_chat_id": external_chat_id,
        "messages_count": len(messages),
        "looks_like_system_dialog": _messages_are_ozon_system_dialog(messages),
        "system_markers_used": markers,
        "sample_messages": sample,
    }


@app.get("/api/debug/messages/{message_id}")
def debug_message_object(message_id: int) -> dict[str, Any]:
    """Return the saved raw marketplace object for one CRM message.

    Use this developer/debug endpoint to inspect exactly what the marketplace sent
    for a saved message. API keys are not returned.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                m.*,
                c.marketplace,
                c.external_chat_id,
                c.customer_name,
                c.customer_public_id,
                c.metadata_json AS chat_metadata_json
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.id=?
            """,
            (message_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    data = dict(row)
    try:
        raw = json.loads(data.pop("raw_json") or "{}")
    except Exception:
        raw = {}
    try:
        chat_metadata = json.loads(data.pop("chat_metadata_json") or "{}")
    except Exception:
        chat_metadata = {}
    return {"message": data, "raw_object": raw, "chat_metadata": chat_metadata}


@app.get("/api/debug/chats/{chat_id}/messages/raw")
def debug_chat_messages_raw(chat_id: int, limit: int = 20, direction: str | None = None) -> dict[str, Any]:
    """Return saved raw marketplace objects for messages in one CRM chat."""
    safe_limit = max(1, min(int(limit or 20), 100))
    where = "m.chat_id=?"
    params: list[Any] = [chat_id]
    if direction in {"inbound", "outbound", "internal"}:
        where += " AND m.direction=?"
        params.append(direction)
    params.append(safe_limit)
    with get_connection() as conn:
        chat = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        rows = conn.execute(
            f"""
            SELECT m.*
            FROM messages m
            WHERE {where}
            ORDER BY datetime(m.created_at) DESC, m.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    chat_data = repo.row_to_dict(chat)
    messages = []
    for row in rows:
        item = repo.row_to_dict(row)
        messages.append({
            "message_id": item.get("id"),
            "external_message_id": item.get("external_message_id"),
            "direction": item.get("direction"),
            "author": item.get("author"),
            "created_at": item.get("created_at"),
            "text": item.get("text"),
            "raw_object": item.get("raw") or {},
        })
    return {
        "chat": {
            "id": chat_data.get("id"),
            "marketplace": chat_data.get("marketplace"),
            "external_chat_id": chat_data.get("external_chat_id"),
            "customer_name": chat_data.get("customer_name"),
        },
        "limit": safe_limit,
        "direction": direction,
        "messages": messages,
    }


@app.get("/api/debug/ozon/chat/{external_chat_id}/raw")
async def debug_ozon_chat_raw(external_chat_id: str, limit: int = 10) -> dict[str, Any]:
    """Fetch messages from Ozon now and return raw message objects.

    This shows the exact incoming customer-message payload before the CRM normalizes it.
    API keys are not returned.
    """
    connector = connectors["ozon"]
    try:
        messages = await connector.get_messages(external_chat_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    safe_limit = max(1, min(int(limit or 10), 50))
    result = []
    for message in messages[-safe_limit:]:
        result.append({
            "direction": direction,
            "author": getattr(message, "author", None),
            "created_at": getattr(message, "created_at", None),
            "text": getattr(message, "text", None),
            "raw_object": getattr(message, "raw", {}) or {},
        })
    return {
        "external_chat_id": external_chat_id,
        "messages_count": len(messages),
        "returned": len(result),
        "messages": result,
    }


@app.get("/api/debug/openai")
async def debug_openai() -> dict[str, Any]:
    """Safe OpenAI connectivity check. Does not return the API key."""
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    if not api_key:
        return {"configured": False, "api_key_present": False, "model": model, "error": "OPENAI_API_KEY is missing"}

    payload = {
        "model": model,
        "input": "Ответь одним словом: OK",
        "max_output_tokens": 20,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as exc:
        return {"configured": True, "api_key_present": True, "model": model, "status": "network_error", "error": str(exc)}

    out: dict[str, Any] = {
        "configured": True,
        "api_key_present": True,
        "model": model,
        "status_code": response.status_code,
        "ok": response.status_code < 400,
    }
    if response.status_code >= 400:
        out["error"] = _openai_error_detail(response)
        return out
    try:
        data = response.json()
        out["sample_output"] = _extract_response_text(data)[:100]
    except Exception:
        out["sample_output"] = ""
    return out


@app.get("/api/sync/status")
def sync_status() -> dict[str, Any]:
    return {
        "last_sync": getattr(app.state, "last_sync", {}),
        "background": getattr(app.state, "last_background_sync", {}),
        "reviews": getattr(app.state, "last_reviews_sync", {}),
    }





def _supply_env_first(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _supply_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def _supply_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_supply_float(value, float(default))))
    except Exception:
        return default


def _supply_pick(data: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return default


def _supply_secret_present(value: str) -> bool:
    return bool(str(value or "").strip())


def _supply_status() -> dict[str, Any]:
    ozon_client_id = _supply_env_first("OZON_CLIENT_ID")
    ozon_api_key = _supply_env_first("OZON_API_KEY")
    wb_analytics_token = _supply_env_first("WB_ANALYTICS_TOKEN", "WB_SUPPLY_ANALYTICS_TOKEN")
    wb_statistics_token = _supply_env_first("WB_STATISTICS_TOKEN")
    ym_api_key = _supply_env_first("YANDEX_MARKET_API_KEY", "YANDEX_API_KEY", "YANDEX_MARKET_TOKEN", "YANDEX_TOKEN")
    ym_campaign_id = _supply_env_first("YANDEX_MARKET_CAMPAIGN_ID", "YANDEX_CAMPAIGN_ID")
    return {
        "ozon": {
            "configured": _supply_secret_present(ozon_client_id) and _supply_secret_present(ozon_api_key),
            "required_env": ["OZON_CLIENT_ID", "OZON_API_KEY"],
        },
        "wildberries": {
            "configured": _supply_secret_present(wb_analytics_token),
            "required_env": ["WB_ANALYTICS_TOKEN — токен WB категории Analytics"],
            "optional_env": ["WB_STATISTICS_TOKEN — токен WB категории Statistics для продаж/день"],
            "analytics_token_present": _supply_secret_present(wb_analytics_token),
            "statistics_token_present": _supply_secret_present(wb_statistics_token),
        },
        "yandex": {
            "configured": _supply_secret_present(ym_api_key) and _supply_secret_present(ym_campaign_id),
            "required_env": ["YANDEX_MARKET_API_KEY", "YANDEX_MARKET_CAMPAIGN_ID"],
        },
    }


async def _supply_http_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None, json_body: Any = None, timeout: float = 60.0) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, headers=headers or {}, params=params, json=json_body)
    if response.status_code >= 400:
        text = _mask_sensitive(response.text[:700])
        raise RuntimeError(f"{method} {url} -> HTTP {response.status_code}: {text}")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _supply_days_ago_iso(days: int) -> str:
    days = max(1, min(int(days or 30), 90))
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _supply_cache_file() -> Path:
    return Path(os.getenv("SUPPLY_PLANNING_CACHE_FILE", ".supply_planning_cache.json"))


def _supply_cache_ttl_seconds(marketplace: str) -> int:
    marketplace_key = re.sub(r"[^A-Z0-9_]", "_", str(marketplace or "").upper())
    specific = os.getenv(f"SUPPLY_{marketplace_key}_CACHE_TTL_SECONDS")
    default = os.getenv("SUPPLY_CACHE_TTL_SECONDS", "1800")
    return _env_int(f"SUPPLY_{marketplace_key}_CACHE_TTL_SECONDS", int(specific or default or 1800), minimum=60, maximum=86400)


def _supply_cache_key(marketplace: str, *, sales_days: int = 30, target_days: int = 45) -> str:
    return f"{str(marketplace or '').lower()}|sales_days={int(sales_days or 30)}|target_days={int(target_days or 45)}"


def _supply_read_cache() -> dict[str, Any]:
    path = _supply_cache_file()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _supply_write_cache(cache: dict[str, Any]) -> None:
    path = _supply_cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _supply_cache_get(key: str, *, max_age_seconds: int | None = None) -> dict[str, Any] | None:
    cache = _supply_read_cache()
    item = cache.get(key)
    if not isinstance(item, dict):
        return None
    saved_at = _supply_float(item.get("saved_at"), 0)
    if max_age_seconds is not None and saved_at > 0 and (time.time() - saved_at) > max_age_seconds:
        return None
    result = item.get("result")
    return result if isinstance(result, dict) else None


def _supply_cache_set(key: str, result: dict[str, Any]) -> None:
    cache = _supply_read_cache()
    safe_result = dict(result)
    safe_result["cached"] = False
    safe_result["cache_saved_at"] = datetime.now(timezone.utc).isoformat()
    cache[key] = {"saved_at": time.time(), "result": safe_result}
    _supply_write_cache(cache)


def _supply_cached_result(key: str, *, max_age_seconds: int | None = None, reason: str = "") -> dict[str, Any] | None:
    cached = _supply_cache_get(key, max_age_seconds=max_age_seconds)
    if not cached:
        return None
    result = dict(cached)
    result["ok"] = True
    result["cached"] = True
    result["cache_reason"] = reason
    result["note"] = (str(result.get("note") or "").rstrip() + " Данные показаны из кэша, чтобы не тратить лимит WB.").strip()
    if reason:
        result["note"] += f" Причина: {reason}."
    return result


class SupplyRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int = 0):
        super().__init__(message)
        self.retry_after_seconds = max(0, int(retry_after_seconds or 0))


def _supply_retry_after_seconds(response: httpx.Response) -> int:
    value = response.headers.get("x-ratelimit-retry") or response.headers.get("retry-after") or ""
    try:
        return max(0, int(float(str(value).strip())))
    except Exception:
        return 0


async def _supply_http_json_wb(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None, json_body: Any = None, timeout: float = 90.0) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, headers=headers or {}, params=params, json=json_body)
    if response.status_code == 429:
        text = _mask_sensitive(response.text[:700])
        retry_after = _supply_retry_after_seconds(response)
        raise SupplyRateLimitError(f"{method} {url} -> HTTP 429: {text}", retry_after_seconds=retry_after)
    if response.status_code >= 400:
        text = _mask_sensitive(response.text[:700])
        raise RuntimeError(f"{method} {url} -> HTTP {response.status_code}: {text}")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _supply_compose_row(*, marketplace: str, sku: Any, product: Any = "", warehouse: Any = "", current_stock: Any = 0, avg_daily_sales: Any = 0, in_transit: Any = 0, target_days: int = 45, delivery_days: Any = "") -> dict[str, Any]:
    return {
        "marketplace": marketplace,
        "sku": str(sku or "").strip(),
        "product": str(product or "").strip(),
        "warehouse": str(warehouse or "").strip(),
        "currentStock": str(max(0, _supply_int(current_stock, 0))),
        "avgDailySales": str(round(max(0.0, _supply_float(avg_daily_sales, 0.0)), 2)).rstrip("0").rstrip("."),
        "deliveryDays": str(delivery_days if str(delivery_days or "").strip() else ""),
        "targetStockDays": str(max(1, int(target_days or 45))),
        "inTransit": str(max(0, _supply_int(in_transit, 0))),
    }


def _supply_row_sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("marketplace") or ""),
        str(row.get("sku") or ""),
        str(row.get("warehouse") or ""),
        str(row.get("product") or ""),
    )



async def _supply_fetch_ozon_sales_map(headers: dict[str, str], *, sales_days: int) -> tuple[dict[str, float], str]:
    """Return average daily Ozon sales by SKU from analytics/data when the token has access."""
    days = max(1, min(int(sales_days or 30), 90))
    date_from = _supply_days_ago_iso(days)
    date_to = datetime.now(timezone.utc).date().isoformat()
    sales: dict[str, float] = {}
    error = ""
    limit = _env_int("SUPPLY_OZON_SALES_LIMIT", 1000, minimum=1, maximum=1000)
    max_pages = _env_int("SUPPLY_OZON_SALES_MAX_PAGES", 20, minimum=1, maximum=200)
    offset = 0
    for _ in range(max_pages):
        try:
            data = await _supply_http_json(
                "POST",
                "https://api-seller.ozon.ru/v1/analytics/data",
                headers=headers,
                json_body={
                    "date_from": date_from,
                    "date_to": date_to,
                    "metrics": ["ordered_units"],
                    "dimension": ["sku"],
                    "filters": [],
                    "sort": [{"key": "ordered_units", "order": "DESC"}],
                    "limit": limit,
                    "offset": offset,
                },
                timeout=90,
            )
        except Exception as exc:
            error = str(exc)[:700]
            break
        result = data.get("result", data) if isinstance(data, dict) else {}
        items = result.get("data") if isinstance(result, dict) else []
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            dimensions = item.get("dimensions") or []
            sku = ""
            if isinstance(dimensions, list) and dimensions:
                first = dimensions[0] if isinstance(dimensions[0], dict) else {}
                sku = str(first.get("id") or first.get("name") or "").strip()
            metrics = item.get("metrics") or []
            qty = 0.0
            if isinstance(metrics, list) and metrics:
                qty = _supply_float(metrics[0], 0.0)
            if sku:
                sales[sku] = sales.get(sku, 0.0) + max(0.0, qty) / days
        if len(items) < limit:
            break
        offset += limit
    return sales, error


def _supply_ozon_extract_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    result = data.get("result", data)
    if not isinstance(result, dict):
        return []
    for key in ("rows", "items", "stocks", "data"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


async def _supply_fetch_ozon(*, sales_days: int, target_days: int) -> dict[str, Any]:
    client_id = _supply_env_first("OZON_CLIENT_ID")
    api_key = _supply_env_first("OZON_API_KEY")
    if not client_id or not api_key:
        return {"ok": False, "marketplace": "ozon", "configured": False, "rows": [], "error": "OZON_CLIENT_ID/OZON_API_KEY are not configured"}

    headers = {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    limit = _env_int("SUPPLY_OZON_STOCK_LIMIT", 1000, minimum=1, maximum=1000)
    max_pages = _env_int("SUPPLY_OZON_STOCK_MAX_PAGES", 10, minimum=1, maximum=100)

    rows_raw: list[dict[str, Any]] = []
    errors: list[str] = []

    async def fetch_stock_on_warehouses() -> bool:
        offset = 0
        got_any = False
        for _ in range(max_pages):
            payload = {"limit": limit, "offset": offset}
            data = await _supply_http_json(
                "POST",
                "https://api-seller.ozon.ru/v2/analytics/stock_on_warehouses",
                headers=headers,
                json_body=payload,
                timeout=90,
            )
            batch = _supply_ozon_extract_rows(data)
            if not batch:
                break
            rows_raw.extend(batch)
            got_any = True
            if len(batch) < limit:
                break
            offset += limit
        return got_any

    async def fetch_manage_stocks_fallback() -> bool:
        offset = 0
        got_any = False
        for _ in range(max_pages):
            payload = {"limit": limit, "offset": offset, "filter": {}}
            data = await _supply_http_json(
                "POST",
                "https://api-seller.ozon.ru/v1/analytics/manage/stocks",
                headers=headers,
                json_body=payload,
                timeout=90,
            )
            batch = _supply_ozon_extract_rows(data)
            if not batch:
                break
            rows_raw.extend(batch)
            got_any = True
            if len(batch) < limit:
                break
            offset += limit
        return got_any

    for loader in (fetch_stock_on_warehouses, fetch_manage_stocks_fallback):
        try:
            if await loader():
                break
        except Exception as exc:
            errors.append(str(exc)[:700])

    if not rows_raw:
        return {
            "ok": False,
            "marketplace": "ozon",
            "configured": True,
            "rows": [],
            "error": "Ozon не вернул остатки по складам. " + ("; ".join(errors[:2]) if errors else "Проверьте права API-ключа на аналитику/остатки."),
        }

    sales_map, sales_error = await _supply_fetch_ozon_sales_map(headers, sales_days=sales_days)

    parsed: list[dict[str, Any]] = []
    for item in rows_raw:
        sku = _supply_pick(item, ["sku", "item_code", "offer_id", "offerId", "product_id", "productId"])
        if not sku:
            continue
        stock = _supply_pick(item, ["free_to_sell_amount", "available_stock_count", "valid_stock_count", "present", "stock", "quantity", "available", "count"], 0)
        warehouse = _supply_pick(item, ["warehouse_name", "warehouse", "warehouseName", "cluster_name", "cluster", "name"], "Склад Ozon")
        product = _supply_pick(item, ["item_name", "product_name", "productName", "name", "offer_name"], sku)
        parsed.append({
            "sku": str(sku).strip(),
            "product": product,
            "warehouse": warehouse,
            "stock": max(0, _supply_int(stock, 0)),
        })

    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for item in parsed:
        sku = item["sku"]
        totals[sku] = totals.get(sku, 0) + int(item.get("stock") or 0)
        counts[sku] = counts.get(sku, 0) + 1

    rows: list[dict[str, Any]] = []
    for item in parsed:
        sku = item["sku"]
        sku_avg = sales_map.get(sku, 0.0)
        if sku_avg > 0:
            total_stock = totals.get(sku, 0)
            if total_stock > 0:
                avg = sku_avg * (int(item.get("stock") or 0) / total_stock)
            else:
                avg = sku_avg / max(1, counts.get(sku, 1))
        else:
            avg = 0.0
        rows.append(_supply_compose_row(
            marketplace="ozon",
            sku=sku,
            product=item.get("product") or sku,
            warehouse=item.get("warehouse") or "Склад Ozon",
            current_stock=item.get("stock") or 0,
            avg_daily_sales=avg,
            target_days=target_days,
        ))

    rows.sort(key=_supply_row_sort_key)
    return {
        "ok": True,
        "marketplace": "ozon",
        "configured": True,
        "rows": rows,
        "rows_count": len(rows),
        "sales_error": sales_error,
        "stock_errors": errors,
        "note": "Ozon: остатки загружены по складам/кластерам. Продажи/день рассчитаны из analytics/data по SKU и распределены между складами пропорционально текущему остатку, если Ozon вернул продажи.",
    }


async def _supply_fetch_wildberries(*, sales_days: int, target_days: int) -> dict[str, Any]:
    # Для остатков на складах WB нужен именно токен категории Analytics.
    # Не используем общий WB_API_TOKEN или токен чатов: WB вернёт 401 token scope not allowed.
    stock_token = _supply_env_first("WB_ANALYTICS_TOKEN", "WB_SUPPLY_ANALYTICS_TOKEN")
    sales_token = _supply_env_first("WB_STATISTICS_TOKEN", "WB_ANALYTICS_TOKEN", "WB_SUPPLY_ANALYTICS_TOKEN")
    if not stock_token:
        return {
            "ok": False,
            "marketplace": "wildberries",
            "configured": False,
            "rows": [],
            "error": "Для поставок WB нужен отдельный токен категории Analytics. Создайте токен в WB API и укажите его в WB_ANALYTICS_TOKEN. WB_API_TOKEN/WB_BUYERS_CHAT_TOKEN от чатов для этого метода не подходят.",
            "token_scope_required": "Analytics",
            "required_env": ["WB_ANALYTICS_TOKEN"],
        }

    cache_key = _supply_cache_key("wildberries", sales_days=sales_days, target_days=target_days)
    cache_ttl = _supply_cache_ttl_seconds("wildberries")
    cached_fresh = _supply_cached_result(cache_key, max_age_seconds=cache_ttl, reason=f"кэш младше {cache_ttl // 60} мин")
    if cached_fresh:
        return cached_fresh

    stock_headers = {"Authorization": stock_token, "Content-Type": "application/json", "Accept": "application/json"}
    stock_limit = _env_int("SUPPLY_WB_STOCK_LIMIT", 250000, minimum=1, maximum=250000)

    try:
        data = await _supply_http_json_wb(
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
            headers=stock_headers,
            json_body={"nmIds": [], "chrtIds": [], "limit": stock_limit, "offset": 0},
            timeout=90,
        )
    except SupplyRateLimitError as exc:
        cached_any = _supply_cached_result(cache_key, max_age_seconds=None, reason="WB вернул 429 Too Many Requests")
        if cached_any:
            cached_any["rate_limited"] = True
            cached_any["retry_after_seconds"] = exc.retry_after_seconds
            if exc.retry_after_seconds:
                cached_any["note"] += f" Повторный живой запрос WB лучше делать через {exc.retry_after_seconds} сек."
            return cached_any
        return {
            "ok": False,
            "marketplace": "wildberries",
            "configured": True,
            "rows": [],
            "error": "WB вернул 429 Too Many Requests: превышен общий лимит запросов по кабинету. Подождите cooldown и не нажимайте обновление повторно. " + str(exc)[:700],
            "rate_limited": True,
            "retry_after_seconds": exc.retry_after_seconds,
        }
    except Exception as exc:
        error_text = str(exc)[:900]
        if "HTTP 401" in error_text and "token scope not allowed" in error_text:
            cached_any = _supply_cached_result(cache_key, max_age_seconds=None, reason="WB вернул 401 token scope not allowed")
            if cached_any:
                cached_any["auth_warning"] = True
                cached_any["token_scope_required"] = "Analytics"
                cached_any["note"] += " Живой запрос WB не выполнен: у токена нет категории Analytics."
                return cached_any
            return {
                "ok": False,
                "marketplace": "wildberries",
                "configured": True,
                "rows": [],
                "error": "WB вернул 401 token scope not allowed: у токена нет доступа к категории Analytics. Создайте новый WB API-токен с категорией Analytics и укажите его в переменной WB_ANALYTICS_TOKEN. Не используйте WB_API_TOKEN или WB_BUYERS_CHAT_TOKEN от чатов.",
                "token_scope_required": "Analytics",
                "how_to_fix": "WB Seller → Профиль/Настройки → API интеграции → Создать токен → категория Analytics → сохранить токен в .env как WB_ANALYTICS_TOKEN → перезапустить CRM.",
                "raw_error": error_text,
            }
        return {
            "ok": False,
            "marketplace": "wildberries",
            "configured": True,
            "rows": [],
            "error": "WB не вернул остатки по складам: " + error_text,
        }

    stock_items = []
    if isinstance(data, dict):
        stock_items = ((data.get("data") or {}).get("items") or data.get("items") or [])
    if not isinstance(stock_items, list):
        stock_items = []

    sales_map: dict[tuple[str, str], float] = {}
    sales_error = ""
    if not sales_token:
        sales_error = "WB_STATISTICS_TOKEN не настроен, поэтому продажи/день могут быть 0. Для продаж нужен токен WB категории Statistics."
    else:
        try:
            sales_from = _supply_days_ago_iso(sales_days)
            sales_data = await _supply_http_json_wb(
                "GET",
                "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
                headers={"Authorization": sales_token, "Accept": "application/json"},
                params={"dateFrom": sales_from},
                timeout=90,
            )
            if isinstance(sales_data, list):
                for sale in sales_data:
                    if not isinstance(sale, dict):
                        continue
                    sku = str(_supply_pick(sale, ["nmId", "supplierArticle", "barcode", "techSize"]) or "").strip()
                    warehouse = str(_supply_pick(sale, ["warehouseName", "warehouse_name"], "") or "").strip()
                    if not sku or not warehouse:
                        continue
                    sign = -1 if str(sale.get("saleID") or "").upper().startswith("R") else 1
                    qty = abs(_supply_float(_supply_pick(sale, ["quantity"], 1), 1)) * sign
                    sales_map[(sku, warehouse)] = sales_map.get((sku, warehouse), 0.0) + qty
        except SupplyRateLimitError as exc:
            sales_error = "WB вернул 429 по продажам. Остатки загружены, продажи/день могут быть 0. Повторить живой запрос лучше позже" + (f" через {exc.retry_after_seconds} сек." if exc.retry_after_seconds else ".")
        except Exception as exc:
            sales_error_raw = str(exc)[:500]
            if "HTTP 401" in sales_error_raw and "token scope not allowed" in sales_error_raw:
                sales_error = "Токен для продаж WB не имеет категории Statistics. Остатки загружены, но продажи/день могут быть 0. Укажите WB_STATISTICS_TOKEN."
            else:
                sales_error = sales_error_raw

    rows: list[dict[str, Any]] = []
    for item in stock_items:
        if not isinstance(item, dict):
            continue
        sku = str(_supply_pick(item, ["nmId", "supplierArticle", "barcode", "chrtId"]) or "").strip()
        if not sku:
            continue
        warehouse = str(_supply_pick(item, ["warehouseName", "officeName"], "Склад WB") or "Склад WB").strip()
        region = str(item.get("regionName") or "").strip()
        warehouse_label = f"{warehouse} · {region}" if region and region not in warehouse else warehouse
        sold = max(0.0, sales_map.get((sku, warehouse), 0.0))
        avg = sold / max(1, min(int(sales_days or 30), 90))
        rows.append(_supply_compose_row(
            marketplace="wildberries",
            sku=sku,
            product=_supply_pick(item, ["vendorCode", "subjectName", "brandName"], sku),
            warehouse=warehouse_label,
            current_stock=_supply_pick(item, ["quantity"], 0),
            avg_daily_sales=avg,
            target_days=target_days,
        ))
    rows.sort(key=_supply_row_sort_key)
    result = {"ok": True, "marketplace": "wildberries", "configured": True, "rows": rows, "rows_count": len(rows), "sales_error": sales_error, "cached": False, "note": "WB: импортированы текущие остатки по складам WB через токен категории Analytics; продажи/день рассчитаны по продажам за выбранный период, если настроен токен с доступом к статистике продаж."}
    _supply_cache_set(cache_key, result)
    return result


def _supply_yandex_headers() -> dict[str, str]:
    api_key = _supply_env_first("YANDEX_MARKET_API_KEY", "YANDEX_API_KEY", "YANDEX_MARKET_TOKEN", "YANDEX_TOKEN")
    oauth_token = _supply_env_first("YANDEX_OAUTH_TOKEN")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Api-Key"] = api_key
    elif oauth_token:
        headers["Authorization"] = f"OAuth {oauth_token}"
    return headers


async def _supply_fetch_yandex_warehouse_names(headers: dict[str, str]) -> dict[str, str]:
    try:
        data = await _supply_http_json("GET", "https://api.partner.market.yandex.ru/v2/warehouses", headers=headers, timeout=30)
        result = data.get("result", data) if isinstance(data, dict) else {}
        warehouses = result.get("warehouses") if isinstance(result, dict) else []
        names = {}
        if isinstance(warehouses, list):
            for item in warehouses:
                if isinstance(item, dict) and item.get("id") is not None:
                    names[str(item.get("id"))] = str(item.get("name") or f"Склад {item.get('id')}")
        return names
    except Exception:
        return {}


async def _supply_fetch_yandex(*, target_days: int) -> dict[str, Any]:
    campaign_id = _supply_env_first("YANDEX_MARKET_CAMPAIGN_ID", "YANDEX_CAMPAIGN_ID")
    headers = _supply_yandex_headers()
    if not campaign_id or not (headers.get("Api-Key") or headers.get("Authorization")):
        return {"ok": False, "marketplace": "yandex", "configured": False, "rows": [], "error": "YANDEX_MARKET_API_KEY/YANDEX_MARKET_CAMPAIGN_ID are not configured"}
    warehouse_names = await _supply_fetch_yandex_warehouse_names(headers)
    rows: list[dict[str, Any]] = []
    page_token = ""
    limit = _env_int("SUPPLY_YANDEX_STOCK_LIMIT", 200, minimum=1, maximum=200)
    max_pages = _env_int("SUPPLY_YANDEX_STOCK_MAX_PAGES", 20, minimum=1, maximum=200)
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": limit}
        if page_token:
            params["pageToken"] = page_token
        data = await _supply_http_json(
            "POST",
            f"https://api.partner.market.yandex.ru/v2/campaigns/{campaign_id}/offers/stocks",
            headers=headers,
            params=params,
            json_body={"withTurnover": True, "archived": False},
            timeout=60,
        )
        result = data.get("result", data) if isinstance(data, dict) else {}
        warehouses = result.get("warehouses") if isinstance(result, dict) else []
        if not isinstance(warehouses, list):
            warehouses = []
        for wh in warehouses:
            if not isinstance(wh, dict):
                continue
            warehouse_id = str(wh.get("warehouseId") or wh.get("id") or "").strip()
            warehouse_label = warehouse_names.get(warehouse_id) or (f"Склад {warehouse_id}" if warehouse_id else "Склад Яндекс")
            offers = wh.get("offers") or []
            if not isinstance(offers, list):
                continue
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                sku = str(offer.get("offerId") or offer.get("shopSku") or "").strip()
                if not sku:
                    continue
                stocks = offer.get("stocks") or []
                current = 0
                if isinstance(stocks, list):
                    available = [s for s in stocks if isinstance(s, dict) and str(s.get("type") or "").upper() == "AVAILABLE"]
                    fit = [s for s in stocks if isinstance(s, dict) and str(s.get("type") or "").upper() == "FIT"]
                    source = available or fit or [s for s in stocks if isinstance(s, dict)]
                    current = sum(_supply_int(s.get("count"), 0) for s in source)
                turnover_days = _supply_float(((offer.get("turnoverSummary") or {}) if isinstance(offer.get("turnoverSummary"), dict) else {}).get("turnoverDays"), 0)
                avg = current / turnover_days if current > 0 and turnover_days > 0 else 0
                rows.append(_supply_compose_row(
                    marketplace="yandex",
                    sku=sku,
                    product=sku,
                    warehouse=warehouse_label,
                    current_stock=current,
                    avg_daily_sales=avg,
                    target_days=target_days,
                ))
        paging = result.get("paging") if isinstance(result, dict) else {}
        page_token = str((paging or {}).get("nextPageToken") or "").strip() if isinstance(paging, dict) else ""
        if not page_token:
            break
    rows.sort(key=_supply_row_sort_key)
    return {"ok": True, "marketplace": "yandex", "configured": True, "rows": rows, "rows_count": len(rows), "note": "Яндекс: импортированы остатки; продажи/день рассчитаны из turnoverDays, если Маркет вернул оборачиваемость."}


@app.get("/api/supply-planning/status")
def supply_planning_api_status(request: Request) -> dict[str, Any]:
    _current_user(request)
    return {"ok": True, "configured": _supply_status()}


@app.api_route("/api/supply-planning/sync", methods=["POST"])
@app.api_route("/api/supply-planning/sync/", methods=["POST"])
async def supply_planning_sync(request: Request) -> dict[str, Any]:
    _current_user(request)
    _rate_limit(
        request,
        "supply-sync",
        limit=_security_env_int("CRM_SUPPLY_SYNC_RATE_LIMIT", 6, minimum=1, maximum=120),
        window_seconds=_security_env_int("CRM_SUPPLY_SYNC_RATE_WINDOW_SECONDS", 300, minimum=60, maximum=86400),
    )
    params = request.query_params
    marketplace = str(params.get("marketplace") or "all").strip().lower()
    sales_days = _env_int("SUPPLY_DEFAULT_SALES_DAYS", int(params.get("sales_days") or 30), minimum=1, maximum=90)
    target_days = _env_int("SUPPLY_DEFAULT_TARGET_DAYS", int(params.get("target_days") or 45), minimum=1, maximum=365)
    requested = ["ozon", "wildberries", "yandex"] if marketplace in {"", "all"} else [marketplace]
    results: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for item in requested:
        try:
            if item == "ozon":
                result = await _supply_fetch_ozon(sales_days=sales_days, target_days=target_days)
            elif item in {"wildberries", "wb"}:
                result = await _supply_fetch_wildberries(sales_days=sales_days, target_days=target_days)
            elif item in {"yandex", "ym", "yandex_market"}:
                result = await _supply_fetch_yandex(target_days=target_days)
            else:
                result = {"ok": False, "marketplace": item, "configured": False, "rows": [], "error": "unknown marketplace"}
        except Exception as exc:
            result = {"ok": False, "marketplace": item, "configured": True, "rows": [], "error": _mask_sensitive(str(exc))[:1200]}
        results.append({k: v for k, v in result.items() if k != "rows"})
        rows.extend(result.get("rows") or [])
    rows.sort(key=_supply_row_sort_key)
    return {"ok": True, "rows": rows, "rows_count": len(rows), "sales_days": sales_days, "target_days": target_days, "results": results, "configured": _supply_status()}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return repo.stats()


@app.get("/api/analytics/chats")
def chat_analytics(
    date_from: str | None = None,
    date_to: str | None = None,
    marketplace: str | None = None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    tz_offset_minutes: int | None = None,
) -> dict[str, Any]:
    """Chat analytics dashboard.

    Heavy SQL is intentionally kept in app.services.analytics so route code stays
    small and future analytics sections can be extended without growing main.py.
    """
    return build_chat_analytics(
        date_from=date_from,
        date_to=date_to,
        marketplace=marketplace,
        hour_from=hour_from,
        hour_to=hour_to,
        tz_offset_minutes=tz_offset_minutes,
    )


@app.get("/api/analytics/chats/drilldown")
def chat_analytics_drilldown(
    date_from: str | None = None,
    date_to: str | None = None,
    marketplace: str | None = None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    tz_offset_minutes: int | None = None,
    limit: int = 1000,
    include_excluded: bool = True,
) -> dict[str, Any]:
    """Audit rows used by hourly chat analytics."""
    return build_chat_analytics_drilldown(
        date_from=date_from,
        date_to=date_to,
        marketplace=marketplace,
        hour_from=hour_from,
        hour_to=hour_to,
        tz_offset_minutes=tz_offset_minutes,
        limit=limit,
        include_excluded=include_excluded,
    )


_ASSET_PROXY_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ASSET_PROXY_MAX_REDIRECTS = 3
_ASSET_PROXY_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_ASSET_PROXY_STREAM_CHUNK_BYTES = 64 * 1024


def _asset_proxy_content_type(response: httpx.Response) -> str:
    values = response.headers.get_list("content-type")
    if len(values) != 1 or "," in values[0]:
        raise HTTPException(status_code=415, detail="URL did not return a supported image")
    content_type = values[0].split(";", 1)[0].strip().lower()
    if content_type not in _ASSET_PROXY_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="URL did not return a supported image")
    return content_type


def _asset_proxy_validate_content_encoding(response: httpx.Response) -> None:
    values = response.headers.get_list("content-encoding")
    if values and (
        len(values) != 1
        or "," in values[0]
        or values[0].strip().lower() != "identity"
    ):
        raise HTTPException(status_code=502, detail="Invalid image response")


def _asset_proxy_signature_matches(content_type: str, body: bytes) -> bool:
    if content_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return body.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP"
    return False


async def _fetch_proxy_image_response(url: str, *, max_bytes: int) -> tuple[bytes, str]:
    current_url = url
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            for redirects_followed in range(_ASSET_PROXY_MAX_REDIRECTS + 1):
                if not _asset_proxy_allowed(current_url):
                    raise HTTPException(status_code=502, detail="Image preview redirect is not allowed")
                if not asset_url_resolves_globally(current_url, resolve_asset_host_addresses):
                    raise HTTPException(status_code=502, detail="Image preview host resolution is not allowed")

                async with client.stream("GET", current_url, headers=_asset_proxy_headers(current_url)) as response:
                    if response.status_code in _ASSET_PROXY_REDIRECT_STATUSES:
                        if redirects_followed >= _ASSET_PROXY_MAX_REDIRECTS:
                            raise HTTPException(status_code=502, detail="Image preview redirect limit exceeded")

                        next_url = resolve_asset_redirect(current_url, response.headers.get("location"))
                        if not next_url or not _asset_proxy_allowed(next_url):
                            raise HTTPException(status_code=502, detail="Image preview redirect is not allowed")
                        current_url = next_url
                        continue

                    response.raise_for_status()
                    _asset_proxy_validate_content_encoding(response)
                    content_type = _asset_proxy_content_type(response)

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except (TypeError, ValueError) as exc:
                            raise HTTPException(status_code=502, detail="Invalid image response") from exc
                        if declared_length < 0:
                            raise HTTPException(status_code=502, detail="Invalid image response")
                        if declared_length > max_bytes:
                            raise HTTPException(status_code=413, detail="Image is too large for preview")

                    body = bytearray()
                    async for chunk in response.aiter_raw(_ASSET_PROXY_STREAM_CHUNK_BYTES):
                        if len(body) + len(chunk) > max_bytes:
                            raise HTTPException(status_code=413, detail="Image is too large for preview")
                        body.extend(chunk)

                    content = bytes(body)
                    if not _asset_proxy_signature_matches(content_type, content):
                        raise HTTPException(status_code=415, detail="URL did not return a supported image")
                    return content, content_type
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Image preview request failed") from exc

    raise HTTPException(status_code=502, detail="Image preview request failed")


@app.get("/api/assets/image")
async def proxy_image(url: str) -> Response:
    if not _asset_proxy_allowed(url):
        raise HTTPException(status_code=400, detail="Image host is not allowed for preview proxy")
    max_bytes = _env_int("IMAGE_PROXY_MAX_BYTES", 10_000_000, minimum=100_000, maximum=30_000_000)
    content, content_type = await _fetch_proxy_image_response(url, max_bytes=max_bytes)

    headers = {
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=content, media_type=content_type, headers=headers)




@app.get("/api/reviews")
def list_reviews(status: str | None = None, unanswered: bool = False) -> list[dict[str, Any]]:
    return repo.list_reviews(marketplace="ozon", status=status, unanswered=unanswered)


@app.get("/api/reviews/{review_id}")
def get_review(review_id: int) -> dict[str, Any]:
    review = repo.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


@app.post("/api/reviews/sync/ozon")
async def sync_ozon_reviews() -> dict[str, Any]:
    return await _sync_ozon_reviews_unlocked(background=False)


@app.post("/api/reviews/{review_id}/reply")
async def reply_to_review(review_id: int, payload: ReviewReplyCreate) -> dict[str, Any]:
    review = repo.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    if review.get("marketplace") != "ozon":
        raise HTTPException(status_code=400, detail="Only Ozon reviews are supported now")
    connector = connectors.get("ozon")
    if not connector or not hasattr(connector, "reply_to_review"):
        raise HTTPException(status_code=500, detail="Ozon reviews connector is not available")
    external_id = review.get("external_review_id")
    try:
        raw_response = await connector.reply_to_review(str(external_id), payload.text)  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    status_result: dict[str, Any] | None = None
    status = review.get("status")
    if payload.mark_processed:
        try:
            status_result = await connector.change_review_status([str(external_id)], "PROCESSED")  # type: ignore[attr-defined]
            status = "PROCESSED"
        except Exception as exc:
            status_result = {"warning": str(exc)}
    updated = repo.mark_review_replied(review_id, payload.text, {"comment_create": raw_response, "change_status": status_result}, status=status)
    return {"ok": True, "review": updated, "marketplace_response": raw_response, "status_response": status_result}


@app.post("/api/reviews/{review_id}/start-chat")
async def start_chat_from_review(review_id: int) -> dict[str, Any]:
    review = repo.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    posting_number = review.get("posting_number") or ""
    if not posting_number:
        raise HTTPException(status_code=400, detail="В этом отзыве нет posting_number. Ozon может создавать чат только по номеру отправления, если он доступен в данных отзыва.")
    connector = connectors.get("ozon")
    if not connector or not hasattr(connector, "start_chat_by_posting"):
        raise HTTPException(status_code=500, detail="Ozon chat connector is not available")
    try:
        raw_response = await connector.start_chat_by_posting(str(posting_number))  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = raw_response.get("result") if isinstance(raw_response.get("result"), dict) else raw_response
    external_chat_id = str((result or {}).get("chat_id") or raw_response.get("chat_id") or "")
    local_chat_id = None
    if external_chat_id:
        local_chat_id = repo.upsert_chat(ChatCreate(marketplace="ozon", external_chat_id=external_chat_id, customer_name=review.get("author_name"), order_id=posting_number, metadata={"source": "review", "review_id": review.get("external_review_id")}))
        repo.link_review_chat(review_id, local_chat_id)
    return {"ok": True, "chat_id": external_chat_id, "local_chat_id": local_chat_id, "marketplace_response": raw_response}


def _pick_ozon_question_api_id(question: dict[str, Any]) -> str:
    """Pick the safest Ozon question_id for answer/create.

    Older CRM builds could store a generic id. Prefer question-specific values
    from raw_json, then fall back to external_question_id.
    """
    raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
    question_obj = raw.get("question") if isinstance(raw.get("question"), dict) else {}

    candidates = [
        raw.get("_crm_question_id"),
        raw.get("question_id"),
        raw.get("questionId"),
        raw.get("question_uuid"),
        raw.get("questionUuid"),
        question_obj.get("question_id"),
        question_obj.get("questionId"),
        question_obj.get("id"),
        question.get("external_question_id"),
    ]

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key, nested_value in value.items():
                key_norm = str(key).lower().replace("_", "")
                if key_norm in {"questionid", "questionuuid"} and nested_value not in (None, ""):
                    return str(nested_value).strip()
            for nested_value in value.values():
                found = walk(nested_value)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return ""

    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate.lower() not in {"none", "null", "undefined"}:
            return candidate

    nested = walk(raw)
    if nested:
        return nested

    return ""



def _pick_ozon_question_api_sku(question: dict[str, Any]) -> int:
    """Pick Ozon SKU required by /v1/question/answer/create."""
    raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    product_info = raw.get("product_info") if isinstance(raw.get("product_info"), dict) else {}
    sku_info = raw.get("sku_info") if isinstance(raw.get("sku_info"), dict) else {}
    question_obj = raw.get("question") if isinstance(raw.get("question"), dict) else {}

    candidates = [
        question.get("sku"),
        raw.get("sku"),
        raw.get("product_sku"),
        raw.get("productSku"),
        raw.get("sku_id"),
        raw.get("skuId"),
        product.get("sku"),
        product.get("sku_id"),
        product.get("skuId"),
        product_info.get("sku"),
        product_info.get("sku_id"),
        product_info.get("skuId"),
        sku_info.get("sku"),
        sku_info.get("sku_id"),
        sku_info.get("skuId"),
        question_obj.get("sku"),
        question_obj.get("product_sku"),
        question_obj.get("productSku"),
    ]

    def to_positive_int(value: Any) -> int:
        if value in (None, ""):
            return 0
        raw_value = str(value).strip()
        if not raw_value:
            return 0
        try:
            number = int(raw_value)
            return number if number > 0 else 0
        except Exception:
            digits = "".join(ch for ch in raw_value if ch.isdigit())
            if digits:
                try:
                    number = int(digits)
                    return number if number > 0 else 0
                except Exception:
                    return 0
        return 0

    for candidate in candidates:
        number = to_positive_int(candidate)
        if number > 0:
            return number

    def walk(value: Any) -> int:
        if isinstance(value, dict):
            # Prefer keys that are specifically about product SKU.
            for key, nested_value in value.items():
                key_norm = str(key).lower().replace("_", "")
                if key_norm in {"sku", "productsku", "skuid"}:
                    number = to_positive_int(nested_value)
                    if number > 0:
                        return number
            for nested_value in value.values():
                number = walk(nested_value)
                if number > 0:
                    return number
        elif isinstance(value, list):
            for item in value:
                number = walk(item)
                if number > 0:
                    return number
        return 0

    return walk(raw)




@app.get("/api/questions")
def list_ozon_questions(status: str | None = None, unanswered: bool = False) -> list[dict[str, Any]]:
    return repo.list_ozon_questions(status=status, unanswered=unanswered)


@app.get("/api/questions/{question_id}")
def get_ozon_question(question_id: int) -> dict[str, Any]:
    question = repo.get_ozon_question(question_id)
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


@app.post("/api/questions/sync/ozon")
async def sync_ozon_questions() -> dict[str, Any]:
    return await _sync_ozon_questions_unlocked(background=False)


@app.post("/api/questions/{question_id}/answer")
async def answer_ozon_question(question_id: int, payload: QuestionAnswerCreate) -> dict[str, Any]:
    question = repo.get_ozon_question(question_id)
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    connector = connectors.get("ozon")
    if not connector or not hasattr(connector, "answer_question"):
        raise HTTPException(status_code=500, detail="Ozon questions connector is not available")
    external_id = _pick_ozon_question_api_id(question)
    if not external_id:
        raise HTTPException(
            status_code=400,
            detail="У этого вопроса не найден Ozon question_id. Нажмите «Обновить» в разделе вопросов и попробуйте ответить на обновлённую карточку.",
        )
    sku = _pick_ozon_question_api_sku(question)
    if sku <= 0:
        raise HTTPException(
            status_code=400,
            detail="У этого вопроса не найден Ozon SKU. Нажмите «Обновить» в разделе вопросов и откройте вопрос заново. Если SKU всё равно пустой — пришлите результат /api/debug/ozon/questions.",
        )
    try:
        raw_response = await connector.answer_question(external_id, payload.text, sku=sku)  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    status_result: dict[str, Any] | None = None
    status = question.get("status")
    if payload.mark_processed and hasattr(connector, "change_question_status"):
        try:
            status_result = await connector.change_question_status([external_id], "PROCESSED")  # type: ignore[attr-defined]
            status = "PROCESSED"
        except Exception as exc:
            status_result = {"warning": str(exc)}
    updated = repo.mark_ozon_question_answered(
        question_id,
        payload.text,
        {"answer_create": raw_response, "change_status": status_result},
        status=status,
    )
    return {"ok": True, "question": updated, "marketplace_response": raw_response, "status_response": status_result}


@app.get("/api/tasks")
def list_tasks(
    request: Request,
    status: str | None = None,
    bucket: str | None = None,
    mine: bool = False,
    q: str | None = None,
    task_type_id: int | None = None,
    due_date: str | None = None,
) -> list[dict[str, Any]]:
    user = _current_user(request)
    assigned_user_id = int(user["id"]) if mine else None
    return repo.list_tasks(
        status=status,
        bucket=bucket,
        assigned_user_id=assigned_user_id,
        q=q,
        task_type_id=task_type_id,
        due_date=due_date,
    )


app.include_router(create_task_types_router(repo, _current_user, _require_admin))



@app.get("/api/debug/ozon/visibility")
async def debug_ozon_visibility() -> dict[str, Any]:
    """Compare what Ozon returns with what is visible in the CRM list.

    This helps diagnose cases when the API returns chats but the UI does not show
    them because of local archive/status filters or old over-aggressive support
    filters from previous versions.
    """
    connector = connectors.get("ozon")
    api_summary: dict[str, Any] = {"configured": False}
    if connector and getattr(connector, "client_id", "") and getattr(connector, "api_key", ""):
        api_summary["configured"] = True
        try:
            with _temporary_connector_overrides(
                connector,
                {
                    "sync_max_chats": _env_int("OZON_DEBUG_VISIBILITY_MAX_CHATS", 100, minimum=1, maximum=1000),
                    "sync_pages_per_variant": _env_int("OZON_DEBUG_VISIBILITY_PAGES", 2, minimum=1, maximum=10),
                    "sync_variant_mode": "full",
                },
            ):
                api_chats = await connector.list_chats()
            api_summary.update(
                {
                    "api_customer_chats_count": len(api_chats),
                    "api_sample_external_ids": [c.external_chat_id for c in api_chats[:30]],
                    "connector_debug": getattr(connector, "last_sync_debug", {}),
                }
            )
        except Exception as exc:
            api_summary.update({"error": str(exc)})

    with get_connection() as conn:
        local_total = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE marketplace='ozon'").fetchone()["c"]
        visible_active = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE marketplace='ozon' AND status != 'closed'").fetchone()["c"]
        visible_archive = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE marketplace='ozon' AND status = 'closed'").fetchone()["c"]
        by_status = [dict(r) for r in conn.execute("SELECT status, COUNT(*) AS count FROM chats WHERE marketplace='ozon' GROUP BY status").fetchall()]
        last_rows = [dict(r) for r in conn.execute(
            """
            SELECT id, external_chat_id, customer_name, status, last_message_at, last_message_preview
            FROM chats
            WHERE marketplace='ozon'
            ORDER BY datetime(COALESCE(last_message_at, updated_at, created_at)) DESC, id DESC
            LIMIT 30
            """
        ).fetchall()]
    return {
        "api": api_summary,
        "local": {
            "ozon_total_in_db": local_total,
            "ozon_active_visible_by_status_rule": visible_active,
            "ozon_archive_closed": visible_archive,
            "by_status": by_status,
            "latest_local_sample": last_rows,
        },
        "hint": "Если api_customer_chats_count больше, чем local/visible — запустите POST /api/sync/ozon. В v40 ручная синхронизация берёт больше страниц Ozon.",
    }


app.include_router(create_chat_settings_router(repo, _require_admin))


@app.get("/api/chats")
def list_chats(
    request: Request,
    status: str | None = None,
    marketplace: str | None = None,
    archived: bool = False,
    mine: bool = False,
    funnel_id: int | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    user = _current_user(request)
    assigned_user_id = int(user["id"]) if mine else None
    return repo.list_chats(
        status=status,
        marketplace=marketplace,
        archived=archived,
        assigned_user_id=assigned_user_id,
        funnel_id=funnel_id,
        q=q,
    )


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: int, messages_limit: int = 120) -> dict[str, Any]:
    safe_limit = max(20, min(int(messages_limit or 120), 500))
    chat = repo.get_chat(chat_id, messages_limit=safe_limit)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat



app.include_router(create_notifications_router(repo, _current_user))


@app.get("/api/push/public-key")
def api_push_public_key(request: Request) -> dict[str, Any]:
    _current_user(request)
    public_key = _web_push_public_key()
    return {
        "enabled": _web_push_enabled(),
        "configured": bool(public_key and _web_push_private_key()),
        "public_key": public_key,
        "subject": _web_push_subject(),
        "desktop_supported": True,
        "mobile_supported": True,
    }


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    payload = await request.json()
    subscription = payload.get("subscription") if isinstance(payload, dict) and isinstance(payload.get("subscription"), dict) else payload
    if not isinstance(subscription, dict) or not subscription.get("endpoint"):
        raise HTTPException(status_code=400, detail="Некорректная push-подписка")
    try:
        saved = repo.save_push_subscription(
            int(user["id"]),
            subscription,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "subscription_id": saved.get("id"), "configured": _web_push_configured()}


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    payload = await request.json()
    endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
    count = repo.delete_push_subscription(int(user["id"]), endpoint=endpoint)
    return {"ok": True, "disabled": count}


@app.post("/api/push/test")
async def api_push_test(request: Request) -> dict[str, Any]:
    _rate_limit(
        request,
        "push-test",
        limit=_security_env_int("CRM_PUSH_TEST_RATE_LIMIT", 3, minimum=1, maximum=60),
        window_seconds=_security_env_int("CRM_PUSH_TEST_RATE_WINDOW_SECONDS", 60, minimum=30, maximum=3600),
    )
    user = _current_user(request)
    payload = {
        "title": "Тестовое уведомление Arti CRM",
        "body": "Если вы видите это уведомление, Web Push работает на этом устройстве.",
        "tag": f"arti-crm-test-{int(time.time())}",
        "url": "/#/chats",
        "type": "test",
    }
    result = await _send_web_push_to_user(int(user["id"]), payload, base_url=_public_base_url(request))
    return {"ok": bool(result.get("sent")), "configured": _web_push_configured(), **result}


@app.get("/api/push/status")
def api_push_status(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    storage: dict[str, Any] = {"ok": False}
    subscriptions: list[dict[str, Any]] = []
    storage_error = ""
    try:
        storage = repo.ensure_push_storage()
        subscriptions = repo.list_push_subscriptions(int(user["id"]), active_only=True)
    except Exception as exc:
        # Do not hide the problem behind a plain 500. The status page is the
        # diagnostic page, so it must return the exact storage error.
        storage_error = str(exc)

    return {
        "ok": not bool(storage_error),
        "enabled": _web_push_enabled(),
        "configured": _web_push_configured(),
        "public_key_present": bool(_web_push_public_key()),
        "private_key_present": bool(_web_push_private_key()),
        "subject": _web_push_subject(),
        "subscriptions": len(subscriptions),
        "storage": storage,
        "storage_error": storage_error,
        "last_push_outbox": getattr(app.state, "last_push_outbox", None),
        "last_background_sync": getattr(app.state, "last_background_sync", None),
        "last_external_background_tick": getattr(app.state, "last_external_background_tick", None),
        "background_tick_token_configured": bool(_background_tick_token()),
    }


@app.post("/api/background/tick")
async def api_background_tick(request: Request) -> dict[str, Any]:
    """External cron/uptime monitor endpoint.

    On shared hosting, the Python process and browser JS timers can sleep when
    nobody has the CRM open. This endpoint lets an external scheduler wake the
    server, poll marketplaces, and drain Web Push outbox without opening CRM.
    """
    _rate_limit(
        request,
        "background-tick",
        limit=_security_env_int("CRM_BACKGROUND_TICK_RATE_LIMIT", 60, minimum=1, maximum=1000),
        window_seconds=_security_env_int("CRM_BACKGROUND_TICK_RATE_WINDOW_SECONDS", 3600, minimum=60, maximum=86400),
    )
    _require_background_tick_access(request)
    return await _run_background_tick_once(source="api")


@app.patch("/api/chats/{chat_id}")
def update_chat(chat_id: int, payload: ChatUpdate, request: Request) -> dict[str, Any]:
    current_user = _current_user(request)
    before = repo.get_chat_summary(chat_id)
    chat = repo.update_chat(chat_id, payload)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    old_assignee = (before or {}).get("assigned_user_id")
    new_assignee = chat.get("assigned_user_id")
    if new_assignee and new_assignee != old_assignee:
        actor = current_user.get("display_name") or current_user.get("username") or "CRM"
        customer = chat.get("customer_name") or chat.get("customer_public_id") or chat.get("external_chat_id") or "чат"
        repo.create_notification(
            user_id=int(new_assignee),
            type="assigned_chat",
            title="Вам назначили чат",
            body=f"{actor} назначил(а) вас ответственным за {customer}",
            chat_id=chat_id,
            entity_type="chat",
            entity_id=str(chat_id),
            dedupe_key=f"assigned-chat:{chat_id}:{new_assignee}:{chat.get('updated_at')}",
            metadata={"assigned_by_user_id": current_user.get("id"), "old_assigned_user_id": old_assignee},
        )
    return chat




@app.post("/api/chats/{chat_id}/ai-reply")
async def ai_reply(chat_id: int, payload: AiReplyCreate) -> dict[str, Any]:
    chat = repo.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    selected_message = next((m for m in chat.get("messages", []) if int(m.get("id")) == int(payload.message_id)), None)
    if not selected_message:
        raise HTTPException(status_code=404, detail="Selected message not found")

    draft = await _generate_ai_reply(chat, selected_message, payload.extra_instruction)
    return {
        "ok": True,
        "draft": draft,
        "selected_message_id": payload.message_id,
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    }



def _safe_chat_image_extension(upload: UploadFile) -> str:
    raw_name = Path(upload.filename or "image").name
    ext = Path(raw_name).suffix.lower()
    content_type = (upload.content_type or "").lower()
    if ext not in ALLOWED_CHAT_IMAGE_EXTENSIONS:
        if content_type == "image/png":
            ext = ".png"
        elif content_type in {"image/jpeg", "image/jpg"}:
            ext = ".jpg"
        elif content_type == "image/webp":
            ext = ".webp"
        elif content_type == "image/gif":
            ext = ".gif"
    if ext not in ALLOWED_CHAT_IMAGE_EXTENSIONS or not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Можно прикреплять только изображения JPG, PNG, WEBP или GIF")
    return ext


async def _read_chat_image(upload: UploadFile) -> tuple[str, bytes]:
    ext = _safe_chat_image_extension(upload)
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Файл изображения пустой")
    if len(data) > MAX_CHAT_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Изображение слишком большое")
    return ext, data


def _store_chat_image(upload: UploadFile, chat_id: int, ext: str, data: bytes) -> dict[str, Any]:
    CHAT_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"chat_{int(chat_id)}_{uuid.uuid4().hex}{ext}"
    path = (CHAT_ATTACHMENTS_DIR / filename).resolve()
    if CHAT_ATTACHMENTS_DIR not in path.parents and path != CHAT_ATTACHMENTS_DIR:
        raise HTTPException(status_code=400, detail="Некорректное имя файла")
    path.write_bytes(data)
    return {
        "filename": filename,
        "original_filename": Path(upload.filename or filename).name,
        "content_type": upload.content_type or "image/*",
        "size_bytes": len(data),
        "url": f"/api/chat-uploads/{filename}",
    }


async def _save_chat_image(upload: UploadFile, chat_id: int) -> dict[str, Any]:
    ext, data = await _read_chat_image(upload)
    return _store_chat_image(upload, chat_id, ext, data)


@app.get("/api/chat-uploads/{filename}")
def api_chat_upload(filename: str, request: Request) -> FileResponse:
    _current_user(request)
    safe_name = Path(filename).name
    if safe_name != filename:
        raise HTTPException(status_code=404, detail="File not found")
    path = (CHAT_ATTACHMENTS_DIR / safe_name).resolve()
    if CHAT_ATTACHMENTS_DIR not in path.parents or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@app.post("/api/chats/{chat_id}/attachments")
async def add_chat_attachments(
    chat_id: int,
    request: Request,
    images: list[UploadFile] = File(...),
    caption: str = Form(default=""),
) -> dict[str, Any]:
    chat = repo.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not images:
        raise HTTPException(status_code=400, detail="Выберите изображение")
    if len(images) > 5:
        raise HTTPException(status_code=400, detail="Можно прикрепить до 5 изображений за раз")

    current_user = _current_user(request)
    current_user_id = int(current_user.get("id") or 0)
    author = (current_user.get("display_name") or current_user.get("username") or "manager").strip()

    prepared: list[dict[str, Any]] = []
    for upload in images:
        ext, data = await _read_chat_image(upload)
        prepared.append({
            "upload": upload,
            "ext": ext,
            "data": data,
            "filename": Path(upload.filename or f"image{ext}").name or f"image{ext}",
            "content_type": upload.content_type or "image/*",
        })

    marketplace = chat["marketplace"]
    connector = connectors.get(marketplace) or connectors["mock"]
    caption_text = (caption or "").strip()
    marketplace_responses: list[dict[str, Any]] = []

    try:
        if chat.get("metadata", {}).get("source") == "mock" or marketplace == "mock":
            # Demo/mock chats do not have a real marketplace API. Keep local mode there.
            attachments = [
                _store_chat_image(item["upload"], chat_id, item["ext"], item["data"])
                for item in prepared
            ]
            image_lines = [f"![Изображение]({item['url']})" for item in attachments]
            text = "\n".join([part for part in [caption_text, *image_lines] if part]).strip() or "[изображение]"
            message_id = repo.add_message(
                chat_id=chat_id,
                direction="outbound",
                text=text,
                author=author,
                external_message_id=f"local-image:{uuid.uuid4().hex}",
                raw={"_crm_local_attachment": True, "attachments": attachments},
            )
            return {"ok": True, "message_id": message_id, "attachments": attachments, "chat": repo.get_chat(chat_id)}

        if not hasattr(connector, "send_file"):
            raise HTTPException(status_code=400, detail=f"Отправка изображений в {marketplace} пока не поддержана")

        if marketplace == "wildberries":
            # WB Buyers Chat public method in the current connector supports text replies only.
            raise HTTPException(status_code=400, detail="WB Buyers Chat API сейчас поддерживает отправку текста из CRM. Для фото нужен отдельный подтверждённый метод WB загрузки/отправки файлов.")

        if caption_text:
            if marketplace == "wildberries" and hasattr(connector, "set_reply_sign_from_metadata"):
                connector.set_reply_sign_from_metadata(chat["external_chat_id"], chat.get("metadata") or {})  # type: ignore[attr-defined]
            caption_response = await connector.send_message(chat["external_chat_id"], caption_text)
            marketplace_responses.append({"type": "text", "response": caption_response})

        for item in prepared:
            try:
                file_response = await connector.send_file(
                    chat["external_chat_id"],
                    filename=item["filename"],
                    content=item["data"],
                    content_type=item["content_type"],
                )
            except NotImplementedError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            marketplace_responses.append({"type": "file", "filename": item["filename"], "response": file_response})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    attachments = [
        _store_chat_image(item["upload"], chat_id, item["ext"], item["data"])
        for item in prepared
    ]
    image_lines = [f"![Изображение]({item['url']})" for item in attachments]
    text = "\n".join([part for part in [caption_text, *image_lines] if part]).strip() or "[изображение]"
    external_ids = [
        _trusted_marketplace_message_id(entry.get("response"))
        for entry in marketplace_responses
        if isinstance(entry.get("response"), dict)
    ]
    external_message_id = ";".join([value for value in external_ids if value]) or f"local-image:{uuid.uuid4().hex}"
    message_id = repo.add_message(
        chat_id=chat_id,
        direction="outbound",
        text=text,
        author=author,
        external_message_id=external_message_id,
        raw={
            "_crm_marketplace_attachment_sent": True,
            "_crm_sent_from_crm": True,
            "_crm_sent_by_label": author,
            "_crm_sent_by_user_id": current_user_id,
            "attachments": attachments,
            "marketplace_responses": marketplace_responses,
        },
    )
    assigned_on_send = False
    if current_user_id and _env_bool("CRM_AUTO_ASSIGN_FIRST_RESPONSE", True):
        assigned_on_send = repo.assign_chat_to_user_if_unassigned(
            chat_id=chat_id,
            user_id=current_user_id,
            reason="first_crm_attachment_reply",
        )
    return {"ok": True, "message_id": message_id, "attachments": attachments, "chat": repo.get_chat(chat_id), "assigned_on_send": assigned_on_send}


@app.post("/api/chats/{chat_id}/messages")
async def send_message(chat_id: int, payload: MessageCreate, request: Request) -> dict[str, Any]:
    chat = repo.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # v88: the first CRM employee who replies to an unassigned chat becomes the
    # responsible manager. This moves the chat into that employee's "Мои чаты"
    # tab and updates the assignee selector in the opened dialog.
    current_user = _current_user(request)
    current_user_id = int(current_user.get("id") or 0)
    current_user_label = (current_user.get("display_name") or current_user.get("username") or "").strip()
    outbound_author = (payload.author or "").strip()
    if not outbound_author or outbound_author.lower() in {"manager", "менеджер", "operator", "оператор"}:
        outbound_author = current_user_label or outbound_author or "manager"

    marketplace = chat["marketplace"]
    connector = connectors.get(marketplace) or connectors["mock"]

    try:
        if chat.get("metadata", {}).get("source") == "mock" or marketplace == "mock":
            raw_response = await connectors["mock"].send_message(chat["external_chat_id"], payload.text)
        else:
            if marketplace == "wildberries" and hasattr(connector, "set_reply_sign_from_metadata"):
                connector.set_reply_sign_from_metadata(chat["external_chat_id"], chat.get("metadata") or {})  # type: ignore[attr-defined]
            raw_response = await connector.send_message(chat["external_chat_id"], payload.text)
    except Exception as exc:
        # Не сохраняем сообщение как отправленное и не назначаем ответственного,
        # если маркетплейс не принял ответ.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    message_id = repo.add_message(
        chat_id=chat_id,
        direction="outbound",
        text=payload.text,
        author=outbound_author,
        external_message_id=_trusted_marketplace_message_id(raw_response),
        raw=_mark_crm_sent_raw(raw_response, author=outbound_author, user_id=current_user_id),
    )
    assigned_on_send = False
    if current_user_id and _env_bool("CRM_AUTO_ASSIGN_FIRST_RESPONSE", True):
        assigned_on_send = repo.assign_chat_to_user_if_unassigned(
            chat_id=chat_id,
            user_id=current_user_id,
            reason="first_crm_reply",
        )
    updated_chat = repo.get_chat(chat_id)
    return {
        "ok": True,
        "message_id": message_id,
        "marketplace_response": raw_response,
        "chat": updated_chat,
        "assigned_on_send": assigned_on_send,
    }


@app.post("/api/chats/{chat_id}/notes")
def add_internal_note(chat_id: int, payload: InternalNoteCreate) -> dict[str, Any]:
    chat = repo.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    message_id = repo.add_message(
        chat_id=chat_id,
        direction="internal",
        text=payload.text,
        author=payload.author,
        raw={"internal": True},
    )
    return {"message_id": message_id, "chat": repo.get_chat(chat_id)}


@app.patch("/api/chats/{chat_id}/notes/{message_id}")
def update_internal_note(chat_id: int, message_id: int, payload: InternalNoteUpdate) -> dict[str, Any]:
    chat = repo.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    message = repo.update_internal_note(chat_id, message_id, payload.text)
    if not message:
        raise HTTPException(status_code=404, detail="Internal note not found")
    return {"message": message, "chat": repo.get_chat(chat_id)}


@app.delete("/api/chats/{chat_id}/notes/{message_id}")
def delete_internal_note(chat_id: int, message_id: int) -> dict[str, Any]:
    chat = repo.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    deleted = repo.delete_internal_note(chat_id, message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Internal note not found")
    return {"ok": True, "chat": repo.get_chat(chat_id)}


@app.post("/api/tasks")
def create_task(payload: TaskCreate, request: Request) -> dict[str, Any]:
    current_user = _current_user(request)
    chat = repo.get_chat(payload.chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    task_id = repo.create_task(payload)
    task = repo.get_task(task_id)
    assert task is not None
    assigned_user_id = task.get("assigned_user_id")
    if assigned_user_id:
        actor = current_user.get("display_name") or current_user.get("username") or "CRM"
        repo.create_notification(
            user_id=int(assigned_user_id),
            type="new_task",
            title="Новая задача",
            body=f"{actor}: {task.get('title') or 'Задача'}",
            chat_id=int(task.get("chat_id") or payload.chat_id),
            task_id=int(task_id),
            entity_type="task",
            entity_id=str(task_id),
            dedupe_key=f"task-created:{task_id}:user:{assigned_user_id}",
            metadata={"created_by_user_id": current_user.get("id")},
        )
    return task


@app.post("/api/tasks/standalone")
def create_standalone_task(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    current_user = _current_user(request)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Task title is required")

    def _optional_int(value: Any) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    assigned_user_id = _optional_int(payload.get("assigned_user_id"))
    task_type_id = _optional_int(payload.get("task_type_id"))
    due_at = str(payload.get("due_at") or "").strip() or None
    description = str(payload.get("description") or "").strip() or None

    try:
        task_id = repo.create_standalone_task(
            title=title,
            description=description,
            task_type_id=task_type_id,
            assigned_user_id=assigned_user_id,
            due_at=due_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=500, detail="Task was created but could not be loaded")

    if assigned_user_id:
        actor = current_user.get("display_name") or current_user.get("username") or "CRM"
        repo.create_notification(
            user_id=int(assigned_user_id),
            type="new_task",
            title="Новая задача",
            body=f"{actor}: {task.get('title') or 'Задача'}",
            chat_id=None,
            task_id=int(task_id),
            entity_type="task",
            entity_id=str(task_id),
            dedupe_key=f"task-created:{task_id}:user:{assigned_user_id}",
            metadata={"created_by_user_id": current_user.get("id"), "standalone": True},
        )
    return task


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    if payload.comment and not payload.comment_author:
        payload.comment_author = user.get("display_name") or user.get("username") or "manager"
    task = repo.update_task(task_id, payload)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _delete_task_or_404(task_id: int, request: Request) -> dict[str, Any]:
    _current_user(request)
    try:
        ok = repo.delete_task(task_id)
    except Exception as exc:
        # Keep the client error readable instead of leaking a raw server traceback.
        raise HTTPException(status_code=500, detail=f"Task delete failed: {_mask_sensitive(str(exc))}") from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, request: Request) -> dict[str, Any]:
    return _delete_task_or_404(task_id, request)


@app.post("/api/tasks/{task_id}/delete")
def delete_task_post(task_id: int, request: Request) -> dict[str, Any]:
    # POST fallback is safer on some hosting/proxy setups where DELETE can be blocked.
    return _delete_task_or_404(task_id, request)


@app.get("/api/knowledge/categories")
def api_knowledge_categories(request: Request) -> list[dict[str, Any]]:
    _current_user(request)
    return repo.list_knowledge_categories()


@app.post("/api/knowledge/categories")
def api_create_knowledge_category(payload: KnowledgeCategoryCreate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    return repo.create_knowledge_category(payload.title, payload.description, payload.sort_order)


@app.get("/api/knowledge/articles")
def api_knowledge_articles(request: Request, category_id: int | None = None, q: str | None = None) -> list[dict[str, Any]]:
    _current_user(request)
    return [
        _knowledge_article_response(article)
        for article in repo.list_knowledge_articles(category_id=category_id, q=q)
    ]


def _knowledge_article_response(article: dict[str, Any]) -> dict[str, Any]:
    response = dict(article)
    response["image_url"] = (
        article_knowledge_image_url(int(article["id"]))
        if article.get("image_url")
        else None
    )
    return response


@app.get("/api/knowledge/articles/{article_id}")
def api_get_knowledge_article(article_id: int, request: Request) -> dict[str, Any]:
    _current_user(request)
    article = repo.get_knowledge_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return _knowledge_article_response(article)


@app.get("/api/knowledge/articles/{article_id}/image")
def api_get_knowledge_image(article_id: int, request: Request) -> FileResponse:
    _current_user(request)
    article = repo.get_knowledge_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Image not found")
    path = resolve_article_image_reference(
        article.get("image_url"),
        legacy_root=STATIC_DIR / "uploads" / "knowledge",
        private_root=KNOWLEDGE_IMAGES_DIR,
        public_static_root=STATIC_DIR,
    )
    media_type = knowledge_image_media_type(path) if path is not None else None
    if path is None or media_type is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _read_knowledge_image_upload_chunk(file: UploadFile) -> bytes:
    return await file.read(1024 * 1024)


def _write_knowledge_image_chunk(output: Any, chunk: bytes) -> None:
    output.write(chunk)


def _knowledge_image_persisted_size(path: Path) -> int:
    return path.stat().st_size


def _delete_new_knowledge_image(path: Path) -> None:
    path.unlink()


def _add_exception_note_best_effort(error: BaseException, note: str) -> None:
    try:
        error.add_note(note)
    except BaseException:
        return


def _cleanup_new_knowledge_image(path: Path, original_error: BaseException) -> None:
    try:
        _delete_new_knowledge_image(path)
    except BaseException as cleanup_error:
        _add_exception_note_best_effort(
            original_error,
            "Knowledge image cleanup failed: "
            f"{cleanup_error.__class__.__name__}",
        )


@app.post("/api/knowledge/articles/{article_id}/image")
async def api_upload_knowledge_image(article_id: int, request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    user = _require_admin(request)
    existing_article = repo.get_knowledge_article(article_id)
    if not existing_article:
        raise HTTPException(status_code=404, detail="Article not found")

    size = 0
    while True:
        chunk = await _read_knowledge_image_upload_chunk(file)
        if not chunk:
            break
        size += len(chunk)
        if size > 8 * 1024 * 1024:
            await file.seek(0)
            raise HTTPException(status_code=413, detail="Изображение слишком большое. Максимум 8 МБ")
    await file.seek(0)
    if size == 0:
        raise HTTPException(status_code=400, detail="Invalid knowledge image")
    try:
        validated = validate_knowledge_image_upload(
            file.file,
            original_filename=file.filename,
            content_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid knowledge image") from exc
    await file.seek(0)

    uploads_dir = private_storage_root(KNOWLEDGE_IMAGES_DIR, STATIC_DIR)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{validated.canonical_extension}"
    path = resolve_knowledge_image_path(uploads_dir, filename)
    image_reference = private_knowledge_image_reference(filename)
    if path is None or image_reference is None:
        raise HTTPException(status_code=500, detail="Image storage error")

    created_file = False
    persisted_size = 0
    try:
        with path.open("xb") as out:
            created_file = True
            while True:
                chunk = await _read_knowledge_image_upload_chunk(file)
                if not chunk:
                    break
                persisted_size += len(chunk)
                _write_knowledge_image_chunk(out, chunk)
        if persisted_size != size or _knowledge_image_persisted_size(path) != size:
            raise OSError("Knowledge image persisted size mismatch")
    except BaseException as exc:
        if created_file:
            _cleanup_new_knowledge_image(path, exc)
        if isinstance(exc, OSError):
            raise HTTPException(status_code=500, detail="Image storage error") from exc
        raise

    try:
        article = repo.set_knowledge_article_image_reference(
            article_id,
            image_reference,
            int(user["id"]),
        )
    except BaseException as exc:
        cleanup_new_file = False
        try:
            current_article = repo.get_knowledge_article(article_id)
            cleanup_new_file = not current_article or current_article.get("image_url") != image_reference
        except BaseException as reconciliation_error:
            _add_exception_note_best_effort(
                exc,
                "Knowledge image database reconciliation failed: "
                f"{reconciliation_error.__class__.__name__}",
            )
        if cleanup_new_file:
            _cleanup_new_knowledge_image(path, exc)
        raise
    if not article:
        error = HTTPException(status_code=404, detail="Article not found")
        _cleanup_new_knowledge_image(path, error)
        raise error
    return _knowledge_article_response(article)


@app.delete("/api/knowledge/articles/{article_id}/image")
def api_delete_knowledge_image(article_id: int, request: Request) -> dict[str, Any]:
    user = _require_admin(request)
    article = repo.clear_knowledge_article_image_reference(article_id, int(user["id"]))
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return _knowledge_article_response(article)


@app.post("/api/knowledge/articles")
def api_create_knowledge_article(payload: KnowledgeArticleCreate, request: Request) -> dict[str, Any]:
    user = _require_admin(request)
    article = repo.create_knowledge_article(category_id=payload.category_id, title=payload.title, content=payload.content, tags=payload.tags, is_published=payload.is_published, user_id=int(user["id"]))
    return _knowledge_article_response(article)


@app.patch("/api/knowledge/articles/{article_id}")
def api_update_knowledge_article(article_id: int, payload: KnowledgeArticleUpdate, request: Request) -> dict[str, Any]:
    user = _require_admin(request)
    article = repo.update_knowledge_article(
        article_id,
        category_id=payload.category_id,
        title=payload.title,
        content=payload.content,
        tags=payload.tags,
        is_published=payload.is_published,
        user_id=int(user["id"]),
    )
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return _knowledge_article_response(article)


app.include_router(create_reply_templates_router(repo, _current_user, _require_admin))


def _frontend_operator_sync_lock() -> asyncio.Lock:
    lock: asyncio.Lock = getattr(app.state, "frontend_operator_sync_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.frontend_operator_sync_lock = lock
    return lock


async def _run_frontend_operator_sync_task() -> dict[str, Any]:
    lock = _frontend_operator_sync_lock()
    if lock.locked():
        return {
            "ok": True,
            "mode": "operator_frontend_async",
            "status": "already_running",
            "background": True,
            "last": getattr(app.state, "last_frontend_operator_sync", None),
        }

    started_at = time.time()
    async with lock:
        try:
            payload = await _sync_operator_frontend_unlocked()
            payload["async_status"] = "finished"
            payload["duration_seconds"] = round(time.time() - started_at, 2)
            app.state.last_frontend_operator_sync = payload
            return payload
        except Exception as exc:
            payload = {
                "ok": False,
                "mode": "operator_frontend_async",
                "async_status": "error",
                "duration_seconds": round(time.time() - started_at, 2),
                "error": str(exc),
                "background": True,
            }
            app.state.last_frontend_operator_sync = payload
            return payload


@app.post("/api/sync/operator")
async def sync_operator_frontend(wait: bool = False) -> dict[str, Any]:
    # Default mode is async: the browser should not wait 20-30 seconds for
    # marketplace APIs. It starts sync and keeps refreshing local DB separately.
    if wait:
        return await _run_frontend_operator_sync_task()

    task: asyncio.Task | None = getattr(app.state, "frontend_operator_sync_task", None)
    if task and not task.done():
        return {
            "ok": True,
            "mode": "operator_frontend_async",
            "status": "running",
            "queued": False,
            "background": True,
            "last": getattr(app.state, "last_frontend_operator_sync", None),
        }

    task = asyncio.create_task(_run_frontend_operator_sync_task())
    app.state.frontend_operator_sync_task = task

    return {
        "ok": True,
        "mode": "operator_frontend_async",
        "status": "started",
        "queued": True,
        "background": True,
        "last": getattr(app.state, "last_frontend_operator_sync", None),
    }


@app.post("/api/sync/{marketplace}")
async def sync_marketplace(marketplace: str) -> dict[str, Any]:
    # Endpoint оставлен для проверки через /docs или curl, но в интерфейсе кнопки нет:
    # Ozon синхронизируется фоново.
    return await _sync_marketplace_locked(marketplace)


@app.post("/api/webhooks/yandex")
async def yandex_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    notification_type = payload.get("notificationType")
    external_id = str(payload.get("chatId") or payload.get("messageId") or "")
    event_id = repo.log_webhook_event("yandex", notification_type, external_id, payload)

    if notification_type == "PING":
        return {"status": "OK"}

    # В production здесь нужно по chatId/messageId сходить в Yandex Market API,
    # загрузить актуальный чат/сообщение и сохранить нормализованные данные.
    return {"status": "OK", "event_id": event_id, "todo": "fetch chat/message by id and upsert"}
