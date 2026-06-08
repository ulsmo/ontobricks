/**
 * Domain → Validation (review workflow)
 *
 * Business-user-oriented validation workspace for the loaded domain
 * version: a soft consistency-check summary, reviewer sign-off panel
 * (quorum-gated publish), and the full audit trail.
 *
 * Data sources:
 *   GET  /domain/version-status        -> loaded folder + version + status
 *   GET  /review/{folder}/{version}    -> review state + actions + events
 *   GET  /validate/detailed            -> soft readiness summary
 *   POST /review/{folder}/{version}/{submit|signoff|publish|reopen}
 */
(function () {
    'use strict';

    const state = { folder: '', version: '', detail: null };

    window.loadDomainReview = loadReview;

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('btnRefreshReview')?.addEventListener(
            'click', loadReview
        );
    });

    async function loadReview() {
        const body = document.getElementById('reviewBody');
        if (!body) return;
        body.innerHTML = spinner('Loading review status...');

        try {
            const vs = await (await fetch('/domain/version-status', {
                credentials: 'same-origin',
            })).json();
            state.folder = vs.domain_folder || '';
            state.version = vs.version || '';

            if (!state.folder || !vs.has_registry) {
                body.innerHTML =
                    '<div class="alert alert-info small mb-0">' +
                    '<i class="bi bi-info-circle me-1"></i>' +
                    'Save this domain to the registry to start the review workflow.' +
                    '</div>';
                return;
            }

            const detail = await (await fetch(
                '/review/' + encodeURIComponent(state.folder) + '/' +
                encodeURIComponent(state.version),
                { credentials: 'same-origin' }
            )).json();

            if (!detail.success) {
                body.innerHTML =
                    '<div class="alert alert-danger small mb-0">' +
                    escapeHtml(detail.message || 'Failed to load review status') +
                    '</div>';
                return;
            }
            state.detail = detail;
            render(detail);
            loadReadiness();
        } catch (err) {
            console.error('loadReview error:', err);
            showNotification('Could not load review status: ' + err.message, 'error');
            body.innerHTML =
                '<div class="alert alert-danger small mb-0">Network error: ' +
                escapeHtml(String(err)) + '</div>';
        }
    }

    function render(d) {
        const body = document.getElementById('reviewBody');
        if (!body) return;
        body.innerHTML =
            statusBanner(d) +
            '<div class="row g-3">' +
            '<div class="col-lg-6">' + consistencyCard() + actionsCard(d) + '</div>' +
            '<div class="col-lg-6">' + timelineCard(d) + '</div>' +
            '</div>';

        wireActions(d);
    }

    /* ── Status banner ─────────────────────────────── */
    function statusBanner(d) {
        const pct = d.quorum > 0
            ? Math.min(100, Math.round((d.approvals / d.quorum) * 100))
            : 0;
        const progress = d.status === 'IN-REVIEW'
            ? '<div class="review-progress mt-2">' +
              '<div class="d-flex justify-content-between small mb-1">' +
              '<span>Sign-offs collected</span>' +
              '<span class="fw-medium">' + d.approvals + ' / ' + d.quorum + '</span>' +
              '</div>' +
              '<div class="progress" style="height:6px;">' +
              '<div class="progress-bar ' + (d.quorum_met ? 'bg-success' : 'bg-warning') +
              '" style="width:' + pct + '%"></div></div></div>'
            : '';

        return '<div class="review-banner review-banner-' +
            d.status.toLowerCase().replace('-', '') + ' mb-4">' +
            '<div class="d-flex align-items-center gap-3">' +
            statusBadge(d.status) +
            '<div><strong>' + escapeHtml(d.domain) + '</strong> ' +
            '<span class="text-muted">v' + escapeHtml(d.version) + '</span></div>' +
            '</div>' + progress + '</div>';
    }

    /* ── Consistency checks (soft) ─────────────────── */
    function consistencyCard() {
        return '<div class="card review-card mb-3"><div class="card-body">' +
            '<h6 class="card-title"><i class="bi bi-clipboard-check me-2"></i>Consistency checks</h6>' +
            '<p class="text-muted small mb-3">A quick readiness summary. ' +
            'These checks are advisory &mdash; they do not block publishing.</p>' +
            '<div id="reviewReadiness">' + spinner('Checking...') + '</div>' +
            '<div class="d-flex gap-2 mt-3">' +
            '<a class="btn btn-sm btn-outline-secondary" href="/domain/?section=validation">' +
            '<i class="bi bi-speedometer2 me-1"></i>Open Cockpit</a>' +
            '<a class="btn btn-sm btn-outline-secondary" href="/ontology/?section=pitfalls">' +
            '<i class="bi bi-bug me-1"></i>Run Pitfalls</a>' +
            '</div></div></div>';
    }

    async function loadReadiness() {
        const el = document.getElementById('reviewReadiness');
        if (!el) return;
        try {
            const d = await (await fetch('/validate/detailed', {
                credentials: 'same-origin',
            })).json();
            const dtwin = d.dtwin || {};
            const warehouse = d.warehouse || {};
            el.innerHTML =
                checkRow('Ontology valid', !!d.ontology_valid) +
                checkRow('Mapping complete', !!d.mapping_valid) +
                checkRow('SQL Warehouse configured', !!warehouse.warehouse_id) +
                checkRow('Digital Twin built', dtwin.indicator === 'green',
                    dtwin.indicator === 'orange');
        } catch (err) {
            el.innerHTML = '<div class="text-muted small">Readiness unavailable.</div>';
        }
    }

    function checkRow(label, ok, warn) {
        const icon = ok
            ? '<i class="bi bi-check-circle-fill text-success"></i>'
            : (warn
                ? '<i class="bi bi-exclamation-triangle-fill text-warning"></i>'
                : '<i class="bi bi-x-circle-fill text-danger"></i>');
        return '<div class="review-check d-flex align-items-center gap-2 mb-1">' +
            icon + '<span class="small">' + escapeHtml(label) + '</span></div>';
    }

    /* ── Actions ───────────────────────────────────── */
    function actionsCard(d) {
        const a = d.actions || {};
        const buttons = [];

        if (a.can_submit) {
            buttons.push(btn('submit', 'btn-info', 'eye', 'Submit for review'));
        }
        if (a.can_submit === false && d.status === 'DRAFT' && a.submit_blocked_reason) {
            buttons.push('<div class="alert alert-warning small mb-2">' +
                '<i class="bi bi-exclamation-triangle me-1"></i>' +
                escapeHtml(a.submit_blocked_reason) + '</div>');
        }
        if (a.can_approve) {
            buttons.push(btn('approve', 'btn-success', 'hand-thumbs-up', 'Approve'));
        }
        if (d.already_approved && d.status === 'IN-REVIEW') {
            buttons.push('<div class="alert alert-success small mb-2 py-2">' +
                '<i class="bi bi-check-circle me-1"></i>You have signed off on this version.</div>');
        }
        if (a.can_request_changes) {
            buttons.push(btn('request_changes', 'btn-outline-danger', 'arrow-counterclockwise', 'Request changes'));
        }
        if (a.can_publish && d.publish_override) {
            buttons.push('<div class="alert alert-warning small mb-2 py-2">' +
                '<i class="bi bi-shield-exclamation me-1"></i>' +
                'Admin override: only ' + d.approvals + ' of ' + d.quorum +
                ' sign-off(s) collected. Publishing will bypass the quorum.</div>');
            buttons.push(btn('publish', 'btn-warning', 'broadcast', 'Publish (override quorum)'));
        } else if (a.can_publish) {
            buttons.push(btn('publish', 'btn-success', 'broadcast', 'Publish'));
        }
        if (d.status === 'IN-REVIEW' && !a.can_publish && d.actions && !d.quorum_met) {
            buttons.push('<div class="text-muted small mt-1">' +
                'Publishing unlocks once ' + d.quorum + ' sign-off(s) are collected.</div>');
        }
        if (a.can_reopen) {
            buttons.push(btn('reopen', 'btn-outline-secondary', 'unlock', 'Reopen for editing'));
        }

        const inner = buttons.length
            ? '<div class="d-grid gap-2">' + buttons.join('') + '</div>'
            : '<p class="text-muted small mb-0">No actions available to you at this stage.</p>';

        return '<div class="card review-card"><div class="card-body">' +
            '<h6 class="card-title"><i class="bi bi-ui-checks me-2"></i>Your actions</h6>' +
            inner + '</div></div>';
    }

    function btn(action, cls, icon, label) {
        return '<button type="button" class="btn btn-sm ' + cls + '" ' +
            'data-review-action="' + action + '">' +
            '<i class="bi bi-' + icon + ' me-1"></i>' + escapeHtml(label) + '</button>';
    }

    function wireActions() {
        document.querySelectorAll('[data-review-action]').forEach((b) => {
            b.addEventListener('click', () => onAction(b.dataset.reviewAction));
        });
    }

    async function onAction(action) {
        if (action === 'approve') {
            const r = await ReviewModals.promptComment({
                title: 'Approve version', confirmText: 'Approve',
                confirmClass: 'btn-success', icon: 'hand-thumbs-up',
                message: 'Record your sign-off on v' + escapeHtml(state.version) +
                    '. This counts toward the publish quorum.',
            });
            if (r.confirmed) {
                // Approve does not change the lifecycle status.
                await postAction('signoff',
                    { decision: 'approve', comment: r.comment }, false);
            }
            return;
        }
        if (action === 'request_changes') {
            const r = await ReviewModals.promptComment({
                title: 'Request changes', confirmText: 'Request changes',
                confirmClass: 'btn-danger', icon: 'arrow-counterclockwise',
                requireComment: true,
                message: 'Send v' + escapeHtml(state.version) +
                    ' back to Draft for changes. Tell the author what to fix.',
            });
            if (r.confirmed) {
                await postAction('signoff',
                    { decision: 'request_changes', comment: r.comment }, true);
            }
            return;
        }
        const dialogs = {
            submit: {
                title: 'Submit for review', confirmText: 'Submit', icon: 'eye',
                message: 'Submit v' + escapeHtml(state.version) +
                    ' for review? Editing locks until it returns to Draft.',
            },
            publish: {
                title: 'Publish version', confirmText: 'Publish', icon: 'broadcast',
                message: 'Publish v' + escapeHtml(state.version) +
                    '? It becomes the live version on the API/MCP surface.',
            },
            reopen: {
                title: 'Reopen for editing', confirmText: 'Reopen', icon: 'unlock',
                message: 'Reopen v' + escapeHtml(state.version) +
                    ' for editing? It returns to Draft.',
            },
        };
        const dlg = dialogs[action];
        if (!dlg) return;
        const r = await ReviewModals.promptComment({
            title: dlg.title, message: dlg.message,
            confirmText: dlg.confirmText, confirmClass: 'btn-primary', icon: dlg.icon,
        });
        if (!r.confirmed) return;
        await postAction(action, { comment: r.comment }, true);
    }

    async function postAction(endpoint, payload, statusChanging) {
        try {
            const resp = await fetch(
                '/review/' + encodeURIComponent(state.folder) + '/' +
                encodeURIComponent(state.version) + '/' + endpoint,
                {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload || {}),
                }
            );
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                showNotification(data.message || 'Action failed', 'error');
                return;
            }
            showNotification('Done — version is now ' + (data.status || '') + '.', 'success');
            if (typeof fetchCachedInvalidate === 'function') {
                fetchCachedInvalidate('/navbar/state');
            }
            if (statusChanging) {
                // Status drives the edit-lock body class; reload to re-apply it.
                setTimeout(() => window.location.reload(), 700);
            } else {
                loadReview();
            }
        } catch (err) {
            showNotification('Error: ' + err.message, 'error');
        }
    }

    /* ── Audit timeline ────────────────────────────── */
    function timelineCard(d) {
        const events = (d.events || []).slice().reverse(); // newest first
        const items = events.length
            ? events.map(timelineItem).join('')
            : '<div class="text-muted small">No activity yet.</div>';
        return '<div class="card review-card"><div class="card-body">' +
            '<h6 class="card-title"><i class="bi bi-clock-history me-2"></i>Audit trail</h6>' +
            '<div class="review-timeline">' + items + '</div></div></div>';
    }

    function timelineItem(e) {
        const meta = ACTION_META[e.action] || { icon: 'dot', cls: 'text-muted', label: e.action };
        const transition = (e.from_status && e.to_status)
            ? '<span class="review-transition">' +
              escapeHtml(e.from_status) + ' &rarr; ' + escapeHtml(e.to_status) + '</span>'
            : '';
        const comment = e.comment
            ? '<div class="review-event-comment">' + escapeHtml(e.comment) + '</div>'
            : '';
        return '<div class="review-event">' +
            '<span class="review-event-icon ' + meta.cls + '">' +
            '<i class="bi bi-' + meta.icon + '"></i></span>' +
            '<div class="review-event-body">' +
            '<div class="review-event-head">' +
            '<span class="fw-medium">' + escapeHtml(meta.label) + '</span> ' +
            transition +
            '<span class="review-event-time">' + formatTime(e.created_at) + '</span>' +
            '</div>' +
            '<div class="review-event-actor small text-muted">' +
            escapeHtml(e.actor || 'unknown') + '</div>' +
            comment + '</div></div>';
    }

    const ACTION_META = {
        submitted: { icon: 'eye', cls: 'text-info', label: 'Submitted for review' },
        approved: { icon: 'hand-thumbs-up', cls: 'text-success', label: 'Approved' },
        changes_requested: { icon: 'arrow-counterclockwise', cls: 'text-danger', label: 'Changes requested' },
        published: { icon: 'broadcast', cls: 'text-success', label: 'Published' },
        reopened: { icon: 'unlock', cls: 'text-secondary', label: 'Reopened' },
        commented: { icon: 'chat-left-text', cls: 'text-muted', label: 'Comment' },
    };

    /* ── Helpers ───────────────────────────────────── */
    function statusBadge(status) {
        const map = {
            'DRAFT': 'bg-secondary',
            'IN-REVIEW': 'bg-warning text-dark',
            'PUBLISHED': 'bg-success',
        };
        const cls = map[status] || 'bg-secondary';
        const label = status === 'IN-REVIEW' ? 'In Review'
            : (status.charAt(0) + status.slice(1).toLowerCase());
        return '<span class="badge ' + cls + '">' + escapeHtml(label) + '</span>';
    }

    function formatTime(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        return '<span class="text-muted small">' +
            escapeHtml(d.toLocaleString()) + '</span>';
    }

    function spinner(text) {
        return '<div class="text-center text-muted small py-3">' +
            '<span class="spinner-border spinner-border-sm me-1"></span>' +
            escapeHtml(text) + '</div>';
    }

    function escapeHtml(text) {
        if (text == null) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }
})();
