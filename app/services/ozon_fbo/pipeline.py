"""Конвейер прогона: расчёт по артикулам, общий план, создание заявок, отчёт. Общий для интерфейса CRM и тестов."""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Callable, Optional

from app.integrations.ozon_fbo.client import ApiError, AuthError, NetworkError, OzonFboApi, RunLog

from .cargo import create_cargoes_for_order
from .config import DRAFT_TTL_MIN, LK_ORDERS_URL, cfg
from .dedupe import fetch_today_orders, key_today_orders, supplied_today, supply_key
from .drafts import ArticleResult, ClusterLine, RunOptions, RunOutcome, process_article, refresh_draft
from .orders import DraftError, LiveError, cargo_summary, create_order, warehouses_text
from .rules import SupplyLine, cargo_capacity, cargo_layout, layout_text, validate_before_create
from .sheet import TableRow, cluster_map_from, expand_all_clusters, resolve_products
from .store import FboStateStore, learn_cluster_warehouses, load_state, load_warehouse_map, previous_order_active, save_state
from .util import norm, slot_text, utc_to_msk_text


def plan_summary(results: list, with_cargo: bool) -> str:
    """Общий план перед единственным подтверждением: что будет создано и что — нет."""
    ready = [r for r in results if r.slot and r.draft_id and not r.manual and not r.note and not r.skipped]
    lines = []
    total_boxes = sum(len(cargo_layout(r.article, ln.fact)) for r in ready for ln in r.lines if ln.fact)
    total_qty = sum(ln.fact for r in ready for ln in r.lines if ln.fact)
    lines.append(f"ПЛАН ПОСТАВОК: заявок {len(ready)}, коробов {total_boxes}, товара {total_qty} шт."
                 + ("" if with_cargo else " (грузоместа передаваться не будут)"))
    dups = [(r.article, ln) for r in ready for ln in r.lines if ln.fact and ln.duplicate]
    if dups:
        lines.append(f"⚠ ДУБЛИ ПО ВАШЕМУ РЕШЕНИЮ: {len(dups)} строк — сегодня туда уже есть заявки, "
                     "будут созданы ещё (ниже помечены «ДУБЛЬ»).")
    for r in ready:
        lines.append(f"{r.article} (SKU {r.sku}) — слот {slot_text(r.slot)}")
        for ln in r.lines:
            if ln.fact:
                lines.append(f"    {ln.cluster}: {ln.fact} шт. — {layout_text(r.article, ln.fact)}"
                             + (f" — ⚠ ДУБЛЬ: {ln.dup_text()}" if ln.duplicate else ""))
            elif ln.note:
                lines.append(f"    {ln.cluster}: 0 шт. ({ln.note})")
    rest = [r for r in results if r not in ready]
    if rest:
        lines.append("Не будут созданы:")
        for r in rest:
            why = ("РУЧНАЯ ПРОВЕРКА: " + "; ".join(r.manual)) if r.manual else (r.note or "нет черновика со слотом")
            lines.append(f"    {r.article} — {why}")
    lines.append(f"Черновики Ozon действуют {DRAFT_TTL_MIN} минут: устаревшие будут пересобраны автоматически.")
    return "\n".join(lines)


def create_orders_batch(api: OzonFboApi, store: FboStateStore, results: list, sheet: str, now: datetime,
                        say: Callable[[str], None], with_cargo: bool = True, force: bool = False,
                        confirm_each: Optional[Callable[[str], bool]] = None,
                        should_stop: Optional[Callable[[], bool]] = None,
                        on_result: Optional[Callable] = None, draft_ttl_min: int = DRAFT_TTL_MIN) -> None:
    """Создать заявки по всем готовым артикулам подряд (после общего подтверждения).
    Перед каждой заявкой: свежесть черновика (иначе пересборка), предохранители validate_before_create,
    при ошибке черновика — одна повторная попытка с пересобранным черновиком.
    on_result(res, "order") — после каждой попытки (создана, пропущена, ошибка)."""
    for res in results:
        if res.skipped or res.manual or res.note or not res.slot or not res.draft_id:
            continue
        if should_stop and should_stop():
            say("Остановлено пользователем — остальные заявки не создаю.")
            break
        say(f"[{res.article}]")
        _create_one(api, store, res, sheet, now, say, with_cargo, force, confirm_each, draft_ttl_min, should_stop)
        say("")
        if on_result:
            on_result(res, "order")


