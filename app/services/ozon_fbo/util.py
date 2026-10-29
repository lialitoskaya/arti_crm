"""Мелкие утилиты: нормализация названий, время по Москве, короткие тексты."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from .config import MSK, cfg


def norm(s) -> str:
    """Нормализованное название для сравнения: без лишних пробелов, без ё, в нижнем регистре."""
    return re.sub(r"\s+", " ", str(s or "").replace("ё", "е").replace("Ё", "Е")).strip().lower()


def safe_name(s) -> str:
    """Имя, пригодное для файла."""
    return re.sub(r"[^\w.-]+", "_", str(s or ""), flags=re.UNICODE).strip("_") or "x"


def short(obj, limit=200) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)[:limit]


def dicts(obj):
    """Все словари внутри ответа, на любой глубине."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from dicts(v)


def utc_to_msk_text(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M") + " МСК"
    except (ValueError, TypeError):
        return str(value or "?")


def floor5(n) -> int:
    step = cfg().step
    return (int(n) // step) * step


def now_msk() -> datetime:
    return datetime.now(MSK)


def min_slot_date(now: datetime) -> date:
    """Самая ранняя допустимая дата слота: сегодня (по Москве) + N дней."""
    return now.date() + timedelta(days=cfg().min_slot_offset_days)


def slot_text(slot: dict) -> str:
    return f"{str(slot.get('from', ''))[:16].replace('T', ' ')}–{str(slot.get('to', ''))[11:16]}"
