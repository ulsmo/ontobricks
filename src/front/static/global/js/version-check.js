// =====================================================
// VERSION STATUS CHECK
//
// On every page load we ask the backend whether the loaded domain is
// the active version. If it is not, we:
//   1. annotate the navbar role pill (rendered by ``permissions.js``)
//      with an "older version, read-only" note via
//      ``window.OB.annotateRoleNavBadge``,
//   2. add ``read-only-version`` to <body> — every gate in
//      permissions.css (form fields, write buttons, OntoViz controls,
//      …) keys off ``:is(.read-only-version, .role-viewer)``, so we
//      no longer need a per-button JS sweep,
//   3. install the shared capture-phase contextmenu blocker for
//      D3/Canvas design surfaces (defined once in ``permissions.js``
//      and reused here for older-version readers).
//
// Domain-role gating (viewer vs editor vs builder) is also done
// declaratively now — viewers get ``body.role-viewer`` from
// ``permissions.js`` synchronously at parse time, no round-trip
// required.
// =====================================================

window.isActiveVersion = true;
// Lifecycle status of the loaded version. Editing is only allowed while
// the status is DRAFT; IN-REVIEW / PUBLISHED versions are read-only.
// Defaults to 'DRAFT' so the UI stays editable until the async check runs.
window.versionStatus = 'DRAFT';

// Lifecycle status → Bootstrap badge classes + label (shared map).
const VERSION_STATUS_BADGE = {
    'DRAFT': { cls: 'bg-warning-subtle text-dark border-warning', icon: 'pencil', label: 'Draft' },
    'IN-REVIEW': { cls: 'bg-info-subtle text-dark border-info', icon: 'eye', label: 'In Review' },
    'PUBLISHED': { cls: 'bg-success-subtle text-dark border-success', icon: 'broadcast', label: 'Published' }
};

// Populate every ``.js-version-status-badge`` placeholder with the current
// lifecycle status. Lets any Jinja template surface the badge by simply
// dropping ``<span class="js-version-status-badge"></span>`` next to a
// ``v{{ current_version }}`` header. Status is a controlled enum (safe).
function renderVersionStatusBadges(status) {
    const cfg = VERSION_STATUS_BADGE[String(status || 'DRAFT').toUpperCase()]
        || VERSION_STATUS_BADGE['DRAFT'];
    document.querySelectorAll('.js-version-status-badge').forEach(el => {
        el.className = 'js-version-status-badge badge border ' + cfg.cls;
        el.style.fontSize = el.style.fontSize || '0.65rem';
        el.innerHTML = '<i class="bi bi-' + cfg.icon + ' me-1"></i>' + cfg.label;
        el.title = 'Lifecycle status: ' + cfg.label;
    });
}
window.OB = window.OB || {};
window.OB.renderVersionStatusBadges = renderVersionStatusBadges;

async function checkVersionStatus() {
    try {
        const data = await fetchOnce('/domain/version-status');
        if (data.success) {
            window.isActiveVersion = data.is_active;
            window.versionStatus = data.status || 'DRAFT';
            renderVersionStatusBadges(window.versionStatus);

            const lockedByStatus = window.versionStatus !== 'DRAFT';
            const readOnly = !data.is_active || lockedByStatus;

            if (lockedByStatus) {
                document.body.classList.add('read-only-status');
            }

            if (readOnly) {
                // Reuse the existing read-only gating (every selector in
                // permissions.css / ontoviz.css keys off
                // ``read-only-version``) so a locked status disables the
                // same write surfaces with no per-rule duplication.
                document.body.classList.add('read-only-version');
                if (window.OB && typeof window.OB.annotateRoleNavBadge === 'function') {
                    const msg = lockedByStatus
                        ? 'This version is ' + window.versionStatus
                          + ' (read-only). Set it back to DRAFT to make changes.'
                        : 'You are viewing an older version of this domain. '
                          + 'Load the latest version to make changes.';
                    window.OB.annotateRoleNavBadge(msg);
                }
                if (window.OB && typeof window.OB.installReadOnlyContextMenuBlocker === 'function') {
                    window.OB.installReadOnlyContextMenuBlocker();
                }
            }
        }
    } catch (e) {
        console.log('Could not fetch version status');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    renderVersionStatusBadges(window.versionStatus);
    checkVersionStatus();
});