def _create_one(api: OzonFboApi, store: FboStateStore, res, sheet: str, now: datetime, say: Callable[[str], None],
                with_cargo: bool, force: bool, confirm_each: Optional[Callable[[str], bool]], draft_ttl_min: int,
                should_stop: Optional[Callable[[], bool]] = None) -> None:
    prev = load_state(store, sheet, res.article)
    prev_active = bool(prev.get("orders")) and not force and previous_order_active(api, prev, say)
    if prev_active:
        res.note = "заявка уже создана: " + ", ".join(map(str, prev["orders"])) + " (пропущено)"
        say("  " + res.note)
        return
    keep_prev = prev_active or force
    if res.computed_at is None or time.monotonic() - res.computed_at > (draft_ttl_min - 5) * 60:
        say("  черновик мог устареть — пересобираю по подобранным количествам")
        if not refresh_draft(api, res, now, say):
            say("  ! " + res.manual[-1])
            save_state(store, sheet, res, "live", keep_prev_orders=keep_prev)
            return
    lines = [SupplyLine(ln.cluster, ln.plan, ln.fact or 0, ln.draft_qty, ln.duplicate, ln.dup_allowed)
             for ln in res.lines if ln.cluster_id is not None]
    errors = validate_before_create(res.article, lines, date.fromisoformat(res.slot["date"]), now, False)
    if errors:
        res.manual.extend("предохранитель: " + e for e in errors)
        say("  ! заявка НЕ создана, сработали предохранители: " + "; ".join(errors))
        save_state(store, sheet, res, "live", keep_prev_orders=keep_prev)
        return
    if confirm_each is not None:
        text = (f"СОЗДАТЬ ЗАЯВКУ по артикулу {res.article}?\n"
                + "".join(f"    {ln.cluster}: {ln.fact} шт.\n" for ln in res.lines if ln.fact)
                + (f"  Грузоместа (коробы не больше {cargo_capacity(res.article)} шт.): {cargo_summary(res)}\n"
                   if with_cargo else "  Грузоместа на этом шаге не передаются.\n")
                + f"  Слот {slot_text(res.slot)}")
        if not confirm_each(text):
            res.note = "создание заявки отменено пользователем"
            say("  " + res.note)
            save_state(store, sheet, res, "live", keep_prev_orders=keep_prev)
            return
    if should_stop and should_stop():
        # остановка (или снятие прогона) успела прийти во время пересборки черновика или вопроса — заявку не создаём
        res.note = "создание заявки отменено: прогон остановлен"
        say("  " + res.note)
        save_state(store, sheet, res, "live", keep_prev_orders=keep_prev)
        return
    try:
        create_order(api, res, say)
    except DraftError as e:
        say(f"  ! {e} — пересобираю черновик и пробую ещё раз")
        if refresh_draft(api, res, now, say):
            try:
                create_order(api, res, say)
            except (LiveError, ApiError) as e2:
                res.manual.append(f"создание заявки: {e2}")
        else:
            res.manual.append(f"создание заявки: {e}")
    except (LiveError, ApiError) as e:
        res.manual.append(f"создание заявки: {e}")
        say(f"  ! {e}")
    if res.created:
        # номер заявки фиксируется сразу: сбой сети или процесса во время грузомест не должен потерять запись
        save_state(store, sheet, res, "live", keep_prev_orders=keep_prev)
    if res.created and with_cargo:
        try:
            create_cargoes_for_order(api, res, {ln.cluster_id: ln.fact for ln in res.lines if ln.fact}, say)
        except (LiveError, ApiError) as e:
            res.manual.append(f"грузоместа не переданы: {e}")
            say(f"  ! грузоместа не переданы: {e}. Заявка создана; грузоместа можно передать позже режимом «Дозаполнить».")
    save_state(store, sheet, res, "live", keep_prev_orders=keep_prev)


