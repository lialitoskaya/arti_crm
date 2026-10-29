"""Константы Ozon FBO и изменяемые настройки раздела.

Неизменяемые константы — модульные. Изменяемые параметры живут в `FboSettings`;
активный набор для текущего прогона хранится в ContextVar и читается через `cfg()`,
чтобы правила не зависели от глобального mutable state между потоками.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta, timezone
from typing import Any, Iterator

BASE_URL = "https://api-seller.ozon.ru"
SUPPLY_TYPE = "MULTI_CLUSTER"       # единая отгрузка в несколько кластеров
DELETION_MODE = "PARTIAL"           # Ozon убирает только непрошедшие единицы SKU
SLOT_WINDOW_DAYS = 27               # одно окно запроса слотов (у Ozon не больше 28 дней за запрос)
MAX_CLUSTERS_PER_DRAFT = 20         # ограничение Ozon на clusters_info в одном черновике
FINAL_REBUILD_LIMIT = 4             # сколько раз подряд собирать итоговый черновик при отказе кластеру
REASON_TEXT = {                     # причины недоступности склада в расчёте черновика — словами
    "NOT_AVAILABLE_MATRIX": "склад не принимает этот товар",
    "NOT_AVAILABLE_TIMESLOT_FOR_BOTH_WAREHOUSES": "нет таймслотов для складов кластера",
}
REQUEST_INTERVAL = 1.1              # секунд между запросами к Ozon
RATE_LIMITS = {"/v1/draft/multi-cluster/create": (2, 62.0), "/v1/draft/crossdock/create": (2, 62.0),
               "/v1/draft/direct/create": (2, 62.0), "/v2/draft/supply/create": (2, 62.0)}
RED_FILL = "FFFF0000"               # заливка ячейки «Факт» при нуле
NOTE_HEADER = "Заявка / примечание"  # заголовок отдельной колонки с номером заявки и пометками
DRAFT_TTL_MIN = 30                  # черновик API живёт 30 минут (dev.ozon.ru)
LK_ORDERS_URL = "https://seller.ozon.ru/app/supply/orders?filter=SupplyPreparation"
MSK = timezone(timedelta(hours=3), "MSK")
ALL_CLUSTERS_WORDS = ("все", "все кластеры", "всё", "all")
ALL_CLUSTERS_COUNTRY = "Россия"
ACTIVE_ORDER_STATES = ["DATA_FILLING", "READY_TO_SUPPLY", "ACCEPTED_AT_SUPPLY_WAREHOUSE", "IN_TRANSIT",
                       "ACCEPTANCE_AT_STORAGE_WAREHOUSE", "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED",
                       "COMPLETED", "OVERDUE"]   # всё, кроме CANCELLED и REJECTED_AT_SUPPLY_WAREHOUSE
KNOWN_CREATE_REASONS = {
    "NOT_AVAILABLE_CLUSTERS": "кластер сейчас недоступен для такой поставки",
}

SETTING_LABELS = {
    "default_cargo_capacity": "Вместимость грузоместа по умолчанию, шт.",
    "cargo_capacity": "Вместимость для отдельных артикулов (артикул = штук, по одному на строку)",
    "cargo_type": "Тип грузоместа для Ozon (BOX — короб)",
    "drop_off_name": "Точка сдачи (название как у Ozon)",
    "min_slot_offset_days": "Слот не раньше чем через N дней",
    "slot_max_windows": "Сколько окон по 28 дней смотреть вперёд при выборе слота",
    "min_qty": "Минимум штук в кластер",
    "step": "Кратность количества (если не равно плану)",
    "unavailable_retry_sec": "Пауза перед повтором, если Ozon ответил «кластер недоступен», сек",
    "all_clusters": "Какие кластеры считать «все» (названия как у Ozon, по одному на строку; пусто — все кластеры России)",
}
SETTING_KEYS = tuple(SETTING_LABELS)


def _default_cargo_capacity() -> dict[str, int]:
    return {"AT-L-AM-S": 5, "AT-L-AM-M": 5, "AT-L-AM-P": 5}


@dataclass(frozen=True)
class FboSettings:
    """Изменяемые параметры раздела (хранятся в таблице ozon_fbo_settings)."""

    default_cargo_capacity: int = 50
    cargo_capacity: dict[str, int] = field(default_factory=_default_cargo_capacity)
    cargo_type: str = "BOX"
    drop_off_name: str = "КИЕВСКИЙ_2"
    min_slot_offset_days: int = 7
    slot_max_windows: int = 6
    min_qty: int = 10
    step: int = 5
    unavailable_retry_sec: int = 30
    all_clusters: list[str] = field(default_factory=list)


def settings_to_dict(settings: FboSettings) -> dict[str, Any]:
    return {
        "default_cargo_capacity": settings.default_cargo_capacity,
        "cargo_capacity": dict(settings.cargo_capacity),
        "cargo_type": settings.cargo_type,
        "drop_off_name": settings.drop_off_name,
        "min_slot_offset_days": settings.min_slot_offset_days,
        "slot_max_windows": settings.slot_max_windows,
        "min_qty": settings.min_qty,
        "step": settings.step,
        "unavailable_retry_sec": settings.unavailable_retry_sec,
        "all_clusters": list(settings.all_clusters),
    }


def _parse_capacity(raw: Any) -> dict[str, int]:
    if isinstance(raw, str):
        pairs: dict[str, Any] = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            if "=" not in line:
                raise ValueError(f"строка вместимости должна быть вида «АРТИКУЛ = 5»: {line.strip()!r}")
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
        raw = pairs
    value = {str(k).strip().upper(): int(v) for k, v in dict(raw).items() if str(k).strip()}
    if any(v <= 0 for v in value.values()):
        raise ValueError("вместимость должна быть больше нуля")
    return value


def parse_settings(data: dict[str, Any], base: FboSettings | None = None) -> tuple[FboSettings, list[str]]:
    """Проверяет значения и возвращает новый набор настроек и список ошибок.

    Неверные значения пропускаются (остаются из `base`), как в исходном приложении."""
    current = base or FboSettings()
    updates: dict[str, Any] = {}
    errors: list[str] = []
    for key in SETTING_KEYS:
        if key not in data:
            continue
        raw = data[key]
        old = getattr(current, key)
        try:
            if key == "cargo_capacity":
                value: Any = _parse_capacity(raw)
            elif isinstance(old, list):
                items = raw.splitlines() if isinstance(raw, str) else list(raw)
                value = [str(x).strip() for x in items if str(x).strip()]
            elif isinstance(old, bool):
                value = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "да", "yes", "on")
            elif isinstance(old, int):
                value = int(raw)
                if key != "min_slot_offset_days" and value <= 0:
                    raise ValueError("должно быть больше нуля")
                if value < 0:
                    raise ValueError("не может быть отрицательным")
            else:
                value = str(raw).strip()
                if not value:
                    raise ValueError("пустое значение")
        except (TypeError, ValueError) as e:
            errors.append(f"{SETTING_LABELS.get(key, key)}: {raw!r} — {e}")
            continue
        updates[key] = value
    return replace(current, **updates), errors


_ACTIVE: contextvars.ContextVar[FboSettings] = contextvars.ContextVar("ozon_fbo_settings", default=FboSettings())


def cfg() -> FboSettings:
    """Активные настройки текущего прогона (по умолчанию — значения из FboSettings)."""
    return _ACTIVE.get()


@contextmanager
def use_settings(settings: FboSettings) -> Iterator[FboSettings]:
    token = _ACTIVE.set(settings)
    try:
        yield settings
    finally:
        _ACTIVE.reset(token)
