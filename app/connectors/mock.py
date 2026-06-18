from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

from app.connectors.base import MarketplaceConnector, UnifiedChat, UnifiedMessage

_counter = itertools.count(1000)


class MockConnector(MarketplaceConnector):
    marketplace = "mock"

    async def list_chats(self) -> list[UnifiedChat]:
        return [
            UnifiedChat(
                marketplace="yandex",
                external_chat_id="ym-chat-1001",
                customer_name="Анна",
                customer_public_id="ym-public-44",
                order_id="YM-557001",
                status="new",
                metadata={"source": "mock", "type": "ORDER"},
            ),
            UnifiedChat(
                marketplace="wildberries",
                external_chat_id="wb-chat-888",
                customer_name="Покупатель WB",
                customer_public_id="wb-anon-888",
                order_id="WB-119203",
                status="in_progress",
                metadata={"source": "mock"},
            ),
            UnifiedChat(
                marketplace="ozon",
                external_chat_id="ozon-chat-73",
                customer_name="Клиент Ozon",
                customer_public_id="ozon-buyer-73",
                order_id="OZON-40192",
                status="waiting_customer",
                metadata={"source": "mock"},
            ),
        ]

    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        samples = {
            "ym-chat-1001": [
                "Здравствуйте! Когда отправите заказ?",
                "И ещё можно поменять цвет на чёрный?",
            ],
            "wb-chat-888": [
                "Добрый день. Товар пришёл с повреждённой упаковкой.",
            ],
            "ozon-chat-73": [
                "Подскажите, подойдёт ли этот аксессуар к модели X5?",
                "Жду ответа, хочу заказать сегодня.",
            ],
        }
        return [
            UnifiedMessage(
                external_message_id=f"mock-msg-{external_chat_id}-{idx}",
                external_chat_id=external_chat_id,
                direction="inbound",
                text=text,
                author="customer",
                created_at=datetime.now(timezone.utc).isoformat(),
                raw={"mock": True},
            )
            for idx, text in enumerate(samples.get(external_chat_id, []), start=1)
        ]

    async def send_message(self, external_chat_id: str, text: str) -> dict[str, Any]:
        return {
            "ok": True,
            "mock": True,
            "message_id": f"mock-out-{next(_counter)}",
            "external_chat_id": external_chat_id,
            "text": text,
        }
