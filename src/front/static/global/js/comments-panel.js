/**
 * Collaborative comments & tasks — anchored thread panel (global)
 *
 * A reusable right-side offcanvas that opens a contextual, threaded
 * discussion bound to a canonical *anchor* — an ontology class/property,
 * a mapping, a graph node/edge, or the whole domain. Any surface can open
 * it through the global API:
 *
 *   OntoComments.openThread({
 *       folder, version,
 *       anchorType,            // ontology_class|ontology_property|mapping|
 *                              // graph_node|graph_edge|domain
 *       anchorRef,             // canonical id (prefer full URIs)
 *       anchorLabel,           // human label for the header (optional)
 *   });
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

    const ANCHOR_LABELS = {
        ontology_class: 'Class',
        ontology_property: 'Property',
        mapping: 'Mapping',
        graph_node: 'Node',
        graph_edge: 'Edge',
        domain: 'Domain',
    };

    let el = null;
    let offcanvas = null;
    let ctx = null;          // { folder, version, anchorType, anchorRef, anchorLabel }
    let membersCache = {};   // key folder/version -> [members]
    let currentUser = null;  // current user's email/principal (for "Assign to me")
    let currentUserPromise = null;

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
            '<button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button>' +
            '</div>' +
            '<div class="offcanvas-body d-flex flex-column p-0">' +
            '<div class="oc-comments-list flex-grow-1 p-3" data-oc-list></div>' +
            '<div class="oc-comments-compose border-top p-3" data-oc-compose>' +
            '<textarea class="form-control form-control-sm mb-2" rows="2" ' +
            'data-oc-input placeholder="Write a comment..."></textarea>' +
            tagWidgetHtml() +
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
    }

    // Render the anchor badge in the header for the active scope.
    function renderAnchorBadge() {
        const kind = ANCHOR_LABELS[ctx.anchorType] || 'Item';
        const label = ctx.anchorLabel || ctx.anchorRef ||
            ctx.folder + ' v' + ctx.version;
        el.querySelector('[data-oc-anchor]').innerHTML =
            '<span class="badge bg-secondary-subtle text-dark border me-1">' +
            esc(kind) + '</span>' + esc(label);
    }

    // ---- Tags ---------------------------------------------------------------
    // When a surface supplies ctx.taggable (a list of entities/relationships),
    // a compose box gets a tag picker. Selected tags are embedded in the
    // comment body via a trailing marker so no backend change is needed; they
    // render as chips on each comment.
    const TAG_MARK = '\n\n[[onto-tags]]';

    function tagWidgetHtml() {
        return '' +
            '<div class="oc-tagbar mb-2 d-none" data-oc-tagbar>' +
            '<div class="oc-tag-chips d-flex flex-wrap gap-1" data-oc-tag-chips></div>' +
            '<select class="form-select form-select-sm mt-1" data-oc-tag-select>' +
            '</select>' +
            '</div>';
    }

    function tagSelectOptions() {
        const classes = ctx.taggable.filter((t) => t.type === 'ontology_class');
        const props = ctx.taggable.filter((t) => t.type !== 'ontology_class');
        let html = '<option value="">+ Tag an entity / relationship…</option>';
        if (classes.length) {
            html += '<optgroup label="Entities">' + classes.map((t) =>
                '<option value="' + escAttr(t.ref) + '" data-type="' + escAttr(t.type) +
                '" data-label="' + escAttr(t.label) + '">' + esc(t.label) +
                '</option>').join('') + '</optgroup>';
        }
        if (props.length) {
            html += '<optgroup label="Relationships">' + props.map((t) =>
                '<option value="' + escAttr(t.ref) + '" data-type="' + escAttr(t.type) +
                '" data-label="' + escAttr(t.label) + '">' + esc(t.label) +
                '</option>').join('') + '</optgroup>';
        }
        return html;
    }

    // Activate (show + wire) the tag picker inside a compose/reply scope.
    function setupTagbar(scope) {
        if (!scope || !ctx.taggable || !ctx.taggable.length) return;
        const bar = scope.querySelector('[data-oc-tagbar]');
        if (!bar) return;
        bar.classList.remove('d-none');
        const sel = bar.querySelector('[data-oc-tag-select]');
        sel.innerHTML = tagSelectOptions();
        sel.onchange = () => {
            const opt = sel.options[sel.selectedIndex];
            if (opt && opt.value) {
                addTagChip(bar.querySelector('[data-oc-tag-chips]'), {
                    ref: opt.value,
                    type: opt.getAttribute('data-type'),
                    label: opt.getAttribute('data-label'),
                });
            }
            sel.value = '';
        };
    }

    function addTagChip(chipsEl, tag) {
        if (!chipsEl || !tag || !tag.ref) return;
        if (chipsEl.querySelector('[data-ref="' + cssEsc(tag.ref) + '"]')) return;
        const chip = document.createElement('span');
        chip.className = 'badge oc-tag-chip border d-inline-flex align-items-center';
        chip.setAttribute('data-ref', tag.ref);
        chip.setAttribute('data-type', tag.type || '');
        chip.setAttribute('data-label', tag.label || tag.ref);
        chip.innerHTML = '<i class="bi bi-tag me-1"></i>' + esc(tag.label || tag.ref) +
            '<button type="button" class="btn-close btn-close-sm ms-1" ' +
            'aria-label="Remove tag"></button>';
        chip.querySelector('button').addEventListener('click', () => chip.remove());
        chipsEl.appendChild(chip);
    }

    function collectTags(scope) {
        if (!scope) return [];
        return Array.from(scope.querySelectorAll('[data-oc-tag-chips] [data-ref]'))
            .map((c) => ({
                ref: c.getAttribute('data-ref'),
                type: c.getAttribute('data-type'),
                label: c.getAttribute('data-label'),
            }));
    }

    function encodeBody(text, tags) {
        return (tags && tags.length) ? text + TAG_MARK + JSON.stringify(tags) : text;
    }

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
        // Optional tag vocabulary: a list of {type, ref, label} entities and
        // relationships the author can attach to individual comments.
        const taggable = Array.isArray(opts.taggable)
            ? opts.taggable.map((t) => ({
                type: t.type || t.anchorType || 'ontology_class',
                ref: t.ref || t.anchorRef || '',
                label: t.label || t.anchorLabel || t.ref || '',
            })).filter((t) => t.ref)
            : [];
        ctx = {
            folder: opts.folder,
            version: opts.version,
            anchorType: opts.anchorType || 'domain',
            anchorRef: opts.anchorRef || '',
            anchorLabel: opts.anchorLabel || '',
            taggable: taggable,
        };
        renderAnchorBadge();
        // Reset + (re)activate the main compose tag picker for this context.
        const compose = el.querySelector('[data-oc-compose]');
        compose.querySelector('[data-oc-tag-chips]').innerHTML = '';
        const tagbar = compose.querySelector('[data-oc-tagbar]');
        if (taggable.length) {
            setupTagbar(compose);
        } else {
            tagbar.classList.add('d-none');
        }

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
    }

    async function reload() {
        const list = el.querySelector('[data-oc-list]');
        list.innerHTML =
            '<div class="text-center text-muted small py-4">' +
            '<span class="spinner-border spinner-border-sm me-1"></span> Loading...</div>';
        const url = '/comments/' + encodeURIComponent(ctx.folder) + '/' +
            encodeURIComponent(ctx.version) +
            '?anchor_type=' + encodeURIComponent(ctx.anchorType) +
            '&anchor_ref=' + encodeURIComponent(ctx.anchorRef);
        try {
            const resp = await fetch(url, { credentials: 'same-origin' });
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                list.innerHTML = '<div class="alert alert-danger small mb-0">' +
                    esc(data.message || 'Failed to load comments') + '</div>';
                return;
            }
            renderList(list, data.comments || []);
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
        return '<div class="oc-thread' + resolvedCls + '" data-thread="' + escAttr(root.id) + '">' +
            bubble(root, false) +
            '<div class="oc-replies">' + replyHtml + '</div>' +
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
            '<div class="oc-text">' + esc(parsed.text) + '</div>' +
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
    }

    function toggleReply(rootId) {
        const box = el.querySelector('[data-reply-box="' + cssEsc(rootId) + '"]');
        if (!box) return;
        if (box.style.display !== 'none') { box.style.display = 'none'; return; }
        box.style.display = '';
        box.innerHTML =
            '<textarea class="form-control form-control-sm mb-2" rows="2" ' +
            'placeholder="Write a reply..."></textarea>' +
            tagWidgetHtml() +
            '<div class="d-flex justify-content-end">' +
            '<button type="button" class="btn btn-sm btn-outline-primary">Reply</button></div>';
        setupTagbar(box);
        const ta = box.querySelector('textarea');
        box.querySelector('button').addEventListener('click', () => {
            postComment((ta.value || '').trim(), rootId, ta, box);
        });
        ta.focus();
    }

    function toggleTask(rootId) {
        const box = el.querySelector('[data-task-box="' + cssEsc(rootId) + '"]');
        if (!box) return;
        if (box.style.display !== 'none') { box.style.display = 'none'; return; }
        box.style.display = '';
        const members = membersCache[ctx.folder + '/' + ctx.version] || [];
        const opts = members.map((m) =>
            '<option value="' + escAttr(m.principal) + '">' +
            esc(m.display_name || m.principal) +
            (m.principal === currentUser ? ' (me)' : '') +
            ' (' + esc(m.role) + ')</option>'
        ).join('');
        box.innerHTML =
            '<div class="oc-task-form border rounded p-2">' +
            '<div class="small fw-medium mb-2"><i class="bi bi-check2-square me-1"></i>New task from this comment</div>' +
            '<input type="text" class="form-control form-control-sm mb-2" data-tk-title placeholder="Task title">' +
            '<div class="d-flex align-items-center justify-content-between mb-1">' +
            '<label class="form-label small text-muted mb-0">Assignee</label>' +
            '<button type="button" class="btn btn-link btn-sm p-0" data-tk-me>' +
            '<i class="bi bi-person-check me-1"></i>Assign to me</button>' +
            '</div>' +
            '<select class="form-select form-select-sm mb-2" data-tk-assignee>' +
            '<option value="">Assign to...</option>' + opts + '</select>' +
            '<input type="date" class="form-control form-control-sm mb-2" data-tk-due title="Due date (optional)">' +
            '<div class="d-flex justify-content-end">' +
            '<button type="button" class="btn btn-sm btn-success" data-tk-create>Create task</button>' +
            '</div></div>';
        box.querySelector('[data-tk-me]').addEventListener('click', () => {
            assignToMe(box);
        });
        box.querySelector('[data-tk-create]').addEventListener('click', () => {
            createTask(rootId, box);
        });
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
    }

    async function postComment(body, parentId, ta, scope) {
        if (!body) { notify('Write something first', 'warning'); return; }
        const tags = collectTags(scope);
        try {
            const resp = await fetch(
                '/comments/' + encodeURIComponent(ctx.folder) + '/' +
                encodeURIComponent(ctx.version),
                {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        anchor_type: ctx.anchorType,
                        anchor_ref: ctx.anchorRef,
                        body: encodeBody(body, tags),
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
            const chips = scope && scope.querySelector('[data-oc-tag-chips]');
            if (chips) chips.innerHTML = '';
            await reload();
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
            notify('Task assigned to ' + assignee, 'success');
            box.style.display = 'none';
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
     * Open a thread for a selection on an editor surface, auto-resolving
     * the loaded domain + version. Use from ontology / mapping / graph.
     * `taggable` (optional) is a vocabulary of {type, ref, label} entities
     * and relationships the author can attach to individual comments via the
     * compose-box tag picker.
     */
    async function openForSelection(anchorType, anchorRef, anchorLabel, taggable) {
        const dc = await resolveDomainContext();
        if (!dc.folder || !dc.hasRegistry) {
            notify('Save this domain to the registry to start a discussion.',
                'warning');
            return;
        }
        openThread({
            folder: dc.folder,
            version: dc.version,
            anchorType: anchorType,
            anchorRef: anchorRef || '',
            anchorLabel: anchorLabel || anchorRef || '',
            taggable: taggable || null,
        });
    }

    window.OntoComments = {
        openThread: openThread,
        openForSelection: openForSelection,
        taggableFromOntology: taggableFromOntology,
        // Split a stored comment body into { text, tags } (strips the
        // internal tag marker). Shared with the Domain → Discussions timeline.
        parseBody: parseBody,
        // Human label for an anchor type (Class / Property / Mapping / …).
        anchorLabel: function (type) { return ANCHOR_LABELS[type] || 'Item'; },
    };
})();
