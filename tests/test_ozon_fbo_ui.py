"""Поставки FBO: характеризация фронтенда — раздел подключён к навигации и маршрутам, admin-only,
состояния прогона и диалог решения есть в разметке, стили на токенах без override-слоёв,
версия кэша согласована."""
from __future__ import annotations

import re
import sys
import unittest
from collections import Counter
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_chat_read_state_ui import _extract_function, _run_node  # noqa: E402

from app.services.ozon_fbo.config import SETTING_LABELS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "app" / "static" / "sw.js").read_text(encoding="utf-8")


def _block(text: str, start_marker: str, end_marker: str | None = None) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start) if end_marker else len(text)
    return text[start:end]


class OzonFboMarkupTests(unittest.TestCase):
    def test_section_and_navigation_are_admin_only(self) -> None:
        self.assertIn('<main id="ozonFboView" class="tasks-page ozon-fbo-page view hidden" aria-hidden="true">', INDEX)
        nav = _block(INDEX, '<button id="navOzonFbo"', "</button>")
        self.assertIn("admin-only hidden", nav)
        self.assertIn('<span class="btn-label">Поставки FBO</span>', nav)
        self.assertIn('<svg viewBox="0 0 24 24">', nav, "иконка из общего SVG-набора, не emoji")
        mobile = _block(INDEX, 'data-mobile-view="ozonFbo"', "</button>")
        self.assertIn("Поставки FBO", mobile)
        self.assertIn('class="mobile-more-item admin-only hidden"', _block(INDEX, '<button class="mobile-more-item admin-only hidden" type="button" data-mobile-view="ozonFbo"', "</button>"))
        self.assertIn('<button id="navSupplyPlanning"', INDEX, "раздел beta остаётся на месте")

    def test_run_form_covers_every_desktop_feature(self) -> None:
        view = _block(INDEX, '<main id="ozonFboView"', '<main id="reviewsView"')
        for mode in ("trial", "live", "live_nocargo", "cargo"):
            self.assertIn(f'name="ozonFboMode" value="{mode}"', view)
        for source in ("file", "manual"):
            self.assertIn(f'name="ozonFboSource" value="{source}"', view)
        for element_id in ("ozonFboFile", "ozonFboSheet", "ozonFboManualText", "ozonFboListName", "ozonFboArticles",
                           "ozonFboForce", "ozonFboConfirmEach", "ozonFboAutoConfirm", "ozonFboStartBtn", "ozonFboStopBtn",
                           "ozonFboReleaseBtn", "ozonFboLoadOrdersBtn", "ozonFboSelectAllOrdersBtn", "ozonFboOrdersRows",
                           "ozonFboLog", "ozonFboRows", "ozonFboHistoryRows", "ozonFboSettingsForm", "ozonFboCheckBtn",
                           "ozonFboLkLink", "ozonFboKeysStatus", "ozonFboQuestionBar", "ozonFboResultLink",
                           "ozonFboReportLink", "ozonFboApiLogLink"):
            self.assertIn(f'id="{element_id}"', view, element_id)
        for key in ("default_cargo_capacity", "cargo_capacity", "cargo_type", "drop_off_name", "min_slot_offset_days",
                    "slot_max_windows", "min_qty", "step", "unavailable_retry_sec", "all_clusters"):
            self.assertIn(f'id="ozonFboSetting_{key}"', view, key)
        self.assertIn('rel="noopener noreferrer"', _block(view, '<a id="ozonFboLkLink"', ">"))
        self.assertIn('accept=".xlsx"', view)
        self.assertNotIn("backdrop-filter", view)

    def test_decision_dialog_is_accessible(self) -> None:
        modal = _block(INDEX, '<div id="ozonFboDecisionModal"', "</main>")
        self.assertIn('role="dialog" aria-modal="true" aria-labelledby="ozonFboDecisionTitle"', modal)
        self.assertIn('class="user-create-modal hidden" aria-hidden="true"', modal)
        self.assertIn('id="ozonFboDecisionYesBtn"', modal)
        self.assertIn('id="ozonFboDecisionNoBtn"', modal)
        self.assertIn('aria-label="Закрыть"', modal)
        self.assertIn('<pre id="ozonFboDecisionText"', modal)

    def test_no_duplicate_ids_in_index(self) -> None:
        ids = re.findall(r'\sid="([^"]+)"', INDEX)
        duplicates = [item for item, count in Counter(ids).items() if count > 1]
        self.assertEqual(duplicates, [])

    def test_empty_states_are_explicit(self) -> None:
        view = _block(INDEX, '<main id="ozonFboView"', '<main id="reviewsView"')
        self.assertIn("Результатов пока нет.", view)
        self.assertIn("Поставок ещё не было.", view)
        self.assertIn("Заявки ещё не загружены.", view)
        self.assertIn('content: "Журнал появится после запуска."', STYLES)


