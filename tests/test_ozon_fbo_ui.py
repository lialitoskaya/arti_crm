"""Поставки FBO: характеризация фронтенда — раздел подключён к навигации и маршрутам, admin-only,
состояния прогона и диалог решения есть в разметке, стили на токенах без override-слоёв,
версия кэша согласована."""
from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path


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
        self.assertIn("Прогонов ещё не было.", view)
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
        self.assertIn("JSON.stringify({ answer })", answer)
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


if __name__ == "__main__":
    unittest.main()
