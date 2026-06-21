(function() {
  'use strict';
  let _mounted = false;

  function _esc(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  async function mount(root) {
    if (_mounted) return;
    _mounted = true;
    const r = await fetch('/modules/presence/view.html');
    root.innerHTML = await r.text();

    // Tab switching
    root.querySelectorAll('.presence-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        root.querySelectorAll('.presence-tab').forEach(t => t.classList.remove('active'));
        root.querySelectorAll('.presence-tab-pane').forEach(p => { p.hidden = true; p.classList.remove('active'); });
        tab.classList.add('active');
        const pane = root.querySelector(`#presence-tab-${tab.dataset.tab}`);
        if (pane) { pane.hidden = false; pane.classList.add('active'); }
        if (tab.dataset.tab === 'listings') _loadPlatforms();
        if (tab.dataset.tab === 'content')  _loadCalendar();
      });
    });

    // Content generator
    root.querySelector('#content-generate-btn')?.addEventListener('click', _generateContent);
    root.querySelector('#content-copy-btn')?.addEventListener('click', () => {
      const txt = root.querySelector('#content-output')?.value;
      if (txt) navigator.clipboard.writeText(txt);
    });
    root.querySelector('#content-save-btn')?.addEventListener('click', _saveToCalendar);
    root.querySelector('#content-output')?.addEventListener('input', _updateCharCount);

    // Listing generator
    root.querySelector('#listing-generate-btn')?.addEventListener('click', _generateListing);

    // Outreach generator
    root.querySelector('#outreach-generate-btn')?.addEventListener('click', _generateOutreach);
    root.querySelector('#outreach-email-copy')?.addEventListener('click', () => {
      const subj = root.querySelector('#outreach-email-subject')?.textContent || '';
      const body = root.querySelector('#outreach-email-body')?.value || '';
      navigator.clipboard.writeText(`Subject: ${subj}\n\n${body}`);
    });
    root.querySelector('#outreach-linkedin-copy')?.addEventListener('click', () => {
      const txt = root.querySelector('#outreach-linkedin')?.value || '';
      navigator.clipboard.writeText(txt);
    });
    root.querySelector('#outreach-linkedin')?.addEventListener('input', () => {
      const val = root.querySelector('#outreach-linkedin')?.value || '';
      const el  = root.querySelector('#outreach-linkedin-count');
      if (el) el.textContent = `${val.length}/290 chars`;
    });

    _loadCalendar();
  }

  function unmount() { _mounted = false; }

  function _getRoot() { return document.getElementById('presence-root'); }

  async function _generateContent() {
    const r     = _getRoot();
    const type  = r.querySelector('#content-type')?.value;
    const topic = r.querySelector('#content-topic')?.value;
    const platform = r.querySelector('#content-platform')?.value;
    const btn  = r.querySelector('#content-generate-btn');
    const wrap = r.querySelector('#content-output-wrap');
    const out  = r.querySelector('#content-output');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Generating…'; }
    try {
      const resp = await fetch('/api/presence/generate/post', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ type, topic, platform }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const { text } = await resp.json();
      if (out)  out.value = text || '';
      if (wrap) wrap.hidden = false;
      _updateCharCount();
    } catch(e) {
      if (window.Shell) window.Shell.toast('Generation failed: ' + e.message, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '✨ Generate'; }
    }
  }

  function _updateCharCount() {
    const r   = _getRoot();
    const val = r.querySelector('#content-output')?.value || '';
    const el  = r.querySelector('#content-charcount');
    if (el) {
      const color = val.length > 280 ? '#ef4444' : val.length > 240 ? '#f59e0b' : '#6b7280';
      el.innerHTML = `<span style="color:${color}">${val.length} chars</span>`
        + (val.length <= 280 ? ' ✓ Twitter OK' : ' ✗ Too long for Twitter');
    }
  }

  async function _saveToCalendar() {
    const r    = _getRoot();
    const text = r.querySelector('#content-output')?.value;
    const type = r.querySelector('#content-type')?.value;
    if (!text) return;
    await fetch('/api/presence/calendar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text, type, created_at: new Date().toISOString() }),
    });
    _loadCalendar();
    if (window.Shell) window.Shell.toast('Saved to calendar!', 'success');
  }

  async function _loadCalendar() {
    const r  = _getRoot();
    const el = r?.querySelector('#presence-calendar');
    if (!el) return;
    try {
      const resp = await fetch('/api/presence/calendar');
      if (!resp.ok) return;
      const { items } = await resp.json();
      if (!items?.length) {
        el.innerHTML = '<div class="presence-empty">No saved content yet.</div>';
        return;
      }
      el.innerHTML = items.slice(0, 20).map(item => `
        <div class="presence-cal-item ${item.posted ? 'posted' : ''}">
          <div class="presence-cal-meta">
            <span class="presence-cal-type">${_esc(item.type || 'post')}</span>
            <span class="presence-cal-date">${_esc(new Date(item.created_at).toLocaleDateString())}</span>
            ${item.posted ? '<span class="presence-cal-posted">✅ Posted</span>' : ''}
          </div>
          <div class="presence-cal-text">${_esc((item.text || '').substring(0, 120))}…</div>
          <div class="presence-cal-actions">
            <button class="presence-btn presence-btn--ghost presence-btn--xs"
              onclick="navigator.clipboard.writeText(${JSON.stringify(item.text || '')})">📋</button>
            ${!item.posted
              ? `<button class="presence-btn presence-btn--success presence-btn--xs"
                   onclick="PresenceView._markPosted('${_esc(item.id)}')">✅ Posted</button>`
              : ''}
            <button class="presence-btn presence-btn--danger presence-btn--xs"
              onclick="PresenceView._deleteItem('${_esc(item.id)}')">🗑</button>
          </div>
        </div>`).join('');
    } catch(e) {
      log && log.warn && log.warn('presence calendar load failed', e);
    }
  }

  async function _generateListing() {
    const r        = _getRoot();
    const platform = r.querySelector('#listing-platform')?.value;
    const service  = r.querySelector('#listing-service')?.value;
    const btn  = r.querySelector('#listing-generate-btn');
    const wrap = r.querySelector('#listing-output-wrap');
    const out  = r.querySelector('#listing-output');
    if (btn)  { btn.disabled = true; btn.textContent = '⏳ Generating (30-60s)…'; }
    if (wrap) wrap.hidden = false;
    if (out)  out.innerHTML = '<div class="presence-loading">✍️ Writing listing…</div>';
    try {
      const resp = await fetch('/api/presence/generate/listing', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ platform, service }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      const pricing = d.pricing || {};
      out.innerHTML = `
        <div class="presence-listing-section">
          <div class="presence-listing-label">Title
            <span class="presence-listing-chars">${(d.title || '').length}/80 chars</span>
          </div>
          <div class="presence-listing-value">
            ${_esc(d.title || '')}
            <button class="presence-btn presence-btn--ghost presence-btn--xs"
              onclick="navigator.clipboard.writeText(${JSON.stringify(d.title || '')})">📋</button>
          </div>
        </div>
        <div class="presence-listing-section">
          <div class="presence-listing-label">Description</div>
          <textarea class="presence-textarea" rows="8" style="width:100%">${_esc(d.description || '')}</textarea>
          <button class="presence-btn presence-btn--ghost presence-btn--xs" style="margin-top:4px"
            onclick="navigator.clipboard.writeText(this.previousElementSibling.value)">📋 Copy</button>
        </div>
        <div class="presence-listing-section">
          <div class="presence-listing-label">Tags</div>
          <div class="presence-tags">${(d.tags || []).map(t => `<span class="presence-tag">${_esc(t)}</span>`).join('')}</div>
          <button class="presence-btn presence-btn--ghost presence-btn--xs"
            onclick="navigator.clipboard.writeText(${JSON.stringify((d.tags || []).join(', '))})">📋 Copy tags</button>
        </div>
        ${Object.keys(pricing).length ? `
        <div class="presence-listing-section">
          <div class="presence-listing-label">Pricing</div>
          ${Object.entries(pricing).map(([tier, info]) =>
            `<div class="presence-pricing-row"><strong>${_esc(tier)}</strong>: ${_esc(typeof info === 'object' ? JSON.stringify(info) : String(info))}</div>`
          ).join('')}
        </div>` : ''}
        ${d.faq?.length ? `
        <div class="presence-listing-section">
          <div class="presence-listing-label">FAQ</div>
          ${d.faq.map(f =>
            `<div class="presence-faq-item"><strong>Q: ${_esc(f.question || '')}</strong><br>A: ${_esc(f.answer || '')}</div>`
          ).join('')}
        </div>` : ''}`;
    } catch(e) {
      if (out) out.innerHTML = `<div class="presence-error">Failed: ${_esc(e.message)}</div>`;
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '✨ Generate Listing'; }
    }
  }

  async function _loadPlatforms() {
    const r  = _getRoot();
    const el = r?.querySelector('#presence-platforms');
    if (!el) return;
    try {
      const resp = await fetch('/api/presence/platforms');
      if (!resp.ok) return;
      const { platforms } = await resp.json();
      const PLATS = ['Upwork', 'Fiverr', 'PeoplePerHour', 'Freelancer', 'LinkedIn'];
      el.innerHTML = PLATS.map(p => {
        const status = platforms[p] || 'not_created';
        return `<div class="presence-platform-row">
          <span class="presence-platform-name">${_esc(p)}</span>
          <select class="presence-select presence-select--sm"
            onchange="PresenceView._updatePlatform('${_esc(p)}', this.value)">
            <option value="not_created" ${status === 'not_created' ? 'selected' : ''}>⚪ Not created</option>
            <option value="draft"       ${status === 'draft'       ? 'selected' : ''}>📝 Draft</option>
            <option value="live"        ${status === 'live'        ? 'selected' : ''}>🟢 Live</option>
          </select>
        </div>`;
      }).join('');
    } catch(e) { /* silent */ }
  }

  async function _generateOutreach() {
    const r       = _getRoot();
    const target  = r.querySelector('#outreach-target')?.value;
    const service = r.querySelector('#outreach-service')?.value;
    const btn     = r.querySelector('#outreach-generate-btn');
    const wrap    = r.querySelector('#outreach-output-wrap');
    const loading = r.querySelector('#outreach-loading');
    if (btn)     { btn.disabled = true; btn.textContent = '⏳ Writing…'; }
    if (loading) loading.hidden = false;
    if (wrap)    wrap.hidden = true;
    try {
      const resp = await fetch('/api/presence/generate/outreach', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ target, service }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d       = await resp.json();
      const subj    = r.querySelector('#outreach-email-subject');
      const body    = r.querySelector('#outreach-email-body');
      const li      = r.querySelector('#outreach-linkedin');
      const liCount = r.querySelector('#outreach-linkedin-count');
      if (subj)    subj.textContent = d.email_subject || '';
      if (body)    body.value       = d.email_body || '';
      if (li)      li.value         = d.linkedin_message || '';
      if (liCount) liCount.textContent = `${(d.linkedin_message || '').length}/290 chars`;
      if (wrap)    wrap.hidden = false;
    } catch(e) {
      if (window.Shell) window.Shell.toast('Failed: ' + e.message, 'error');
    } finally {
      if (btn)     { btn.disabled = false; btn.textContent = '✨ Generate Outreach'; }
      if (loading) loading.hidden = true;
    }
  }

  window.PresenceView = {
    mount,
    unmount,
    focusContent() {
      const r = _getRoot();
      r?.querySelector('[data-tab="content"]')?.click();
    },
    async _markPosted(id) {
      await fetch(`/api/presence/calendar/${id}/posted`, { method: 'POST' });
      _loadCalendar();
    },
    async _deleteItem(id) {
      await fetch(`/api/presence/calendar/${id}`, { method: 'DELETE' });
      _loadCalendar();
    },
    async _updatePlatform(platform, status) {
      await fetch(`/api/presence/platforms/${encodeURIComponent(platform)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ status }),
      });
    },
  };
})();
