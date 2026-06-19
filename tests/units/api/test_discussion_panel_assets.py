"""
Contract tests for the Discussion panel front-end assets.

The Discussion-panel behaviour added for AI-Agent tasks lives entirely in
static JS/CSS (the repo has no JS unit-test harness). These tests fetch the
served assets through the app's ``/static`` mount and assert the wiring is
present, so an accidental removal/rename of a key hook is caught by CI.

They are deliberately token-level (not behavioural) — they guard that the
contract between the panel and the rest of the app stays intact:

* comment bodies are rendered as markdown (via the global ``marked``);
* AI-Agent task status + progress are surfaced in the pane and answerable;
* finishing an AI-Agent task broadcasts ``ontobricks:design-updated`` and the
  ontology / mapping pages listen for it to refresh their design.
"""

from __future__ import annotations

import pytest

PANEL_JS = "/static/global/js/comments-panel.js"
ONTOLOGY_INIT_JS = "/static/ontology/js/ontology-init.js"
MAPPING_INIT_JS = "/static/mapping/js/mapping-init.js"
COLLAB_JS = "/static/domain/js/domain-collaboration.js"
REVIEW_CSS = "/static/global/css/review-modals.css"

# Event name shared between the panel (dispatch) and the pages (listeners).
DESIGN_UPDATED_EVENT = "ontobricks:design-updated"


def _static(client, path: str) -> str:
    """Fetch a served static asset, asserting it is reachable."""
    resp = client.get(path)
    assert resp.status_code == 200, f"GET {path} returned {resp.status_code}"
    return resp.text


@pytest.fixture
def panel_js(client) -> str:
    return _static(client, PANEL_JS)


class TestDiscussionMarkdownRendering:
    """Comment bodies render markdown (not raw source) via the global marked."""

    def test_panel_defines_markdown_renderer(self, panel_js):
        assert "function renderMarkdown" in panel_js

    def test_renderer_uses_global_marked(self, panel_js):
        assert "window.marked" in panel_js
        assert "marked.parse" in panel_js

    def test_renderer_has_plaintext_fallback(self, panel_js):
        # When marked is unavailable it must still escape + line-break, never
        # inject raw text as HTML.
        assert "replace(/\\n/g, '<br>')" in panel_js

    def test_bubble_renders_body_as_markdown(self, panel_js):
        # The comment bubble pipes the parsed body through the renderer into a
        # markdown-styled container rather than escaping it verbatim.
        assert "oc-md" in panel_js
        assert "renderMarkdown(parsed.text)" in panel_js

    def test_markdown_styles_present(self, client):
        css = _static(client, REVIEW_CSS)
        assert ".oc-md" in css


class TestDiscussionAgentStatus:
    """AI-Agent runs surface progress + status inside the pane."""

    def test_loads_ai_tasks_and_runs(self, panel_js):
        assert "function loadAiTasks" in panel_js
        assert "function loadAgentRuns" in panel_js
        # AI-Agent background runs are the router/plan/run worker.
        assert "'task_router'" in panel_js

    def test_progress_strip_rendered(self, panel_js):
        assert "function renderAgentStrip" in panel_js
        assert "oc-agent-strip" in panel_js

    def test_live_polling_loop(self, panel_js):
        assert "function panelPollTick" in panel_js
        assert "function startPanelPolling" in panel_js
        # Polling must not clobber a half-written reply.
        assert "function userIsComposing" in panel_js

    def test_per_thread_status_chip(self, panel_js):
        assert "function agentChipHtml" in panel_js
        assert "waiting for your reply" in panel_js

    def test_strip_and_chip_styles_present(self, client):
        css = _static(client, REVIEW_CSS)
        for token in (".oc-agent-strip", "oc-agent-working", "oc-agent-waiting"):
            assert token in css, f"missing CSS token {token!r}"


class TestAnswerTheAgent:
    """A parked AI-Agent thread is answerable from the pane."""

    def test_answer_box_present(self, panel_js):
        assert "function agentAnswerHtml" in panel_js
        assert "Answer the AI Agent" in panel_js

    def test_answer_send_wired_to_reply(self, panel_js):
        # The send button posts a reply (parent = thread root) which resumes the
        # agent server-side, then re-checks tracking.
        assert "data-agent-send" in panel_js
        assert "ensureAgentTracking" in panel_js

    def test_answer_box_styles_present(self, client):
        css = _static(client, REVIEW_CSS)
        assert ".oc-agent-answer" in css


class TestDesignUpdatedRefresh:
    """Finishing an AI-Agent design task refreshes the open pages."""

    def test_panel_dispatches_event_on_completion(self, panel_js):
        assert "function announceAgentCompletions" in panel_js
        assert DESIGN_UPDATED_EVENT in panel_js

    def test_ontology_page_listens_and_refreshes(self, client):
        js = _static(client, ONTOLOGY_INIT_JS)
        assert DESIGN_UPDATED_EVENT in js
        # Pulls the agent's saved changes and re-renders the active section.
        assert "loadOntologyFromSession" in js
        assert "_initSectionByName(SidebarNav.getActiveSection())" in js

    def test_mapping_page_listens_and_refreshes(self, client):
        js = _static(client, MAPPING_INIT_JS)
        assert DESIGN_UPDATED_EVENT in js
        # Re-pulls the loaded ontology and redraws the mapping design.
        assert "/ontology/get-loaded-ontology" in js
        assert "refreshMappingDesign" in js


class TestDiscussionDomainScope:
    """The panel is a single domain-wide thread with no tagging UI."""

    def test_no_anchor_in_requests(self, panel_js):
        # Comments are domain-wide: the panel must not send the (removed)
        # anchor_type / anchor_ref to the /comments API.
        assert "anchor_type" not in panel_js
        assert "anchor_ref" not in panel_js
        assert "anchorType" not in panel_js
        assert "anchorRef" not in panel_js

    def test_no_kind_badge_separator(self, panel_js):
        # The header no longer renders the "Class/Domain/Mapping" kind badge
        # that separated discussions by selection.
        assert "bg-secondary-subtle text-dark border me-1" not in panel_js

    def test_tag_picker_removed_from_compose(self, panel_js):
        # No entity/relationship tag widget when writing a comment/reply.
        assert "tagWidgetHtml" not in panel_js
        assert "data-oc-tag-select" not in panel_js
        assert "data-oc-tagbar" not in panel_js

    def test_no_tag_encoding_on_post(self, panel_js):
        # New comments post the raw body — tags are no longer embedded.
        assert "encodeBody" not in panel_js
        assert "collectTags" not in panel_js


class TestDiscussionTimelineMarkdown:
    """Domain → Discussions timeline renders comment bodies as markdown."""

    def test_timeline_defines_markdown_renderer(self, client):
        js = _static(client, COLLAB_JS)
        assert "function renderMarkdown" in js
        assert "window.marked" in js
        assert "marked.parse" in js

    def test_timeline_entry_renders_markdown(self, client):
        js = _static(client, COLLAB_JS)
        # Timeline entry pipes the parsed body through the renderer into a
        # markdown-styled container rather than escaping it verbatim.
        assert "renderMarkdown(parsed.text)" in js
        assert "oc-md" in js

    def test_timeline_markdown_styles_apply(self, client):
        # The Domain page loads review-modals.css, and the `.oc-md` reset is
        # unscoped so it applies to the timeline's `.oc-tl-text.oc-md`.
        css = _static(client, REVIEW_CSS)
        assert ".oc-md {" in css