class OzonFboScriptTests(unittest.TestCase):
    def test_view_is_wired_into_routing_and_navigation(self) -> None:
        self.assertIn("'supplyPlanning', 'ozonFbo', 'tasks'", APP_JS)
        self.assertIn("ozonFbo: 'fbo',", APP_JS)
        self.assertIn("fbo: 'ozonFbo',", APP_JS)
        self.assertIn("ozonFbo: $('ozonFboView'),", APP_JS)
        self.assertIn("ozonFbo: $('navOzonFbo'),", APP_JS)
        self.assertIn("if (normalizedView === 'ozonFbo') loadOzonFbo();", APP_JS)
        self.assertIn("bind('navOzonFbo', 'click', () => showView('ozonFbo'));", APP_JS)
        self.assertIn("navOzonFbo: 'ozonFbo'", APP_JS)
        self.assertIn("#navOzonFbo", APP_JS)
        self.assertIn("'supplyPlanning', 'ozonFbo', 'questions'", APP_JS, "мобильный пункт «Ещё» подсвечивается")
        self.assertIn("bindOzonFboControls();", _block(APP_JS, "bindSupplyPlanningControls();", "bind('navChats'"))

    def test_polling_stops_when_leaving_the_view(self) -> None:
        show_view = _block(APP_JS, "function showView(view, options = {}) {", "function escapeHtml(value)")
        self.assertIn("if (normalizedView !== 'ozonFbo') {\n    stopOzonFboPolling();\n    closeOzonFboDecision();\n  }", show_view)
        poll = _block(APP_JS, "async function pollOzonFboRun()", "function appendOzonFboLog")
        self.assertIn("if (ozonFbo.pollInFlight || !ozonFbo.runId) return;", poll, "single-flight опрос")
        self.assertIn("if (ozonFbo.runId !== id) return;", poll, "устаревший ответ не перезаписывает новый прогон")
        self.assertIn("if (activeView === 'ozonFbo') scheduleOzonFboPoll(", poll)
        self.assertIn("stopOzonFboPolling();", poll)

    def test_rendering_escapes_server_text_and_uses_delegation(self) -> None:
        rows = _block(APP_JS, "function renderOzonFboRows(rows)", "function ozonFboHeartbeatAge")
        self.assertEqual(rows.count("escapeHtml("), 5)
        self.assertIn("ozonFboToneBadge(row.tone)", rows, "состояние строки — текстовый бейдж, а не только цвет")
        self.assertIn("zero: 'исключён', manual: 'ручная проверка', ok: 'принято'", APP_JS)
        history = _block(APP_JS, "function renderOzonFboHistory(runs)", "function bindOzonFboControls")
        self.assertIn("data-ozon-fbo-open=", history)
        binding = _block(APP_JS, "function bindOzonFboControls()", "document.addEventListener('keydown'")
        self.assertIn("$('ozonFboHistoryRows')?.addEventListener('click'", binding, "одна делегированная обработка истории")
        self.assertIn("form.dataset.boundOzonFbo", binding, "обработчики не дублируются при повторном bind")
        self.assertIn("[data-ozon-fbo-order]", binding)

    def test_settings_labels_match_server_labels(self) -> None:
        view = _block(INDEX, '<main id="ozonFboView"', '<main id="reviewsView"')
        for key, label in SETTING_LABELS.items():
            self.assertIn(f"<label>{label}<", view, key)

    def test_render_run_behaviour_in_node(self) -> None:
        names = ("renderOzonFboRun", "renderOzonFboRows", "ozonFboToneBadge", "ozonFboStatusBadge", "ozonFboDecisionLabels",
                 "ozonFboHeartbeatAge", "ozonFboParseServerTime", "ozonFboFormatTime", "escapeHtml")
        consts = _block(APP_JS, "const OZON_FBO_POLL_MS", "function ozonFboSetStatus")
        tone_labels = _block(APP_JS, "const OZON_FBO_TONE_LABELS", "function ozonFboToneBadge")
        source = consts + tone_labels + "\n".join(_extract_function(APP_JS, name) for name in names)
        _run_node(source + r"""
const assert = require('node:assert/strict');
function element(id) {
  return { id, dataset: {}, markup: '', textContent: '', disabled: false, href: '', classes: new Set(),
    set innerHTML(v) { this.markup = v; }, get innerHTML() { return this.markup; },
    classList: { toggle(name, force) { if (force) this.el.classes.add(name); else this.el.classes.delete(name); return force; },
                 remove(name) { this.el.classes.delete(name); }, add(name) { this.el.classes.add(name); }, contains(name) { return this.el.classes.has(name); } },
    setAttribute() {}, querySelectorAll() { return []; } };
}
const elements = {};
for (const id of ['ozonFboRunTitle', 'ozonFboRunMeta', 'ozonFboReleaseBtn', 'ozonFboStopBtn', 'ozonFboRunFiles', 'ozonFboResultLink',
                  'ozonFboReportLink', 'ozonFboApiLogLink', 'ozonFboQuestionBar', 'ozonFboQuestionBarText', 'ozonFboRows', 'ozonFboDecisionModal']) {
  elements[id] = element(id); elements[id].classList.el = elements[id];
}
elements.ozonFboDecisionModal.classes.add('hidden');
function $(id) { return elements[id] || null; }
let opened = 0; let closed = 0;
function openOzonFboDecision() { opened += 1; }
function closeOzonFboDecision() { closed += 1; }
const Date_now = Date.now;

const active = { id: 7, status: 'running', is_active: true, mode: 'live', sheet_name: 'лист <x>', created_by_label: 'admin',
  created_at: '2026-10-29 10:00:00', heartbeat_at: new Date(Date_now() - 5000).toISOString(), stop_requested: false, has_result_file: false, has_api_log: false };
renderOzonFboRun(active);
assert.equal(elements.ozonFboRunTitle.textContent, 'Поставка №7');
assert.match(elements.ozonFboRunMeta.markup, /ozon-fbo-state-running/);
assert.match(elements.ozonFboRunMeta.markup, /лист &lt;x&gt;/);
assert.equal(elements.ozonFboStopBtn.disabled, false);
assert.ok(elements.ozonFboReleaseBtn.classes.has('hidden'), 'живой прогон снять нельзя');
assert.ok(elements.ozonFboApiLogLink.classes.has('hidden'), 'нет дампов — нет ссылки');
assert.ok(elements.ozonFboQuestionBar.classes.has('hidden'));

const stale = { ...active, heartbeat_at: new Date(Date_now() - 400000).toISOString(), stop_requested: true, has_api_log: true };
renderOzonFboRun(stale);
assert.ok(!elements.ozonFboReleaseBtn.classes.has('hidden'), 'без heartbeat появляется «Снять»');
assert.match(elements.ozonFboRunMeta.markup, /не отвечает/);
assert.equal(elements.ozonFboStopBtn.disabled, true, 'после запроса остановки кнопка заблокирована');
assert.equal(elements.ozonFboStopBtn.textContent, 'Останавливаю…');
assert.ok(!elements.ozonFboApiLogLink.classes.has('hidden'));

const asking = { ...active, status: 'awaiting_answer', question_kind: 'duplicates', question_text: 'СЕГОДНЯ УЖЕ ЕСТЬ ЗАЯВКИ', question_seq: 3 };
renderOzonFboRun(asking);
assert.equal(opened, 1, 'новый вопрос открывает диалог');
assert.ok(!elements.ozonFboQuestionBar.classes.has('hidden'));
assert.match(elements.ozonFboQuestionBarText.textContent, /такие заявки/i);
renderOzonFboRun(asking);
assert.equal(opened, 1, 'тот же вопрос второй раз не переоткрывается');
renderOzonFboRun({ ...asking, question_seq: 4 });
assert.equal(opened, 2, 'следующий вопрос с тем же текстом открывается заново по question_seq');
const finished = { ...active, status: 'finished', is_active: false, has_result_file: true, finished_at: '2026-10-29 10:20:00', error_text: '' };
renderOzonFboRun(finished);
assert.equal(closed, 1, 'после ответа диалог закрывается');
assert.equal(elements.ozonFboStopBtn.disabled, true);
assert.ok(!elements.ozonFboResultLink.classes.has('hidden'));
assert.match(elements.ozonFboRunMeta.markup, /ozon-fbo-state-finished/);

assert.deepEqual(ozonFboDecisionLabels({ question_kind: 'plan', question_text: 'ГРУЗОМЕСТА: заявок 2' }).yes, 'Передать грузоместа');
assert.deepEqual(ozonFboDecisionLabels({ question_kind: 'plan', question_text: 'ПЛАН ПОСТАВОК' }).yes, 'Создать заявки');
assert.deepEqual(ozonFboDecisionLabels({ question_kind: 'duplicates', question_text: 'x' }).no, 'Пропустить дубли');

renderOzonFboRows([{ article: 'AT-<b>', cluster: 'Тверь', plan: 40, fact: 0, note: 'исключён: <script>', tone: 'zero' },
                   { article: 'AT-2', cluster: 'Казань', plan: 20, fact: 20, note: '', tone: 'ok' },
                   { article: 'AT-3', cluster: 'Уфа', plan: 10, fact: null, note: 'x', tone: 'manual' }]);
assert.match(elements.ozonFboRows.markup, /AT-&lt;b&gt;/);
assert.doesNotMatch(elements.ozonFboRows.markup, /<script>/);
assert.match(elements.ozonFboRows.markup, /ozon-fbo-tone-zero">исключён/);
assert.match(elements.ozonFboRows.markup, /ozon-fbo-tone-manual">ручная проверка/);
assert.equal((elements.ozonFboRows.markup.match(/<tr>/g) || []).length, 3);
renderOzonFboRows([]);
assert.match(elements.ozonFboRows.markup, /Результатов пока нет/);
""")

    def test_decision_dialog_lifecycle(self) -> None:
        self.assertIn("function openOzonFboDecision(run)", APP_JS)
        self.assertIn("function closeOzonFboDecision()", APP_JS)
        self.assertIn("if (event.key === 'Escape' && !$('ozonFboDecisionModal')?.classList.contains('hidden')) closeOzonFboDecision();", APP_JS)
        labels = _block(APP_JS, "function ozonFboDecisionLabels(run)", "function openOzonFboDecision")
        self.assertIn("Всё равно сделать", labels)
        self.assertIn("Пропустить дубли", labels)
        self.assertIn("Создать заявки", labels)
        self.assertIn("Передать грузоместа", labels)
        answer = _block(APP_JS, "async function answerOzonFboDecision(answer)", "async function stopOzonFboRun")
        self.assertIn("JSON.stringify({ answer, question_seq: run.question_seq ?? null })", answer, "ответ привязан к вопросу")
        self.assertIn("function trapOzonFboDecisionFocus(event)", APP_JS, "диалог удерживает фокус")
        self.assertIn("ozonFbo.decisionOpener", APP_JS, "фокус возвращается открывшему элементу")
        submit = _block(APP_JS, "async function submitOzonFboRun(event)", "function stopOzonFboPolling")
        self.assertIn("if (ozonFbo.submitInFlight) return;", submit, "защита от двойной отправки")
        self.assertIn("window.confirm(", submit, "боевой режим требует подтверждения до запуска")
        self.assertIn("apiForm('/api/ozon-fbo/runs', form)", submit)

    def test_frontend_cache_version_bumped_and_consistent(self) -> None:
        match = re.search(r"ARTI_CRM_SW_VERSION = '([^']+)'", SERVICE_WORKER)
        self.assertIsNotNone(match)
        version = match.group(1)
        self.assertIn("ozon-fbo", version)
        self.assertIn(f"styles.css?v={version}", INDEX)
        self.assertIn(f"app.js?v={version}", INDEX)


class OzonFboStyleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.block = STYLES[STYLES.index("/* ---- Поставки FBO Ozon"):]

    def test_styles_use_tokens_without_ai_slop(self) -> None:
        self.assertNotIn("!important", self.block)
        self.assertNotIn("backdrop-filter", self.block)
        self.assertNotIn("linear-gradient", self.block)
        self.assertNotIn("text-shadow", self.block)
        self.assertNotIn("box-shadow", self.block)
        for token in ("--crm-red", "--crm-yellow", "--crm-green", "--crm-orange", "--crm-line", "--crm-muted", "--crm-panel-soft", "--crm-radius-md"):
            self.assertIn(f"var({token}", self.block, token)
        self.assertNotRegex(self.block, r"#[0-9a-fA-F]{3,8}\b", "цвета только из токенов")

    def test_log_and_tables_handle_long_content(self) -> None:
        log = _block(self.block, ".ozon-fbo-log {", "}")
        self.assertIn("overflow: auto", log)
        self.assertIn("white-space: pre", log)
        self.assertIn("max-height", log)
        self.assertIn("overflow-wrap: anywhere", self.block)
        for tone in ("zero", "manual", "ok"):
            self.assertIn(f".ozon-fbo-tone-{tone} {{", self.block, tone)
        self.assertNotIn("tr.ozon-fbo-row-", self.block, "подложка строки в тёмной теме обнуляется общим правилом таблиц")

    def test_mobile_layout_collapses_to_one_column(self) -> None:
        mobile = _block(self.block, "@media (max-width: 900px)")
        self.assertIn(".ozon-fbo-grid,", mobile)
        self.assertIn("grid-template-columns: 1fr;", mobile)
        self.assertIn(".ozon-fbo-form-actions button { width: 100%; }", mobile)
        self.assertIn("font-size: 16px;", mobile, "поля ввода не вызывают zoom на iOS")
        self.assertEqual(self.block.count("min-height: auto"), 1, "сброс чекбоксов и радио объявлен один раз")


if __name__ == "__main__":
    unittest.main()
