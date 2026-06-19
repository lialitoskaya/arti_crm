from __future__ import annotations

import os
from typing import Any

import httpx

from app.connectors.base import MarketplaceConnector, UnifiedChat, UnifiedMessage


class YandexMarketConnector(MarketplaceConnector):
    """Yandex Market Partner API chats connector.

    Uses official chat methods:
    - POST /v2/businesses/{businessId}/chats
    - POST /v2/businesses/{businessId}/chats/history?chatId=...
    - POST /v2/businesses/{businessId}/chats/message?chatId=...
    """

    marketplace = "yandex"
    base_url = "https://api.partner.market.yandex.ru"

    def __init__(self) -> None:
        self.token = os.getenv("YANDEX_MARKET_TOKEN", "")
        self.business_id = os.getenv("YANDEX_MARKET_BUSINESS_ID", "")
        self.campaign_id = os.getenv("YANDEX_MARKET_CAMPAIGN_ID", "")
        try:
            self.max_chats = max(1, min(200, int(os.getenv("YANDEX_SYNC_MAX_CHATS", "50"))))
        except Exception:
            self.max_chats = 50
        try:
            self.max_pages = max(1, min(20, int(os.getenv("YANDEX_SYNC_MAX_PAGES", "3"))))
        except Exception:
            self.max_pages = 3

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Api-Key": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/v2/businesses/{self.business_id}{path}"

    async def _post(self, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(self._url(path), headers=self.headers, params=params or {}, json=json_body or {})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Yandex Market API error {response.status_code} at {path}: {response.text[:1500]}") from exc
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "ERROR":
            raise RuntimeError(f"Yandex Market API returned ERROR at {path}: {str(data)[:1500]}")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _result(data: dict[str, Any]) -> dict[str, Any]:
        result = data.get("result")
        return result if isinstance(result, dict) else data

    @staticmethod
    def _status_to_crm(status: str | None) -> str:
        s = str(status or "").upper()
        if s in {"NEW", "WAITING_FOR_PARTNER"}:
            return "new"
        if s == "WAITING_FOR_CUSTOMER":
            return "waiting_customer"
        if s == "FINISHED":
            return "closed"
        return "in_progress"

    async def list_chats(self) -> list[UnifiedChat]:
        if not self.token or not self.business_id:
            return []

        chats: list[UnifiedChat] = []
        page_token: str | None = None
        pages = 0
        while len(chats) < self.max_chats and pages < self.max_pages:
            pages += 1
            params: dict[str, Any] = {"limit": 20}
            if page_token:
                params["pageToken"] = page_token
            # Only buyer chats, not arbitration. Do not filter statuses so archive/active state is preserved.
            data = await self._post("/chats", params=params, json_body={"types": ["CHAT"]})
            result = self._result(data)
            raw_chats = result.get("chats") or []
            if not isinstance(raw_chats, list):
                raw_chats = []

            for item in raw_chats:
                if not isinstance(item, dict):
                    continue
                chat_id = item.get("chatId") or item.get("id")
                if not chat_id:
                    continue
                context = item.get("context") if isinstance(item.get("context"), dict) else {}
                customer = context.get("customer") if isinstance(context.get("customer"), dict) else {}
                name = customer.get("name") or item.get("customerName") or item.get("buyerName")
                public_id = customer.get("publicId") or customer.get("public_id")
                order_id = item.get("orderId") or context.get("orderId") or context.get("returnId") or ""
                raw_status = str(item.get("status") or "").upper()
                metadata = dict(item)
                metadata["_sync_hint"] = {
                    "yandex_chat_status": raw_status,
                    "chat_status": raw_status,
                    "yandex_needs_partner_reply": raw_status in {"NEW", "WAITING_FOR_PARTNER"},
                }
                chats.append(
                    UnifiedChat(
                        marketplace=self.marketplace,
                        external_chat_id=str(chat_id),
                        customer_name=str(name) if name else None,
                        customer_public_id=str(public_id) if public_id else None,
                        order_id=str(order_id) if order_id else None,
                        status=self._status_to_crm(item.get("status")),
                        metadata=metadata,
                    )
                )
                if len(chats) >= self.max_chats:
                    break

            paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
            page_token = paging.get("nextPageToken")
            if not page_token or not raw_chats:
                break
        return chats

    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        if not self.token or not self.business_id:
            return []
        data = await self._post(
            "/chats/history",
            params={"chatId": external_chat_id, "limit": 100},
            json_body={},
        )
        result = self._result(data)
        raw_messages = result.get("messages") or []
        if not isinstance(raw_messages, list):
            raw_messages = []

        messages: list[UnifiedMessage] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender") or "").upper()
            if sender == "PARTNER":
                direction = "outbound"
                author = "seller"
            elif sender == "CUSTOMER":
                direction = "inbound"
                author = "customer"
            else:
                direction = "internal"
                author = sender.lower() or "market"

            text = str(item.get("message") or "").strip()
            payload = item.get("payload") if isinstance(item.get("payload"), list) else []
            attachment_lines: list[str] = []
            for file_item in payload:
                if isinstance(file_item, dict):
                    name = file_item.get("name") or "файл"
                    url = file_item.get("url") or ""
                    attachment_lines.append(f"[{name}] {url}".strip())
            if attachment_lines:
                text = (text + "\n" if text else "") + "\n".join(attachment_lines)
            if not text:
                text = "[сообщение без текста / вложение]"

            msg_id = item.get("messageId") or item.get("id")
            messages.append(
                UnifiedMessage(
                    external_message_id=str(msg_id) if msg_id else None,
                    external_chat_id=external_chat_id,
                    direction=direction,
                    text=text,
                    author=author,
                    created_at=item.get("createdAt"),
                    raw=item,
                )
            )
        messages.sort(key=lambda m: m.created_at or "")
        return messages

    async def send_message(self, external_chat_id: str, text: str) -> dict[str, Any]:
        if not self.token or not self.business_id:
            raise RuntimeError("YANDEX_MARKET_TOKEN/YANDEX_MARKET_BUSINESS_ID are not configured")
        data = await self._post(
            "/chats/message",
            params={"chatId": external_chat_id},
            json_body={"message": text},
        )
        return data

    async def send_file(
        self,
        external_chat_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Send an image/file to a Yandex Market chat via multipart/form-data."""
        if not self.token or not self.business_id:
            raise RuntimeError("YANDEX_MARKET_TOKEN/YANDEX_MARKET_BUSINESS_ID are not configured")
        if len(content) > 5 * 1024 * 1024:
            raise RuntimeError("Yandex Market принимает файлы в чат до 5 МБ")
        headers = {
            "Api-Key": self.token,
            "Accept": "application/json",
        }
        safe_name = os.path.basename(filename or "image.jpg") or "image.jpg"
        files = {"file": (safe_name, content, content_type or "application/octet-stream")}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                self._url("/chats/file/send"),
                headers=headers,
                params={"chatId": external_chat_id},
                files=files,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Yandex Market API error {response.status_code} at /chats/file/send: {response.text[:1500]}") from exc
        try:
            data = response.json()
        except Exception:
            data = {"ok": True}
        if isinstance(data, dict) and data.get("status") == "ERROR":
            raise RuntimeError(f"Yandex Market API returned ERROR at /chats/file/send: {str(data)[:1500]}")
        return data if isinstance(data, dict) else {"ok": True}
