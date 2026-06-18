from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import httpx

from app.connectors.base import MarketplaceConnector, UnifiedChat, UnifiedMessage


class WildberriesConnector(MarketplaceConnector):
    """WB Buyers Chat connector.

    v49 notes:
    - WB events are global for all chats. The older implementation requested
      /seller/events separately for every chat during one sync. That could hit
      WB limits and also made some chats miss their latest messages.
    - We now fetch chat list once, events once, group events by chatID, and then
      get_messages() reads from the cached grouped events plus lastMessage.
    """

    marketplace = "wildberries"
    base_url = "https://buyer-chat-api.wildberries.ru"

    def __init__(self) -> None:
        self.token = os.getenv("WB_BUYERS_CHAT_TOKEN") or os.getenv("WB_API_TOKEN", "")
        self.reply_signs: dict[str, str] = {}
        self.last_messages: dict[str, dict[str, Any]] = {}
        self.events_by_chat: dict[str, list[dict[str, Any]]] = {}
        self.last_debug: dict[str, Any] = {}
        self.rate_limited_until: float = 0.0
        self.rate_limit_state_file = Path(os.getenv("WB_RATE_LIMIT_STATE_FILE", ".wb_rate_limit_until"))
        try:
            if self.rate_limit_state_file.exists():
                self.rate_limited_until = float(self.rate_limit_state_file.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            self.rate_limited_until = 0.0
        try:
            self.rate_limit_cooldown_seconds = max(30, min(7200, int(os.getenv("WB_RATE_LIMIT_COOLDOWN_SECONDS", "3700"))))
        except Exception:
            self.rate_limit_cooldown_seconds = 3700
        try:
            self.max_events = max(1, min(10000, int(os.getenv("WB_SYNC_MAX_EVENTS", "2000"))))
        except Exception:
            self.max_events = 2000
        try:
            self.event_pages = max(1, min(100, int(os.getenv("WB_SYNC_EVENT_PAGES", "10"))))
        except Exception:
            self.event_pages = 10
        try:
            self.events_lookback_days = max(1, min(365, int(os.getenv("WB_EVENTS_LOOKBACK_DAYS", "30"))))
        except Exception:
            self.events_lookback_days = 30
        try:
            self.events_start_timestamp_ms = int(os.getenv("WB_EVENTS_START_TIMESTAMP_MS", "0") or "0")
        except Exception:
            self.events_start_timestamp_ms = 0
        self.fetch_events_with_chat_list = os.getenv("WB_FETCH_EVENTS_WITH_CHAT_LIST", "false").strip().lower() in {"1", "true", "yes", "on", "да"}

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.token,
            "Accept": "application/json",
        }

    def _cooldown_remaining(self) -> int:
        return max(0, int(self.rate_limited_until - time.time()))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        remaining = self._cooldown_remaining()
        if remaining > 0:
            raise RuntimeError(f"WB API cooldown active: Too Many Requests. Повторить можно через {remaining} сек.")

        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self.headers, params=params or {})

        if response.status_code == 429:
            # WB can return Retry-After or X-Ratelimit-Retry / X-Ratelimit-Reset.
            # The old cap of 1800 sec was too short for tokens whose /seller/chats
            # limit is effectively hourly, so 429 storms could repeat forever.
            retry_after = (
                response.headers.get("Retry-After")
                or response.headers.get("X-Ratelimit-Retry")
                or response.headers.get("X-RateLimit-Retry")
                or response.headers.get("x-ratelimit-retry")
            )
            cooldown = self.rate_limit_cooldown_seconds
            try:
                if retry_after:
                    cooldown = int(float(str(retry_after).strip()))
            except Exception:
                cooldown = self.rate_limit_cooldown_seconds

            reset_header = (
                response.headers.get("X-Ratelimit-Reset")
                or response.headers.get("X-RateLimit-Reset")
                or response.headers.get("x-ratelimit-reset")
            )
            try:
                if reset_header:
                    reset_value = float(str(reset_header).strip())
                    # Some APIs return an epoch timestamp, others return seconds-until-reset.
                    if reset_value > time.time():
                        cooldown = max(cooldown, int(reset_value - time.time()))
                    else:
                        cooldown = max(cooldown, int(reset_value))
            except Exception:
                pass

            cooldown = max(30, min(7200, cooldown))
            self.rate_limited_until = time.time() + cooldown
            try:
                self.rate_limit_state_file.write_text(str(self.rate_limited_until), encoding="utf-8")
            except Exception:
                pass
            self.last_debug.update({
                "last_429_path": path,
                "last_429_headers": {
                    "Retry-After": response.headers.get("Retry-After"),
                    "X-Ratelimit-Retry": response.headers.get("X-Ratelimit-Retry") or response.headers.get("X-RateLimit-Retry"),
                    "X-Ratelimit-Reset": response.headers.get("X-Ratelimit-Reset") or response.headers.get("X-RateLimit-Reset"),
                    "X-Ratelimit-Limit": response.headers.get("X-Ratelimit-Limit") or response.headers.get("X-RateLimit-Limit"),
                },
                "cooldown_seconds": cooldown,
            })
            raise RuntimeError(
                f"WB API error 429 at {path}: too many requests. "
                f"CRM поставила WB на паузу на {cooldown} сек. "
                f"Заголовки лимита: Retry-After={response.headers.get('Retry-After')}, "
                f"X-Ratelimit-Retry={response.headers.get('X-Ratelimit-Retry') or response.headers.get('X-RateLimit-Retry')}, "
                f"X-Ratelimit-Reset={response.headers.get('X-Ratelimit-Reset') or response.headers.get('X-RateLimit-Reset')}, "
                f"X-Ratelimit-Limit={response.headers.get('X-Ratelimit-Limit') or response.headers.get('X-RateLimit-Limit')}. "
                f"Ответ WB: {response.text[:1200]}"
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"WB API error {response.status_code} at {path}: {response.text[:1500]}") from exc
        data = response.json()
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"_list": data}
        return {}

    @staticmethod
    def _result_list(data: dict[str, Any]) -> list[dict[str, Any]]:
        result = (
            data.get("result")
            or data.get("data")
            or data.get("chats")
            or data.get("items")
            or data.get("list")
            or data.get("_list")
            or []
        )
        if isinstance(result, dict):
            result = (
                result.get("chats")
                or result.get("items")
                or result.get("list")
                or result.get("data")
                or result.get("result")
                or []
            )
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    @staticmethod
    def _first_value(obj: dict[str, Any] | None, *keys: str) -> Any:
        if not isinstance(obj, dict):
            return None
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        # tolerate case changes from WB payloads
        lower_map = {str(k).lower(): v for k, v in obj.items()}
        for key in keys:
            value = lower_map.get(key.lower())
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def _find_nested_value(cls, value: Any, key_names: set[str], depth: int = 0) -> Any:
        if depth > 8:
            return None
        normalized = {str(k).lower().replace("_", "").replace("-", "") for k in key_names}
        if isinstance(value, dict):
            for key, nested in value.items():
                key_norm = str(key).lower().replace("_", "").replace("-", "")
                if key_norm in normalized and nested not in (None, ""):
                    return nested
            for nested in value.values():
                found = cls._find_nested_value(nested, normalized, depth + 1)
                if found not in (None, ""):
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_nested_value(item, normalized, depth + 1)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _chat_id_from_obj(cls, obj: dict[str, Any]) -> str:
        value = cls._first_value(obj, "chatID", "chatId", "chat_id", "chat_id_str", "id")
        if value in (None, ""):
            for wrapper_key in ("chat", "conversation", "dialog", "message", "payload", "data", "event"):
                nested = cls._first_value(obj, wrapper_key)
                if isinstance(nested, dict):
                    value = cls._first_value(nested, "chatID", "chatId", "chat_id", "chat_id_str", "id")
                    if value not in (None, ""):
                        break
        if value in (None, ""):
            value = cls._find_nested_value(obj, {"chatID", "chatId", "chat_id", "chat_id_str"})
        return str(value or "")

    @staticmethod
    def _timestamp_ms_to_iso(value: Any) -> str | None:
        try:
            ms = int(value)
        except Exception:
            return None
        if ms <= 0:
            return None
        # WB may occasionally send seconds instead of milliseconds.
        if ms < 10_000_000_000:
            return datetime.fromtimestamp(ms, tz=timezone.utc).isoformat()
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

    def set_reply_sign_from_metadata(self, external_chat_id: str, metadata: dict[str, Any]) -> None:
        sign = None
        if isinstance(metadata, dict):
            sign = metadata.get("replySign") or metadata.get("reply_sign")
            if not sign and isinstance(metadata.get("_sync_hint"), dict):
                sign = metadata["_sync_hint"].get("replySign")
        if sign:
            self.reply_signs[str(external_chat_id)] = str(sign)

    async def list_chats(self) -> list[UnifiedChat]:
        if not self.token:
            return []

        self.last_debug = {"configured": True}
        self.events_by_chat = {}

        data = await self._get("/api/v1/seller/chats")
        raw_chats = self._result_list(data)

        events_error = None
        if self.fetch_events_with_chat_list:
            try:
                all_events = await self._events()
                self.events_by_chat = self._group_events_by_chat(all_events)
            except Exception as exc:
                all_events = []
                events_error = str(exc)
        else:
            all_events = []
            events_error = "disabled_by_WB_FETCH_EVENTS_WITH_CHAT_LIST=false"

        chats: list[UnifiedChat] = []
        self.last_messages = {}
        self.reply_signs = {}

        for item in raw_chats:
            external_id = self._chat_id_from_obj(item)
            if not external_id:
                continue
            reply_sign = self._first_value(item, "replySign", "reply_sign")
            if reply_sign:
                self.reply_signs[external_id] = str(reply_sign)

            good_card = self._first_value(item, "goodCard", "good_card")
            if not isinstance(good_card, dict):
                good_card = {}

            last_message = self._first_value(item, "lastMessage", "last_message")
            if not isinstance(last_message, dict):
                last_message = {}
            if last_message:
                self.last_messages[external_id] = {**last_message, "_chat_item": item}

            order_id = (
                self._first_value(item, "orderId", "orderID", "order_id", "srid")
                or self._first_value(good_card, "rid", "nmID", "nmId")
                or ""
            )
            chats.append(
                UnifiedChat(
                    marketplace=self.marketplace,
                    external_chat_id=external_id,
                    customer_name=self._first_value(item, "clientName", "buyerName", "customerName") or None,
                    customer_public_id=str(self._first_value(item, "clientID", "clientId", "customerId") or "") or None,
                    order_id=str(order_id) if order_id else None,
                    status="new" if self._first_value(item, "isNewChat", "is_new_chat") else "in_progress",
                    metadata={
                        **item,
                        "_sync_hint": {
                            "replySign": reply_sign,
                            "lastMessage": last_message,
                            "wb_cached_events_count": len(self.events_by_chat.get(external_id, [])),
                        },
                    },
                )
            )

        self.last_debug.update(
            {
                "chats_count": len(raw_chats),
                "unified_chats_count": len(chats),
                "events_count": len(all_events),
                "events_grouped_chats_count": len(self.events_by_chat),
                "events_error": events_error,
                "fetch_events_with_chat_list": self.fetch_events_with_chat_list,
                "first_chat_keys": list(raw_chats[0].keys()) if raw_chats else [],
                "first_chat_id": self._chat_id_from_obj(raw_chats[0]) if raw_chats else None,
                "first_last_message": (self._first_value(raw_chats[0], "lastMessage", "last_message") if raw_chats else None),
                "sample_event_keys": list(all_events[0].keys()) if all_events else [],
                "sample_event_chat_ids": sorted({self._chat_id_from_obj(e) for e in all_events if self._chat_id_from_obj(e)})[:20],
                "rate_limited_until": self.rate_limited_until,
                "cooldown_remaining_seconds": self._cooldown_remaining(),
            }
        )
        return chats

    async def _events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        # WB docs for /api/v1/seller/events: first request must be WITHOUT
        # next, then repeat with next from the previous response until
        # totalEvents == 0. v64 incorrectly sent a timestamp as first next,
        # so WB could return only lastMessage from /seller/chats and no history.
        first_next = self.events_start_timestamp_ms if self.events_start_timestamp_ms > 0 else None
        next_value: Any = first_next
        self.last_debug["events_first_request_without_next"] = first_next is None
        self.last_debug["events_start_next"] = first_next
        self.last_debug["events_lookback_days"] = self.events_lookback_days
        pages_debug: list[dict[str, Any]] = []
        for page_no in range(self.event_pages):
            params = {"next": next_value} if next_value not in (None, "") else {}
            data = await self._get("/api/v1/seller/events", params=params)
            result = data.get("result") if isinstance(data.get("result"), dict) else data
            if isinstance(data.get("_list"), list):
                page_events = data.get("_list") or []
            else:
                page_events = (
                    result.get("events")
                    or result.get("items")
                    or result.get("data")
                    or result.get("list")
                    or result.get("result")
                    or []
                )
            if isinstance(page_events, dict):
                page_events = page_events.get("events") or page_events.get("items") or page_events.get("list") or []
            if not isinstance(page_events, list):
                page_events = []
            clean_events = [e for e in page_events if isinstance(e, dict)]
            events.extend(clean_events)
            total_events = result.get("totalEvents") or result.get("total_events")
            returned_next = result.get("next") or result.get("cursor") or result.get("nextCursor") or result.get("next_cursor")
            pages_debug.append({
                "page": page_no + 1,
                "request_next": next_value,
                "events_count": len(clean_events),
                "totalEvents": total_events,
                "response_next": returned_next,
                "first_event_keys": list(clean_events[0].keys())[:30] if clean_events else [],
                "first_event_chat_id": self._chat_id_from_obj(clean_events[0]) if clean_events else None,
            })
            if len(events) >= self.max_events:
                self.last_debug["events_pages_debug"] = pages_debug[:20]
                return events[: self.max_events]
            next_value = returned_next
            if not next_value or total_events == 0 or not page_events:
                break
        self.last_debug["events_pages_debug"] = pages_debug[:20]
        return events

    def _group_events_by_chat(self, events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            chat_id = self._chat_id_from_obj(event)
            if not chat_id:
                continue
            reply_sign = self._first_value(event, "replySign", "reply_sign")
            if reply_sign:
                self.reply_signs[chat_id] = str(reply_sign)
            grouped.setdefault(chat_id, []).append(event)
        return grouped

    @staticmethod
    def _extract_message_text(message_obj: dict[str, Any]) -> str:
        def first_text(value: Any, depth: int = 0) -> str:
            if depth > 6:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (int, float, bool)):
                return ""
            if isinstance(value, dict):
                for key in ("text", "message", "body", "caption", "content", "value"):
                    nested = value.get(key)
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
                for key in ("message", "payload", "data", "event"):
                    nested = value.get(key)
                    found = first_text(nested, depth + 1)
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = first_text(item, depth + 1)
                    if found:
                        return found
            return ""

        text = first_text(message_obj)
        attachments = message_obj.get("attachments") if isinstance(message_obj.get("attachments"), dict) else {}
        attachment_lines: list[str] = []

        def collect_files(value: Any, depth: int = 0) -> None:
            if depth > 5 or not value:
                return
            if isinstance(value, dict):
                name = value.get("name") or value.get("fileName") or value.get("filename") or value.get("downloadID") or value.get("downloadId") or value.get("id")
                url = value.get("url") or value.get("src") or value.get("downloadUrl") or value.get("downloadURL") or value.get("link")
                if url or name:
                    line = f"[{name or 'файл'}] {url or ''}".strip()
                    if line and line not in attachment_lines:
                        attachment_lines.append(line if str(url or "").startswith("http") else line.replace("[файл]", "WB файл"))
                for nested in value.values():
                    collect_files(nested, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    collect_files(item, depth + 1)

        collect_files(attachments)
        for key in ("downloadID", "downloadId", "fileID", "fileId", "image", "photo", "file", "url", "link"):
            value = message_obj.get(key)
            if isinstance(value, str) and value:
                line = value if value.startswith("http") else f"WB файл: {value}"
                if line not in attachment_lines:
                    attachment_lines.append(line)
        if attachment_lines:
            text = (text + "\n" if text else "") + "\n".join(attachment_lines[:10])
        return text or "[сообщение без текста / вложение]"

    def _message_created_at(self, obj: dict[str, Any]) -> str | None:
        # WB lastMessage officially contains addTimestamp. Prefer numeric
        # millisecond timestamps over generic date/time fields because some WB
        # objects still contain placeholder dates like 0001-01-01.
        ts_value = (
            self._first_value(obj, "addTimestamp", "add_timestamp", "timestamp", "createdAtMs", "created_at_ms", "timeMs")
            or self._find_nested_value(obj, {"addTimestamp", "add_timestamp", "timestamp", "createdAtMs", "created_at_ms", "timeMs"})
        )
        parsed_ts = self._timestamp_ms_to_iso(ts_value)
        if parsed_ts:
            return parsed_ts

        direct = (
            self._first_value(obj, "addTime", "createdAt", "created_at", "time", "date")
            or self._find_nested_value(obj, {"addTime", "createdAt", "created_at", "time", "date"})
        )
        if direct in (None, ""):
            return None
        direct_s = str(direct).strip()
        if not direct_s or direct_s.startswith("0001-") or direct_s.startswith("0000-"):
            return None
        # If WB returns seconds/milliseconds in a generic field, normalize it.
        if direct_s.isdigit():
            parsed_direct_ts = self._timestamp_ms_to_iso(direct_s)
            if parsed_direct_ts:
                return parsed_direct_ts
        return direct_s

    def _message_direction(self, obj: dict[str, Any], nested_message: dict[str, Any] | None = None) -> str:
        sender = str(
            self._first_value(obj, "sender", "senderType", "author", "userType", "authorType", "source", "from")
            or self._first_value(nested_message, "sender", "senderType", "author", "userType", "authorType", "source", "from")
            or self._find_nested_value(obj, {"sender", "senderType", "authorType", "userType", "source", "from"})
            or ""
        ).lower()
        if sender in {"seller", "продавец", "supplier", "vendor", "manager", "operator", "support", "employee"}:
            return "outbound"
        if sender in {"client", "customer", "buyer", "покупатель", "user"}:
            return "inbound"
        # WB sometimes uses flags instead of sender names.
        if self._first_value(obj, "isSeller", "is_seller", "fromSeller", "from_seller") is True:
            return "outbound"
        if self._first_value(obj, "isClient", "is_client", "fromClient", "from_client") is True:
            return "inbound"
        return "inbound"

    def _stable_message_id(self, external_chat_id: str, obj: dict[str, Any], text: str, created_at: str | None, direction: str) -> str:
        explicit = self._first_value(obj, "eventID", "eventId", "messageID", "messageId", "id")
        if explicit:
            return str(explicit)
        source = str(obj.get("_crm_source") or "").lower()
        if source == "wb_lastmessage":
            # lastMessage is one stable row in the chat list. Do not include
            # created_at in the fallback id, because older builds could have
            # saved it with a fallback time and then create a duplicate after
            # parsing addTimestamp correctly.
            base = f"{external_chat_id}|lastMessage|{direction}|{text[:500]}"
            digest = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:20]
            return f"wb:last:{digest}"
        base = f"{external_chat_id}|{created_at or ''}|{direction}|{text[:500]}"
        digest = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:20]
        return f"wb:{digest}"

    def _message_from_last_message(self, external_chat_id: str, fallback: dict[str, Any] | None = None) -> UnifiedMessage | None:
        item = self.last_messages.get(str(external_chat_id)) or fallback or {}
        if not isinstance(item, dict) or not item:
            return None
        chat_item = item.get("_chat_item") if isinstance(item.get("_chat_item"), dict) else {}
        direction = self._message_direction(item, chat_item)
        created_at = self._message_created_at(item) or self._message_created_at(chat_item)
        text = self._extract_message_text(item)
        # Chat list lastMessage often has no messageID, so use deterministic ID to
        # avoid duplicates on every background sync.
        raw = {**item, "_chat_item": chat_item, "_crm_source": "wb_lastMessage"}
        message_id = self._stable_message_id(str(external_chat_id), raw, text, created_at, direction)
        return UnifiedMessage(
            external_message_id=message_id,
            external_chat_id=str(external_chat_id),
            direction=direction,
            text=text,
            author="seller" if direction == "outbound" else (item.get("clientName") or chat_item.get("clientName") or "customer"),
            created_at=created_at,
            raw=raw,
        )

    def _event_to_message(self, external_chat_id: str, event: dict[str, Any]) -> UnifiedMessage | None:
        event_type = str(self._first_value(event, "eventType", "event_type", "type", "event") or "").lower()
        msg_obj = self._first_value(event, "message", "payload", "data")
        if isinstance(msg_obj, dict) and isinstance(msg_obj.get("message"), dict):
            msg_obj = msg_obj["message"]
        if not isinstance(msg_obj, dict):
            msg_obj = event
        text = self._extract_message_text(msg_obj)
        # Some WB payloads omit eventType; if there is message text/attachment,
        # treat it as a message. Skip clearly non-message events.
        if event_type and "message" not in event_type and "chat" not in event_type and text == "[сообщение без текста / вложение]":
            return None
        reply_sign = self._first_value(event, "replySign", "reply_sign") or self._find_nested_value(event, {"replySign", "reply_sign"})
        if reply_sign:
            self.reply_signs[str(external_chat_id)] = str(reply_sign)
        direction = self._message_direction(event, msg_obj)
        created_at = self._message_created_at(event) or self._message_created_at(msg_obj)
        message_id = self._stable_message_id(str(external_chat_id), event, text, created_at, direction)
        author = "seller" if direction == "outbound" else (
            self._first_value(event, "clientName", "buyerName", "customerName")
            or self._first_value(msg_obj, "clientName", "buyerName", "customerName")
            or "customer"
        )
        return UnifiedMessage(
            external_message_id=message_id,
            external_chat_id=str(external_chat_id),
            direction=direction,
            text=text,
            author=author,
            created_at=created_at,
            raw={**event, "_crm_wb_msg_obj": msg_obj},
        )

    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        if not self.token:
            return []
        chat_id = str(external_chat_id)
        # Prefer cached events loaded once in list_chats(). If get_messages() is
        # called directly in a debug context, fetch events as a fallback.
        chat_events = self.events_by_chat.get(chat_id)
        if chat_events is None:
            chat_events = self._group_events_by_chat(await self._events()).get(chat_id, [])

        messages: list[UnifiedMessage] = []
        seen_ids: set[str] = set()
        for event in chat_events:
            message = self._event_to_message(chat_id, event)
            if not message:
                continue
            if message.external_message_id in seen_ids:
                continue
            seen_ids.add(str(message.external_message_id))
            messages.append(message)

        fallback = self._message_from_last_message(chat_id)
        if fallback and str(fallback.external_message_id) not in seen_ids:
            messages.append(fallback)

        messages.sort(key=lambda m: m.created_at or "")
        return messages

    async def send_message(self, external_chat_id: str, text: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("WB_BUYERS_CHAT_TOKEN/WB_API_TOKEN is not configured")
        reply_sign = self.reply_signs.get(str(external_chat_id))
        if not reply_sign:
            raise RuntimeError("WB replySign is missing. Дождитесь фоновой синхронизации WB или обновите чаты, чтобы CRM получила актуальный replySign.")
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/seller/message",
                headers={"Authorization": self.token, "Accept": "application/json"},
                data={"replySign": reply_sign, "message": text[:1000]},
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"WB API error {response.status_code} at /api/v1/seller/message: {response.text[:1500]}") from exc
        data = response.json()
        return data if isinstance(data, dict) else {"ok": True}
