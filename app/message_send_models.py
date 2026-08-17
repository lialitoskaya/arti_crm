from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final


MESSAGE_SEND_GUARANTEE: Final = (
    "Durable registration, не более одной одновременной marketplace-попытки "
    "для одной operation и отсутствие автоматического повтора после "
    "неоднозначного исхода. Новая автоматическая попытка разрешена только "
    "после структурированного результата, доказывающего отсутствие внешнего "
    "side effect."
)

MESSAGE_SEND_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "sending",
    "retry_wait",
    "accepted",
    "confirmed",
    "uncertain",
    "permanent_failed",
)
MESSAGE_SEND_ACTIVE_RECONCILIATION_STATUSES: Final[tuple[str, ...]] = (
    "sending",
    "retry_wait",
    "accepted",
    "uncertain",
)
MESSAGE_SEND_ECHO_CONFIRMABLE_STATUSES: Final[tuple[str, ...]] = (
    "sending",
    "accepted",
    "uncertain",
)
MESSAGE_SEND_RECONCILIATION_STATUS_SQL: Final = ", ".join(
    f"'{status}'" for status in MESSAGE_SEND_ACTIVE_RECONCILIATION_STATUSES
)

MESSAGE_SEND_ATTEMPT_TIMEOUT_SECONDS: Final = 60
MESSAGE_SEND_LEASE_SECONDS: Final = 90
MESSAGE_SEND_ECHO_MATCH_EARLY_SECONDS: Final = 120
MESSAGE_SEND_ECHO_MATCH_LATE_SECONDS: Final = 900
MESSAGE_SEND_MAX_SAFE_ATTEMPTS: Final = 5
MESSAGE_SEND_SAFE_BACKOFF_SECONDS: Final[tuple[int, ...]] = (5, 15, 45, 120, 300)

_SAFE_ERROR_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SAFE_PROVIDER_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_TOKEN_LIKE_RE = re.compile(
    r"(?i)((?:token|secret|api[_-]?key|cookie|authorization)"
    r"\s*[:=]\s*(?:bearer\s+)?\S+|"
    r"bearer\s+[A-Za-z0-9._~+/=-]+)"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d ()-]{7,}\d(?!\d)")


def canonical_message_payload(text: str) -> tuple[str, str, str]:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        raise ValueError("Message text must not be empty")
    payload_json = json.dumps(
        {"version": 1, "text": normalized_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return normalized_text, payload_json, payload_hash


def safe_error_category(value: str | None, *, fallback: str = "unknown") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _SAFE_ERROR_CATEGORY_RE.fullmatch(candidate) else fallback


def safe_provider_value(value: Any, *, limit: int) -> str | None:
    candidate = str(value or "").strip()[:limit]
    if not candidate or not _SAFE_PROVIDER_VALUE_RE.fullmatch(candidate):
        return None
    return candidate


def safe_error_summary(value: Any, *, fallback: str = "Marketplace send failed") -> str:
    summary = str(value or fallback)
    summary = _CONTROL_RE.sub(" ", summary)
    summary = _TOKEN_LIKE_RE.sub("[redacted]", summary)
    summary = _EMAIL_RE.sub("[redacted-email]", summary)
    summary = _PHONE_RE.sub("[redacted-phone]", summary)
    summary = " ".join(summary.split()).strip()
    return (summary or fallback)[:256]


def _is_crm_reserved_provider_key(key: Any) -> bool:
    normalized = str(key or "").strip().casefold()
    if not normalized:
        return False
    return bool(
        normalized.startswith("_crm")
        or normalized.startswith("crm_")
        or "crm_sent" in normalized
        or normalized
        in {
            "is_crm_sent",
            "crm_author_user_id",
            "crm_author_label",
            "client_operation_id",
        }
    )


def _sanitize_provider_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_provider_value(nested)
            for key, nested in value.items()
            if not _is_crm_reserved_provider_key(key)
        }
    if isinstance(value, list):
        return [_sanitize_provider_value(item) for item in value]
    return value


def sanitize_provider_payload(payload: Any) -> dict[str, Any]:
    """Deep-copy provider JSON while removing every CRM-reserved provenance key."""
    if not isinstance(payload, dict):
        return {}
    sanitized = _sanitize_provider_value(payload)
    return sanitized if isinstance(sanitized, dict) else {}


@dataclass(frozen=True)
class MarketplaceSendOutcome:
    response: dict[str, Any]
    provider_external_message_id: str | None = None


class MarketplaceSendError(RuntimeError):
    """Structured, persistence-safe marketplace send failure."""

    def __init__(
        self,
        *,
        category: str,
        safe_summary: str,
        side_effect_possible: bool,
        retryable: bool = False,
        http_status: int | None = None,
        provider_code: str | None = None,
        correlation_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.category = safe_error_category(category)
        self.safe_summary = safe_error_summary(safe_summary)
        self.side_effect_possible = bool(side_effect_possible)
        self.retryable = bool(retryable) and not self.side_effect_possible
        self.http_status = (
            int(http_status)
            if http_status is not None and 100 <= int(http_status) <= 599
            else None
        )
        self.provider_code = safe_provider_value(provider_code, limit=64)
        self.correlation_id = safe_provider_value(correlation_id, limit=128)
        self.retry_after_seconds = (
            max(5, min(900, int(retry_after_seconds)))
            if retry_after_seconds is not None
            else None
        )
        super().__init__(self.safe_summary)
