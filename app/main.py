from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import repository as repo
from app.services.analytics import build_chat_analytics, build_chat_analytics_drilldown
from app.connectors.mock import MockConnector
from app.connectors.ozon import OzonConnector
from app.connectors.wildberries import WildberriesConnector
from app.connectors.yandex_market import YandexMarketConnector
from app.db import get_connection, init_db
from app.schemas import AiReplyCreate, ChatCreate, ChatUpdate, InternalNoteCreate, InternalNoteUpdate, LoginCreate, MessageCreate, ReviewReplyCreate, QuestionAnswerCreate, TaskCreate, TaskUpdate, UserCreate, UserPasswordUpdate, UserUpdate, ProfileUpdate, KnowledgeCategoryCreate, KnowledgeArticleCreate, KnowledgeArticleUpdate, ChatFunnelCreate, ChatFunnelUpdate, ChatStatusCreate, ChatStatusUpdate, ReplyTemplateCreate, TaskTypeCreate, TaskTypeUpdate

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CHAT_ATTACHMENTS_DIR = Path(os.getenv("CRM_CHAT_ATTACHMENTS_DIR", str(Path.cwd() / "chat_attachments"))).resolve()
MAX_CHAT_IMAGE_BYTES = int(os.getenv("CRM_MAX_CHAT_IMAGE_MB", "12")) * 1024 * 1024
ALLOWED_CHAT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

CRM_BUILD_VERSION = "v103_analytics_ui_polish_2026-06-18"

app = FastAPI(title="Arti CRM", version="1.0.3")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

AUTH_COOKIE_NAME = "arti_crm_session"
AUTH_DISABLED = os.getenv("CRM_AUTH_DISABLED", "0").strip().lower() in {"1", "true", "yes", "on", "да"}


def _auth_public_path(path: str) -> bool:
    return (
        path == "/"
        or path == "/health"
        or path.startswith("/static/")
        or path in {"/api/auth/login", "/api/auth/me"}
    )


@app.middleware("http")
async def require_auth_for_api(request: Request, call_next):
    if AUTH_DISABLED or not request.url.path.startswith("/api/") or _auth_public_path(request.url.path):
        return await call_next(request)
    token = request.cookies.get(AUTH_COOKIE_NAME)
    user = repo.get_user_by_session(token)
    if not user:
        return Response(
            content=json.dumps({"detail": "Требуется авторизация"}, ensure_ascii=False),
            status_code=401,
            media_type="application/json",
        )
    request.state.user = user
    return await call_next(request)


@app.middleware("http")
async def add_fastfox_cache_headers(request: Request, call_next):
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
    return response


def _current_user(request: Request) -> dict[str, Any]:
    if AUTH_DISABLED:
        return {"id": 0, "username": "local", "display_name": "Local", "role": "admin", "is_active": True}
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    return user


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
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def _system_sender_matches(value: Any, markers: tuple[str, ...]) -> bool:
    normalized = _normalize_system_sender(value)
    if not normalized:
        return False
    normalized_markers = {_normalize_system_sender(marker) for marker in markers if marker}
    return normalized in normalized_markers


def _extract_sender_designations(value: Any, depth: int = 0) -> list[str]:
    """Extract sender/user names only; do not scan arbitrary raw text."""
    if depth > 4 or value in (None, ""):
        return []
    values: list[str] = []
    name_keys = {
        "name", "login", "username", "user_name", "display_name", "displayname",
        "nickname", "author_name", "authorname", "sender_name", "sendername",
        "from_name", "fromname", "system_name", "systemname",
    }
    container_keys = {"user", "author", "sender", "from", "participant", "profile"}
    if isinstance(value, dict):
        for key, nested in value.items():
            key_l = str(key).lower()
            if key_l in name_keys and isinstance(nested, (str, int, float)):
                values.append(str(nested))
            elif key_l in container_keys:
                if isinstance(nested, (str, int, float)):
                    values.append(str(nested))
                elif isinstance(nested, dict):
                    values.extend(_extract_sender_designations(nested, depth + 1))
    elif isinstance(value, list):
        for item in value[:20]:
            values.extend(_extract_sender_designations(item, depth + 1))
    return values


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
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False

    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass

    raw_allowed = os.getenv(
        "IMAGE_PROXY_ALLOWED_HOSTS",
        "ozon.ru,ozone.ru,ozonusercontent.com,cdn.ngenix.net,o3static.com,o3.ru",
    )
    allowed = [item.strip().lower() for item in raw_allowed.split(",") if item.strip()]
    return any(host == domain or host.endswith("." + domain) or domain in host for domain in allowed)


