"""Расчётные черновики Ozon: проверка кластеров, подбор количества, итоговый черновик и слот по артикулу."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

from app.integrations.ozon_fbo.client import OzonFboApi, draft_create_problem, slot_days

from .config import FINAL_REBUILD_LIMIT, MAX_CLUSTERS_PER_DRAFT, REASON_TEXT
from .rules import CheckResult, earliest_slot_date, find_max_accepted, pick_latest_slot
from .util import min_slot_date, norm, safe_name, slot_text


def draft_cluster_states(info: dict) -> dict[int, dict]:
    """По кластеру из расчёта черновика: состояния складов, причины, bundle_id."""
    out: dict[int, dict] = {}
    for cl in info.get("clusters") or []:
        cid = cl.get("macrolocal_cluster_id")
        if cid is None:
            continue
        entry = out.setdefault(int(cid), {"states": [], "reasons": [], "bundle_ids": [], "restricted": []})
        for w in cl.get("warehouses") or []:
            st = w.get("availability_status") or {}
            if st.get("state"):
                entry["states"].append(st["state"])
            if st.get("invalid_reason") and st["invalid_reason"] != "UNSPECIFIED":
                entry["reasons"].append(st["invalid_reason"])
            if w.get("bundle_id"):
                entry["bundle_ids"].append(w["bundle_id"])
            if w.get("restricted_bundle_id"):
                entry["restricted"].append(w["restricted_bundle_id"])
    return out


def unavailable_text(st: Optional[dict]) -> str:
    """«склад недоступен (причина словами, КОД)» по состоянию кластера из расчёта черновика."""
    if not st:
        return "склад недоступен (кластера нет в расчёте)"
    codes = st.get("reasons") or st.get("states") or ["?"]
    parts = [f"{REASON_TEXT[c]}, {c}" if c in REASON_TEXT else str(c) for c in codes]
    return "склад недоступен (" + "; ".join(parts) + ")"


def draft_problem(info: dict) -> str:
    """Текст проблемы расчёта черновика или '' если расчёт успешен."""
    status = str(info.get("status", "")).upper()
    errs = [str(e.get("error_message") or e) for e in info.get("errors") or []]
    if status != "SUCCESS" or errs:
        return f"расчёт черновика {status or '?'}" + (": " + ", ".join(errs) if errs else "")
    return ""


def cluster_check(api: OzonFboApi, sku: int, cluster_id: int, qty: int, drop_off: dict,
                  not_before: date, today: date, tag: str) -> CheckResult:
    """Проверка «кластер принимает qty штук» отдельным черновиком на один кластер.
    Критерий (рабочая гипотеза): черновик создан, расчёт SUCCESS без ошибок,
    склад не NOT_AVAILABLE, в составе ровно qty, есть слот не раньше not_before."""
    draft_id, created = api.create_draft(sku, {cluster_id: qty}, drop_off, f"{tag}_create")
    if not draft_id:
        return CheckResult(False, reason=draft_create_problem(created))
    info = api.wait_draft(draft_id, f"{tag}_info")
    problem = draft_problem(info)
    if problem:
        return CheckResult(False, reason=problem, draft_id=draft_id)
    st = draft_cluster_states(info).get(cluster_id)
    if not st or "NOT_AVAILABLE" in st["states"] or not st["bundle_ids"]:
        return CheckResult(False, reason=unavailable_text(st), draft_id=draft_id)
    got = sum(api.bundle_qty(b, sku, f"{tag}_bundle") for b in st["bundle_ids"])
    if got < qty:
        return CheckResult(False, trimmed_to=got, reason=f"Ozon урезал до {got} шт.", draft_id=draft_id)
    if got > qty:
        return CheckResult(False, reason=f"в составе {got} шт. вместо {qty}", draft_id=draft_id)
    ts = api.timeslots(draft_id, [cluster_id], today, f"{tag}_slots")
    if ts.get("error_reason") not in (None, "", "UNSPECIFIED"):
        return CheckResult(False, reason=f"слоты: {ts.get('error_reason')}", draft_id=draft_id)
    days = slot_days(ts)
    earliest = earliest_slot_date(days, not_before)
    if not earliest:
        have = [d.get("date_in_timezone") for d in days if d.get("timeslots")]
        return CheckResult(False, reason=f"нет слотов не раньше {not_before:%d.%m}"
                           + (f" (есть только {have[0]}…{have[-1]})" if have else " (слотов нет вообще)"),
                           draft_id=draft_id)
    return CheckResult(True, draft_id=draft_id, earliest_slot=earliest)


@dataclass
class ClusterLine:
    row: int
    cluster: str
    cluster_id: Optional[int]
    plan: int
    fact: Optional[int] = None
    draft_qty: Optional[int] = None
    note: str = ""
    checks: list = field(default_factory=list)
    warehouse_id: Optional[int] = None   # конкретный склад кластера (пока не используется: API единой отгрузки его не принимает)
    duplicate: bool = False     # сегодня такая же поставка (артикул + склад, или артикул + кластер) уже есть у Ozon
    dup_orders: list = field(default_factory=list)   # номера этих сегодняшних заявок
    dup_allowed: bool = False   # пользователь ответил «всё равно сделать»: кластер обрабатывается, несмотря на дубль

    @property
    def excluded_as_duplicate(self) -> bool:
        return self.duplicate and not self.dup_allowed

    def dup_text(self) -> str:
        """«сегодня уже есть такая заявка на этот кластер: №…»."""
        where = "на этот склад" if self.warehouse_id else "на этот кластер"
        return f"сегодня уже есть такая заявка {where}: " + ", ".join("№" + n for n in self.dup_orders)


@dataclass
class ArticleResult:
    article: str
    sku: Optional[int]
    lines: list
    draft_id: int = 0
    slot: Optional[dict] = None
    manual: list = field(default_factory=list)     # причины ручной проверки
    note: str = ""                                  # общий комментарий по артикулу
    orders: list = field(default_factory=list)      # номера заявок (боевой режим)
    order_details: list = field(default_factory=list)   # order_id, состояние, supply_id, срок заполнения
    cargoes: dict = field(default_factory=dict)     # supply_id (str) → {"cluster_id", "layout", "cargo_ids"}
    created: bool = False                           # заявка создана в этом запуске
    skipped: bool = False
    drop_off: Optional[dict] = None                 # точка сдачи (нужна, чтобы пересобрать черновик)
    computed_at: Optional[float] = None             # time.monotonic() создания итогового черновика
    wh_map: dict = field(default_factory=dict)      # warehouse_id → id кластера (чтобы назвать кластер у поставки)
    warehouses: dict = field(default_factory=dict)  # кластер → склад размещения, назначенный Ozon после создания


@dataclass
class CombinedResult:
    """Результат общего черновика на несколько кластеров."""
    draft_id: int = 0
    problem: str = ""              # черновик не создан или расчёт не SUCCESS
    per_cluster: dict = field(default_factory=dict)   # cid → (status, qty, reason); status: ok/trimmed/mismatch/unavailable
    slot: Optional[dict] = None
    slot_problem: str = ""

    @property
    def all_ok(self) -> bool:
        return not self.problem and bool(self.per_cluster) and all(v[0] == "ok" for v in self.per_cluster.values())


def evaluate_combined(api: OzonFboApi, sku: int, cluster_qty: dict, drop_off: dict,
                      not_before: date, today: date, tag: str) -> CombinedResult:
    """Общий черновик: создать, дождаться расчёта, сверить состав по каждому кластеру и, если всё
    принято, запросить слоты и выбрать самый поздний не раньше not_before."""
    out = CombinedResult()
    draft_id, created = api.create_draft(sku, cluster_qty, drop_off, f"{tag}_create")
    if not draft_id:
        out.problem = draft_create_problem(created)
        return out
    out.draft_id = draft_id
    info = api.wait_draft(draft_id, f"{tag}_info")
    out.problem = draft_problem(info)
    if out.problem:
        return out
    states = draft_cluster_states(info)
    for cid, qty in cluster_qty.items():
        st = states.get(cid)
        if not st or "NOT_AVAILABLE" in st["states"] or not st["bundle_ids"]:
            out.per_cluster[cid] = ("unavailable", 0, unavailable_text(st))
            continue
        got = sum(api.bundle_qty(b, sku, f"{tag}_bundle_{cid}") for b in st["bundle_ids"])
        if got < qty:
            out.per_cluster[cid] = ("trimmed", got, f"Ozon урезал до {got} шт.")
        elif got > qty:
            out.per_cluster[cid] = ("mismatch", got, f"в составе {got} шт. вместо {qty}")
        else:
            out.per_cluster[cid] = ("ok", got, "")
    if not out.all_ok:
        return out
    days, reason = api.timeslot_days(draft_id, list(cluster_qty), today, f"{tag}_slots")
    if reason:
        out.slot_problem = f"слоты: {reason}"
        return out
    out.slot = pick_latest_slot(days, not_before)
    if not out.slot:
        have = [d.get("date_in_timezone") for d in days if d.get("timeslots")]
        out.slot_problem = (f"нет слота не раньше {not_before:%d.%m.%Y}"
                            + (f" (есть {have[0]}…{have[-1]})" if have else " (слотов нет)"))
    return out


def process_article(api: OzonFboApi, article: str, lines: list, sku: Optional[int], drop_off: dict,
                    now: datetime, say: Callable[[str], None], check_fn=cluster_check,
                    mode: str = "trial") -> ArticleResult:
    res = ArticleResult(article=article, sku=sku, lines=lines, drop_off=drop_off)
    not_before = min_slot_date(now)
    today = now.date()
    if not sku:
        res.manual.append("не найден SKU Ozon для артикула (проверьте артикул в ЛК)")
        say("  ! SKU не найден — артикул не обработан")
        return res

    seen = set()
    for ln in lines:
        if norm(ln.cluster) in seen:
            res.manual.append(f"кластер «{ln.cluster}» встречается в таблице дважды")
        seen.add(norm(ln.cluster))

    for ln in lines:
        if ln.excluded_as_duplicate:
            ln.fact = 0
            say(f"  {ln.cluster:<32} план {ln.plan:>4}   факт    0   ({ln.note})")
        elif ln.duplicate:
            say(f"  {ln.cluster:<32} {ln.dup_text()} — пользователь решил сделать всё равно")
    known = [ln for ln in lines if ln.cluster_id is not None and not ln.excluded_as_duplicate]
    for ln in lines:
        if ln.cluster_id is None:
            res.manual.append(f"кластер «{ln.cluster}» не найден в списке кластеров Ozon")
            ln.note = "кластер не найден у Ozon — РУЧНАЯ ПРОВЕРКА"
            say(f"  {ln.cluster:<32} план {ln.plan:>4}   ? кластер не найден у Ozon")
    if not known:
        res.note = ("поставка не создаётся: по всем кластерам сегодня уже есть такие же заявки"
                    if any(ln.excluded_as_duplicate for ln in lines)
                    and all(ln.excluded_as_duplicate for ln in lines if ln.cluster_id is not None)
                    else "поставка не создаётся: ни один кластер не найден у Ozon")
        say("  → " + res.note)
        return res
    if len(known) > MAX_CLUSTERS_PER_DRAFT:
        res.manual.append(f"у артикула {len(known)} кластеров, а в одну поставку помещается не больше {MAX_CLUSTERS_PER_DRAFT}")
        say("  ! " + res.manual[-1])
        return res

    def search(ln: ClusterLine, first: Optional[CheckResult] = None) -> None:
        """Подбор количества по кластеру отдельными черновиками.
        first — уже известный из общего черновика результат проверки плана, чтобы не проверять его заново."""
        pending = {"first": first}

        def check(q):
            if pending["first"] is not None and q == ln.plan:
                r, pending["first"] = pending["first"], None
                say(f"      {ln.cluster}: {q} шт. — нет, по общему черновику: {r.reason}")
                return r
            r = check_fn(api, sku, ln.cluster_id, q, drop_off, not_before, today, f"{safe_name(ln.cluster)}_{q}")
            say(f"      {ln.cluster}: {q} шт. отдельным черновиком — " + ("принято" if r.accepted else "нет: " + r.reason))
            return r

        qty, history = find_max_accepted(ln.plan, check)
        ln.fact = qty
        ln.checks = [(q, r.accepted, r.reason) for q, r in history]
        if qty == 0:
            last = next((r.reason for q, r in reversed(history) if not r.accepted), "кластер не принимает")
            ln.note = "исключён: " + last

    # Шаг A: один общий черновик сразу по плану. Если Ozon принял всё и дал слот — это и есть итог:
    # один черновик на артикул вместо черновика на каждый кластер (у Ozon лимит 2 черновика в минуту).
    plan_qty = {ln.cluster_id: ln.plan for ln in known}
    say(f"  общий черновик по плану: {len(known)} кластеров, {sum(plan_qty.values())} шт.")
    probe = evaluate_combined(api, sku, plan_qty, drop_off, not_before, today, "plan")
    done = False
    provisional: list[ClusterLine] = []   # приняты по плану в общем черновике, отдельно не проверялись
    if probe.problem:
        say(f"  общий черновик не прошёл ({probe.problem}) — проверяю кластеры по отдельности")
        for ln in known:
            search(ln)
    elif probe.all_ok and probe.slot:
        done = True
        for ln in known:
            ln.fact, ln.draft_qty = ln.plan, ln.plan
        res.draft_id, res.slot = probe.draft_id, probe.slot
        res.computed_at = time.monotonic()
        say("  Ozon принял весь план, слот есть")
    elif probe.all_ok:
        say(f"  Ozon принял весь план, но {probe.slot_problem} — проверяю кластеры по отдельности")
        for ln in known:
            search(ln)
    else:
        # Шаг B: подбор только для кластеров, которые Ozon урезал или не принял
        for ln in known:
            status, got, reason = probe.per_cluster.get(ln.cluster_id, ("unavailable", 0, "кластера нет в расчёте"))
            if status == "ok":
                ln.fact = ln.plan
                provisional.append(ln)
            else:
                search(ln, CheckResult(False, trimmed_to=got if status == "trimmed" else None, reason=reason))

    if not done:
        active = [ln for ln in known if ln.fact]
        if not active:
            res.note = "поставка не создаётся: ни один кластер не принял товар"
        else:
            # Шаг C: итоговый общий черновик с подобранными количествами + самый поздний слот
            final = evaluate_combined(api, sku, {ln.cluster_id: ln.fact for ln in active}, drop_off,
                                      not_before, today, "final")
            # Ozon может принять кластер по плану, а в итоговом черновике отказать или урезать. Один повтор того же
            # состава (отказ бывает временным), затем отказанные кластеры исключаются, урезанные подбираются заново,
            # и итоговый черновик собирается снова — не больше FINAL_REBUILD_LIMIT черновиков подряд.
            retried = False
            n_final = 2
            while (not final.problem and final.per_cluster and not final.all_ok and n_final <= FINAL_REBUILD_LIMIT
                   and all(final.per_cluster.get(ln.cluster_id, ("unavailable",))[0] != "mismatch" for ln in active)):
                bad = [(ln, final.per_cluster.get(ln.cluster_id, ("unavailable", 0, "кластера нет в расчёте")))
                       for ln in active]
                bad = [(ln, st) for ln, st in bad if st[0] != "ok"]
                if not retried:
                    retried = True
                    say("  итоговый черновик: " + "; ".join(f"{ln.cluster} — {st[2]}" for ln, st in bad)
                        + " — повторяю тот же состав ещё раз")
                else:
                    for ln, (status, got, reason) in bad:
                        if status == "trimmed":
                            say(f"  итоговый черновик: {ln.cluster} — {reason} — подбираю количество заново")
                            search(ln, CheckResult(False, trimmed_to=got, reason=reason))
                        else:
                            ln.fact = 0
                            ln.note = "исключён в итоговом черновике: " + reason
                            say(f"  итоговый черновик: {ln.cluster} — {reason} — исключаю из поставки")
                    active = [ln for ln in known if ln.fact]
                    if not active:
                        final = CombinedResult()
                        break
                final = evaluate_combined(api, sku, {ln.cluster_id: ln.fact for ln in active}, drop_off,
                                          not_before, today, f"final{n_final}")
                n_final += 1
            if active and final.all_ok and final.slot_problem and provisional:
                # слот пропал из-за кого-то из принятых «оптом» кластеров — проверяю их по отдельности
                say(f"  итоговый черновик: {final.slot_problem} — проверяю принятые по плану кластеры по отдельности")
                for ln in provisional:
                    search(ln)
                active = [ln for ln in known if ln.fact]
                final = (evaluate_combined(api, sku, {ln.cluster_id: ln.fact for ln in active}, drop_off,
                                           not_before, today, f"final{n_final}") if active else CombinedResult())
            if not active:
                res.note = "поставка не создаётся: ни один кластер не принял товар"
            elif final.problem:
                res.manual.append("итоговый черновик: " + final.problem)
            else:
                for ln in active:
                    status, got, reason = final.per_cluster.get(ln.cluster_id, ("unavailable", 0, "кластера нет в расчёте"))
                    ln.draft_qty = got
                    if status == "unavailable":
                        res.manual.append(f"в итоговом черновике кластер «{ln.cluster}» недоступен: {reason}")
                    elif status != "ok":
                        res.manual.append(f"в итоговом черновике по «{ln.cluster}» {got} шт. вместо {ln.fact}")
                if final.slot:
                    res.draft_id, res.slot = final.draft_id, final.slot
                elif not res.manual:
                    res.manual.append(f"для итогового черновика {final.slot_problem}")

    for ln in known:
        say(f"  {ln.cluster:<32} план {ln.plan:>4}   факт {ln.fact or 0:>4}" + (f"   ({ln.note})" if ln.note else ""))
    if res.note:
        say("  → " + res.note)
    if res.slot:
        res.computed_at = time.monotonic()
        say(f"  → расчёт готов: слот {slot_text(res.slot)}; расчётный черновик {res.draft_id}"
            + {"trial": " (заявка НЕ создана — пробный режим)",
               "plan": " (заявка будет создана после общего подтверждения)"}.get(mode, ""))
    for reason in res.manual:
        say(f"  ! РУЧНАЯ ПРОВЕРКА: {reason}")
    return res


def refresh_draft(api: OzonFboApi, res: ArticleResult, now: datetime, say: Callable[[str], None]) -> bool:
    """Пересобрать итоговый черновик по уже подобранным количествам (черновик Ozon живёт 30 минут).
    Состав должен совпасть с фактом, слот выбирается заново (самый поздний)."""
    cluster_qty = {ln.cluster_id: ln.fact for ln in res.lines if ln.fact and ln.cluster_id is not None}
    if not cluster_qty or not res.sku or res.drop_off is None:
        res.manual.append("пересборка черновика невозможна: нет данных по артикулу")
        return False
    final = evaluate_combined(api, res.sku, cluster_qty, res.drop_off, min_slot_date(now), now.date(), "refresh")
    if final.problem:
        res.manual.append("пересборка черновика: " + final.problem)
        return False
    bad = [f"{ln.cluster}: {final.per_cluster.get(ln.cluster_id, ('?', 0, ''))[2] or 'нет в расчёте'}"
           for ln in res.lines if ln.fact and final.per_cluster.get(ln.cluster_id, ("?",))[0] != "ok"]
    if bad:
        res.manual.append("при пересборке черновика Ozon уже не принимает прежнее количество: " + "; ".join(bad))
        return False
    if not final.slot:
        res.manual.append("при пересборке черновика " + final.slot_problem)
        return False
    for ln in res.lines:
        if ln.fact:
            ln.draft_qty = final.per_cluster[ln.cluster_id][1]
    res.draft_id, res.slot, res.computed_at = final.draft_id, final.slot, time.monotonic()
    say(f"  черновик пересобран: {res.draft_id}, слот {slot_text(res.slot)}")
    return True


@dataclass
class RunOptions:
    mode: str = "trial"              # trial — пробный прогон; live — создание заявок; plan — расчёт перед общим подтверждением
    with_cargo: bool = True          # в live сразу передавать грузоместа
    force: bool = False              # повторять артикулы, по которым заявка уже создана
    articles: Optional[set] = None   # нормализованные артикулы (norm); None — все
    auto_confirm: bool = False       # не спрашивать подтверждение
    confirm_each: bool = False       # live: подтверждать каждый артикул отдельно (иначе — один общий план)
    on_result: Optional[Callable] = None   # on_result(ArticleResult, этап): «calc» после расчёта артикула,
    # «order» после попытки создать заявку — структурные события для интерфейса
    allow_duplicates: bool = False   # «всё равно сделать»: не пропускать кластеры, куда сегодня уже есть заявка
    ask_duplicates: Optional[Callable[[str], bool]] = None   # вопрос пользователю, когда дубли найдены, а allow_duplicates
    # не задан: ask_duplicates(текст со списком) → True = всё равно сделать, False = пропустить. None — пропустить без вопроса.
    # auto_confirm этот вопрос не подтверждает: разрешение на дубли всегда явное.
    confirm_plan: Optional[Callable[[str], bool]] = None     # подтверждение общего плана (вид вопроса «план»); None — обычный confirm


@dataclass
class RunOutcome:
    results: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)    # строка таблицы → факт
    notes: dict = field(default_factory=dict)    # строка таблицы → примечание
    hidden: list = field(default_factory=list)   # скрытые фильтром строки с планом
    interrupted: Optional[BaseException] = None
    stopped: bool = False
    duplicates: list = field(default_factory=list)   # (артикул, ClusterLine) — строки, куда сегодня уже есть заявка у Ozon
    remembered: list = field(default_factory=list)   # (артикул, номера заявок, когда создана) — память по этому списку
    duplicates_allowed: bool = False                 # решение пользователя «всё равно сделать» (или allow_duplicates)