def duplicates_text(dups: list, remembered: list, mode: str) -> str:
    """Вопрос пользователю: что уже есть (заявки Ozon за сегодня по строкам, заявки приложения по этому списку
    из памяти) и что значит «всё равно сделать»."""
    lines = []
    if dups:
        lines.append(f"СЕГОДНЯ УЖЕ ЕСТЬ ЗАЯВКИ по {len(dups)} строкам (правило: по артикулу на один склад — не больше одной заявки в день):")
        for article, ln in dups:
            lines.append(f"    {article} — {ln.cluster}: план {ln.plan} шт.; сегодня уже есть "
                         + ", ".join("№" + n for n in ln.dup_orders))
    if remembered:
        lines.append(f"ПРИЛОЖЕНИЕ УЖЕ СОЗДАВАЛО ЗАЯВКУ по этому списку для {len(remembered)} артикулов:")
        for article, orders, created in remembered:
            when = ""
            if created:
                try:
                    when = f" (создана {datetime.fromisoformat(created):%d.%m %H:%M})"
                except ValueError:
                    when = ""
            lines.append(f"    {article} — " + ", ".join("№" + str(n) for n in orders) + when)
    what = "эти строки и артикулы" if dups and remembered else ("эти строки" if dups else "эти артикулы")
    if mode == "trial":
        lines.append(f"Пропустить {what} (факт 0) или всё равно посчитать? Заявки в пробном режиме не создаются.")
    else:
        lines.append(f"Пропустить {what} (остальное пойдёт как обычно) или всё равно сделать? "
                     "«Всё равно» — заявки будут созданы и по ним, то есть на один склад уйдёт вторая заявка за день.")
    return "\n".join(lines)


def decide_duplicates(out: RunOutcome, options: RunOptions, say: Callable[[str], None]) -> bool:
    """Одно решение на прогон по всему, что иначе было бы пропущено как уже существующее (out.duplicates — заявки
    Ozon за сегодня, out.remembered — память): разрешено заранее (allow_duplicates), спрошено у
    пользователя (ask_duplicates) или пропущено. auto_confirm этот вопрос не подтверждает."""
    text = duplicates_text(out.duplicates, out.remembered, options.mode)
    say(text)
    if options.allow_duplicates:
        allowed = True
        say("  → всё равно сделать: разрешено заранее")
    elif options.ask_duplicates is None:
        allowed = False
        say("  → пропущены (защита от дублей; разрешить можно только явно)")
    else:
        allowed = bool(options.ask_duplicates(text))
        say("  → решение пользователя: " + ("всё равно сделать" if allowed else "пропустить"))
    return allowed


def line_note(ln: ClusterLine, summary: str) -> str:
    if ln.note and ln.fact and summary:
        return f"{ln.note}; {summary}"     # дубль по решению пользователя: и пометка, и номер заявки
    if ln.note:
        return ln.note
    return summary if ln.fact else (summary or "исключён")


def outcome_rows(res: ArticleResult, mode: str) -> tuple[dict, dict]:
    """Факт и примечание по строкам таблицы. Если из одной строки пришло несколько кластеров
    («все кластеры»), факт в строке — сумма по ним, примечание — перечень по кластерам."""
    summary = article_summary(res, mode)
    by_row: dict[int, list] = {}
    for ln in res.lines:
        by_row.setdefault(ln.row, []).append(ln)
    facts, notes = {}, {}
    for row, lns in by_row.items():
        vals = [ln.fact for ln in lns if ln.fact is not None]
        if vals and not res.skipped:
            facts[row] = sum(vals)
        if res.skipped:
            notes[row] = res.note
        elif len(lns) == 1:
            notes[row] = line_note(lns[0], summary)
        else:
            parts = [f"{ln.cluster} {'?' if ln.fact is None else ln.fact}" + (f" ({ln.note})" if ln.note else "") for ln in lns]
            notes[row] = "по кластерам: " + "; ".join(parts) + (f" | {summary}" if summary else "")
    return facts, notes


