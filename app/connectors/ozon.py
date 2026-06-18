from __future__ import annotations

import os
import json
import hashlib
import re
from typing import Any

import httpx

from app.connectors.base import MarketplaceConnector, UnifiedChat, UnifiedMessage


class OzonConnector(MarketplaceConnector):
    """Ozon Seller API Chat connector.

    Использует актуальные методы чатов:
    - /v3/chat/list
    - /v3/chat/history
    - /v1/chat/send/message

    v10: ускорена ручная синхронизация:
    - сначала забираем непрочитанные чаты;
    - ограничиваем количество чатов за один проход через OZON_SYNC_MAX_CHATS;
    - больше не создаётся ощущение, что curl «ничего не даёт» из-за долгого полного обхода.

    v9: исправлен лимит Ozon API (максимум 100):
    - список чатов теперь запрашивается страницами по 100;
    - история сообщений запрашивается с limit=100;
    - ошибка validation error: invalid ChatListRequest.Limit больше не возникает.

    v7: синхронизация стала более «цепкой»:
    - дополнительно запрашиваем чаты несколькими фильтрами: без фильтра, ALL, OPENED, CLOSED, unread_only;
    - не падаем, если один из фильтров Ozon не принял;
    - добавлен diagnostic/debug summary, чтобы увидеть, что реально отдаёт Ozon API;
    - аккуратнее извлекаем chat_id и message_id из разных форматов ответа.
    """

    marketplace = "ozon"
    base_url = "https://api-seller.ozon.ru"

    def __init__(self) -> None:
        self.client_id = os.getenv("OZON_CLIENT_ID", "")
        self.api_key = os.getenv("OZON_API_KEY", "")
        self.last_sync_debug: dict[str, Any] = {}
        # Чтобы ручная синхронизация не «висела» на больших кабинетах,
        # по умолчанию берём только первые 50 наиболее важных чатов.
        # Можно увеличить в .env: OZON_SYNC_MAX_CHATS=200
        try:
            self.sync_max_chats = max(1, min(1000, int(os.getenv("OZON_SYNC_MAX_CHATS", "500"))))
        except Exception:
            self.sync_max_chats = 500
        try:
            self.sync_pages_per_variant = max(1, min(10, int(os.getenv("OZON_SYNC_PAGES_PER_VARIANT", "5"))))
        except Exception:
            self.sync_pages_per_variant = 5
        # full: unread + default + opened + closed + all.
        # fast: unread + default. Used for background polling so new chats appear faster.
        self.sync_variant_mode = os.getenv("OZON_SYNC_VARIANT_MODE", "full")
        self.sync_include_closed = os.getenv("OZON_SYNC_INCLUDE_CLOSED", "0").strip().lower() in {"1", "true", "yes", "on", "да"}
        try:
            self.history_pages = max(1, min(50, int(os.getenv("OZON_HISTORY_PAGES", "1"))))
        except Exception:
            self.history_pages = 1

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{self.base_url}{path}", headers=self.headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text[:2000]
            raise RuntimeError(f"Ozon API error {response.status_code} at {path}: {body}") from exc
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"Ozon API returned non-JSON response at {path}: {response.text[:1000]}") from exc

    async def _post_no_raise(self, path: str, payload: dict[str, Any]) -> tuple[int, str, dict[str, Any] | None]:
        """Call Ozon API for diagnostics/filter probing.

        Возвращает status_code, body text, parsed JSON. Не бросает исключение,
        чтобы один неподдержанный фильтр не ломал всю синхронизацию.
        """
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{self.base_url}{path}", headers=self.headers, json=payload)
        text = response.text[:5000]
        parsed: dict[str, Any] | None = None
        try:
            if text:
                obj = response.json()
                if isinstance(obj, dict):
                    parsed = obj
        except Exception:
            parsed = None
        return response.status_code, text, parsed

    @staticmethod
    def _result(data: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        result = data.get("result", data)
        return result if isinstance(result, dict) else data

    @staticmethod
    def _chat_id_from_item(item: dict[str, Any]) -> str | None:
        chat_obj = item.get("chat") if isinstance(item.get("chat"), dict) else item
        for source in (chat_obj, item):
            if not isinstance(source, dict):
                continue
            for key in ("chat_id", "id", "chatId"):
                value = source.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    @staticmethod
    def _page_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        result = OzonConnector._result(data)
        page_items = result.get("chats") or result.get("items") or []
        if not isinstance(page_items, list):
            return []
        return [item for item in page_items if isinstance(item, dict)]

    @staticmethod
    def _page_total(data: dict[str, Any] | None) -> int:
        result = OzonConnector._result(data)
        try:
            return int(result.get("total_chats_count") or result.get("total") or 0)
        except Exception:
            return 0

    def _list_payload_variants(self, *, limit: int, offset: int) -> list[tuple[str, dict[str, Any]]]:
        # Ozon в документации описывает filter.chat_status и filter.unread_only.
        # Для ручной синхронизации можно использовать полный набор фильтров, а для
        # фоновой — быстрый режим: непрочитанные + последние чаты. Так новые чаты
        # появляются в CRM быстрее и сервер не тратит время на закрытые/служебные ветки.
        unread = ("unread", {"filter": {"chat_status": "ALL", "unread_only": True}, "limit": limit, "offset": offset})
        # In the Ozon seller UI recent buyer dialogs are primarily OPENED chats.
        # Earlier versions requested the unfiltered/default page before OPENED and
        # could fill sync_max_chats with older/non-priority dialogs before reaching
        # the truly active buyer inbox. Active-first order fixes missing recent chats.
        opened = ("opened", {"filter": {"chat_status": "OPENED", "unread_only": False}, "limit": limit, "offset": offset})
        all_chats = ("all", {"filter": {"chat_status": "ALL", "unread_only": False}, "limit": limit, "offset": offset})
        default = ("default", {"limit": limit, "offset": offset})
        closed = ("closed", {"filter": {"chat_status": "CLOSED", "unread_only": False}, "limit": limit, "offset": offset})

        mode = str(getattr(self, "sync_variant_mode", "full") or "full").strip().lower()
        include_closed = bool(getattr(self, "sync_include_closed", False)) or os.getenv("OZON_SYNC_INCLUDE_CLOSED", "0").strip().lower() in {"1", "true", "yes", "on", "да"}
        if mode in {"unread", "unread_only"}:
            return [unread]
        if mode in {"fast", "background", "recent"}:
            return [unread, opened, all_chats]
        variants = [unread, opened, all_chats, default]
        if include_closed:
            variants.append(closed)
        return variants

    async def _list_chats_by_variant(
        self,
        variant_name: str,
        payload_template: dict[str, Any],
        *,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        limit = int(payload_template.get("limit") or 1000)
        offset = int(payload_template.get("offset") or 0)
        items: list[dict[str, Any]] = []
        debug: dict[str, Any] = {
            "variant": variant_name,
            "requests": 0,
            "status_code": None,
            "total": None,
            "items": 0,
            "sample_chat_ids": [],
            "error": None,
        }

        page_number = 0
        while True:
            page_number += 1
            payload = json.loads(json.dumps(payload_template, ensure_ascii=False))
            payload["limit"] = limit
            payload["offset"] = offset
            status_code, text, data = await self._post_no_raise("/v3/chat/list", payload)
            debug["requests"] += 1
            debug["status_code"] = status_code

            if status_code >= 400:
                debug["error"] = text[:1000]
                break

            page_items = self._page_items(data)
            total = self._page_total(data)
            if total:
                debug["total"] = total
            items.extend(page_items)

            for item in page_items:
                chat_id = self._chat_id_from_item(item)
                if chat_id and len(debug["sample_chat_ids"]) < 5:
                    debug["sample_chat_ids"].append(chat_id)

            offset += limit
            if max_items and len(items) >= max_items:
                items = items[:max_items]
                debug["stopped"] = f"max_items={max_items}"
                break
            if max_pages and page_number >= max_pages:
                debug["stopped"] = f"max_pages={max_pages}"
                break
            if not page_items or len(page_items) < limit or (total and offset >= total):
                break
            if offset > 20000:
                debug["error"] = "Stopped after offset > 20000 to avoid infinite pagination"
                break

        debug["items"] = len(items)
        return items, debug


    @staticmethod
    def _clean_person_value(value: Any) -> str | None:
        """Return a safe person-looking value or None.

        Ozon responses can include many nested objects. We only want to use values
        that look like a buyer name/public display name, not product names, files,
        UUIDs or technical labels.
        """
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text:
            return None
        lowered = text.lower().strip()
        generic = {
            "customer", "buyer", "client", "user", "покупатель", "клиент",
            "seller", "продавец", "operator", "admin", "support",
            "notificationuser", "notification_user", "system", "systemuser", "system_user",
        }
        if lowered in generic:
            return None
        # UUID-like and numeric-only values are IDs, not names.
        compact = text.replace("-", "")
        if len(compact) >= 24 and all(ch in "0123456789abcdefABCDEF" for ch in compact):
            return None
        if text.isdigit():
            return None
        if len(text) > 120:
            return None
        return text

    @classmethod
    def _extract_name_from_dict(cls, obj: dict[str, Any]) -> str | None:
        """Try to find a human display name inside a dict.

        Works defensively because Ozon may change response shape and different chat
        types may return sender info in different nested fields.
        """
        # First/last name pair has priority.
        first = cls._clean_person_value(
            obj.get("first_name") or obj.get("firstName") or obj.get("firstname")
        )
        last = cls._clean_person_value(
            obj.get("last_name") or obj.get("lastName") or obj.get("lastname")
        )
        if first or last:
            return " ".join(part for part in [first, last] if part)

        for key in (
            "customer_name", "customerName", "buyer_name", "buyerName", "client_name", "clientName",
            "full_name", "fullName", "display_name", "displayName", "visible_name", "visibleName",
            "user_name", "username", "nickname", "login", "fio", "fio_name", "contact_name", "contactName", "name",
        ):
            value = cls._clean_person_value(obj.get(key))
            if value:
                return value
        return None

    @classmethod
    def _extract_customer_name_from_any(cls, payload: Any) -> str | None:
        """Find buyer name in chat/message payload without leaking technical fields.

        Ozon may return different shapes for different chat types. We first look in
        obvious buyer/author containers and then do a cautious recursive search,
        but only on paths that look person-related.
        """
        if isinstance(payload, str):
            value = payload.strip()
            if value.startswith("{") and value.endswith("}"):
                try:
                    return cls._extract_customer_name_from_any(json.loads(value))
                except Exception:
                    return None
            return None

        if not isinstance(payload, dict):
            return None

        # Direct first/last or display-name fields on a known person object.
        direct = cls._extract_name_from_dict(payload)
        if direct:
            # Avoid using arbitrary object names unless the object itself looks user-like.
            keys = {str(k).lower() for k in payload.keys()}
            if keys & {"first_name", "firstname", "lastname", "last_name", "display_name", "displayname", "user_name", "username", "nickname", "buyer_name", "customer_name", "client_name"}:
                return direct

        # Prefer obvious buyer/customer/person containers.
        for key in (
            "customer", "buyer", "client", "consumer", "user", "author", "sender", "from",
            "participant", "interlocutor", "counterparty", "person", "profile", "owner",
        ):
            nested = payload.get(key)
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except Exception:
                    nested = None
            if isinstance(nested, dict):
                name = cls._extract_name_from_dict(nested) or cls._extract_customer_name_from_any(nested)
                if name:
                    return name

        # Some APIs return participants/users arrays. Pick non-seller person first.
        for key in ("participants", "users", "members", "authors", "senders"):
            nested_list = payload.get(key)
            if isinstance(nested_list, list):
                for nested in nested_list:
                    if not isinstance(nested, dict):
                        continue
                    role = str(
                        nested.get("type") or nested.get("role") or nested.get("user_type") or nested.get("author_type") or ""
                    ).lower()
                    if role in {"seller", "operator", "admin", "support", "manager", "продавец"}:
                        continue
                    name = cls._extract_name_from_dict(nested) or cls._extract_customer_name_from_any(nested)
                    if name:
                        return name

        # Cautious recursive fallback: only use name-like fields when the path contains
        # person-related words, not product/card/order/title sections.
        def walk(value: Any, path: str = "", depth: int = 0) -> str | None:
            if depth > 5:
                return None
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        return walk(json.loads(stripped), path, depth + 1)
                    except Exception:
                        return None
                return None
            if isinstance(value, list):
                for item in value[:20]:
                    found = walk(item, path, depth + 1)
                    if found:
                        return found
                return None
            if not isinstance(value, dict):
                return None

            path_l = path.lower()
            person_path = any(token in path_l for token in ("customer", "buyer", "client", "user", "author", "sender", "participant", "interlocutor", "person", "profile"))
            if person_path:
                found = cls._extract_name_from_dict(value)
                if found:
                    return found
            for key, nested in value.items():
                key_l = str(key).lower()
                if key_l in {"product", "products", "item", "items", "offer", "sku", "posting", "order"}:
                    continue
                found = walk(nested, f"{path}.{key_l}", depth + 1)
                if found:
                    return found
            return None

        return walk(payload)

    @classmethod
    def _extract_public_id_from_any(cls, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        for container_key in ("customer", "buyer", "client", "user", "author", "sender"):
            nested = payload.get(container_key)
            if isinstance(nested, dict):
                for key in ("id", "customer_id", "buyer_id", "client_id", "user_id", "uuid"):
                    value = nested.get(key)
                    if value not in (None, ""):
                        return str(value)
        for key in ("customer_id", "buyer_id", "client_id", "user_id"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @classmethod
    def _extract_customer_name_from_chat_item(cls, item: dict[str, Any]) -> str | None:
        chat_obj = item.get("chat") if isinstance(item.get("chat"), dict) else item
        return cls._extract_customer_name_from_any(chat_obj) or cls._extract_customer_name_from_any(item)


    @staticmethod
    def _payload_contains_token(payload: Any, tokens: tuple[str, ...], *, max_depth: int = 7) -> bool:
        """Recursive safe search for system-user markers in Ozon payloads."""
        lowered_tokens = tuple(t.lower() for t in tokens if t)
        if not lowered_tokens:
            return False

        def walk(value: Any, depth: int = 0) -> bool:
            if depth > max_depth:
                return False
            if value in (None, ""):
                return False
            if isinstance(value, str):
                text = value.lower()
                return any(token in text for token in lowered_tokens)
            if isinstance(value, (int, float, bool)):
                return False
            if isinstance(value, list):
                return any(walk(item, depth + 1) for item in value[:80])
            if isinstance(value, dict):
                for key, nested in value.items():
                    key_l = str(key).lower()
                    if any(token in key_l for token in lowered_tokens):
                        return True
                    if walk(nested, depth + 1):
                        return True
            return False

        return walk(payload)

    @classmethod
    def _looks_like_notification_user_chat(cls, item: dict[str, Any]) -> bool:
        """Detect Ozon system notification conversations such as notificationuser.

        These are not buyer dialogs and should not appear in the operator inbox.
        """
        if os.getenv("OZON_EXCLUDE_NOTIFICATIONUSER_CHATS", "1").strip().lower() in {"0", "false", "no", "off", "нет"}:
            return False
        tokens_raw = os.getenv("OZON_NOTIFICATION_USER_MARKERS", "notificationuser,notification_user,systemuser,system_user")
        tokens = tuple(t.strip().lower() for t in tokens_raw.split(",") if t.strip())
        return cls._payload_contains_token(item, tokens)


    @classmethod
    def _chat_type_from_item(cls, item: dict[str, Any]) -> str:
        chat_obj = item.get("chat") if isinstance(item.get("chat"), dict) else item
        for source in (chat_obj, item):
            if not isinstance(source, dict):
                continue
            for key in ("chat_type", "chatType", "type", "category", "kind", "channel"):
                value = source.get(key)
                if value not in (None, ""):
                    return str(value)
        return ""

    @classmethod
    def _chat_title_from_item(cls, item: dict[str, Any]) -> str:
        chat_obj = item.get("chat") if isinstance(item.get("chat"), dict) else item
        values: list[str] = []
        for source in (chat_obj, item):
            if not isinstance(source, dict):
                continue
            for key in ("title", "name", "subject", "caption", "chat_name", "chatName"):
                value = source.get(key)
                if value not in (None, ""):
                    values.append(str(value))
        return " ".join(values)

    @classmethod
    def _support_chat_reason(cls, item: dict[str, Any]) -> str | None:
        """Return reason only for clearly identified Ozon service/support chats.

        v80: the previous filter was too broad: words such as "service",
        "system", "notification" or "news" can appear in normal Ozon chat
        metadata/type names. That caused thousands of buyer dialogs to be
        skipped as support. This filter skips only explicit support/news/API
        conversations and keeps everything uncertain.
        """
        chat_type = cls._chat_type_from_item(item).lower()
        title = cls._chat_title_from_item(item).lower()
        combined = f"{chat_type} {title}".strip()

        customer_markers = (
            "buyer", "customer", "client", "consumer", "покупател", "клиент",
            "order", "posting", "return", "claim", "dispute", "заказ", "возврат",
        )
        if any(marker in combined for marker in customer_markers):
            return None

        exact_user_markers = (
            "notificationuser", "notification_user", "systemuser", "system_user",
        )
        if any(marker in combined for marker in exact_user_markers):
            return "technical_user_marker"

        explicit_support_markers = (
            "seller_support", "tech_support", "helpdesk", "ozon support",
            "служба поддержки", "поддержка продавца", "seller api", "api update",
            "o4d", "spotlight", "digest", "newsletter",
        )
        if any(marker in combined for marker in explicit_support_markers):
            return "explicit_support_title_or_type"

        # Russian "поддерж" is safe enough only when it is in visible title/name,
        # not in a broad technical type.
        if "поддерж" in title:
            return "support_title"

        # English "support" is safe only as a separate support word in title.
        if title and re.search(r"(^|[^a-z])support([^a-z]|$)", title):
            return "support_title"

        for key in ("participants", "users", "members"):
            nested_list = item.get(key)
            if isinstance(item.get("chat"), dict):
                nested_list = nested_list or item["chat"].get(key)
            if isinstance(nested_list, list):
                roles = " ".join(
                    str((p or {}).get("role") or (p or {}).get("type") or (p or {}).get("user_type") or "").lower()
                    for p in nested_list if isinstance(p, dict)
                )
                if any(marker in roles for marker in ("support", "admin", "operator", "поддерж")) and not any(marker in roles for marker in ("buyer", "customer", "client", "покупател")):
                    return "support_participant_role"
        return None

    @classmethod
    def _looks_like_support_chat(cls, item: dict[str, Any]) -> bool:
        return cls._support_chat_reason(item) is not None

    @classmethod
    def _is_customer_chat_item(cls, item: dict[str, Any]) -> bool:
        # v81: by default keep everything Ozon returns. Do not risk losing buyer
        # dialogs during deep backfill. Strict filtering can be enabled manually.
        mode = os.getenv("OZON_EXCLUDE_SUPPORT_CHATS", "0").strip().lower()
        if mode in {"0", "false", "no", "off", "нет"}:
            return True
        return cls._support_chat_reason(item) is None

    async def list_chats(self) -> list[UnifiedChat]:
        if not self.client_id or not self.api_key:
            self.last_sync_debug = {"error": "OZON_CLIENT_ID/OZON_API_KEY are not configured"}
            return []

        # Ozon API принимает limit только в диапазоне [1, 100].
        # Раньше тут стояло 1000, из-за этого синхронизация падала с validation error.
        limit = 100
        raw_items: list[dict[str, Any]] = []
        seen_chat_ids: set[str] = set()
        skipped_support_count = 0
        skipped_support_samples: list[Any] = []
        duplicate_count = 0
        variants_debug: list[dict[str, Any]] = []

        for variant_name, payload in self._list_payload_variants(limit=limit, offset=0):
            if len(raw_items) >= self.sync_max_chats:
                break
            remaining = self.sync_max_chats - len(raw_items)
            items, dbg = await self._list_chats_by_variant(
                variant_name,
                payload,
                max_pages=self.sync_pages_per_variant,
                max_items=remaining,
            )
            variants_debug.append(dbg)
            for item in items:
                chat_id = self._chat_id_from_item(item)
                if not chat_id:
                    continue
                if chat_id not in seen_chat_ids:
                    if not self._is_customer_chat_item(item):
                        # Do not put Ozon support/API/news/notificationuser chats into the CRM inbox.
                        skipped_support_count += 1
                        if len(skipped_support_samples) < 20:
                            skipped_support_samples.append({
                                "chat_id": chat_id,
                                "reason": self._support_chat_reason(item),
                                "chat_type": self._chat_type_from_item(item),
                                "title": self._chat_title_from_item(item),
                            })
                        continue
                    seen_chat_ids.add(chat_id)
                    raw_items.append(item)
                    if len(raw_items) >= self.sync_max_chats:
                        break
                else:
                    duplicate_count += 1

        self.last_sync_debug = {
            "sync_max_chats": self.sync_max_chats,
            "sync_pages_per_variant": self.sync_pages_per_variant,
            "variants": variants_debug,
            "unique_chats": len(raw_items),
            "duplicate_count": duplicate_count,
            "skipped_support_count": skipped_support_count,
            "skipped_support_samples": skipped_support_samples,
            "unique_sample_chat_ids": list(seen_chat_ids)[:20],
        }

        chats: list[UnifiedChat] = []
        for item in raw_items:
            chat_obj = item.get("chat") if isinstance(item.get("chat"), dict) else item
            chat_id = self._chat_id_from_item(item)
            if not chat_id:
                continue

            customer = chat_obj.get("customer") if isinstance(chat_obj.get("customer"), dict) else {}
            user = chat_obj.get("user") if isinstance(chat_obj.get("user"), dict) else {}
            order = chat_obj.get("order") if isinstance(chat_obj.get("order"), dict) else {}

            unread_count = item.get("unread_count") or chat_obj.get("unread_count") or 0
            order_id = (
                chat_obj.get("order_id")
                or chat_obj.get("posting_number")
                or order.get("id")
                or order.get("posting_number")
                or ""
            )

            customer_name = self._extract_customer_name_from_chat_item(item)
            customer_public_id = (
                self._extract_public_id_from_any(chat_obj)
                or self._extract_public_id_from_any(item)
                or customer.get("id")
                or user.get("id")
                or chat_obj.get("customer_id")
                or chat_obj.get("buyer_id")
                or ""
            )

            metadata = dict(item)
            metadata["_sync_hint"] = {
                "chat_id": chat_id,
                "unread_count": unread_count,
                "chat_status": chat_obj.get("chat_status") or item.get("chat_status"),
                "chat_type": chat_obj.get("chat_type") or item.get("chat_type"),
                "chat_title": self._chat_title_from_item(item),
                "is_customer_chat": self._is_customer_chat_item(item),
                "last_message_id": chat_obj.get("last_message_id") or item.get("last_message_id"),
                "first_unread_message_id": chat_obj.get("first_unread_message_id") or item.get("first_unread_message_id"),
                "customer_name_found_in_list": bool(customer_name),
            }

            chats.append(
                UnifiedChat(
                    marketplace=self.marketplace,
                    external_chat_id=str(chat_id),
                    customer_name=str(customer_name) if customer_name else None,
                    customer_public_id=str(customer_public_id) if customer_public_id else None,
                    order_id=str(order_id) if order_id else None,
                    status="new" if unread_count else "in_progress",
                    metadata=metadata,
                )
            )
        return chats

    def _extract_message_text(self, item: dict[str, Any]) -> str:
        """Return human-readable message text from different Ozon response shapes."""
        direct = (
            item.get("text")
            or item.get("message")
            or item.get("message_text")
            or item.get("content")
            or item.get("body")
        )
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        def stringify(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (int, float, bool)):
                return str(value)
            if isinstance(value, dict):
                for key in (
                    "text", "message", "message_text", "content", "body",
                    "value", "title", "name", "file_name", "filename", "url", "link",
                ):
                    nested_text = stringify(value.get(key))
                    if nested_text:
                        return nested_text
                if value.get("type"):
                    return f"[{value.get('type')}]"
                return json.dumps(value, ensure_ascii=False)
            if isinstance(value, list):
                parts = [stringify(v) for v in value]
                return "\n".join(part for part in parts if part)
            return str(value)

        data_text = stringify(item.get("data"))
        if data_text:
            return data_text

        attachments_text = stringify(item.get("attachments") or item.get("files"))
        if attachments_text:
            return attachments_text

        return "[сообщение без текста / вложение]"

    @staticmethod
    def _fallback_message_id(item: dict[str, Any], external_chat_id: str, text: str, direction: str) -> str:
        created_at = str(item.get("created_at") or item.get("createdAt") or "")
        base = json.dumps(item, ensure_ascii=False, sort_keys=True) if item else f"{created_at}:{text}:{direction}"
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
        return f"ozon:{external_chat_id}:{created_at}:{digest}"

    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        if not self.client_id or not self.api_key:
            return []

        # Ozon /v3/chat/history can return history by pages. Normal sync keeps
        # OZON_HISTORY_PAGES=1 for speed; deep backfill temporarily increases it.
        limit = 100
        pages = max(1, min(50, int(getattr(self, "history_pages", 1) or 1)))
        messages_raw_all: list[dict[str, Any]] = []
        seen_message_ids: set[str] = set()
        page_debug: list[dict[str, Any]] = []
        cursor_message_id: str | None = None

        for page in range(pages):
            payload: dict[str, Any] = {"chat_id": external_chat_id, "limit": limit, "direction": "Backward"}
            if cursor_message_id:
                payload["from_message_id"] = cursor_message_id

            try:
                data = await self._post("/v3/chat/history", payload)
            except Exception as exc:
                # Some cabinets/API versions may reject from_message_id. Keep already
                # imported pages instead of failing the entire chat sync.
                page_debug.append({
                    "page": page + 1,
                    "payload": payload,
                    "error": str(exc)[:1000],
                })
                if page == 0:
                    raise
                break

            result = self._result(data)
            messages_raw = result.get("messages") or result.get("items") or []
            if not isinstance(messages_raw, list):
                messages_raw = []

            page_items: list[dict[str, Any]] = []
            for item in messages_raw:
                if isinstance(item, dict):
                    mid = str(item.get("message_id") or item.get("id") or item.get("uuid") or item.get("messageId") or "")
                    if mid and mid in seen_message_ids:
                        continue
                    if mid:
                        seen_message_ids.add(mid)
                    page_items.append(item)

            messages_raw_all.extend(page_items)

            has_next = bool(result.get("has_next") if "has_next" in result else result.get("hasNext"))
            page_debug.append({
                "page": page + 1,
                "items": len(page_items),
                "has_next": has_next,
                "used_from_message_id": cursor_message_id,
            })

            if not page_items or not has_next:
                break

            # For Backward direction, the next page should start from the oldest
            # message we have just received. If timestamps are unavailable, use
            # the last item from the response as a safe fallback.
            def created_value(obj: dict[str, Any]) -> str:
                return str(obj.get("created_at") or obj.get("createdAt") or "")

            oldest = min(page_items, key=created_value) if any(created_value(i) for i in page_items) else page_items[-1]
            next_cursor = str(oldest.get("message_id") or oldest.get("id") or oldest.get("uuid") or oldest.get("messageId") or "")
            if not next_cursor or next_cursor == cursor_message_id:
                break
            cursor_message_id = next_cursor

        # Expose compact diagnostics for /api/debug/ozon.
        self.last_sync_debug["last_history_fetch"] = {
            "chat_id": external_chat_id,
            "history_pages_requested": pages,
            "history_pages_debug": page_debug[-10:],
            "messages_raw_total": len(messages_raw_all),
        }

        messages: list[UnifiedMessage] = []
        for item in messages_raw_all:
            if not isinstance(item, dict):
                continue

            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            user_type = str(user.get("type") or item.get("user_type") or item.get("author_type") or "").lower()
            is_seller = bool(item.get("is_seller")) or user_type in {"seller", "operator", "admin", "support", "manager"}
            direction = "outbound" if is_seller else "inbound"
            text = self._extract_message_text(item)
            author_name = self._extract_customer_name_from_any(item)
            author_public_id = self._extract_public_id_from_any(item)
            external_message_id = str(item.get("message_id") or item.get("id") or item.get("uuid") or item.get("messageId") or "")
            if not external_message_id:
                external_message_id = self._fallback_message_id(item, external_chat_id, text, direction)

            messages.append(
                UnifiedMessage(
                    external_message_id=external_message_id,
                    external_chat_id=external_chat_id,
                    direction=direction,
                    text=str(text),
                    author="seller" if is_seller else (author_name or user_type or "customer"),
                    created_at=item.get("created_at") or item.get("createdAt"),
                    raw={**item, "_crm_author_name": author_name, "_crm_author_public_id": author_public_id},
                )
            )

        messages.sort(key=lambda m: m.created_at or "")
        return messages

    async def send_message(self, external_chat_id: str, text: str) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise RuntimeError("OZON_CLIENT_ID/OZON_API_KEY are not configured")
        payload = {"chat_id": external_chat_id, "text": text}
        return await self._post("/v1/chat/send/message", payload)



    @staticmethod
    def _find_nested_value(payload: Any, key_names: set[str]) -> Any:
        """Find first nested value for any key from key_names."""
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).lower() in key_names and value not in (None, ""):
                    return value
            for value in payload.values():
                found = OzonConnector._find_nested_value(value, key_names)
                if found not in (None, ""):
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = OzonConnector._find_nested_value(item, key_names)
                if found not in (None, ""):
                    return found
        return None

    @staticmethod
    def _collect_review_media(payload: Any) -> list[str]:
        urls: list[str] = []
        def walk(value: Any, hint: str = "") -> None:
            if not value:
                return
            if isinstance(value, str):
                if value.startswith("http") and (
                    any(ext in value.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))
                    or any(word in hint.lower() for word in ("photo", "image", "picture", "media", "url"))
                ):
                    urls.append(value)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item, hint)
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    walk(nested, f"{hint} {key}")
        walk(payload)
        # dedupe, keep order
        seen = set()
        result = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result[:20]

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _nested_dict(payload: Any, *keys: str) -> dict[str, Any]:
        current = payload
        for key in keys:
            if not isinstance(current, dict):
                return {}
            current = current.get(key)
        return current if isinstance(current, dict) else {}

    @classmethod
    def _review_product_name(cls, item: dict[str, Any]) -> str | None:
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        product_info = item.get("product_info") if isinstance(item.get("product_info"), dict) else {}
        sku_info = item.get("sku_info") if isinstance(item.get("sku_info"), dict) else {}
        return cls._clean_person_value(
            cls._first_non_empty(
                item.get("product_name"), item.get("productName"), item.get("model_name"), item.get("modelName"),
                product.get("name"), product.get("title"), product.get("product_name"), product.get("model_name"),
                product_info.get("name"), product_info.get("title"), product_info.get("product_name"), product_info.get("model_name"),
                sku_info.get("name"), sku_info.get("title"), sku_info.get("product_name"), sku_info.get("model_name"),
            )
        )

    @classmethod
    def _review_text(cls, item: dict[str, Any]) -> str:
        direct = cls._first_non_empty(
            item.get("text"), item.get("review_text"), item.get("reviewText"), item.get("content"),
            item.get("comment"), item.get("message"), item.get("description"),
        )
        if direct:
            return str(direct).strip()
        pieces: list[str] = []
        for label, keys in (
            ("Плюсы", ("advantages", "pros", "positive", "dignity", "plus")),
            ("Минусы", ("disadvantages", "cons", "negative", "defects", "minus")),
            ("Комментарий", ("commentary", "comment_text", "review_comment", "additional")),
        ):
            value = None
            for key in keys:
                value = item.get(key)
                if value:
                    break
            if value:
                pieces.append(f"{label}: {value}")
        return "\n".join(pieces).strip()

    @classmethod
    def _normalize_review(cls, item: dict[str, Any], comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        comments = comments or []
        # Some Ozon responses wrap the actual object in result/review. Keep parsing tolerant.
        if isinstance(item.get("review"), dict):
            item = {**item, **item["review"]}
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        author_obj = item.get("author") if isinstance(item.get("author"), dict) else {}
        external_review_id = str(
            item.get("id")
            or item.get("review_id")
            or item.get("reviewId")
            or item.get("uuid")
            or item.get("review_uuid")
            or ""
        )
        sku = cls._first_non_empty(
            item.get("sku"), item.get("product_sku"), item.get("productSku"),
            item.get("sku_id"), product.get("sku"), product.get("sku_id")
        )
        product_name = cls._review_product_name(item)
        author_name = cls._clean_person_value(
            cls._first_non_empty(
                item.get("author_name"), item.get("authorName"), item.get("buyer_name"), item.get("buyerName"),
                item.get("client_name"), item.get("clientName"), author_obj.get("name")
            )
        )
        text = cls._review_text(item)
        published_at = cls._first_non_empty(
            item.get("published_at"), item.get("publishedAt"), item.get("created_at"), item.get("createdAt"),
            item.get("date"), item.get("updated_at"), item.get("updatedAt")
        )
        rating = cls._first_non_empty(item.get("rating"), item.get("score"), item.get("stars"), item.get("rate"))
        try:
            rating = int(rating) if rating not in (None, "") else None
        except Exception:
            rating = None
        status = cls._first_non_empty(
            item.get("status"), item.get("state"), item.get("processing_status"), item.get("processingStatus"),
            item.get("review_status"), item.get("reviewStatus")
        )
        posting_number = cls._find_nested_value(item, {"posting_number", "postingnumber"})
        reply_text = cls._first_non_empty(item.get("reply_text"), item.get("seller_answer"), item.get("answer"))
        reply_created_at = cls._first_non_empty(item.get("reply_created_at"), item.get("answer_created_at"))
        for c in comments:
            if not isinstance(c, dict):
                continue
            ctext = cls._first_non_empty(c.get("text"), c.get("comment"), c.get("message"), c.get("content"))
            if ctext:
                reply_text = str(ctext)
                reply_created_at = cls._first_non_empty(c.get("created_at"), c.get("createdAt"), c.get("published_at"), c.get("publishedAt"))
                break
        return {
            "marketplace": "ozon",
            "external_review_id": external_review_id,
            "sku": str(sku or ""),
            "product_name": product_name or (f"SKU {sku}" if sku else "Модель не указана"),
            "rating": rating,
            "status": str(status or ""),
            "author_name": author_name,
            "text": str(text or ""),
            "published_at": published_at,
            "comments_amount": int(cls._first_non_empty(item.get("comments_amount"), item.get("commentsAmount"), len(comments), 0) or 0),
            "photos_amount": int(cls._first_non_empty(item.get("photos_amount"), item.get("photosAmount"), 0) or 0),
            "videos_amount": int(cls._first_non_empty(item.get("videos_amount"), item.get("videosAmount"), 0) or 0),
            "reply_text": reply_text,
            "reply_created_at": reply_created_at,
            "posting_number": str(posting_number or ""),
            "media": cls._collect_review_media(item),
            "comments": comments,
            "raw": item,
        }

    @staticmethod
    def _review_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        result = OzonConnector._result(data)
        items = result.get("reviews") or result.get("items") or result.get("result") or []
        if isinstance(items, dict):
            items = items.get("reviews") or items.get("items") or items.get("list") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _review_last_id(data: dict[str, Any] | None) -> str | None:
        result = OzonConnector._result(data)
        value = result.get("last_id") or result.get("lastId")
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _review_has_next(data: dict[str, Any] | None, items_count: int) -> bool:
        result = OzonConnector._result(data)
        value = result.get("has_next") if "has_next" in result else result.get("hasNext")
        if value is None:
            return items_count > 0
        return bool(value)

    async def get_review_info(self, review_id: str) -> dict[str, Any]:
        if not self.client_id or not self.api_key or not review_id:
            return {}
        # Main documented shape is review_id. Some SDKs mirror the field as id, so we try both.
        for payload in ({"review_id": review_id}, {"id": review_id}):
            status, text, data = await self._post_no_raise("/v1/review/info", payload)
            if status < 400 and data:
                result = self._result(data)
                if isinstance(result.get("review"), dict):
                    return result["review"]
                return result if isinstance(result, dict) else {}
        return {}

    async def get_review_comments(self, review_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not self.client_id or not self.api_key:
            return []
        payload = {"review_id": review_id, "limit": max(1, min(100, int(limit))), "sort_dir": "ASC"}
        status, text, data = await self._post_no_raise("/v1/review/comment/list", payload)
        if status >= 400:
            # comments can require Premium Plus; don't break review sync because of it
            return []
        result = self._result(data)
        comments = result.get("comments") or result.get("items") or []
        return [item for item in comments if isinstance(item, dict)] if isinstance(comments, list) else []

    def _review_payload_variants(self) -> list[tuple[str, dict[str, Any]]]:
        """Payload shapes for /v1/review/list.

        The documented request body contains only last_id, limit, sort_dir and status.
        We intentionally avoid unsupported wrappers like {"filter": ...}, because Ozon
        can reject the whole request and the CRM used to hide that as an empty list.
        """
        raw_statuses = os.getenv("OZON_REVIEWS_STATUSES", "UNPROCESSED,PROCESSED")
        statuses = [s.strip() for s in raw_statuses.split(",") if s.strip()]
        variants: list[tuple[str, dict[str, Any]]] = [("all_without_status", {})]
        for status in statuses:
            variants.append((status.lower(), {"status": status}))
        return variants

    @staticmethod
    def _review_id_from_item(item: dict[str, Any]) -> str:
        return str(
            item.get("id")
            or item.get("review_id")
            or item.get("reviewId")
            or item.get("uuid")
            or item.get("review_uuid")
            or ""
        )

    async def list_reviews(self, *, limit: int | None = None, pages: int | None = None) -> list[dict[str, Any]]:
        """Return normalized Ozon reviews: old and new.

        Uses /v1/review/list for IDs and /v1/review/info for full fields such as
        model/product name, stars and full review text. If Ozon rejects review methods
        because of missing Premium Plus/API role, the error is now surfaced instead of
        silently returning an empty reviews section.
        """
        if not self.client_id or not self.api_key:
            return []
        limit = max(20, min(100, int(limit or os.getenv("OZON_REVIEWS_SYNC_LIMIT", "100"))))
        pages = max(1, min(50, int(pages or os.getenv("OZON_REVIEWS_SYNC_PAGES", "10"))))
        include_info = os.getenv("OZON_REVIEWS_FETCH_INFO", "true").lower() not in {"0", "false", "no", "off"}
        include_comments = os.getenv("OZON_REVIEWS_FETCH_COMMENTS", "false").lower() in {"1", "true", "yes", "on"}

        seen: dict[str, dict[str, Any]] = {}
        debug: dict[str, Any] = {
            "limit": limit,
            "pages": pages,
            "variants": [],
            "reviews_seen": 0,
            "include_info": include_info,
            "include_comments": include_comments,
        }
        hard_errors: list[str] = []
        ok_requests = 0

        for variant_name, extra in self._review_payload_variants():
            last_id = ""
            variant_debug: dict[str, Any] = {"variant": variant_name, "requests": 0, "items": 0, "status_code": None, "error": None}
            for _ in range(pages):
                payload: dict[str, Any] = {"limit": limit, "sort_dir": "DESC", **extra}
                if last_id:
                    payload["last_id"] = last_id
                status, text, data = await self._post_no_raise("/v1/review/list", payload)
                variant_debug["requests"] += 1
                variant_debug["status_code"] = status
                if status >= 400:
                    # Missing role/Premium Plus must be visible to the operator. Unsupported
                    # status values are kept in diagnostics but do not stop other variants.
                    message = f"{variant_name}: HTTP {status}: {text[:700]}"
                    variant_debug["error"] = text[:1000]
                    hard_errors.append(message)
                    break

                ok_requests += 1
                items = self._review_items(data)
                variant_debug["items"] += len(items)
                if items:
                    variant_debug["first_item_keys"] = list(items[0].keys())[:40]
                    variant_debug["sample_review_ids"] = [self._review_id_from_item(i) for i in items[:5]]
                for item in items:
                    rid = self._review_id_from_item(item)
                    if not rid or rid in seen:
                        continue
                    info: dict[str, Any] = {}
                    if include_info:
                        info = await self.get_review_info(rid)
                    merged = {**item, **info} if info else item
                    comments = await self.get_review_comments(rid, limit=100) if include_comments else []
                    norm = self._normalize_review(merged, comments=comments)
                    if norm.get("external_review_id"):
                        seen[rid] = norm
                next_last_id = self._review_last_id(data)
                if not items or not self._review_has_next(data, len(items)) or not next_last_id or next_last_id == last_id:
                    break
                last_id = next_last_id
            debug["variants"].append(variant_debug)

        debug["reviews_seen"] = len(seen)
        debug["ok_requests"] = ok_requests
        debug["errors_count"] = len(hard_errors)
        debug["errors"] = hard_errors[:5]
        self.last_reviews_debug = debug

        if not seen and ok_requests == 0 and hard_errors:
            raise RuntimeError("Ozon Reviews API не вернул отзывы. Последняя ошибка: " + hard_errors[0])
        return list(seen.values())

    async def reviews_diagnostics(self) -> dict[str, Any]:
        """Safe diagnostics for Ozon reviews without exposing API keys."""
        if not self.client_id or not self.api_key:
            return {"configured": False, "client_id_present": bool(self.client_id), "api_key_present": bool(self.api_key)}
        out: dict[str, Any] = {
            "configured": True,
            "client_id_present": bool(self.client_id),
            "api_key_present": bool(self.api_key),
            "list_variants": [],
            "last_reviews_debug": getattr(self, "last_reviews_debug", {}),
        }
        status, text, data = await self._post_no_raise("/v1/review/count", {})
        out["count_request"] = {
            "status_code": status,
            "ok": status < 400,
            "body_preview": None if status < 400 else text[:1200],
            "keys": list((self._result(data) if data else {}).keys())[:50] if status < 400 else [],
        }
        for name, extra in self._review_payload_variants():
            payload = {"limit": 20, "sort_dir": "DESC", **extra}
            status, text, data = await self._post_no_raise("/v1/review/list", payload)
            items = self._review_items(data) if status < 400 else []
            row: dict[str, Any] = {
                "variant": name,
                "request": payload,
                "status_code": status,
                "items_count": len(items),
                "error_body": None if status < 400 else text[:1500],
                "response_keys": list((self._result(data) if data else {}).keys())[:50] if status < 400 else [],
            }
            if items:
                first = items[0]
                row.update({
                    "sample_review_ids": [self._review_id_from_item(i) for i in items[:5]],
                    "first_item_keys": list(first.keys())[:50],
                    "first_item_rating": first.get("rating"),
                    "first_item_text_preview": str(first.get("text") or "")[:140],
                    "first_item_sku": first.get("sku"),
                    "first_item_status": first.get("status"),
                })
            out["list_variants"].append(row)
        return out

    async def reply_to_review(self, review_id: str, text: str) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise RuntimeError("OZON_CLIENT_ID/OZON_API_KEY are not configured")
        return await self._post("/v1/review/comment/create", {"review_id": review_id, "text": text})

    async def change_review_status(self, review_ids: list[str], status: str = "PROCESSED") -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise RuntimeError("OZON_CLIENT_ID/OZON_API_KEY are not configured")
        return await self._post("/v1/review/change-status", {"review_ids": review_ids[:100], "status": status})

    # -----------------------------
    # Ozon product questions / answers
    # -----------------------------

    def _question_payload_variants(self) -> list[tuple[str, dict[str, Any]]]:
        raw_statuses = os.getenv("OZON_QUESTIONS_STATUSES", "UNPROCESSED,PROCESSED")
        statuses = [s.strip() for s in raw_statuses.split(",") if s.strip()]
        variants: list[tuple[str, dict[str, Any]]] = [("all_without_status", {})]
        for status in statuses:
            # Ozon has changed question/list examples over time. Try both the
            # direct documented style and the older filter-wrapper style. The
            # sync keeps the first successful shape and records failures in diagnostics.
            variants.append((status.lower(), {"status": status}))
            variants.append((f"filter_{status.lower()}", {"filter": {"status": status}}))
        return variants

    @staticmethod
    def _question_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        result = OzonConnector._result(data)
        items = result.get("questions") or result.get("items") or result.get("list") or result.get("result") or []
        if isinstance(items, dict):
            items = items.get("questions") or items.get("items") or items.get("list") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _question_last_id(data: dict[str, Any] | None) -> str | None:
        result = OzonConnector._result(data)
        value = result.get("last_id") or result.get("lastId")
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _question_has_next(data: dict[str, Any] | None, items_count: int) -> bool:
        result = OzonConnector._result(data)
        value = result.get("has_next") if "has_next" in result else result.get("hasNext")
        if value is None:
            return items_count > 0
        return bool(value)

    @staticmethod
    def _question_id_from_item(item: dict[str, Any]) -> str:
        """Return the API question_id used by /v1/question/answer/create.

        Ozon responses may contain several generic id fields. For answering we
        must prefer question-specific identifiers; using a generic nested answer
        id can make /answer/create fail.
        """
        if isinstance(item.get("question"), dict):
            question_obj = item.get("question") or {}
            candidates = (
                item.get("_crm_question_id"),
                item.get("question_id"),
                item.get("questionId"),
                question_obj.get("question_id"),
                question_obj.get("questionId"),
                question_obj.get("id"),
                item.get("id"),
                item.get("uuid"),
            )
        else:
            candidates = (
                item.get("_crm_question_id"),
                item.get("question_id"),
                item.get("questionId"),
                item.get("id"),
                item.get("uuid"),
            )
        for value in candidates:
            if value not in (None, ""):
                return str(value).strip()
        nested = OzonConnector._find_nested_value(item, {"question_id", "questionid", "question_uuid", "questionuuid"})
        return str(nested).strip() if nested not in (None, "") else ""

    @classmethod
    def _question_product_name(cls, item: dict[str, Any]) -> str | None:
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        product_info = item.get("product_info") if isinstance(item.get("product_info"), dict) else {}
        sku_info = item.get("sku_info") if isinstance(item.get("sku_info"), dict) else {}
        return cls._clean_person_value(
            cls._first_non_empty(
                item.get("product_name"), item.get("productName"), item.get("model_name"), item.get("modelName"),
                item.get("name"), item.get("title"),
                product.get("name"), product.get("title"), product.get("product_name"), product.get("model_name"),
                product_info.get("name"), product_info.get("title"), product_info.get("product_name"), product_info.get("model_name"),
                sku_info.get("name"), sku_info.get("title"), sku_info.get("product_name"), sku_info.get("model_name"),
            )
        )

    @classmethod
    def _question_text(cls, item: dict[str, Any]) -> str:
        direct = cls._first_non_empty(
            item.get("text"), item.get("question_text"), item.get("questionText"),
            item.get("content"), item.get("message"), item.get("body"), item.get("comment"),
        )
        if direct:
            return str(direct).strip()
        question = item.get("question") if isinstance(item.get("question"), dict) else {}
        direct = cls._first_non_empty(question.get("text"), question.get("question_text"), question.get("content"))
        return str(direct or "").strip()

    @classmethod
    def _question_answer_text(cls, item: dict[str, Any]) -> tuple[str | None, str | None]:
        direct = cls._first_non_empty(
            item.get("answer_text"), item.get("answerText"), item.get("answer"), item.get("seller_answer"),
            item.get("sellerAnswer"), item.get("response"),
        )
        created_at = cls._first_non_empty(item.get("answer_created_at"), item.get("answerCreatedAt"), item.get("answered_at"), item.get("answeredAt"))
        if isinstance(direct, dict):
            created_at = cls._first_non_empty(created_at, direct.get("created_at"), direct.get("createdAt"), direct.get("published_at"), direct.get("publishedAt"))
            direct = cls._first_non_empty(direct.get("text"), direct.get("message"), direct.get("content"), direct.get("answer"))
        if not direct:
            answers = item.get("answers") or item.get("comments") or []
            if isinstance(answers, list):
                for answer in answers:
                    if not isinstance(answer, dict):
                        continue
                    text = cls._first_non_empty(answer.get("text"), answer.get("message"), answer.get("content"), answer.get("answer"))
                    if text:
                        created_at = cls._first_non_empty(created_at, answer.get("created_at"), answer.get("createdAt"), answer.get("published_at"), answer.get("publishedAt"))
                        direct = text
                        break
        return (str(direct).strip() if direct else None, str(created_at) if created_at else None)

    @classmethod
    def _normalize_question(cls, item: dict[str, Any]) -> dict[str, Any]:
        raw_item = item
        if isinstance(item.get("question"), dict):
            item = {**item, **item["question"]}
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        author_obj = item.get("author") if isinstance(item.get("author"), dict) else {}
        external_question_id = cls._question_id_from_item(item)
        sku = cls._first_non_empty(
            item.get("sku"), item.get("product_sku"), item.get("productSku"), item.get("sku_id"),
            product.get("sku"), product.get("sku_id"), cls._find_nested_value(item, {"sku", "sku_id", "product_sku"})
        )
        product_name = cls._question_product_name(item)
        product_url = cls._first_non_empty(
            item.get("product_url"), item.get("productUrl"), item.get("url"), item.get("link"),
            product.get("url"), product.get("link"), product.get("product_url"), product.get("productUrl"),
        )
        author_name = cls._clean_person_value(
            cls._first_non_empty(
                item.get("author_name"), item.get("authorName"), item.get("buyer_name"), item.get("buyerName"),
                item.get("client_name"), item.get("clientName"), author_obj.get("name"), author_obj.get("display_name")
            )
        )
        status = cls._first_non_empty(item.get("status"), item.get("state"), item.get("question_status"), item.get("questionStatus"))
        published_at = cls._first_non_empty(
            item.get("published_at"), item.get("publishedAt"), item.get("created_at"), item.get("createdAt"),
            item.get("date"), item.get("updated_at"), item.get("updatedAt")
        )
        answer_text, answer_created_at = cls._question_answer_text(item)
        return {
            "external_question_id": external_question_id,
            "sku": str(sku or ""),
            "product_name": product_name or (f"SKU {sku}" if sku else "Товар не указан"),
            "product_url": str(product_url or ""),
            "status": str(status or ""),
            "author_name": author_name,
            "text": cls._question_text(item),
            "published_at": published_at,
            "answer_text": answer_text,
            "answer_created_at": answer_created_at,
            "raw": raw_item,
        }

    async def get_question_info(self, question_id: str) -> dict[str, Any]:
        if not self.client_id or not self.api_key or not question_id:
            return {}
        for payload in ({"question_id": question_id}, {"id": question_id}):
            status, text, data = await self._post_no_raise("/v1/question/info", payload)
            if status < 400 and data:
                result = self._result(data)
                if isinstance(result.get("question"), dict):
                    return result["question"]
                return result if isinstance(result, dict) else {}
        return {}

    async def list_questions(self, *, limit: int | None = None, pages: int | None = None) -> list[dict[str, Any]]:
        if not self.client_id or not self.api_key:
            return []
        limit = max(1, min(100, int(limit or os.getenv("OZON_QUESTIONS_SYNC_LIMIT", "50"))))
        pages = max(1, min(50, int(pages or os.getenv("OZON_QUESTIONS_SYNC_PAGES", "3"))))
        include_info = os.getenv("OZON_QUESTIONS_FETCH_INFO", "true").lower() not in {"0", "false", "no", "off"}

        seen: dict[str, dict[str, Any]] = {}
        debug: dict[str, Any] = {
            "limit": limit,
            "pages": pages,
            "include_info": include_info,
            "variants": [],
            "questions_seen": 0,
        }
        hard_errors: list[str] = []
        ok_requests = 0

        for variant_name, extra in self._question_payload_variants():
            last_id = ""
            offset = 0
            variant_debug: dict[str, Any] = {"variant": variant_name, "requests": 0, "items": 0, "status_code": None, "error": None}
            for _ in range(pages):
                payload: dict[str, Any] = {"limit": limit, **extra}
                sort_dir = os.getenv("OZON_QUESTIONS_SORT_DIR", "DESC").strip().upper()
                if sort_dir in {"ASC", "DESC"}:
                    payload["sort_dir"] = sort_dir
                if last_id:
                    payload["last_id"] = last_id
                elif offset:
                    payload["offset"] = offset
                status, text, data = await self._post_no_raise("/v1/question/list", payload)
                variant_debug["requests"] += 1
                variant_debug["status_code"] = status
                if status >= 400:
                    message = f"{variant_name}: HTTP {status}: {text[:700]}"
                    variant_debug["error"] = text[:1000]
                    hard_errors.append(message)
                    break

                ok_requests += 1
                items = self._question_items(data)
                variant_debug["items"] += len(items)
                if items:
                    variant_debug["first_item_keys"] = list(items[0].keys())[:50]
                    variant_debug["sample_question_ids"] = [self._question_id_from_item(i) for i in items[:5]]
                for item in items:
                    qid = self._question_id_from_item(item)
                    if not qid or qid in seen:
                        continue
                    info: dict[str, Any] = {}
                    if include_info:
                        info = await self.get_question_info(qid)
                    merged = {**item, **info, "_crm_question_id": qid} if info else {**item, "_crm_question_id": qid}
                    norm = self._normalize_question(merged)
                    if not norm.get("external_question_id"):
                        norm["external_question_id"] = qid
                    if norm.get("external_question_id"):
                        seen[qid] = norm
                next_last_id = self._question_last_id(data)
                if not items or not self._question_has_next(data, len(items)):
                    break
                if next_last_id and next_last_id != last_id:
                    last_id = next_last_id
                else:
                    offset += limit
            debug["variants"].append(variant_debug)

        debug["questions_seen"] = len(seen)
        debug["ok_requests"] = ok_requests
        debug["errors_count"] = len(hard_errors)
        debug["errors"] = hard_errors[:5]
        self.last_questions_debug = debug

        if not seen and ok_requests == 0 and hard_errors:
            raise RuntimeError("Ozon Questions API не вернул вопросы. Последняя ошибка: " + hard_errors[0])
        return list(seen.values())

    async def questions_diagnostics(self) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            return {"configured": False, "client_id_present": bool(self.client_id), "api_key_present": bool(self.api_key)}
        out: dict[str, Any] = {
            "configured": True,
            "client_id_present": bool(self.client_id),
            "api_key_present": bool(self.api_key),
            "list_variants": [],
            "last_questions_debug": getattr(self, "last_questions_debug", {}),
        }
        for name, extra in self._question_payload_variants():
            payload = {"limit": 20, **extra}
            status, text, data = await self._post_no_raise("/v1/question/list", payload)
            items = self._question_items(data) if status < 400 else []
            row: dict[str, Any] = {
                "variant": name,
                "request": payload,
                "status_code": status,
                "items_count": len(items),
                "error_body": None if status < 400 else text[:1500],
                "response_keys": list((self._result(data) if data else {}).keys())[:50] if status < 400 else [],
            }
            if items:
                first = items[0]
                row.update({
                    "sample_question_ids": [self._question_id_from_item(i) for i in items[:5]],
                    "first_item_keys": list(first.keys())[:50],
                    "first_item_text_preview": str(first.get("text") or first.get("question_text") or "")[:140],
                    "first_item_sku": first.get("sku"),
                    "first_item_status": first.get("status"),
                })
            out["list_variants"].append(row)
        return out

    async def answer_question(self, question_id: str, text: str, sku: int | str | None = None) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise RuntimeError("OZON_CLIENT_ID/OZON_API_KEY are not configured")
        question_id = str(question_id or "").strip()
        text = (text or "").strip()
        if not question_id:
            raise RuntimeError("Не найден Ozon question_id для отправки ответа. Обновите вопросы Ozon и попробуйте снова.")
        if not text:
            raise RuntimeError("Ответ не может быть пустым")

        # /v1/question/answer/create validates BOTH question_id and sku.
        # Without sku Ozon returns: QuestionAnswerCreateRequest.Sku must be > 0.
        try:
            sku_int = int(str(sku or "").strip())
        except Exception:
            sku_int = 0
        if sku_int <= 0:
            raise RuntimeError("Не найден корректный Ozon SKU для ответа на вопрос. Нажмите «Обновить» в разделе вопросов, затем откройте вопрос заново. Если ошибка останется — пришлите /api/debug/ozon/questions.")

        attempts = [
            ("text", {"question_id": question_id, "sku": sku_int, "text": text}),
            ("answer", {"question_id": question_id, "sku": sku_int, "answer": text}),
            ("answer_object", {"question_id": question_id, "sku": sku_int, "answer": {"text": text}}),
        ]
        errors: list[str] = []
        for attempt_name, payload in attempts:
            status, body, data = await self._post_no_raise("/v1/question/answer/create", payload)
            if status < 400:
                return data or {"ok": True, "attempt": attempt_name, "question_id": question_id, "sku": sku_int}
            errors.append(f"{attempt_name}: HTTP {status}: {body[:900]}")
        raise RuntimeError("Ozon question answer create failed. question_id="
                           f"{question_id}, sku={sku_int}. Attempts: " + " | ".join(errors))

    async def change_question_status(self, question_ids: list[str], status: str = "PROCESSED") -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise RuntimeError("OZON_CLIENT_ID/OZON_API_KEY are not configured")
        ids = [str(qid) for qid in question_ids if qid]
        if not ids:
            return {"ok": True, "changed": 0}
        # Ozon method path uses underscore: /v1/question/change_status.
        return await self._post("/v1/question/change_status", {"question_ids": ids[:100], "status": status})

    async def start_chat_by_posting(self, posting_number: str) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise RuntimeError("OZON_CLIENT_ID/OZON_API_KEY are not configured")
        if not posting_number:
            raise RuntimeError("В отзыве нет posting_number. Ozon создаёт чат через /v1/chat/start только по номеру отправления.")
        return await self._post("/v1/chat/start", {"posting_number": posting_number})

    async def diagnostics(self) -> dict[str, Any]:
        """Return safe diagnostics for troubleshooting Ozon sync.

        Не возвращаем API-key, только признак наличия ключей, результаты разных фильтров
        и примеры chat_id.
        """
        if not self.client_id or not self.api_key:
            return {"configured": False, "error": "OZON_CLIENT_ID/OZON_API_KEY are not configured"}

        limit = 10
        variants = []
        for variant_name, payload in self._list_payload_variants(limit=limit, offset=0):
            status_code, text, data = await self._post_no_raise("/v3/chat/list", payload)
            items = self._page_items(data)
            result = self._result(data)
            variants.append(
                {
                    "variant": variant_name,
                    "request": payload,
                    "status_code": status_code,
                    "total_chats_count": result.get("total_chats_count"),
                    "total_unread_count": result.get("total_unread_count"),
                    "items_count": len(items),
                    "sample_chat_ids": [self._chat_id_from_item(item) for item in items[:5]],
                    "first_item_keys": list(items[0].keys()) if items else [],
                    "first_item_chat_keys": list(items[0].get("chat", {}).keys()) if items and isinstance(items[0].get("chat"), dict) else [],
                    "first_item_customer_name": self._extract_customer_name_from_chat_item(items[0]) if items else None,
                    "first_item_chat_type": self._chat_type_from_item(items[0]) if items else None,
                    "first_item_chat_title": self._chat_title_from_item(items[0]) if items else None,
                    "first_item_looks_like_support": self._looks_like_support_chat(items[0]) if items else None,
                    "first_item_looks_like_system_by_list_metadata": self._looks_like_support_chat(items[0]) if items else None,
                    "error_body": text[:1000] if status_code >= 400 else None,
                }
            )
        return {
            "configured": True,
            "client_id_present": bool(self.client_id),
            "api_key_present": bool(self.api_key),
            "variants": variants,
            "last_sync_debug": self.last_sync_debug,
        }