def _asset_proxy_headers(url: str) -> dict[str, str]:
    headers = {"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}
    host = (urlparse(url).hostname or "").lower()
    if "ozon" in host or "ozone" in host or "o3" in host:
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

    connector.sync_max_chats = _env_int("OZON_FAST_SYNC_MAX_CHATS", 300, minimum=20, maximum=1000)
    connector.sync_pages_per_variant = _env_int("OZON_FAST_SYNC_PAGES_PER_VARIANT", 3, minimum=1, maximum=20)
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

    concurrency = _env_int("OZON_FAST_MESSAGE_FETCH_CONCURRENCY", 10, minimum=1, maximum=20)
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


@app.get("/api/debug/ozon/fast-sync")
@app.post("/api/debug/ozon/fast-sync")
async def debug_ozon_fast_sync() -> dict[str, Any]:
    """Run the lightweight Ozon new/recent chats sync once."""
    return await _sync_ozon_fast_inbox_unlocked(background=False)


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

    concurrency = _env_int(
        "MARKETPLACE_MESSAGE_FETCH_CONCURRENCY",
        8 if background else 6,
        minimum=1,
        maximum=20,
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
    lock: asyncio.Lock = app.state.sync_lock
    async with lock:
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
                result = await _sync_ozon_fast_inbox_unlocked(background=True)
            else:
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
        return _env_int("OZON_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS", 20, minimum=5, maximum=3600)
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
                    result = await _sync_ozon_fast_inbox_unlocked(background=True)
                else:
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

        app.state.last_background_sync = {
            "enabled": True,
            "interval_seconds": interval,
            "status": "ok" if all(v.get("status") in {"ok", "skipped", "throttled", "cooldown"} or v.get("enabled") is False for v in per_marketplace.values()) else "partial_error",
            "marketplaces": per_marketplace,
        }
        await asyncio.sleep(interval)


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    admin_user = os.getenv("CRM_ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("CRM_ADMIN_PASSWORD", "admin123")
    created_admin = repo.ensure_initial_admin(admin_user, admin_pass, os.getenv("CRM_ADMIN_DISPLAY_NAME", "Администратор"))
    if created_admin:
        print(f"[Arti CRM] Created initial admin user: {created_admin.get('username')}. Change CRM_ADMIN_PASSWORD after first login.")
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
    app.state.frontend_operator_sync_last_poll_at = {}
    app.state.last_frontend_operator_sync = {}
    _ensure_wb_events_auto_plan_from_env()
    app.state.background_sync_task = asyncio.create_task(_background_sync_loop())
    app.state.wb_events_import_planner_task = asyncio.create_task(_wb_events_import_planner_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task_name in ("background_sync_task", "wb_events_import_planner_task"):
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




@app.post("/api/auth/login")
def auth_login(payload: LoginCreate, request: Request, response: Response) -> dict[str, Any]:
    user = repo.authenticate_user(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = repo.create_session(
        int(user["id"]),
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=os.getenv("CRM_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"},
        max_age=14 * 24 * 60 * 60,
        path="/",
    )
    return {"ok": True, "user": user}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    repo.revoke_session(request.cookies.get(AUTH_COOKIE_NAME))
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    if AUTH_DISABLED:
        return {"authenticated": True, "user": {"id": 0, "username": "local", "display_name": "Local", "role": "admin"}, "auth_disabled": True}
    user = repo.get_user_by_session(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return {"authenticated": True, "user": user}



@app.patch("/api/auth/profile")
def auth_update_profile(payload: ProfileUpdate, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    user_id = int(user["id"])
    try:
        updated = repo.update_user_profile(user_id, username=payload.username, display_name=payload.display_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.new_password:
        if not payload.current_password or not repo.verify_user_password(user_id, payload.current_password):
            raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
        repo.update_user_password(user_id, payload.new_password)
    return {"ok": True, "user": repo.get_user_by_id(user_id) or updated}


@app.get("/api/users")
def api_list_users(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    return repo.list_users()




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
    if not repo.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    repo.update_user_password(user_id, payload.password)
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


@app.get("/api/debug/ozon/backfill-chats")
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


@app.get("/api/debug/wb/import-events")
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


@app.get("/api/debug/wb/import-events-auto")
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


@app.get("/api/assets/image")
async def proxy_image(url: str) -> Response:
    if not _asset_proxy_allowed(url):
        raise HTTPException(status_code=400, detail="Image host is not allowed for preview proxy")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.get(url, headers=_asset_proxy_headers(url))
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Image preview failed: {exc}") from exc

    content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0].lower()
    if not (content_type.startswith("image/") or content_type in {"application/octet-stream", "binary/octet-stream"}):
        raise HTTPException(status_code=415, detail=f"URL did not return an image: {content_type}")

    max_bytes = _env_int("IMAGE_PROXY_MAX_BYTES", 10_000_000, minimum=100_000, maximum=30_000_000)
    if len(response.content) > max_bytes:
        raise HTTPException(status_code=413, detail="Image is too large for preview")

    headers = {"Cache-Control": "private, max-age=3600"}
    return Response(content=response.content, media_type=content_type if content_type.startswith("image/") else "image/jpeg", headers=headers)




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


@app.get("/api/task-types")
def api_list_task_types(request: Request, include_inactive: bool = False) -> list[dict[str, Any]]:
    _current_user(request)
    return repo.list_task_types(include_inactive=include_inactive)


@app.post("/api/task-types")
def api_create_task_type(payload: TaskTypeCreate, request: Request) -> dict[str, Any]:
    _current_user(request)
    try:
        return repo.create_task_type(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/task-types/{type_id}")
def api_update_task_type(type_id: int, payload: TaskTypeUpdate, request: Request) -> dict[str, Any]:
    _current_user(request)
    task_type = repo.update_task_type(type_id, payload)
    if not task_type:
        raise HTTPException(status_code=404, detail="Task type not found")
    return task_type


@app.delete("/api/task-types/{type_id}")
def api_delete_task_type(type_id: int, request: Request) -> dict[str, bool]:
    _current_user(request)
    ok = repo.delete_task_type(type_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task type not found")
    return {"ok": True}



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


@app.get("/api/chat-settings")
def get_chat_settings() -> dict[str, Any]:
    return repo.get_chat_settings()


@app.post("/api/chat-settings/funnels")
def create_chat_funnel(payload: ChatFunnelCreate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        return repo.create_chat_funnel(payload.title, payload.sort_order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/chat-settings/funnels/{funnel_id}")
def update_chat_funnel(funnel_id: int, payload: ChatFunnelUpdate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        funnel = repo.update_chat_funnel(funnel_id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not funnel:
        raise HTTPException(status_code=404, detail="Funnel not found")
    return funnel


@app.delete("/api/chat-settings/funnels/{funnel_id}")
def delete_chat_funnel(funnel_id: int, request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        ok = repo.delete_chat_funnel(funnel_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Funnel not found")
    return {"ok": True}


@app.post("/api/chat-settings/statuses")
def create_chat_status(payload: ChatStatusCreate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        return repo.create_chat_status(payload.title, payload.key, payload.funnel_id, payload.color, payload.sort_order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/chat-settings/statuses/{status_id}")
def update_chat_status(status_id: int, payload: ChatStatusUpdate, request: Request) -> dict[str, Any]:
    _require_admin(request)
    status = repo.update_chat_status(status_id, payload.model_dump(exclude_unset=True))
    if not status:
        raise HTTPException(status_code=404, detail="Status not found")
    return status


@app.delete("/api/chat-settings/statuses/{status_id}")
def delete_chat_status(status_id: int, request: Request) -> dict[str, Any]:
    _require_admin(request)
    ok = repo.delete_chat_status(status_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Status not found")
    return {"ok": True}


@app.get("/api/chats")
def list_chats(request: Request, status: str | None = None, marketplace: str | None = None, archived: bool = False, mine: bool = False, funnel_id: int | None = None) -> list[dict[str, Any]]:
    user = _current_user(request)
    assigned_user_id = int(user["id"]) if mine else None
    return repo.list_chats(status=status, marketplace=marketplace, archived=archived, assigned_user_id=assigned_user_id, funnel_id=funnel_id)


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: int, messages_limit: int = 120) -> dict[str, Any]:
    safe_limit = max(20, min(int(messages_limit or 120), 500))
    chat = repo.get_chat(chat_id, messages_limit=safe_limit)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat



@app.get("/api/notifications")
def api_notifications(request: Request, limit: int = 30, unread_only: bool = False) -> dict[str, Any]:
    user = _current_user(request)
    return repo.list_notifications(int(user["id"]), limit=limit, unread_only=unread_only)


@app.post("/api/notifications/{notification_id}/read")
def api_mark_notification_read(notification_id: int, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    ok = repo.mark_notification_read(notification_id, int(user["id"]))
    return {"ok": ok, "unread_count": repo.list_notifications(int(user["id"]), limit=1)["unread_count"]}


@app.post("/api/notifications/read-all")
def api_mark_all_notifications_read(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    count = repo.mark_all_notifications_read(int(user["id"]))
    return {"ok": True, "marked": count, "unread_count": 0}


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


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    if payload.comment and not payload.comment_author:
        payload.comment_author = user.get("display_name") or user.get("username") or "manager"
    task = repo.update_task(task_id, payload)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/knowledge/categories")
def api_knowledge_categories(request: Request) -> list[dict[str, Any]]:
    _current_user(request)
    return repo.list_knowledge_categories()


@app.post("/api/knowledge/categories")
def api_create_knowledge_category(payload: KnowledgeCategoryCreate, request: Request) -> dict[str, Any]:
    _current_user(request)
    return repo.create_knowledge_category(payload.title, payload.description, payload.sort_order)


@app.get("/api/knowledge/articles")
def api_knowledge_articles(request: Request, category_id: int | None = None, q: str | None = None) -> list[dict[str, Any]]:
    _current_user(request)
    return repo.list_knowledge_articles(category_id=category_id, q=q)


@app.get("/api/knowledge/articles/{article_id}")
def api_get_knowledge_article(article_id: int, request: Request) -> dict[str, Any]:
    _current_user(request)
    article = repo.get_knowledge_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@app.post("/api/knowledge/upload-image")
async def api_upload_knowledge_image(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    _current_user(request)
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    content_type = (file.content_type or "").lower()
    ext = allowed.get(content_type)
    if not ext:
        original = (file.filename or "").lower()
        for suffix in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
            if original.endswith(suffix):
                ext = ".jpg" if suffix == ".jpeg" else suffix
                break
    if not ext:
        raise HTTPException(status_code=400, detail="Можно загрузить только изображение JPG, PNG, WEBP или GIF")
    uploads_dir = STATIC_DIR / "uploads" / "knowledge"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    path = uploads_dir / filename
    size = 0
    with path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                with suppress(Exception):
                    path.unlink()
                raise HTTPException(status_code=413, detail="Изображение слишком большое. Максимум 8 МБ")
            out.write(chunk)
    return {"url": f"/static/uploads/knowledge/{filename}", "filename": filename, "size": size}


@app.post("/api/knowledge/articles")
def api_create_knowledge_article(payload: KnowledgeArticleCreate, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    return repo.create_knowledge_article(category_id=payload.category_id, title=payload.title, content=payload.content, tags=payload.tags, image_url=payload.image_url, is_published=payload.is_published, user_id=int(user["id"]))


@app.patch("/api/knowledge/articles/{article_id}")
def api_update_knowledge_article(article_id: int, payload: KnowledgeArticleUpdate, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    article = repo.update_knowledge_article(
        article_id,
        category_id=payload.category_id,
        title=payload.title,
        content=payload.content,
        tags=payload.tags,
        image_url=payload.image_url,
        clear_image=bool(payload.clear_image),
        is_published=payload.is_published,
        user_id=int(user["id"]),
    )
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@app.get("/api/reply-templates")
def api_list_reply_templates(request: Request, q: str | None = None) -> list[dict[str, Any]]:
    _current_user(request)
    return repo.list_reply_templates(q=q)


@app.post("/api/reply-templates")
def api_create_reply_template(payload: ReplyTemplateCreate, request: Request) -> dict[str, Any]:
    user = _current_user(request)
    try:
        return repo.create_reply_template(
            title=payload.title,
            content=payload.content,
            sort_order=payload.sort_order,
            user_id=int(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/sync/operator")
async def sync_operator_frontend() -> dict[str, Any]:
    lock: asyncio.Lock = getattr(app.state, "frontend_operator_sync_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.frontend_operator_sync_lock = lock
    async with lock:
        return await _sync_operator_frontend_unlocked()


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
