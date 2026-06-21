/* ─────────────────────────────────────────────────────────────────────────
 * Reports module — mount / unmount.
 *
 * Exposes ReportsView.mount(root) and ReportsView.unmount().
 *
 * Features:
 *  • Generate intelligence briefings (news + case data → Ollama)
 *  • Sidebar list of saved reports (Neo4j ReportDoc nodes)
 *  • Select / read any past report
 *  • Delete reports
 *  • Print / export as PDF via browser print
 * ─────────────────────────────────────────────────────────────────────── */

window.ReportsView = (function () {
    'use strict';

    const state = {
        reports:        [],   // list of saved report summaries
        activeId:       null, // currently displayed report id (null = freshly generated)
        windowH:        24,
        generating:     false,
    };

    // ── Data loading ───────────────────────────────────────────────────
    async function loadList() {
        try {
            const data = await Shell.api('/api/reports/list');
            state.reports = data.reports || [];
            renderList();
        } catch (e) {
            Shell.toast('Could not load report list: ' + e.message, 'warning');
        }
    }

    async function generate(force = false) {
        if (state.generating) return;
        state.generating = true;

        const btn = document.getElementById('reports-generate-btn');
        if (btn) { btn.disabled = true; btn.textContent = '⏳ Generating…'; }

        showGenerating();

        try {
            const q = new URLSearchParams({ window: state.windowH, save: true });
            if (force) q.set('force', 'true');
            const report = await Shell.api(`/api/reports/generate?${q}`);

            showReport(report);
            state.activeId = report.id || null;

            // Refresh sidebar so the new report appears
            await loadList();
            if (state.activeId) highlightActive(state.activeId);

            Shell.toast('Report generated', 'success', 2500);
        } catch (e) {
            showError('Generation failed: ' + e.message);
            Shell.toast('Report generation failed: ' + e.message, 'error');
        } finally {
            state.generating = false;
            if (btn) { btn.disabled = false; btn.textContent = '⚡ Generate'; }
        }
    }

    async function openReport(id) {
        try {
            const report = await Shell.api(`/api/reports/${encodeURIComponent(id)}`);
            showReport(report);
            state.activeId = id;
            highlightActive(id);
        } catch (e) {
            Shell.toast('Could not load report: ' + e.message, 'error');
        }
    }

    async function deleteReport(id) {
        try {
            await Shell.api(`/api/reports/${encodeURIComponent(id)}`, { method: 'DELETE' });
            state.reports = state.reports.filter(r => r.id !== id);
            renderList();
            if (state.activeId === id) {
                state.activeId = null;
                showPlaceholder();
            }
            Shell.toast('Report deleted', 'info', 2000);
        } catch (e) {
            Shell.toast('Delete failed: ' + e.message, 'error');
        }
    }

    // ── Rendering ──────────────────────────────────────────────────────
    function renderList() {
        const list = document.getElementById('reports-list');
        const cnt  = document.getElementById('reports-sidebar-count');
        if (!list) return;

        if (cnt) cnt.textContent = state.reports.length
            ? `(${state.reports.length})`
            : '';

        if (!state.reports.length) {
            list.innerHTML = `<div class="reports-dim" style="padding:0.6rem 0.75rem;font-size:0.8rem">
                No saved reports yet. Click <strong>⚡ Generate</strong> to create one.
            </div>`;
            return;
        }

        list.innerHTML = state.reports.map(r => {
            const dt   = r.generated_at ? formatRelative(r.generated_at) : '—';
            const prev = (r.preview || '').replace(/[#*`_]/g, '').slice(0, 90);
            const artPill  = r.article_count != null ? `${r.article_count} art.` : '';
            const casePill = r.case_count    != null ? `${r.case_count} cases`   : '';
            return `<div class="reports-list-item ${state.activeId === r.id ? 'active' : ''}"
                        data-id="${escapeHtml(r.id)}">
                <div class="rli-date">${escapeHtml(dt)}</div>
                <div class="rli-preview">${escapeHtml(prev || r.title || '—')}</div>
                <div class="rli-meta">
                    ${artPill ? `<span>${escapeHtml(artPill)}</span>` : ''}
                    ${casePill ? `<span>${escapeHtml(casePill)}</span>` : ''}
                    <button class="rli-del" data-del="${escapeHtml(r.id)}" title="Delete">🗑</button>
                </div>
            </div>`;
        }).join('');

        list.querySelectorAll('.reports-list-item').forEach(el => {
            el.addEventListener('click', (e) => {
                // Don't open if delete button was clicked
                if (e.target.closest('[data-del]')) return;
                openReport(el.dataset.id);
            });
        });
        list.querySelectorAll('[data-del]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (confirm('Delete this report?')) deleteReport(btn.dataset.del);
            });
        });
    }

    function highlightActive(id) {
        document.querySelectorAll('.reports-list-item').forEach(el => {
            el.classList.toggle('active', el.dataset.id === id);
        });
    }

    function showReport(report) {
        const body   = document.getElementById('reports-body');
        const bar    = document.getElementById('reports-stats-bar');
        if (!body) return;

        // Stats bar
        if (bar) {
            bar.classList.remove('hidden');
            const setEl = (id, txt) => {
                const el = document.getElementById(id);
                if (el) el.textContent = txt;
            };
            const ts = report.generated_at
                ? new Date(report.generated_at).toLocaleString()
                : '—';
            setEl('rst-date',     `🕐 ${ts}`);
            setEl('rst-articles', `📰 ${report.article_count ?? '?'} articles`);
            setEl('rst-cases',    `🔍 ${report.case_count ?? '?'} cases`);
            setEl('rst-window',   `⏱ ${report.window_hours ?? '?'}h window`);
        }

        // Content
        const md = report.content || '_No content_';
        const titleHtml = report.title
            ? `<h1>${escapeHtml(report.title)}</h1>`
            : '';
        body.innerHTML = `<div class="reports-content">${titleHtml}${renderMarkdown(md)}</div>`;
    }

    function showGenerating() {
        const body = document.getElementById('reports-body');
        const bar  = document.getElementById('reports-stats-bar');
        if (bar) bar.classList.add('hidden');
        if (body) body.innerHTML = `
            <div class="reports-generating">
                <div class="reports-spinner"></div>
                <div>Gathering intelligence data…</div>
                <div class="reports-dim" style="font-size:0.78rem">
                    Querying news + cases, then calling the LLM.<br>
                    This may take 20–60 s.
                </div>
            </div>`;
    }

    function showPlaceholder() {
        const body = document.getElementById('reports-body');
        const bar  = document.getElementById('reports-stats-bar');
        if (bar) bar.classList.add('hidden');
        if (body) body.innerHTML = `
            <div class="reports-placeholder">
                <div class="reports-placeholder-icon">📋</div>
                <div>Click <strong>⚡ Generate</strong> to produce an intelligence brief,<br>
                or select a saved report from the left panel.</div>
            </div>`;
    }

    function showError(msg) {
        const body = document.getElementById('reports-body');
        if (body) body.innerHTML = `
            <div class="reports-placeholder" style="color:var(--danger)">
                <div class="reports-placeholder-icon">⚠️</div>
                <div>${escapeHtml(msg)}</div>
            </div>`;
    }

    // ── Helpers ────────────────────────────────────────────────────────
    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g,
            m => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]));
    }

    function renderMarkdown(md) {
        let h = escapeHtml(md);
        h = h.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        h = h.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
        h = h.replace(/^# (.+)$/gm,   '<h1>$1</h1>');
        h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        h = h.replace(/\*([^*\n]+)\*/g,   '<em>$1</em>');
        h = h.replace(/`([^`]+)`/g,       '<code>$1</code>');
        h = h.replace(/^---+$/gm,         '<hr>');
        h = h.replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>');
        h = h.replace(/(<li>.*?<\/li>(?:\s*<li>.*?<\/li>)*)/gs, '<ul>$1</ul>');
        h = h.split(/\n{2,}/).map(p =>
            /^<(h\d|ul|ol|li|pre|hr)/.test(p.trim())
                ? p
                : `<p>${p.replace(/\n/g, '<br>')}</p>`
        ).join('');
        return h;
    }

    function formatRelative(ts) {
        if (!ts) return '';
        const t = new Date(ts);
        if (isNaN(t)) return ts.slice(0, 16).replace('T', ' ');
        const diff = (Date.now() - t.getTime()) / 1000;
        if (diff <  60)    return 'just now';
        if (diff <  3600)  return `${Math.floor(diff / 60)} m ago`;
        if (diff <  86400) return `${Math.floor(diff / 3600)} h ago`;
        return t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    // ── Mount / unmount ────────────────────────────────────────────────
    async function mount(root) {
        const html = await fetch('/modules/reports/view.html').then(r => r.text());
        root.innerHTML = html;

        // Window selector
        const winSel = document.getElementById('reports-window');
        if (winSel) {
            winSel.value = String(state.windowH);
            winSel.addEventListener('change', () => {
                state.windowH = parseInt(winSel.value, 10) || 24;
            });
        }

        // Generate button
        document.getElementById('reports-generate-btn')
            ?.addEventListener('click', () => generate(true));

        // Print button
        document.getElementById('reports-print-btn')
            ?.addEventListener('click', () => window.print());

        // Load saved reports list
        await loadList();

        // Auto-open the most recent report if available
        if (state.reports.length && !state.activeId) {
            await openReport(state.reports[0].id);
        }
    }

    function unmount() {
        // Nothing stateful to clean up — sidebar list is in-memory only.
    }

    return { mount, unmount };
})();
