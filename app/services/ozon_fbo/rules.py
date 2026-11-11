"""Бизнес-правила без сети: подбор количества, раскладка по коробам, выбор слота, предохранители."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional

from .config import cfg
from .util import floor5, min_slot_date


def candidates(plan) -> list[int]:
    """Кандидаты на проверку: план, затем кратные 5 строго меньше плана, по убыванию до 10.
    40 → [40, 35, 30, 25, 20, 15, 10];  38 → [38, 35, ..., 10];  12 → [12, 10];  8 → [8]."""
    plan = int(plan)
    if plan <= 0:
        return []
    result = [plan]
    q = floor5(plan - 1)
    while q >= cfg().min_qty:
        result.append(q)
        q -= cfg().step
    return result


@dataclass
class CheckResult:
    """Результат проверки «кластер принимает q штук»."""
    accepted: bool
    trimmed_to: Optional[int] = None   # Ozon сам урезал количество в составе до этого значения
    reason: str = ""                   # почему не принято (для отчёта)
    draft_id: int = 0                  # номер черновика, которым проверяли
    earliest_slot: Optional[str] = None  # ближайшая подходящая дата слота


def find_max_accepted(plan, check: Callable[[int], CheckResult]) -> tuple[int, list]:
    """Наибольшее количество, которое кластер принимает, по правилу:
       A ≥ P → P;  A < P и A < 10 → 0;  A < P и A ≥ 10 → A, округлённое вниз до кратного 5.
    Ozon не сообщает A, поэтому проверяем кандидатов половинным делением (считаем, что если
    кластер принимает q, то принимает и меньше). Если Ozon сам урезал количество до A —
    используем это как подсказку. Возвращает (количество, история [(q, CheckResult)])."""
    history: list[tuple[int, CheckResult]] = []
    cands = candidates(plan)
    if not cands:
        return 0, history
    lo, hi = 0, len(cands) - 1
    best = 0
    probe_top = True   # первую проверку делаем по плану; после подсказки Ozon — по верхнему кандидату
    while lo <= hi:
        idx = lo if probe_top else (lo + hi) // 2
        probe_top = False
        q = cands[idx]
        res = check(q)
        history.append((q, res))
        if res.accepted:
            best = q
            hi = idx - 1
            continue
        lo = idx + 1
        if res.trimmed_to is not None:
            if res.trimmed_to < cfg().min_qty:
                break   # кластер принимает меньше 10 → исключаем
            target = floor5(res.trimmed_to)
            while lo <= hi and cands[lo] > target:
                lo += 1
            probe_top = True
    return best, history


def cargo_capacity(article) -> int:
    settings = cfg()
    return settings.cargo_capacity.get(str(article).strip().upper(), settings.default_cargo_capacity)


def cargo_layout(article, qty) -> list[int]:
    """Раскладка количества по грузоместам: 110 → [50, 50, 10]."""
    cap = cargo_capacity(article)
    qty = int(qty)
    if qty <= 0:
        return []
    result = [cap] * (qty // cap)
    if qty % cap:
        result.append(qty % cap)
    return result


def _day_date(d) -> Optional[date]:
    try:
        return date.fromisoformat(str(d.get("date_in_timezone", ""))[:10])
    except ValueError:
        return None


def pick_latest_slot(days, not_before: date) -> Optional[dict]:
    """Самый поздний слот с датой не раньше not_before: самая поздняя дата, в ней самое позднее
    время начала. Возвращает {'date', 'from', 'to'} или None."""
    best = None
    for d in days or []:
        day = _day_date(d)
        if day is None or day < not_before:
            continue
        for s in d.get("timeslots") or []:
            fr = s.get("from_in_timezone")
            if not fr:
                continue
            cand = {"date": day.isoformat(), "from": fr, "to": s.get("to_in_timezone")}
            if best is None or (cand["date"], cand["from"]) > (best["date"], best["from"]):
                best = cand
    return best


def earliest_slot_date(days, not_before: date) -> Optional[str]:
    dates = [d for d in (_day_date(x) for x in days or [] if x.get("timeslots")) if d and d >= not_before]
    return min(dates).isoformat() if dates else None


@dataclass
class SupplyLine:
    """Строка для предохранителей перед созданием заявки."""
    cluster: str
    plan: int
    fact: int
    draft_qty: Optional[int] = None    # количество по кластеру в общем черновике
    duplicate: bool = False            # в этот кластер по артикулу сегодня уже есть заявка у Ozon
    dup_allowed: bool = False          # пользователь явно ответил «всё равно сделать»


def validate_before_create(article, lines, slot_date: Optional[date], now: datetime,
                           already_created: bool) -> list[str]:
    """Предохранители: список нарушений. Пустой список — можно создавать заявку.
    Эта функция ОБЯЗАНА вызываться перед любым созданием заявки."""
    settings = cfg()
    errors = []
    if already_created:
        errors.append(f"{article}: по этому артикулу заявка уже создана")
    for ln in lines:
        p, f = int(ln.plan), int(ln.fact)
        where = f"{article} / {ln.cluster}"
        if f > p:
            errors.append(f"{where}: факт {f} больше плана {p}")
        if f != p and f % settings.step != 0:
            errors.append(f"{where}: факт {f} не равен плану {p} и не кратен {settings.step}")
        if f != 0 and f < settings.min_qty and not (f == p and p < settings.min_qty):
            errors.append(f"{where}: факт {f} меньше {settings.min_qty} (а план {p} не принят целиком)")
        dq = int(ln.draft_qty or 0)
        if dq != f:
            errors.append(f"{where}: в черновике {dq} шт., а факт {f}")
        if ln.duplicate and f > 0 and not ln.dup_allowed:
            errors.append(f"{where}: сегодня такая заявка (артикул + склад или кластер) уже есть у Ozon")
    if slot_date is None:
        errors.append(f"{article}: слот не выбран")
    elif slot_date < min_slot_date(now):
        errors.append(f"{article}: слот {slot_date} раньше чем {min_slot_date(now)} "
                      f"(сегодня + {settings.min_slot_offset_days} дней)")
    return errors


def layout_text(article: str, qty: int) -> str:
    """«1 короб — 10 шт.», «3 короба — 50+50+10 шт.»."""
    layout = cargo_layout(article, qty)
    n = len(layout)
    word = "короб" if n % 10 == 1 and n % 100 != 11 else ("короба" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "коробов")
    return f"{n} {word} — {'+'.join(map(str, layout))} шт."