def article_summary(res: ArticleResult, mode: str) -> str:
    """Общий текст по артикулу: номер заявки, слот, грузоместа или причина ручной проверки."""
    if res.skipped:
        return res.note
    if res.manual:
        summary = "РУЧНАЯ ПРОВЕРКА: " + "; ".join(res.manual)
    elif res.orders:
        summary = "заявка " + ", ".join(f"№{n}" for n in res.orders)
        if res.slot:
            summary += f"; слот {slot_text(res.slot)}"
        deadline = next((o.get("deadline_utc") for o in res.order_details if o.get("deadline_utc")), None)
        if res.cargoes:
            n = sum(len(c.get("cargo_ids") or []) for c in res.cargoes.values())
            summary += f"; грузоместа переданы: {n}"
        else:
            summary += "; грузоместа не переданы" + (f", заполнить до {utc_to_msk_text(deadline)}" if deadline else "")
        if res.warehouses:
            summary += "; склады: " + warehouses_text(res)
    elif res.slot and mode == "trial":
        summary = (f"ЗАЯВКА НЕ СОЗДАНА (пробный прогон); слот {slot_text(res.slot)}; "
                   f"расчётный черновик {res.draft_id}")
    elif res.slot:
        summary = (res.note or "заявка не создана") + f"; расчёт: слот {slot_text(res.slot)}, черновик {res.draft_id}"
    else:
        summary = res.note
    return summary


