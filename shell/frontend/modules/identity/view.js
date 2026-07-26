/*
 * Identity Forge view — generate + manage synthetic OSINT cover personas.
 * Exposes IdentityView.mount(root) / .unmount().
 */
window.IdentityView = (() => {
    'use strict';
    const API = '/api/identity';
    let current = null;   // the persona currently on screen

    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
        m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));

    async function api(path, opts) {
        const r = await fetch(API + path, {
            ...opts,
            headers: opts?.body ? { 'Content-Type': 'application/json', ...(opts.headers || {}) } : opts?.headers,
        });
        if (!r.ok) throw new Error(`${r.status}: ${(await r.text().catch(() => '')).slice(0, 160)}`);
        return r.status === 204 ? null : r.json();
    }

    const chips = (arr, cls) => (arr || []).map(v =>
        `<button class="idf-chip ${cls}" data-copy="${esc(v)}" title="Click to copy">${esc(v)}</button>`).join('');

    function renderPersona(p) {
        current = p;
        const el = document.getElementById('idf-persona');
        if (!el) return;
        const engine = p.backstory_engine === 'claude' || p.backstory_engine === 'claude-code'
            ? '<span class="idf-eng idf-eng-ai">Claude</span>'
            : `<span class="idf-eng">${esc(p.backstory_engine || '')}</span>`;
        el.style.display = '';
        el.innerHTML = `
          <div class="idf-p-head">
            <div class="idf-avatar">${p.avatar_svg || ''}</div>
            <div class="idf-p-id">
              <div class="idf-p-name">${esc(p.full_name)}</div>
              <div class="idf-p-meta">${esc(p.age)} · ${esc(p.gender)} · ${esc(p.city)}, ${esc(p.country)}</div>
              <div class="idf-p-meta idf-dim">${esc(p.occupation)} · DOB ${esc(p.dob)}</div>
            </div>
            <div class="idf-p-actions">
              <button id="idf-save" class="idf-btn idf-btn-primary">＋ Save</button>
              <button id="idf-regen" class="idf-btn">↻ Reroll</button>
            </div>
          </div>

          <div class="idf-block"><div class="idf-lbl">USERNAMES</div><div class="idf-chips">${chips(p.usernames, 'c-user')}</div></div>
          <div class="idf-block"><div class="idf-lbl">EMAIL PATTERNS</div><div class="idf-chips">${chips(p.email_suggestions, 'c-mail')}</div></div>
          <div class="idf-block"><div class="idf-lbl">INTERESTS</div><div class="idf-chips">${chips(p.interests, 'c-int')}</div></div>
          <div class="idf-block"><div class="idf-lbl">BACKSTORY ${engine}</div><div class="idf-story">${esc(p.backstory)}</div></div>
          <p class="idf-disc">${esc(p.disclaimer || '')}</p>`;

        el.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', () => {
            navigator.clipboard?.writeText(b.dataset.copy);
            Shell.toast('Copied: ' + b.dataset.copy, 'info', 1400);
        }));
        document.getElementById('idf-save').addEventListener('click', savePersona);
        document.getElementById('idf-regen').addEventListener('click', generate);
    }

    async function generate() {
        const btn = document.getElementById('idf-generate');
        const body = {
            locale: document.getElementById('idf-locale').value || null,
            gender: document.getElementById('idf-gender').value || null,
            age_min: parseInt(document.getElementById('idf-agemin').value, 10) || 22,
            age_max: parseInt(document.getElementById('idf-agemax').value, 10) || 48,
        };
        if (btn) { btn.disabled = true; btn.textContent = '⏳ Forging…'; }
        const el = document.getElementById('idf-persona');
        if (el) { el.style.display = ''; el.innerHTML = '<div class="idf-loading">Forging persona… (writing backstory)</div>'; }
        try {
            renderPersona(await api('/generate', { method: 'POST', body: JSON.stringify(body) }));
        } catch (e) {
            if (el) el.innerHTML = `<div class="idf-err">Error: ${esc(e.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '🎲 Forge persona'; }
        }
    }

    async function savePersona() {
        if (!current) return;
        try {
            await api('/personas', { method: 'POST', body: JSON.stringify({ persona: current }) });
            Shell.toast('Persona saved', 'success', 1600);
            loadSaved();
        } catch (e) { Shell.toast('Save failed: ' + e.message, 'error', 2600); }
    }

    async function loadSaved() {
        const wrap = document.getElementById('idf-saved');
        const count = document.getElementById('idf-count');
        try {
            const { personas } = await api('/personas');
            if (count) count.textContent = `(${personas.length})`;
            if (!wrap) return;
            wrap.innerHTML = personas.length ? personas.map(p => `
              <div class="idf-saved-row" data-id="${esc(p.id)}">
                <div class="idf-saved-av">${p.avatar_svg || ''}</div>
                <div class="idf-saved-info">
                  <div class="idf-saved-name">${esc(p.full_name)}</div>
                  <div class="idf-dim">${esc(p.age)} · ${esc(p.city)}, ${esc(p.country)}</div>
                </div>
                <button class="idf-icon" data-del="${esc(p.id)}" title="Delete">✕</button>
              </div>`).join('')
                : '<div class="idf-dim idf-empty">No saved personas yet.</div>';
            wrap.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async (e) => {
                e.stopPropagation();
                try { await api('/personas/' + b.dataset.del, { method: 'DELETE' }); loadSaved(); }
                catch (err) { Shell.toast('Delete failed', 'error', 2000); }
            }));
            wrap.querySelectorAll('.idf-saved-row').forEach(row => row.addEventListener('click', async () => {
                const { personas: ps } = await api('/personas');
                const p = ps.find(x => x.id === row.dataset.id);
                if (p) { renderPersona(p); document.getElementById('idf-persona')?.scrollIntoView({ behavior: 'smooth' }); }
            }));
        } catch (e) { if (wrap) wrap.innerHTML = `<div class="idf-err">${esc(e.message)}</div>`; }
    }

    async function mount(root) {
        root.innerHTML = await fetch('/modules/identity/view.html').then(r => r.text());
        // populate locales
        try {
            const { locales } = await api('/locales');
            const sel = document.getElementById('idf-locale');
            locales.forEach(l => { const o = document.createElement('option'); o.value = l.code; o.textContent = l.label; sel.appendChild(o); });
        } catch (e) { /* random still works */ }
        document.getElementById('idf-generate')?.addEventListener('click', generate);
        loadSaved();
    }

    function unmount() { current = null; }

    return { mount, unmount };
})();
