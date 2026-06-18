from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UnifiedChat:
    marketplace: str
    external_chat_id: str
    customer_name: str | None = None
    customer_public_id: str | None = None
    order_id: str | None = None
    status: str = "new"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnifiedMessage:
    external_message_id: str | None
    external_chat_id: str
    direction: str
    text: str
    author: str | None = None
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MarketplaceConnector(ABC):
    marketplace: str

    @abstractmethod
    async def list_chats(self) -> list[UnifiedChat]:
        """Return marketplace chats normalized to UnifiedChat."""

    @abstractmethod
    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        """Return chat messages normalized to UnifiedMessage."""

    @abstractmethod
    async def send_message(self, external_chat_id: str, text: str) -> dict[str, Any]:
        """Send message back to marketplace and return raw response."""