def run_pipeline(api: OzonFboApi, store: FboStateStore, rows: list, sheet: str, options: RunOptions, now: datetime,
                 say: Callable[[str], None], confirm: Callable[[str], bool], log: RunLog,
                 should_stop: Optional[Callable[[], bool]] = None) -> RunOutcome:
    """Основной конвейер: справочники Ozon → защита от дублей → расчёт по каждому артикулу.
    Заявки создаёт run_live_batch после общего плана."""
    out = RunOutcome()
    out.hidden = [r for r in rows if r.hidden]
    visible = [r for r in rows if not r.hidden]
    if out.hidden:
        say(f"Скрытые фильтром строки с планом — пропускаю ({len(out.hidden)}): "
            + ", ".join(f"стр.{r.row} {r.article}/{r.cluster} ({r.plan})" for r in out.hidden))
    log.set_dir("_общее")
    cluster_list = api.cluster_list()
    clusters = cluster_map_from(cluster_list)
    say(f"Кластеров у Ozon: {len(clusters)}.")
    visible, messages = expand_all_clusters(visible, cluster_list)
    for msg in messages:
        say("«Все кластеры»: " + msg)
    by_article: dict[str, list[TableRow]] = {}
    for r in visible:
        by_article.setdefault(r.article, []).append(r)
    if options.articles:
        missing = options.articles - {norm(a) for a in by_article}
        if missing:
            say(f"Артикулов из списка нет среди видимых строк с планом: {sorted(missing)}")
        by_article = {a: rs for a, rs in by_article.items() if norm(a) in options.articles}
    if not by_article:
        say("Нет видимых строк с планом > 0 по выбранным артикулам. Нечего делать.")
        return out
    n_rows = sum(len(v) for v in by_article.values())

    def est(n_drafts: int) -> int:
        return max(1, -(-n_drafts * 31 // 60))

    say(f"Артикулов: {len(by_article)}, строк с планом: {n_rows}. Ozon даёт создать не больше 2 черновиков "
        f"в минуту: если Ozon примет весь план — около {est(len(by_article))} мин, "
        f"если по каждому кластеру придётся подбирать — до {est(n_rows + 2 * len(by_article))} мин.")
    say("")

    drop_off_name = cfg().drop_off_name
    drop_off = api.drop_off_point(drop_off_name)
    say(f"Точка сдачи {drop_off_name}: {drop_off}")
    # артикул или SKU Ozon → артикул как у Ozon; дальше всё (состояние, грузоместа, дубли) идёт по артикулу
    resolved = resolve_products(api, list(by_article))
    groups: dict[str, list[TableRow]] = {}
    skus: dict[str, int] = {}
    for key, trs in by_article.items():
        offer, sku = resolved.get(key, (None, None))
        if offer and sku:
            groups.setdefault(offer, []).extend(trs)
            skus[offer] = int(sku)
            if norm(offer) != norm(key):
                say(f"SKU {key} — это артикул {offer}")
        else:
            groups.setdefault(key, []).extend(trs)
    by_article = groups
    no_sku = [a for a in by_article if a not in skus]
    say(f"SKU найдены для {len(by_article) - len(no_sku)} из {len(by_article)} артикулов."
        + (f" Не найдены: {no_sku}" if no_sku else ""))
    unknown = sorted({r.cluster for r in visible if norm(r.cluster) not in clusters})
    if unknown:
        say(f"Кластеры, которых нет в списке Ozon (не угадываю, помечу для ручной проверки): {unknown}")
        say("  Названия у Ozon: " + ", ".join(sorted(v[1] for v in clusters.values())))
    # Защита от дублей по самим заявкам Ozon за сегодня (включая заявки из ЛК): см. key_today_orders
    wh_map = load_warehouse_map(store, cluster_list)
    try:
        todays = fetch_today_orders(api, now.date())
        _found, unknown = key_today_orders(todays, wh_map)
        articles_norm = {norm(a) for a in by_article}
        unknown = {u for u in unknown if u[0] in articles_norm}
        if unknown:
            # склад заявки из ЛК не значится в справочнике кластеров — узнаём склады кластеров этого прогона
            # через черновики прямой поставки (один раз, запоминается)
            planned = sorted({clusters[norm(r.cluster)][0] for rs in by_article.values() for r in rs if norm(r.cluster) in clusters})
            todo = [c for c in planned if c not in store.learned_clusters()]
            say(f"Склады заявок за сегодня, не привязанные к кластеру: {sorted({u[2] for u in unknown})}. "
                + (f"Узнаю склады {len(todo)} кластеров через черновики прямой поставки (около {-(-len(todo) * 31 // 60)} мин)..."
                   if todo else "Склады этих кластеров уже выучены — такие склады не учитываются."))
            sku_any = next((s for s in skus.values() if s), None)
            if todo and sku_any:
                learn_cluster_warehouses(api, store, cluster_list, todo, sku_any, say)
                wh_map = load_warehouse_map(store, cluster_list)
                _found, unknown = key_today_orders(todays, wh_map)
                unknown = {u for u in unknown if u[0] in articles_norm}
                if unknown:
                    say(f"  всё ещё без кластера: {sorted({u[2] for u in unknown})} — не учитываются.")
        supplied = supplied_today(api, store, cluster_list, now.date(), say, todays=todays, wh_map=wh_map)
    except ApiError as e:
        if options.mode != "trial":
            raise
        supplied = {}
        say(f"Проверка дублей по заявкам Ozon не удалась ({e}) — в пробном режиме продолжаю без неё.")
    say("")

    # Всё, что иначе было бы пропущено как уже существующее, собирается до расчёта: строки, куда сегодня уже есть заявка
    # у Ozon (out.duplicates), и артикулы, по которым приложение уже создавало заявку по этому списку (память,
    # out.remembered; force снимает её без вопроса). По умолчанию всё это пропускается; «всё равно сделать» — одно явное
    # решение пользователя на весь прогон.
    planned: dict[str, list[ClusterLine]] = {}
    prev_states: dict[str, dict] = {}
    for article, trs in by_article.items():
        lines = [ClusterLine(r.row, r.cluster, (clusters.get(norm(r.cluster)) or (None,))[0], r.plan) for r in trs]
        for ln in lines:
            nums = supplied.get(supply_key(article, ln.cluster_id, ln.warehouse_id)) if ln.cluster_id is not None else None
            if nums:
                ln.duplicate, ln.dup_orders = True, sorted(nums)
                out.duplicates.append((article, ln))
        planned[article] = lines
        prev = load_state(store, sheet, article)
        if prev.get("orders") and not options.force and previous_order_active(api, prev, say):
            prev_states[article] = prev
            out.remembered.append((article, list(prev["orders"]), prev.get("orders_created_at")))
    if out.duplicates or out.remembered:
        out.duplicates_allowed = decide_duplicates(out, options, say)
        for _article, ln in out.duplicates:
            ln.dup_allowed = out.duplicates_allowed
            ln.note = (f"дубль по решению пользователя ({ln.dup_text()})" if ln.dup_allowed else ln.dup_text() + " — пропущен")
        say("")

    try:
        for i, (article, lines) in enumerate(planned.items(), 1):
            if should_stop and should_stop():
                out.stopped = True
                say("Остановлено пользователем — остальные артикулы не обрабатываю.")
                break
            t0 = time.monotonic()
            sku = skus.get(article)
            say(f"[{i}/{len(by_article)}] {article}" + (f" (SKU {sku})" if sku else ""))
            prev = prev_states.get(article) or {}
            log.set_dir(article)
            prev_active = bool(prev) and not out.duplicates_allowed
            if prev and out.duplicates_allowed:
                say("  заявка " + ", ".join(map(str, prev["orders"])) + " уже есть — создаю ещё одну по решению пользователя")
            if prev_active:
                res = ArticleResult(article, sku, lines, skipped=True, orders=prev["orders"])
                res.note = "заявка уже создана: " + ", ".join(map(str, prev["orders"])) + " (пропущено)"
                say("  " + res.note)
            else:
                try:
                    res = process_article(api, article, lines, sku, drop_off, now, say, mode=options.mode)
                except AuthError:
                    raise
                except ApiError as e:
                    # ошибка по одному артикулу не должна ронять весь прогон
                    res = ArticleResult(article, sku, lines)
                    res.manual.append(f"ошибка Ozon: {e}")
                    say(f"  ! ошибка Ozon, артикул помечен для ручной проверки: {e}")
                save_state(store, sheet, res, options.mode, keep_prev_orders=bool(prev) or options.force)
            res.wh_map = wh_map          # чтобы после создания заявки назвать кластер по складу, который выбрал Ozon
            out.results.append(res)
            if options.on_result:
                options.on_result(res, "calc")
            facts, notes = outcome_rows(res, options.mode)
            out.facts.update(facts)
            out.notes.update(notes)
            say(f"  ({time.monotonic() - t0:.0f} с)")
            say("")
    except (NetworkError, AuthError, KeyboardInterrupt) as e:
        out.interrupted = e
        say(f"\nПРЕРВАНО: {e if not isinstance(e, KeyboardInterrupt) else 'остановлено пользователем'}. "
            f"Сохраняю то, что успел ({len(out.results)} из {len(by_article)} артикулов).")
    return out


def run_live_batch(api: OzonFboApi, store: FboStateStore, rows: list, sheet: str, options: RunOptions, now: datetime,
                   say: Callable[[str], None], confirm: Callable[[str], bool], log: RunLog,
                   should_stop: Optional[Callable[[], bool]] = None, draft_ttl_min: int = DRAFT_TTL_MIN) -> RunOutcome:
    """Боевой режим с одним подтверждением: сначала расчёт по всем артикулам, затем общий план,
    после «да» — заявки (и грузоместа) подряд без остановок."""
    plan_options = RunOptions(mode="plan", with_cargo=options.with_cargo, force=options.force,
                              articles=options.articles, auto_confirm=options.auto_confirm, on_result=options.on_result,
                              allow_duplicates=options.allow_duplicates, ask_duplicates=options.ask_duplicates)
    out = run_pipeline(api, store, rows, sheet, plan_options, now, say, confirm, log, should_stop)
    if not out.results:
        return out
    ready = [r for r in out.results if r.slot and r.draft_id and not r.manual and not r.note and not r.skipped]
    text = plan_summary(out.results, options.with_cargo)
    say("=" * 100)
    say(text)
    say("")
    if not ready:
        say("Создавать нечего: ни один артикул не готов к заявке.")
    elif out.stopped or out.interrupted:
        say("Расчёт не завершён — заявки не создаю.")
    elif options.auto_confirm or options.confirm_each or (options.confirm_plan or confirm)(text + "\n\nСоздать заявки на Ozon?"):
        say("Создаю заявки без подтверждения." if options.auto_confirm else
            "Подтверждение по каждому артикулу отдельно." if options.confirm_each else "Подтверждено — создаю заявки.")
        say("")
        try:
            # «всё равно сделать» действует и на память: иначе _create_one снова пропустил бы артикул по записи
            create_orders_batch(api, store, out.results, sheet, now, say, with_cargo=options.with_cargo,
                                force=options.force or out.duplicates_allowed,
                                confirm_each=(confirm if options.confirm_each and not options.auto_confirm else None),
                                should_stop=should_stop, on_result=options.on_result, draft_ttl_min=draft_ttl_min)
        except (NetworkError, AuthError, KeyboardInterrupt) as e:
            out.interrupted = e
            say(f"\nПРЕРВАНО при создании заявок: {e if not isinstance(e, KeyboardInterrupt) else 'остановлено пользователем'}.")
            for r in out.results:
                if r.created:      # созданные заявки фиксируем ещё раз: прерывание не должно потерять их номера
                    save_state(store, sheet, r, "live")
    else:
        say("Создание заявок отменено пользователем.")
        for r in ready:
            r.note = "создание заявки отменено пользователем"
            save_state(store, sheet, r, "plan")
    out.facts, out.notes = {}, {}
    for res in out.results:
        facts, notes = outcome_rows(res, "live")
        out.facts.update(facts)
        out.notes.update(notes)
    return out


def print_summary(out: RunOutcome, options: RunOptions, say: Callable[[str], None]) -> None:
    live = options.mode == "live"
    unfinished = " (прогон не завершён)" if out.interrupted or out.stopped else ""
    say("=" * 100)
    if live:
        n_created = sum(1 for r in out.results if r.created)
        n_cargo = sum(len(c.get("cargo_ids") or []) for r in out.results for c in r.cargoes.values())
        say("ИТОГ БОЕВОГО РЕЖИМА" + unfinished)
        say(f"Создано заявок на поставку: {n_created}, передано грузомест: {n_cargo}. "
            f"Проверьте заявки в ЛК: FBO → Заявки на поставку → Подготовка к поставкам ({LK_ORDERS_URL}).")
    else:
        say("ИТОГ ПРОБНОГО ПРОГОНА" + unfinished)
        say("Заявки на поставку НЕ создавались. Расчётные черновики созданы через API только для проверки "
            "количеств и слотов — в личном кабинете Ozon они не отображаются. Настоящие заявки появятся "
            "в кабинете только после запуска в боевом режиме.")
    say("")
    for res in out.results:
        if res.skipped:
            say(f"{res.article}: {res.note}")
            continue
        parts = ", ".join(f"{ln.cluster} {ln.plan}→{'?' if ln.fact is None else ln.fact}"
                          + (" (дубль по решению пользователя)" if ln.duplicate and ln.dup_allowed and ln.fact else "")
                          for ln in res.lines)
        if res.created:
            deadline = next((o.get("deadline_utc") for o in res.order_details if o.get("deadline_utc")), None)
            status = ("СОЗДАНА заявка " + ", ".join(f"№{n}" for n in res.orders) + f"; слот {slot_text(res.slot)}"
                      + (f"; грузомест передано {sum(len(c.get('cargo_ids') or []) for c in res.cargoes.values())}"
                         if res.cargoes else
                         "; грузоместа НЕ переданы" + (f", заполнить до {utc_to_msk_text(deadline)}" if deadline else "")))
        elif res.manual:
            status = "РУЧНАЯ ПРОВЕРКА — " + "; ".join(res.manual)
        elif res.slot and not res.note:
            status = f"готово к созданию заявки: слот {slot_text(res.slot)}; расчётный черновик {res.draft_id}"
        else:
            status = res.note
        say(f"{res.article}: {parts}")
        say(f"    {status}")
        if res.warehouses:
            say("    склады размещения (назначил Ozon): " + warehouses_text(res))
        if any(ln.fact for ln in res.lines):
            say("    грузоместа: " + cargo_summary(res))
