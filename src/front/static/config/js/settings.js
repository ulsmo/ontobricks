/**
 * OntoBricks - settings.js
 * Settings page JavaScript – tabbed layout; global Save persists all sections including Graph DB
 */

document.addEventListener('DOMContentLoaded', function () {

    let currentWarehouseId = null;
    let warehouseLocked = false;

    function escapeHtmlSettings(str) { return escapeHtml(str); }

    loadCurrentConfig();
    loadBaseUri();
    loadCurrentDefaultEmoji();

    loadRegistryCacheTtl();
    loadNavbarLogo();

    // =====================================================================
    //  DATABRICKS TAB
    // =====================================================================

    async function loadCurrentConfig() {
        try {
            const response = await fetch('/settings/current', { credentials: 'same-origin' });
            const data = await response.json();

            const tokenBadge = document.getElementById('tokenBadge');
            const authModeDisplay = document.getElementById('authModeDisplay');

            if (data.auth_mode === 'oauth') {
                tokenBadge.className = 'badge bg-success';
                tokenBadge.innerHTML = '<i class="bi bi-shield-check"></i> OAuth configured';
                authModeDisplay.textContent = data.token || '';
                document.getElementById('tokenHelp').textContent = 'Using OAuth Service Principal (Databricks Apps mode)';
            } else if ((data.auth_mode === 'token' || data.auth_mode === 'pat') && data.token) {
                tokenBadge.className = 'badge bg-success';
                tokenBadge.innerHTML = '<i class="bi bi-check-circle"></i> Token configured';
                authModeDisplay.textContent = '';
                document.getElementById('tokenHelp').textContent = data.from_env ? 'From environment variable' : 'From session';
            } else if (data.auth_mode === 'app') {
                tokenBadge.className = 'badge bg-success';
                tokenBadge.innerHTML = '<i class="bi bi-cloud-check"></i> Databricks App';
                authModeDisplay.textContent = '';
                document.getElementById('tokenHelp').textContent = 'Using Databricks Apps authentication';
            } else {
                tokenBadge.className = 'badge bg-danger';
                tokenBadge.innerHTML = '<i class="bi bi-x-circle"></i> Not configured';
                authModeDisplay.textContent = '';
                document.getElementById('tokenHelp').innerHTML = '<i class="bi bi-exclamation-triangle text-warning"></i> Set DATABRICKS_TOKEN or use Databricks Apps';
            }

            currentWarehouseId = data.warehouse_id;
            warehouseLocked = !!data.warehouse_locked;

            if (warehouseLocked) {
                const whSelect = document.getElementById('settingsWarehouseSelect');
                if (whSelect) {
                    whSelect.innerHTML = '<option value="' + escapeHtmlSettings(data.warehouse_id || '') + '" selected>'
                        + escapeHtmlSettings(data.warehouse_id || '(not set)') + '</option>';
                    whSelect.disabled = true;
                }
                const btnRefresh = document.getElementById('btnRefreshWarehouses');
                if (btnRefresh) btnRefresh.disabled = true;
                const whHelp = document.getElementById('warehouseHelp');
                if (whHelp) whHelp.innerHTML = '<i class="bi bi-lock-fill text-muted me-1"></i> Configured via Databricks App resource';
            } else {
                await loadWarehouseSelect(data.warehouse_id);
            }

            const hostDisplay = document.getElementById('currentHostDisplay');
            if (data.host) {
                hostDisplay.innerHTML = '<i class="bi bi-cloud text-success"></i> ' + escapeHtmlSettings(data.host);
            } else {
                hostDisplay.innerHTML = '<i class="bi bi-exclamation-circle text-warning"></i> Not configured';
            }

            if (data.from_env) {
                document.getElementById('envNotice').style.display = 'block';
            }
        } catch (error) {
            console.error('Error loading config:', error);
        }
    }

    async function loadWarehouseSelect(preselectId) {
        const select = document.getElementById('settingsWarehouseSelect');
        if (!select) return;

        try {
            const response = await fetch('/settings/warehouses', { credentials: 'same-origin' });
            const data = await response.json();

            select.innerHTML = '<option value="">-- Select a SQL Warehouse --</option>';

            if (data.warehouses && data.warehouses.length > 0) {
                data.warehouses.forEach(wh => {
                    const stateLabel = wh.state === 'RUNNING' ? ' (running)' : '';
                    const opt = document.createElement('option');
                    opt.value = wh.id;
                    opt.textContent = wh.name + stateLabel;
                    select.appendChild(opt);
                });
            } else if (data.error) {
                select.innerHTML = '<option value="">Error: ' + escapeHtmlSettings(data.error) + '</option>';
            } else {
                select.innerHTML = '<option value="">No warehouses available</option>';
            }

            if (preselectId) {
                select.value = preselectId;
            }
        } catch (error) {
            console.error('Error loading warehouses:', error);
            select.innerHTML = '<option value="">Error loading warehouses</option>';
        }
    }

    document.getElementById('btnRefreshWarehouses')?.addEventListener('click', () => loadWarehouseSelect(currentWarehouseId));

    document.getElementById('btnTestConnection')?.addEventListener('click', async function () {
        const whId = document.getElementById('settingsWarehouseSelect').value || currentWarehouseId;
        const resultDiv = document.getElementById('connectionResult');

        if (!whId) {
            showNotification('Please select a SQL Warehouse first', 'warning');
            return;
        }

        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '<div class="alert alert-info"><i class="bi bi-hourglass-split"></i> Testing connection...</div>';

        try {
            const response = await fetch('/settings/test-connection', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ warehouse_id: whId })
            });
            const result = await response.json();

            if (result.success) {
                resultDiv.innerHTML = `<div class="alert alert-success"><i class="bi bi-check-circle"></i> ${result.message}</div>`;
            } else {
                resultDiv.innerHTML = `<div class="alert alert-danger"><i class="bi bi-x-circle"></i> ${result.message}</div>`;
            }
        } catch (error) {
            resultDiv.innerHTML = `<div class="alert alert-danger"><i class="bi bi-x-circle"></i> Error: ${error.message}</div>`;
        }
    });

    // =====================================================================
    //  GLOBAL TAB – Base URI
    // =====================================================================

    async function loadBaseUri() {
        try {
            const response = await fetch('/settings/get-base-uri', { credentials: 'same-origin' });
            const result = await response.json();
            if (result.success && result.base_uri) {
                document.getElementById('baseUriDefault').value = result.base_uri;
            }
        } catch (error) {
            console.log('Using default base URI');
        }
    }

    // =====================================================================
    //  GLOBAL TAB – Registry Cache TTL
    // =====================================================================

    async function loadRegistryCacheTtl() {
        try {
            const resp = await fetch('/settings/get-registry-cache-ttl', { credentials: 'same-origin' });
            const result = await resp.json();
            if (result.success && result.registry_cache_ttl != null) {
                document.getElementById('registryCacheTtl').value = result.registry_cache_ttl;
            }
        } catch (error) {
            console.log('Using default registry cache TTL');
        }
    }


    // =====================================================================
    //  GLOBAL TAB – Default Emoji Picker (uses shared EmojiPicker module)
    // =====================================================================

    async function loadCurrentDefaultEmoji() {
        try {
            const response = await fetch('/settings/get-default-emoji', { credentials: 'same-origin' });
            const result = await response.json();
            if (result.success && result.emoji) {
                document.getElementById('currentDefaultEmoji').textContent = result.emoji;
            }
        } catch (error) {
            console.log('Using default emoji');
        }
    }

    async function selectDefaultEmoji(emoji) {
        try {
            const response = await fetch('/settings/set-default-emoji', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ emoji })
            });
            const result = await response.json();
            if (result.success) {
                document.getElementById('currentDefaultEmoji').textContent = emoji;
                showNotification('Default class icon updated to ' + emoji, 'success', 2000);
            } else {
                showNotification('Error: ' + result.message, 'error');
            }
        } catch (error) {
            showNotification('Error saving default emoji: ' + error.message, 'error');
        }
    }

    const changeBtn = document.getElementById('changeDefaultEmoji');
    if (changeBtn) {
        EmojiPicker.create({
            triggerEl:   changeBtn,
            previewEl:   document.getElementById('currentDefaultEmoji'),
            containerEl: document.getElementById('defaultEmojiPickerMount'),
            showSearch:  false,
            onSelect:    function (emoji) { selectDefaultEmoji(emoji); }
        });
    }

    // =====================================================================
    //  GLOBAL TAB – Application Logo (top-bar branding)
    // =====================================================================

    async function loadNavbarLogo() {
        try {
            const resp = await fetch('/settings/navbar-logo', { credentials: 'same-origin' });
            const result = await resp.json();
            if (!result.success) return;
            const previewEl = document.getElementById('navbarLogoPreview');
            if (previewEl && result.logo_url) previewEl.src = result.logo_url;
            const statusEl = document.getElementById('navbarLogoStatus');
            if (statusEl) {
                statusEl.textContent = result.is_custom ? 'Custom logo active' : 'Using default logo';
            }
        } catch (e) {
            console.log('Could not load navbar logo settings');
        }
    }

    const logoFileInput = document.getElementById('navbarLogoFile');
    const logoUploadBtn = document.getElementById('btnUploadNavbarLogo');
    const logoResetBtn  = document.getElementById('btnResetNavbarLogo');
    const logoPreviewEl = document.getElementById('navbarLogoPreview');
    const logoStatusEl  = document.getElementById('navbarLogoStatus');

    if (logoFileInput) {
        logoFileInput.addEventListener('change', () => {
            const file = logoFileInput.files && logoFileInput.files[0];
            if (logoUploadBtn) logoUploadBtn.disabled = !file;
            if (file && logoPreviewEl) {
                const reader = new FileReader();
                reader.onload = (ev) => { logoPreviewEl.src = ev.target.result; };
                reader.readAsDataURL(file);
            }
        });
    }

    if (logoUploadBtn) {
        logoUploadBtn.addEventListener('click', async () => {
            const file = logoFileInput && logoFileInput.files && logoFileInput.files[0];
            if (!file) return;
            const MAX = 1024 * 1024;
            if (file.size > MAX) {
                showNotification(`Image too large (${file.size} bytes); max ${MAX} bytes`, 'error');
                return;
            }
            logoUploadBtn.disabled = true;
            const original = logoUploadBtn.innerHTML;
            logoUploadBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Uploading...';
            try {
                const fd = new FormData();
                fd.append('file', file);
                const resp = await fetch('/settings/navbar-logo', {
                    method: 'POST',
                    body: fd,
                    credentials: 'same-origin'
                });
                const result = await resp.json();
                if (result.success) {
                    if (logoPreviewEl && result.logo_url) logoPreviewEl.src = result.logo_url;
                    if (logoStatusEl) logoStatusEl.textContent = 'Custom logo active';
                    if (logoFileInput) logoFileInput.value = '';
                    if (typeof fetchCachedInvalidate === 'function') {
                        fetchCachedInvalidate('/navbar/state');
                    }
                    const navImg = document.getElementById('brandLogoImg');
                    if (navImg && result.logo_url) navImg.src = result.logo_url;
                    showNotification('Application logo updated', 'success', 2500);
                } else {
                    showNotification('Error: ' + (result.message || 'upload failed'), 'error');
                }
            } catch (e) {
                showNotification('Error uploading logo: ' + e.message, 'error');
            } finally {
                logoUploadBtn.innerHTML = original;
                logoUploadBtn.disabled = !(logoFileInput && logoFileInput.files && logoFileInput.files[0]);
            }
        });
    }

    if (logoResetBtn) {
        logoResetBtn.addEventListener('click', async () => {
            const confirmed = await (typeof showConfirmDialog === 'function'
                ? showConfirmDialog({
                    title: 'Reset application logo',
                    message: 'Restore the default OntoBricks logo for all users?',
                    confirmText: 'Reset',
                    confirmClass: 'btn-warning',
                    icon: 'arrow-counterclockwise'
                })
                : Promise.resolve(window.confirm('Restore the default logo?')));
            if (!confirmed) return;
            logoResetBtn.disabled = true;
            try {
                const resp = await fetch('/settings/navbar-logo', {
                    method: 'DELETE',
                    credentials: 'same-origin'
                });
                const result = await resp.json();
                if (result.success) {
                    if (logoPreviewEl && result.logo_url) logoPreviewEl.src = result.logo_url;
                    if (logoStatusEl) logoStatusEl.textContent = 'Using default logo';
                    if (logoFileInput) logoFileInput.value = '';
                    if (logoUploadBtn) logoUploadBtn.disabled = true;
                    if (typeof fetchCachedInvalidate === 'function') {
                        fetchCachedInvalidate('/navbar/state');
                    }
                    const navImg = document.getElementById('brandLogoImg');
                    if (navImg && result.logo_url) navImg.src = result.logo_url;
                    showNotification('Application logo reset to default', 'success', 2500);
                } else {
                    showNotification('Error: ' + (result.message || 'reset failed'), 'error');
                }
            } catch (e) {
                showNotification('Error resetting logo: ' + e.message, 'error');
            } finally {
                logoResetBtn.disabled = false;
            }
        });
    }

    // =====================================================================
    //  GRAPH DB TAB – Graph Engine selector
    // =====================================================================

    /** Show Lakebase picker section from graph engine select. */
    function applyGraphDbEnginePanels() {
        const sel = document.getElementById('graphEngineSelect');
        const lakePanel = document.getElementById('lakebaseGraphPanel');
        if (!sel) return;
        const eng = sel.value;
        if (lakePanel) {
            lakePanel.style.display = eng === 'lakebase' ? 'block' : 'none';
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    function _setSelectLoading(sel, msg) {
        if (!sel) return;
        sel.innerHTML = '<option value="">' + escapeHtmlSettings(msg) + '</option>';
        sel.disabled = true;
    }

    function _setSelectError(sel, msg) {
        if (!sel) return;
        sel.innerHTML = '<option value="">' + escapeHtmlSettings(msg) + '</option>';
        sel.disabled = false;
    }

    function _getCurrentSchemaValue() {
        const schSel = document.getElementById('lakebaseGraphSchema');
        const schIn  = document.getElementById('lakebaseGraphSchemaInput');
        const btn    = document.getElementById('btnToggleLakebaseSchemaInput');
        if (btn && btn.dataset.mode === 'input') {
            return (schIn ? schIn.value : '').trim() || 'ontobricks_graph';
        }
        return (schSel ? schSel.value : '').trim() || 'ontobricks_graph';
    }

    // ── cascading pickers ─────────────────────────────────────────────────────

    async function loadLakebaseProjects() {
        const projSel   = document.getElementById('lakebaseProject');
        const branchSel = document.getElementById('lakebaseBranch');
        const dbSel     = document.getElementById('lakebaseGraphDb');
        const schSel    = document.getElementById('lakebaseGraphSchema');
        const btn       = document.getElementById('btnLoadLakebaseProjects');
        const help      = document.getElementById('lakebaseProjectHelp');
        if (!projSel) return;

        _setSelectLoading(projSel, 'Loading projects…');
        if (btn) btn.disabled = true;

        // read current configured values to restore selection after reload
        let cfgDb = '', cfgProject = '', cfgBranch = '';
        try {
            const o = JSON.parse(document.getElementById('graphEngineConfig')?.value || '{}');
            cfgDb      = o.database || '';
            cfgProject = o.lakebase_project || '';
            cfgBranch  = o.lakebase_branch  || '';
        } catch (_) {}

        try {
            const resp = await fetch('/settings/graph-engine/lakebase-projects', { credentials: 'same-origin' });
            const data = resp.ok ? await resp.json() : {};
            if (!data.success || !data.projects.length) {
                _setSelectError(projSel, '(no projects found — check workspace auth)');
                if (help) help.textContent = data.message || 'Could not list projects.';
                return;
            }
            projSel.innerHTML = '<option value="">(select a project)</option>';
            let matched = false;
            for (const p of data.projects) {
                const opt = document.createElement('option');
                opt.value = p.name;
                opt.textContent = p.short_name + (p.state ? ' — ' + p.state : '');
                if (p.name === cfgProject || p.short_name === cfgProject) {
                    opt.selected = true;
                    matched = true;
                }
                projSel.appendChild(opt);
            }
            projSel.disabled = false;
            if (help) help.textContent = data.projects.length + ' project(s) found.';

            if (matched && projSel.value) {
                await loadLakebaseBranches(projSel.value, cfgBranch, cfgDb);
            }
        } catch (e) {
            _setSelectError(projSel, '(error — ' + (e.message || 'network') + ')');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    async function loadLakebaseBranches(projectPath, cfgBranch, cfgDb) {
        const branchSel = document.getElementById('lakebaseBranch');
        const help      = document.getElementById('lakebaseBranchHelp');
        if (!branchSel || !projectPath) return;

        _setSelectLoading(branchSel, 'Loading branches…');

        try {
            const resp = await fetch(
                '/settings/graph-engine/lakebase-branches?project=' + encodeURIComponent(projectPath),
                { credentials: 'same-origin' }
            );
            const data = resp.ok ? await resp.json() : {};
            if (!data.success || !data.branches.length) {
                _setSelectError(branchSel, '(no branches found)');
                if (help) help.textContent = data.message || 'No branches.';
                return;
            }
            branchSel.innerHTML = '<option value="">(select a branch)</option>';
            let matched = false;
            for (const b of data.branches) {
                const opt = document.createElement('option');
                opt.value = b.name;
                opt.textContent = b.short_name + (b.state ? ' — ' + b.state : '');
                if (b.name === cfgBranch || b.short_name === cfgBranch) {
                    opt.selected = true;
                    matched = true;
                }
                branchSel.appendChild(opt);
            }
            branchSel.disabled = false;
            if (help) help.textContent = data.branches.length + ' branch(es) found.';

            if (matched && branchSel.value) {
                await loadLakebasePgDatabases(branchSel.value, cfgDb);
            }
        } catch (e) {
            _setSelectError(branchSel, '(error — ' + (e.message || 'network') + ')');
        }
    }

    async function loadLakebasePgDatabases(branchPath, cfgDb) {
        const dbSel  = document.getElementById('lakebaseGraphDb');
        const schSel = document.getElementById('lakebaseGraphSchema');
        const help   = document.getElementById('lakebaseGraphDbHelp');
        if (!dbSel || !branchPath) return;

        _setSelectLoading(dbSel, 'Loading databases…');
        _setSelectLoading(schSel, '(select a database first)');

        // read current schema from config textarea so we can restore it
        let cfgSchema = 'ontobricks_graph';
        try {
            const o = JSON.parse(document.getElementById('graphEngineConfig')?.value || '{}');
            if (o.schema) cfgSchema = o.schema;
        } catch (_) {}

        try {
            const resp = await fetch(
                '/settings/graph-engine/lakebase-pg-databases?branch=' + encodeURIComponent(branchPath),
                { credentials: 'same-origin' }
            );
            const data = resp.ok ? await resp.json() : {};
            if (!data.success || !data.databases.length) {
                _setSelectError(dbSel, '(no databases found)');
                if (help) help.textContent = data.message || 'No databases on this branch.';
                return;
            }
            dbSel.innerHTML = '<option value="">(default — bound database)</option>';
            let matched = false;
            for (const name of data.databases) {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                if (name === cfgDb) { opt.selected = true; matched = true; }
                dbSel.appendChild(opt);
            }
            dbSel.disabled = false;
            if (help) help.textContent = data.databases.length + ' database(s) found.';

            if (matched && dbSel.value) {
                await loadLakebasePgSchemas(dbSel.value, cfgSchema);
            }
        } catch (e) {
            _setSelectError(dbSel, '(error — ' + (e.message || 'network') + ')');
        }
    }

    async function loadLakebasePgSchemas(database, cfgSchema) {
        const schSel = document.getElementById('lakebaseGraphSchema');
        const schIn  = document.getElementById('lakebaseGraphSchemaInput');
        const help   = document.getElementById('lakebaseGraphSchemaHelp');
        if (!schSel || !database) return;

        _setSelectLoading(schSel, 'Loading schemas…');

        try {
            const resp = await fetch(
                '/settings/graph-engine/lakebase-pg-schemas?database=' + encodeURIComponent(database),
                { credentials: 'same-origin' }
            );
            const data = resp.ok ? await resp.json() : {};
            if (!data.success || !data.schemas.length) {
                _setSelectError(schSel, '(no schemas — ' + (data.message || 'empty database') + ')');
                if (help) help.textContent = 'No schemas found. Use the pencil to type one manually.';
                return;
            }
            schSel.innerHTML = '<option value="">(default — ontobricks_graph)</option>';
            let matched = false;
            for (const name of data.schemas) {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                if (name === cfgSchema) { opt.selected = true; matched = true; }
                schSel.appendChild(opt);
            }
            schSel.disabled = false;
            if (help) help.textContent = data.schemas.length + ' schema(s) found.';
            if (!matched && cfgSchema) {
                // schema configured but not listed (might not exist yet) — add as option
                const opt = document.createElement('option');
                opt.value = cfgSchema;
                opt.textContent = cfgSchema + ' (configured)';
                opt.selected = true;
                schSel.appendChild(opt);
            }
            if (schIn) schIn.value = schSel.value || cfgSchema;
        } catch (e) {
            _setSelectError(schSel, '(error — ' + (e.message || 'network') + ')');
        }
        mergeLakebasePanelIntoConfigTextarea();
    }

    // ── schema toggle (select ↔ manual input) ────────────────────────────────

    function _initSchemaToggle() {
        const btn   = document.getElementById('btnToggleLakebaseSchemaInput');
        const schSel = document.getElementById('lakebaseGraphSchema');
        const schIn  = document.getElementById('lakebaseGraphSchemaInput');
        if (!btn || !schSel || !schIn) return;

        btn.addEventListener('click', function () {
            const isSelect = btn.dataset.mode === 'select';
            if (isSelect) {
                // switch to manual input
                schSel.classList.add('d-none');
                schIn.classList.remove('d-none');
                schIn.value = schSel.value || 'ontobricks_graph';
                btn.dataset.mode = 'input';
                btn.title = 'Use dropdown';
                btn.innerHTML = '<i class="bi bi-list"></i>';
            } else {
                // switch back to select
                schIn.classList.add('d-none');
                schSel.classList.remove('d-none');
                btn.dataset.mode = 'select';
                btn.title = 'Type schema name manually';
                btn.innerHTML = '<i class="bi bi-pencil"></i>';
            }
            mergeLakebasePanelIntoConfigTextarea();
        });

        schIn.addEventListener('input', function () {
            mergeLakebasePanelIntoConfigTextarea();
            const ucSchDisplay = document.getElementById('lakebaseUcSchemaDisplay');
            if (ucSchDisplay) ucSchDisplay.value = this.value || '';
        });
        schIn.addEventListener('change', mergeLakebasePanelIntoConfigTextarea);
    }

    // ── merge / apply ─────────────────────────────────────────────────────────

    /** Merge Lakebase form fields + optional managed-sync options into the JSON textarea. */
    function mergeLakebasePanelIntoConfigTextarea() {
        const ta         = document.getElementById('graphEngineConfig');
        const dbSel      = document.getElementById('lakebaseGraphDb');
        const projSel    = document.getElementById('lakebaseProject');
        const branchSel  = document.getElementById('lakebaseBranch');
        const syncModeEl = document.getElementById('lakebaseSyncMode');
        if (!ta || !dbSel) return;
        let o = {};
        try { o = JSON.parse(ta.value || '{}'); } catch (_) { o = {}; }
        if (typeof o !== 'object' || Array.isArray(o)) o = {};

        o.database          = dbSel.value || '';
        o.schema            = _getCurrentSchemaValue();
        o.lakebase_project  = (projSel   ? projSel.value   : '') || '';
        o.lakebase_branch   = (branchSel ? branchSel.value : '') || '';

        const mode = (syncModeEl && syncModeEl.value === 'managed_synced') ? 'managed_synced' : 'app_managed';
        if (mode === 'managed_synced') {
            o.sync_mode = 'managed_synced';
            const stEl   = document.getElementById('lakebaseSyncTableMode');
            const toutEl = document.getElementById('lakebaseSyncTimeout');
            const ucCat  = document.getElementById('lakebaseUcCatalog');
            if (stEl) o.sync_table_mode = stEl.value || 'snapshot';
            if (toutEl) {
                const n = parseInt(toutEl.value, 10);
                o.sync_timeout_s = (!isNaN(n) && n > 0) ? n : 600;
            }
            const cat = (ucCat ? ucCat.value : '').trim();
            if (cat) o.sync_uc_catalog = cat; else delete o.sync_uc_catalog;
            // sync_uc_schema is always derived from the Postgres graph schema — never persisted
            delete o.sync_uc_schema;
        } else {
            o.sync_mode = 'app_managed';
            delete o.sync_table_mode;
            delete o.sync_timeout_s;
            delete o.sync_uc_catalog;
            delete o.sync_uc_schema;
        }
        ta.value = JSON.stringify(o, null, 2);
    }

    function toggleLakebaseManagedSyncPanel() {
        const sm    = document.getElementById('lakebaseSyncMode');
        const panel = document.getElementById('lakebaseManagedSyncPanel');
        if (!sm || !panel) return;
        panel.classList.toggle('d-none', sm.value !== 'managed_synced');
    }

    function updateLakebaseSyncModeHelp() {
        const sm = document.getElementById('lakebaseSyncMode');
        const v  = sm && sm.value === 'managed_synced' ? 'managed_synced' : 'app_managed';
        document.querySelectorAll('[data-lk-mode]').forEach(function (el) {
            el.classList.toggle('d-none', el.getAttribute('data-lk-mode') !== v);
        });
    }

    // ── UC catalog + schema pickers ───────────────────────────────────────────

    async function loadUcCatalogsForGraphEngine() {
        const catSel = document.getElementById('lakebaseUcCatalog');
        const msg    = document.getElementById('lakebaseUcCatalogLoadMsg');
        const btn    = document.getElementById('btnLoadUcCatalogs');
        if (!catSel) return;
        if (msg) { msg.classList.remove('d-none'); msg.className = 'form-text small mt-1 text-muted'; msg.textContent = 'Loading catalogs…'; }
        if (btn) btn.disabled = true;

        let cfgCat = '';
        try {
            const o = JSON.parse(document.getElementById('graphEngineConfig')?.value || '{}');
            cfgCat = o.sync_uc_catalog || '';
        } catch (_) {}

        try {
            const resp = await fetch('/settings/graph-engine/uc-catalogs', { credentials: 'same-origin' });
            const data = resp.ok ? await resp.json() : {};
            if (data.success && Array.isArray(data.catalogs)) {
                catSel.innerHTML = '<option value="">(none — use Registry catalog)</option>';
                let matched = false;
                for (const name of data.catalogs) {
                    const opt = document.createElement('option');
                    opt.value = name;
                    opt.textContent = name;
                    if (name === cfgCat) { opt.selected = true; matched = true; }
                    catSel.appendChild(opt);
                }
                catSel.disabled = false;
                if (msg) { msg.className = 'form-text small mt-1 text-success'; msg.textContent = data.catalogs.length + ' catalog(s) loaded.'; }
                // no-op: UC schema is always derived from Postgres graph schema
            } else {
                if (msg) { msg.className = 'form-text small mt-1 text-warning'; msg.textContent = data.message || 'Could not list catalogs.'; }
                catSel.disabled = false;
            }
        } catch (e) {
            if (msg) { msg.className = 'form-text small mt-1 text-warning'; msg.textContent = e.message || 'Network error'; }
            catSel.disabled = false;
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function applyLakebaseFormFromConfigTextarea() {
        const ta         = document.getElementById('graphEngineConfig');
        const syncModeEl = document.getElementById('lakebaseSyncMode');
        if (!ta) return;
        let o = {};
        try { o = JSON.parse(ta.value || '{}'); } catch (_) {}

        if (syncModeEl) syncModeEl.value = (o.sync_mode === 'managed_synced') ? 'managed_synced' : 'app_managed';

        const stEl   = document.getElementById('lakebaseSyncTableMode');
        if (stEl && o.sync_table_mode) stEl.value = o.sync_table_mode;

        const toutEl = document.getElementById('lakebaseSyncTimeout');
        if (toutEl && o.sync_timeout_s != null) toutEl.value = String(parseInt(o.sync_timeout_s, 10) || 600);

        // UC catalog — set value but don't reload options here (happens in loadUcCatalogsForGraphEngine)
        const ucCat = document.getElementById('lakebaseUcCatalog');
        if (ucCat && o.sync_uc_catalog != null) ucCat.value = String(o.sync_uc_catalog);

        // schema input mirror + UC schema display (always mirrors Postgres graph schema)
        const schIn = document.getElementById('lakebaseGraphSchemaInput');
        if (schIn && o.schema) schIn.value = o.schema;
        const ucSchDisplay = document.getElementById('lakebaseUcSchemaDisplay');
        if (ucSchDisplay) ucSchDisplay.value = o.schema || '';

        toggleLakebaseManagedSyncPanel();
        updateLakebaseSyncModeHelp();
    }

    async function loadLakebaseGraphHealth() {
        const msgEl = document.getElementById('lakebaseGraphHealthMessage');
        const dl = document.getElementById('lakebaseGraphHealthDl');
        const btn = document.getElementById('btnRefreshLakebaseGraphHealth');
        const engSel = document.getElementById('graphEngineSelect');
        if (!msgEl || !dl || engSel?.value !== 'lakebase') return;

        if (btn) btn.disabled = true;
        dl.innerHTML = '';
        msgEl.style.display = '';
        msgEl.className = 'small mb-2 text-muted';
        msgEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Checking Lakebase…';

        function row(label, value) {
            return '<dt class="col-sm-4 text-muted">' + escapeHtmlSettings(label) + '</dt>'
                + '<dd class="col-sm-8 font-monospace text-break">' + value + '</dd>';
        }

        try {
            const resp = await fetch('/settings/graph-engine/lakebase-health', { credentials: 'same-origin' });
            const data = resp.ok ? await resp.json() : {};
            if (!data.success) {
                msgEl.className = 'small mb-2 text-warning';
                const m = data.message || data.reason || 'Health check failed';
                msgEl.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + escapeHtmlSettings(m);
                if (data.host) {
                    dl.innerHTML = row('PGHOST', escapeHtmlSettings(String(data.host)));
                }
                return;
            }
            msgEl.className = 'small mb-2 ' + (data.schema_exists ? 'text-success' : 'text-warning');
            msgEl.innerHTML = '<i class="bi bi-' + (data.schema_exists ? 'check-circle' : 'exclamation-triangle') + ' me-1"></i>'
                + escapeHtmlSettings(data.message || 'OK');
            dl.innerHTML = (
                row('PGHOST', escapeHtmlSettings(String(data.host || '')))
                + row('Port', escapeHtmlSettings(String(data.port != null ? data.port : '')))
                + row('Bound PGDATABASE', escapeHtmlSettings(String(data.bound_database || '')))
                + row('Effective database', escapeHtmlSettings(String(data.effective_database || '')))
                + row('Graph schema', escapeHtmlSettings(String(data.graph_schema || '')))
                + row('Schema exists', data.schema_exists ? 'yes' : 'no')
                + row('Tables in schema', escapeHtmlSettings(String(data.tables_in_schema != null ? data.tables_in_schema : '')))
            );
        } catch (e) {
            msgEl.className = 'small mb-2 text-danger';
            msgEl.innerHTML = '<i class="bi bi-x-circle me-1"></i>' + escapeHtmlSettings(e.message || 'Network error');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function setGraphDbTabLoading(loading) {
        const banner  = document.getElementById('graphDbTabLoadingBanner');
        const content = document.getElementById('graphDbTabContent');
        if (banner) {
            banner.classList.toggle('d-none', !loading);
            banner.classList.toggle('d-flex', loading);
        }
        if (content) content.classList.toggle('d-none', loading);
    }

    /** Reload engine + JSON from server so the tab matches persisted settings after every visit. */
    async function refreshGraphDbTabFromServer() {
        const sel = document.getElementById('graphEngineSelect');
        const ta  = document.getElementById('graphEngineConfig');
        if (!sel || !ta) return;
        try {
            const [engResp, cfgResp] = await Promise.all([
                fetch('/settings/graph-engine',        { credentials: 'same-origin' }),
                fetch('/settings/graph-engine-config', { credentials: 'same-origin' }),
            ]);
            const engData = engResp.ok ? await engResp.json() : {};
            const cfgData = cfgResp.ok ? await cfgResp.json() : {};
            if (cfgData.success) {
                ta.value = JSON.stringify(cfgData.graph_engine_config || {}, null, 2);
            }
            const rawEng = engData.graph_engine;
            if (engData.success && rawEng && typeof rawEng === 'string') {
                const allowed = Array.isArray(engData.allowed_engines) ? engData.allowed_engines : [];
                if (allowed.length === 0 && rawEng === 'lakebase') {
                    sel.value = rawEng;
                } else if (allowed.indexOf(rawEng) >= 0) {
                    sel.value = rawEng;
                } else {
                    sel.value = 'lakebase';
                }
            }
            applyLakebaseFormFromConfigTextarea();
            if (sel.value === 'lakebase') {
                // auto-load the cascading chain if a project is already configured
                await loadLakebaseProjects();
                await loadLakebaseGraphHealth();
                // restore UC catalog/schema dropdowns when managed_synced was persisted
                const syncModeEl = document.getElementById('lakebaseSyncMode');
                if (syncModeEl && syncModeEl.value === 'managed_synced') {
                    await loadUcCatalogsForGraphEngine();
                }
            }
        } catch (e) {
            console.log('Graph DB tab refresh failed', e);
        } finally {
            applyGraphDbEnginePanels();
        }
    }

    document.getElementById('graphEngineSelect')?.addEventListener('change', async function () {
        applyGraphDbEnginePanels();
        if (this.value === 'lakebase') {
            applyLakebaseFormFromConfigTextarea();
            await loadLakebaseProjects();
            await loadLakebaseGraphHealth();
        }
    });

    // cascading project → branch → database → schema
    document.getElementById('btnLoadLakebaseProjects')?.addEventListener('click', () => loadLakebaseProjects());
    document.getElementById('lakebaseProject')?.addEventListener('change', async function () {
        const branchSel = document.getElementById('lakebaseBranch');
        const dbSel     = document.getElementById('lakebaseGraphDb');
        const schSel    = document.getElementById('lakebaseGraphSchema');
        _setSelectLoading(branchSel, '(select a project first)');
        _setSelectLoading(dbSel,     '(select a branch first)');
        _setSelectLoading(schSel,    '(select a database first)');
        mergeLakebasePanelIntoConfigTextarea();
        if (this.value) await loadLakebaseBranches(this.value, '', '');
    });
    document.getElementById('lakebaseBranch')?.addEventListener('change', async function () {
        const dbSel  = document.getElementById('lakebaseGraphDb');
        const schSel = document.getElementById('lakebaseGraphSchema');
        _setSelectLoading(dbSel,  '(select a branch first)');
        _setSelectLoading(schSel, '(select a database first)');
        mergeLakebasePanelIntoConfigTextarea();
        if (this.value) await loadLakebasePgDatabases(this.value, '');
    });
    document.getElementById('lakebaseGraphDb')?.addEventListener('change', async function () {
        const schSel = document.getElementById('lakebaseGraphSchema');
        _setSelectLoading(schSel, '(select a database first)');
        mergeLakebasePanelIntoConfigTextarea();
        if (this.value) await loadLakebasePgSchemas(this.value, _getCurrentSchemaValue());
    });
    document.getElementById('lakebaseGraphSchema')?.addEventListener('change', function () {
        mergeLakebasePanelIntoConfigTextarea();
        const ucSchDisplay = document.getElementById('lakebaseUcSchemaDisplay');
        if (ucSchDisplay) ucSchDisplay.value = this.value || '';
    });

    // managed-sync options
    document.getElementById('lakebaseSyncMode')?.addEventListener('change', function () {
        toggleLakebaseManagedSyncPanel();
        updateLakebaseSyncModeHelp();
        mergeLakebasePanelIntoConfigTextarea();
    });
    document.getElementById('lakebaseSyncTableMode')?.addEventListener('change', mergeLakebasePanelIntoConfigTextarea);
    document.getElementById('lakebaseSyncTimeout')?.addEventListener('input',  mergeLakebasePanelIntoConfigTextarea);
    document.getElementById('lakebaseSyncTimeout')?.addEventListener('change', mergeLakebasePanelIntoConfigTextarea);

    // UC catalog change
    document.getElementById('btnLoadUcCatalogs')?.addEventListener('click', () => loadUcCatalogsForGraphEngine());
    document.getElementById('lakebaseUcCatalog')?.addEventListener('change', mergeLakebasePanelIntoConfigTextarea);

    document.getElementById('btnRefreshLakebaseGraphHealth')?.addEventListener('click', () => loadLakebaseGraphHealth());

    _initSchemaToggle();

    /** Persist graph engine and JSON config (used by global Save). */
    async function saveGraphDbSettings(errors) {
        const sel = document.getElementById('graphEngineSelect');
        const ta = document.getElementById('graphEngineConfig');
        const errDiv = document.getElementById('graphEngineConfigError');
        if (!sel || !ta) return;

        if (errDiv) errDiv.style.display = 'none';

        try {
            const resp = await fetch('/settings/graph-engine', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ graph_engine: sel.value }),
            });
            const result = await resp.json();
            if (!result.success) {
                errors.push('Graph DB engine: ' + (result.message || 'Unknown error'));
                return;
            }

            if (sel.value === 'lakebase') {
                mergeLakebasePanelIntoConfigTextarea();
            }

            let parsed;
            try {
                parsed = JSON.parse(ta.value || '{}');
            } catch (parseErr) {
                errors.push('Graph DB config: invalid JSON (' + parseErr.message + ')');
                if (errDiv) {
                    errDiv.textContent = 'Invalid JSON: ' + parseErr.message;
                    errDiv.style.display = 'block';
                }
                return;
            }
            if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
                errors.push('Graph DB config: must be a JSON object');
                if (errDiv) {
                    errDiv.textContent = 'Configuration must be a JSON object (not an array or primitive)';
                    errDiv.style.display = 'block';
                }
                return;
            }

            const cfgResp = await fetch('/settings/graph-engine-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ graph_engine_config: parsed }),
            });
            const cfgJson = await cfgResp.json();
            if (!cfgJson.success) {
                errors.push('Graph DB config: ' + (cfgJson.message || 'Unknown error'));
                return;
            }
            ta.value = JSON.stringify(cfgJson.graph_engine_config || parsed, null, 2);
            applyGraphDbEnginePanels();
            if (sel.value === 'lakebase') loadLakebaseGraphHealth();
        } catch (e) {
            errors.push('Graph DB: ' + e.message);
        }
    }

    // =====================================================================
    //  GRAPH DB TAB – tab activation
    // =====================================================================

    document.getElementById('tab-graphdb')?.addEventListener('shown.bs.tab', async () => {
        setGraphDbTabLoading(true);
        try {
            await refreshGraphDbTabFromServer();
        } finally {
            setGraphDbTabLoading(false);
        }
    });

    // =====================================================================
    //  GLOBAL SAVE BUTTON – warehouse, global prefs, CloudFetch, Graph DB
    // =====================================================================

    document.getElementById('btnSaveAllSettings')?.addEventListener('click', async function () {
        const btn = this;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Saving...';

        const errors = [];

        // 1. Save warehouse (skip when locked by Databricks App resource)
        const whId = document.getElementById('settingsWarehouseSelect').value;
        if (whId && !warehouseLocked) {
            try {
                const resp = await fetch('/settings/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ warehouse_id: whId })
                });
                const r = await resp.json();
                if (r.success) currentWarehouseId = whId;
                else errors.push('Warehouse: ' + r.message);
            } catch (e) { errors.push('Warehouse: ' + e.message); }
        }

        // 2. Save base URI
        const baseUri = document.getElementById('baseUriDefault').value.trim();
        if (baseUri) {
            try {
                const resp = await fetch('/settings/save-base-uri', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ base_uri: baseUri })
                });
                const r = await resp.json();
                if (!r.success) errors.push('Base URI: ' + r.message);
            } catch (e) { errors.push('Base URI: ' + e.message); }
        }

        // 3. Save registry cache TTL
        const ttlInput = document.getElementById('registryCacheTtl');
        if (ttlInput) {
            const ttl = parseInt(ttlInput.value, 10);
            if (!isNaN(ttl) && ttl >= 10) {
                try {
                    const resp = await fetch('/settings/save-registry-cache-ttl', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        credentials: 'same-origin',
                        body: JSON.stringify({ registry_cache_ttl: ttl })
                    });
                    const r = await resp.json();
                    if (!r.success) errors.push('Cache TTL: ' + r.message);
                } catch (e) { errors.push('Cache TTL: ' + e.message); }
            }
        }

        // 4. Graph DB engine + JSON config (same tab; top Save only)
        await saveGraphDbSettings(errors);

        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check-circle me-1"></i> Save';

        if (errors.length > 0) {
            showNotification('Some settings failed to save:\n' + errors.join('\n'), 'error');
        } else {
            showNotification('All settings saved', 'success', 2000);
        }
    });
});
