/**
 * Collaborative comments & tasks — domain discussion panel (global)
 *
 * A reusable right-side offcanvas that opens the single domain-wide,
 * threaded discussion for ``(folder, version)``. Any surface can open it
 * through the global API:
 *
 *   OntoComments.openThread({ folder, version });
 *
 * Backed by the /comments API (see CommentService). A comment can be
 * turned into a task assigned to a teammate; the assignee picker is loaded
 * from the domain access roster (/review/<folder>/<version>/team).
 *
 * Depends on Bootstrap 5 (Offcanvas) and the global escapeHtml in
 * utils.js (falls back to a local implementation when absent).
 */
(function () {
    'use strict';

    // Sentinel assignee that routes the task to the AI Agent (see
    // back/objects/registry/agent_task_runner.AI_AGENT_PRINCIPAL).
    const AI_AGENT_PRINCIPAL = 'agent://router';

    let el = null;
    let offcanvas = null;
    let ctx = null;          // { folder, version }
    let membersCache = {};   // key folder/version -> [members]
    let currentUser = null;  // current user's email/principal (for "Assign to me")
    let currentUserPromise = null;
    let aiTasksByComment = {};  // root comment_id -> AI-Agent DomainTask (this version)
    let agentRuns = [];         // active task_router background runs (from /tasks/)
    let panelPollTimer = null;  // live-refresh timer while the panel is open
    let lastListSig = '';       // signature of the last rendered comment set
    let aiStatusSnapshot = {};  // comment_id -> last-seen AI task status (transition guard)
    let aiSnapshotReady = false;// becomes true after the first AI-task load (baseline)

    function esc(text) {
        if (typeof window.escapeHtml === 'function') return window.escapeHtml(text);
        if (text == null) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }

    function escAttr(text) {
        return esc(text).replace(/"/g, '&quot;');
    }

    // Render a comment body's markdown to HTML. Uses `marked` (loaded globally
    // in base.html, same as the ontology chat assistant); falls back to escaped
    // text with <br> when it isn't available.
    function renderMarkdown(text) {
        const src = text || '';
        if (typeof window.marked !== 'undefined' && window.marked.parse) {
            try {
                window.marked.setOptions({ breaks: true, gfm: true });
                return window.marked.parse(src);
            } catch (e) { /* fall through to plain text */ }
        }
        return esc(src).replace(/\n/g, '<br>');
    }

    function notify(msg, kind) {
        if (typeof window.showNotification === 'function') {
            window.showNotification(msg, kind || 'info');
        }
    }

    function build() {
        if (el) return;
        el = document.createElement('div');
        el.className = 'offcanvas offcanvas-end oc-comments';
        el.tabIndex = -1;
        el.setAttribute('aria-labelledby', 'ocCommentsTitle');
        el.style.width = '460px';
        el.innerHTML =
            '<div class="offcanvas-header border-bottom align-items-start">' +
            '<div class="flex-grow-1 me-2">' +
            '<h6 class="offcanvas-title mb-0" id="ocCommentsTitle">' +
            '<i class="bi bi-chat-dots me-2"></i>Discussion</h6>' +
            '<div class="small text-muted" data-oc-anchor></div>' +
            '</div>' +
            '<button type="button" class="btn btn-sm btn-outline-success me-2" ' +
            'data-oc-new-task title="Create a task (assign to a teammate or the AI Agent)">' +
            '<i class="bi bi-check2-square me-1"></i>New task</button>' +
            '<button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button>' +
            '</div>' +
            '<div class="offcanvas-body d-flex flex-column p-0">' +
            '<div class="oc-newtask-box border-bottom p-3 d-none" data-oc-newtask></div>' +
            '<div class="oc-agent-strip border-bottom px-3 py-2 d-none" data-oc-agent-strip></div>' +
            '<div class="oc-comments-list flex-grow-1 p-3" data-oc-list></div>' +
            '<div class="oc-comments-compose border-top p-3" data-oc-compose>' +
            '<textarea class="form-control form-control-sm mb-2" rows="2" ' +
            'data-oc-input placeholder="Write a comment..."></textarea>' +
            '<div class="d-flex justify-content-end">' +
            '<button type="button" class="btn btn-sm btn-primary" data-oc-send>' +
            '<i class="bi bi-send me-1"></i>Comment</button>' +
            '</div></div>' +
            '</div>';
        document.body.appendChild(el);

        const compose = el.querySelector('[data-oc-compose]');
        el.querySelector('[data-oc-send]').addEventListener('click', () => {
            const ta = compose.querySelector('[data-oc-input]');
            postComment((ta.value || '').trim(), null, ta, compose);
        });
        el.querySelector('[data-oc-new-task]').addEventListener('click', openNewTask);
        el.addEventListener('hidden.bs.offcanvas', stopPanelPolling);
    }

    // Header subtitle: discussions are domain-wide, so just name the domain
    // (no per-anchor "kind" badge that would separate threads by selection).
    function renderAnchorBadge() {
        const label = ctx.folder
            ? esc(ctx.folder) + ' · v' + esc(ctx.version)
            : '';
        el.querySelector('[data-oc-anchor]').innerHTML = label;
    }

    // ---- Tags (legacy) ------------------------------------------------------
    // The entity/relationship tag picker has been removed — discussions are
    // domain-wide and untagged. We keep the marker + parser/renderer so older
    // comments that embedded tags still display their chips (read-only).
    const TAG_MARK = '\n\n[[onto-tags]]';

    function parseBody(body) {
        const raw = body || '';
        const idx = raw.indexOf(TAG_MARK);
        if (idx === -1) return { text: raw, tags: [] };
        let tags = [];
        try { tags = JSON.parse(raw.slice(idx + TAG_MARK.length)) || []; }
        catch (e) { tags = []; }
        return { text: raw.slice(0, idx), tags: tags };
    }

    function tagsHtml(tags) {
        if (!tags || !tags.length) return '';
        return '<div class="oc-bubble-tags mt-1">' + tags.map((t) =>
            '<span class="badge oc-tag-chip border me-1"><i class="bi bi-tag me-1"></i>' +
            esc(t.label || t.ref) + '</span>').join('') + '</div>';
    }

    async function openThread(opts) {
        opts = opts || {};
        if (!opts.folder || !opts.version) {
            notify('Cannot open discussion: missing domain/version', 'error');
            return;
        }
        build();
        // Reset AI-Agent live-status state for the new context.
        stopPanelPolling();
        agentRuns = [];
        aiTasksByComment = {};
        aiStatusSnapshot = {};
        aiSnapshotReady = false;
        lastListSig = '';
        renderAgentStrip();
        // Discussions are domain-wide: every entry point opens the single
        // Domain thread (no per-anchor separation, no entity/relationship tags).
        // The caller's anchor hints (type/ref/label/taggable) are ignored.
        ctx = {
            folder: opts.folder,
            version: opts.version,
        };
        renderAnchorBadge();

        if (window.bootstrap) {
            offcanvas = bootstrap.Offcanvas.getOrCreateInstance(el);
            offcanvas.show();
            // Lift the shared backdrop above the navbar (z-index:1050) so
            // the whole screen dims behind the panel (see review-modals.css).
            setTimeout(() => {
                document.querySelectorAll('.offcanvas-backdrop.show')
                    .forEach((b) => b.classList.add('oc-comments-backdrop'));
            }, 0);
        }
        await reload();
        loadMembers();
        loadCurrentUser();
        ensureAgentTracking();
    }

    async function reload() {
        const list = el.querySelector('[data-oc-list]');
        list.innerHTML =
            '<div class="text-center text-muted small py-4">' +
            '<span class="spinner-border spinner-border-sm me-1"></span> Loading...</div>';
        const url = '/comments/' + encodeURIComponent(ctx.folder) + '/' +
            encodeURIComponent(ctx.version);
        try {
            const resp = await fetch(url, { credentials: 'same-origin' });
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                list.innerHTML = '<div class="alert alert-danger small mb-0">' +
                    esc(data.message || 'Failed to load comments') + '</div>';
                return;
            }
            await loadAiTasks();
            const comments = data.comments || [];
            renderList(list, comments);
            lastListSig = listSignature(comments);
        } catch (err) {
            list.innerHTML = '<div class="alert alert-danger small mb-0">Network error: ' +
                esc(String(err)) + '</div>';
        }
    }

    // Resolve the signed-in user once (for the "Assign to me" shortcut).
    function loadCurrentUser() {
        if (currentUserPromise) return currentUserPromise;
        currentUserPromise = fetch('/domain/current-user', { credentials: 'same-origin' })
            .then((r) => r.json())
            .then((d) => {
                currentUser = (d && d.success && d.email) ? d.email : null;
                return currentUser;
            })
            .catch(() => { currentUser = null; return null; });
        return currentUserPromise;
    }

    async function loadMembers() {
        const key = ctx.folder + '/' + ctx.version;
        if (membersCache[key]) return;
        try {
            const resp = await fetch(
                '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                encodeURIComponent(ctx.version) + '/assignees',
                { credentials: 'same-origin' }
            );
            const data = await resp.json();
            membersCache[key] = (resp.ok && data.success && data.members)
                ? data.members : [];
        } catch (err) {
            membersCache[key] = [];
        }
    }

    // ---- AI-Agent live status ----------------------------------------------
    // An AI-Agent task runs asynchronously: a short "working" phase
    // (route -> plan -> run) then it parks in_progress, waiting for the
    // author's reply. We surface both states — a progress strip at the top of
    // the panel and a per-thread chip — and poll while the panel is open so
    // the agent's questions and outcomes appear without a manual refresh.

    // Load this version's AI-Agent tasks, keyed by their thread-root comment.
    async function loadAiTasks() {
        aiTasksByComment = {};
        try {
            const resp = await fetch(
                '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                encodeURIComponent(ctx.version) + '/tasks',
                { credentials: 'same-origin' }
            );
            const data = await resp.json();
            if (!resp.ok || !data.success) return;
            (data.tasks || []).forEach((t) => {
                if ((t.assignee || '').toLowerCase() !== AI_AGENT_PRINCIPAL) return;
                const cid = t.comment_id || '';
                if (cid) aiTasksByComment[cid] = t;
            });
            announceAgentCompletions();
        } catch (err) { /* best-effort: chips just won't show */ }
    }

    // Fire a global "design updated" event when an AI-Agent task transitions to
    // done, so design-consuming pages (ontology designer, mapping, …) can pull
    // the agent's saved changes and re-render. The first load only records a
    // baseline — we never refresh on initial paint, only on a live transition.
    function announceAgentCompletions() {
        const statuses = {};
        Object.keys(aiTasksByComment).forEach((k) => {
            statuses[k] = aiTasksByComment[k].status;
        });
        if (!aiSnapshotReady) {
            aiStatusSnapshot = statuses;
            aiSnapshotReady = true;
            return;
        }
        const completed = Object.keys(statuses).filter((k) => {
            const prev = aiStatusSnapshot[k];
            return statuses[k] === 'done' && prev && prev !== 'done';
        });
        aiStatusSnapshot = statuses;
        if (completed.length) {
            window.dispatchEvent(new CustomEvent('ontobricks:design-updated', {
                detail: { source: 'agent', commentIds: completed },
            }));
        }
    }

    // Active AI-Agent background runs (the router/plan/run worker).
    async function loadAgentRuns() {
        agentRuns = [];
        try {
            const resp = await fetch('/tasks/', { credentials: 'same-origin' });
            const data = await resp.json();
            if (!data || !data.success) return;
            agentRuns = (data.tasks || []).filter((t) =>
                t.task_type === 'task_router' &&
                (t.status === 'pending' || t.status === 'running'));
        } catch (err) { /* best-effort: strip just stays hidden */ }
    }

    function isAgentWorking() { return agentRuns.length > 0; }

    // Any in-flight AI-Agent work on this version (running OR parked/queued)?
    function hasLiveAgentWork() {
        if (isAgentWorking()) return true;
        return Object.keys(aiTasksByComment).some((k) => {
            const s = aiTasksByComment[k].status;
            return s === 'in_progress' || s === 'open';
        });
    }

    // Render the top progress strip from the active background run(s).
    function renderAgentStrip() {
        const strip = el && el.querySelector('[data-oc-agent-strip]');
        if (!strip) return;
        if (!agentRuns.length) {
            strip.classList.add('d-none');
            strip.innerHTML = '';
            return;
        }
        const run = agentRuns[0];
        const pct = Math.max(3, Math.min(100, Number(run.progress) || 0));
        let step = run.message || '';
        if (run.steps && run.steps.length && run.current_step < run.steps.length) {
            step = run.steps[run.current_step].description || step;
        }
        const extra = agentRuns.length > 1
            ? ' <span class="text-muted">(+' + (agentRuns.length - 1) + ' more)</span>'
            : '';
        strip.classList.remove('d-none');
        strip.innerHTML =
            '<div class="d-flex align-items-center gap-2 mb-1">' +
            '<span class="oc-agent-spin"><i class="bi bi-robot"></i></span>' +
            '<span class="small fw-semibold">' + esc(run.name || 'AI Agent') + '</span>' +
            extra +
            '</div>' +
            '<div class="progress" style="height:4px;">' +
            '<div class="progress-bar progress-bar-striped progress-bar-animated" ' +
            'style="width:' + pct + '%"></div></div>' +
            (step ? '<div class="small text-muted mt-1">' + esc(step) + '</div>' : '');
    }

    // Refresh the AI strip and (re)start polling whenever there is live work.
    async function ensureAgentTracking() {
        await loadAgentRuns();
        renderAgentStrip();
        if (hasLiveAgentWork() && !panelPollTimer) startPanelPolling();
    }

    function startPanelPolling() {
        stopPanelPolling();
        panelPollTimer = setInterval(panelPollTick, 4000);
    }

    function stopPanelPolling() {
        if (panelPollTimer) { clearInterval(panelPollTimer); panelPollTimer = null; }
    }

    async function panelPollTick() {
        if (!el || !ctx) { stopPanelPolling(); return; }
        await loadAgentRuns();
        await loadAiTasks();
        renderAgentStrip();
        // Re-render the thread list only when the comment set changed, and
        // never while the user is mid-reply (don't clobber an open answer box).
        if (!userIsComposing()) {
            const list = el.querySelector('[data-oc-list]');
            try {
                const url = '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                    encodeURIComponent(ctx.version);
                const resp = await fetch(url, { credentials: 'same-origin' });
                const data = await resp.json();
                if (resp.ok && data.success) {
                    const comments = data.comments || [];
                    const sig = listSignature(comments);
                    if (sig !== lastListSig) {
                        renderList(list, comments);
                        lastListSig = sig;
                    }
                }
            } catch (err) { /* keep the last render on a transient error */ }
        }
        // Once everything is idle, stop polling to avoid needless traffic.
        if (!hasLiveAgentWork()) stopPanelPolling();
    }

    // True while the user is actively typing a reply/answer somewhere in the
    // panel — used to defer disruptive list re-renders during polling.
    function userIsComposing() {
        if (!el) return false;
        const active = document.activeElement;
        if (active && el.contains(active) && active.tagName === 'TEXTAREA') return true;
        return Array.from(el.querySelectorAll('textarea'))
            .some((t) => (t.value || '').trim().length > 0);
    }

    // Cheap change-detector for the rendered comment set + AI task statuses,
    // so polling only re-renders when something actually changed.
    function listSignature(comments) {
        const base = comments.map((c) => c.id + ':' + (c.created_at || '')).join('|');
        const ai = Object.keys(aiTasksByComment).sort()
            .map((k) => k + '=' + aiTasksByComment[k].status).join('|');
        return comments.length + '#' + base + '#' + (isAgentWorking() ? 'W' : '') + '#' + ai;
    }

    // Status chip shown atop an AI-Agent task thread.
    function agentChipHtml(t) {
        let cls = 'oc-agent-chip';
        let icon = 'bi-robot';
        let label = 'AI Agent';
        if (t.status === 'done') {
            cls += ' oc-agent-done'; icon = 'bi-check-circle-fill'; label = 'AI Agent · done';
        } else if (t.status === 'cancelled') {
            cls += ' oc-agent-done'; icon = 'bi-slash-circle'; label = 'AI Agent · cancelled';
        } else if (t.status === 'in_progress' && isAgentWorking()) {
            cls += ' oc-agent-working'; icon = 'bi-robot'; label = 'AI Agent · working…';
        } else if (t.status === 'in_progress') {
            cls += ' oc-agent-waiting'; icon = 'bi-hourglass-split';
            label = 'AI Agent · waiting for your reply';
        } else if (t.status === 'open') {
            cls += ' oc-agent-queued'; icon = 'bi-clock'; label = 'AI Agent · queued';
        }
        return '<div class="' + cls + ' mb-2"><i class="bi ' + icon + ' me-1"></i>' +
            esc(label) + '</div>';
    }

    // Prominent, always-visible answer box on a parked AI-Agent thread. A reply
    // here resumes the agent (see CommentService._maybe_resume_agent).
    function agentAnswerHtml(rootId, t) {
        if (!(t.status === 'in_progress' && !isAgentWorking())) return '';
        return '<div class="oc-agent-answer" data-agent-answer="' + escAttr(rootId) + '">' +
            '<div class="small fw-semibold mb-1">' +
            '<i class="bi bi-reply me-1"></i>Answer the AI Agent</div>' +
            '<textarea class="form-control form-control-sm mb-2" rows="2" ' +
            'placeholder="Type your answer to continue…"></textarea>' +
            '<div class="d-flex justify-content-end">' +
            '<button type="button" class="btn btn-sm btn-primary" data-agent-send="' +
            escAttr(rootId) + '"><i class="bi bi-send me-1"></i>Send answer</button>' +
            '</div></div>';
    }

    function renderList(list, comments) {
        if (!comments.length) {
            list.innerHTML =
                '<div class="text-center text-muted py-4">' +
                '<i class="bi bi-chat-square-dots d-block mb-2" style="font-size:1.6rem;"></i>' +
                'No comments yet. Start the discussion.</div>';
            return;
        }
        // Build a parent -> replies map; root comments keep document order.
        const roots = [];
        const replies = {};
        comments.forEach((c) => {
            if (c.parent_id) {
                (replies[c.parent_id] = replies[c.parent_id] || []).push(c);
            } else {
                roots.push(c);
            }
        });
        list.innerHTML = roots.map((r) => threadHtml(r, replies[r.id] || [])).join('');
        bindThreadActions(list);
        list.scrollTop = list.scrollHeight;
    }

    function threadHtml(root, replies) {
        const replyHtml = replies.map((r) => bubble(r, true)).join('');
        const resolvedCls = root.resolved ? ' oc-resolved' : '';
        const aiTask = aiTasksByComment[root.id];
        const aiCls = aiTask ? ' oc-thread-agent' : '';
        return '<div class="oc-thread' + resolvedCls + aiCls + '" data-thread="' + escAttr(root.id) + '">' +
            (aiTask ? agentChipHtml(aiTask) : '') +
            bubble(root, false) +
            '<div class="oc-replies">' + replyHtml + '</div>' +
            (aiTask ? agentAnswerHtml(root.id, aiTask) : '') +
            '<div class="oc-thread-tools">' +
            '<button type="button" class="btn btn-link btn-sm p-0 me-3" data-reply="' + escAttr(root.id) + '">' +
            '<i class="bi bi-reply me-1"></i>Reply</button>' +
            '<button type="button" class="btn btn-link btn-sm p-0 me-3" data-task="' + escAttr(root.id) + '">' +
            '<i class="bi bi-check2-square me-1"></i>Create task</button>' +
            '<button type="button" class="btn btn-link btn-sm p-0 text-muted" data-resolve="' + escAttr(root.id) + '" ' +
            'data-resolved="' + (root.resolved ? '1' : '0') + '">' +
            '<i class="bi bi-' + (root.resolved ? 'arrow-counterclockwise' : 'check-circle') + ' me-1"></i>' +
            (root.resolved ? 'Reopen' : 'Resolve') + '</button>' +
            '</div>' +
            '<div class="oc-reply-box" data-reply-box="' + escAttr(root.id) + '" style="display:none;"></div>' +
            '<div class="oc-task-box" data-task-box="' + escAttr(root.id) + '" style="display:none;"></div>' +
            '</div>';
    }

    function bubble(c, isReply) {
        const actor = c.author || 'unknown';
        const initials = actor.replace(/@.*/, '').slice(0, 2).toUpperCase();
        const parsed = parseBody(c.body);
        return '<div class="oc-bubble' + (isReply ? ' oc-bubble-reply' : '') + '">' +
            '<div class="oc-avatar">' + esc(initials) + '</div>' +
            '<div class="oc-bubble-body">' +
            '<div class="oc-bubble-head">' +
            '<span class="oc-author">' + esc(actor) + '</span>' +
            '<span class="oc-time">' + formatTime(c.created_at) + '</span>' +
            (c.resolved && !isReply ? '<span class="badge bg-success-subtle text-success border ms-2">Resolved</span>' : '') +
            '</div>' +
            '<div class="oc-text oc-md">' + renderMarkdown(parsed.text) + '</div>' +
            tagsHtml(parsed.tags) +
            '</div></div>';
    }

    function bindThreadActions(list) {
        list.querySelectorAll('button[data-reply]').forEach((btn) => {
            btn.addEventListener('click', () => toggleReply(btn.dataset.reply));
        });
        list.querySelectorAll('button[data-resolve]').forEach((btn) => {
            btn.addEventListener('click', () => {
                resolveThread(btn.dataset.resolve, btn.dataset.resolved !== '1');
            });
        });
        list.querySelectorAll('button[data-task]').forEach((btn) => {
            btn.addEventListener('click', () => toggleTask(btn.dataset.task));
        });
        // Answering a parked AI-Agent thread: a reply here resumes the agent.
        list.querySelectorAll('button[data-agent-send]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const box = btn.closest('[data-agent-answer]');
                const ta = box ? box.querySelector('textarea') : null;
                const text = ta ? (ta.value || '').trim() : '';
                postComment(text, btn.dataset.agentSend, ta, box)
                    .then(() => ensureAgentTracking());
            });
        });
    }

    function toggleReply(rootId) {
        const box = el.querySelector('[data-reply-box="' + cssEsc(rootId) + '"]');
        if (!box) return;
        if (box.style.display !== 'none') { box.style.display = 'none'; return; }
        box.style.display = '';
        box.innerHTML =
            '<textarea class="form-control form-control-sm mb-2" rows="2" ' +
            'placeholder="Write a reply..."></textarea>' +
            '<div class="d-flex justify-content-end">' +
            '<button type="button" class="btn btn-sm btn-outline-primary">Reply</button></div>';
        const ta = box.querySelector('textarea');
        box.querySelector('button').addEventListener('click', () => {
            postComment((ta.value || '').trim(), rootId, ta, box);
        });
        ta.focus();
    }

    // Build the inner markup of a task-creation form. Shared by the
    // per-comment task box and the standalone "New task" box in the header.
    function taskFormHtml(heading, withCancel) {
        const members = membersCache[ctx.folder + '/' + ctx.version] || [];
        const opts = members.map((m) => {
            const label = m.principal_type === 'agent'
                ? '\uD83E\uDD16 ' + esc(m.display_name || 'AI Agent') + ' (auto)'
                : esc(m.display_name || m.principal) +
                  (m.principal === currentUser ? ' (me)' : '') +
                  ' (' + esc(m.role) + ')';
            return '<option value="' + escAttr(m.principal) + '">' + label + '</option>';
        }).join('');
        const cancel = withCancel
            ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-tk-cancel>Cancel</button>'
            : '';
        return '<div class="oc-task-form border rounded p-2">' +
            '<div class="small fw-medium mb-2"><i class="bi bi-check2-square me-1"></i>' +
            esc(heading) + '</div>' +
            '<input type="text" class="form-control form-control-sm mb-2" data-tk-title placeholder="Task title">' +
            '<div class="d-flex align-items-center justify-content-between mb-1">' +
            '<label class="form-label small text-muted mb-0">Assignee</label>' +
            '<button type="button" class="btn btn-link btn-sm p-0" data-tk-me>' +
            '<i class="bi bi-person-check me-1"></i>Assign to me</button>' +
            '</div>' +
            '<select class="form-select form-select-sm mb-2" data-tk-assignee>' +
            '<option value="">Assign to...</option>' + opts + '</select>' +
            '<input type="date" class="form-control form-control-sm mb-2" data-tk-due title="Due date (optional)">' +
            '<div class="d-flex justify-content-end gap-2">' + cancel +
            '<button type="button" class="btn btn-sm btn-success" data-tk-create>Create task</button>' +
            '</div></div>';
    }

    function wireTaskForm(box, commentId) {
        box.querySelector('[data-tk-me]').addEventListener('click', () => {
            assignToMe(box);
        });
        box.querySelector('[data-tk-create]').addEventListener('click', () => {
            createTask(commentId, box);
        });
        const cancel = box.querySelector('[data-tk-cancel]');
        if (cancel) cancel.addEventListener('click', () => hideTaskBox(box));
        const sel = box.querySelector('[data-tk-assignee]');
        if (sel) sel.addEventListener('change', () => syncDueVisibility(box));
        syncDueVisibility(box);
    }

    // The AI Agent runs the task immediately, so a due date is meaningless —
    // hide (and clear) it whenever the AI Agent is the selected assignee.
    function syncDueVisibility(box) {
        const sel = box.querySelector('[data-tk-assignee]');
        const due = box.querySelector('[data-tk-due]');
        if (!sel || !due) return;
        const isAgent = sel.value === AI_AGENT_PRINCIPAL;
        due.classList.toggle('d-none', isAgent);
        if (isAgent) due.value = '';
    }

    function hideTaskBox(box) {
        box.innerHTML = '';
        box.style.display = 'none';
        box.classList.add('d-none');
    }

    function toggleTask(rootId) {
        const box = el.querySelector('[data-task-box="' + cssEsc(rootId) + '"]');
        if (!box) return;
        if (box.style.display !== 'none') { box.style.display = 'none'; return; }
        box.classList.remove('d-none');
        box.style.display = '';
        box.innerHTML = taskFormHtml('New task from this comment', false);
        wireTaskForm(box, rootId);
    }

    // Standalone task creation (not tied to a comment), opened from the
    // panel header. Lets the user assign a task to a teammate or the AI Agent.
    async function openNewTask() {
        const box = el.querySelector('[data-oc-newtask]');
        if (!box) return;
        if (!box.classList.contains('d-none')) { hideTaskBox(box); return; }
        await loadMembers();
        box.classList.remove('d-none');
        box.style.display = '';
        box.innerHTML = taskFormHtml('New task', true);
        wireTaskForm(box, null);
    }

    // Select the current user in the assignee picker, adding an option for
    // them when they are not already in the roster.
    async function assignToMe(box) {
        const me = await loadCurrentUser();
        if (!me) { notify('Could not determine the current user', 'warning'); return; }
        const sel = box.querySelector('[data-tk-assignee]');
        if (!sel) return;
        const exists = Array.from(sel.options).some((o) => o.value === me);
        if (!exists) {
            const opt = document.createElement('option');
            opt.value = me;
            opt.textContent = me + ' (me)';
            sel.appendChild(opt);
        }
        sel.value = me;
        syncDueVisibility(box);
    }

    async function postComment(body, parentId, ta, scope) {
        if (!body) { notify('Write something first', 'warning'); return; }
        try {
            const resp = await fetch(
                '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                encodeURIComponent(ctx.version),
                {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        body: body,
                        parent_id: parentId || null,
                    }),
                }
            );
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                notify(data.message || 'Failed to post comment', 'error');
                return;
            }
            if (ta) ta.value = '';
            await reload();
            ensureAgentTracking();
        } catch (err) {
            notify('Error: ' + err.message, 'error');
        }
    }

    async function resolveThread(rootId, resolved) {
        try {
            const resp = await fetch(
                '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                encodeURIComponent(ctx.version) + '/' +
                encodeURIComponent(rootId) + '/resolve',
                {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ resolved: resolved }),
                }
            );
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                notify(data.message || 'Failed to update comment', 'error');
                return;
            }
            await reload();
        } catch (err) {
            notify('Error: ' + err.message, 'error');
        }
    }

    async function createTask(commentId, box) {
        const title = (box.querySelector('[data-tk-title]').value || '').trim();
        const assignee = box.querySelector('[data-tk-assignee]').value || '';
        const due = box.querySelector('[data-tk-due]').value || '';
        if (!title) { notify('Task title is required', 'warning'); return; }
        if (!assignee) { notify('Pick an assignee', 'warning'); return; }
        try {
            const resp = await fetch(
                '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                encodeURIComponent(ctx.version) + '/tasks',
                {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        assignee: assignee,
                        title: title,
                        due_date: due || null,
                        comment_id: commentId,
                    }),
                }
            );
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                notify(data.message || 'Failed to create task', 'error');
                return;
            }
            if (data.agent_task_id) {
                notify('AI Agent started — routing your task to the right agent', 'success');
                if (typeof window.refreshTasks === 'function') { window.refreshTasks(); }
            } else {
                notify('Task assigned to ' + assignee, 'success');
            }
            hideTaskBox(box);
            await reload();
            if (data.agent_task_id) ensureAgentTracking();
        } catch (err) {
            notify('Error: ' + err.message, 'error');
        }
    }

    function formatTime(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        return esc(d.toLocaleString());
    }

    function cssEsc(s) {
        if (window.CSS && CSS.escape) return CSS.escape(s);
        return String(s).replace(/"/g, '\\"');
    }

    // Resolve the loaded domain folder + version once, then cache. Used by
    // the editor surfaces (ontology / mapping / graph) which always operate
    // on the loaded session domain, so they don't carry folder/version.
    let _ctxPromise = null;
    function resolveDomainContext() {
        if (_ctxPromise) return _ctxPromise;
        _ctxPromise = fetch('/domain/version-status', { credentials: 'same-origin' })
            .then((r) => r.json())
            .then((vs) => ({
                folder: vs.domain_folder || '',
                version: vs.version || '',
                hasRegistry: !!vs.has_registry,
            }))
            .catch(() => ({ folder: '', version: '', hasRegistry: false }));
        return _ctxPromise;
    }

    /**
     * Build the comment tag vocabulary ({type, ref, label}[]) from an
     * ontology config ({ classes, properties }). Shared by every surface
     * (ontology designer, mapping, digital twin) so the entity/relationship
     * tag picker is built identically everywhere.
     */
    function taggableFromOntology(config) {
        const cfg = config || {};
        const out = [];
        (cfg.classes || []).forEach((c) => out.push({
            type: 'ontology_class',
            ref: c.uri || c.name,
            label: (c.emoji || '🔷') + ' ' + (c.name || c.uri),
        }));
        (cfg.properties || []).forEach((p) => out.push({
            type: 'ontology_property',
            ref: p.uri || p.name,
            label: '🔗 ' + (p.name || p.uri),
        }));
        return out;
    }

    /**
     * Open the domain discussion from any editor surface (ontology / mapping
     * / graph), auto-resolving the loaded domain + version. Legacy anchor
     * arguments are accepted for backward compatibility but ignored —
     * discussions are domain-wide.
     */
    async function openForSelection() {
        const dc = await resolveDomainContext();
        if (!dc.folder || !dc.hasRegistry) {
            notify('Save this domain to the registry to start a discussion.',
                'warning');
            return;
        }
        openThread({ folder: dc.folder, version: dc.version });
    }

    window.OntoComments = {
        openThread: openThread,
        openForSelection: openForSelection,
        taggableFromOntology: taggableFromOntology,
        // Split a stored comment body into { text, tags } (strips the
        // internal tag marker). Shared with the Domain → Discussions timeline.
        parseBody: parseBody,
    };
})();
