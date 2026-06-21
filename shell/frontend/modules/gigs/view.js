(function() {
  'use strict';

  let _mounted = false;
  let _currentFilter = 'all';
  let _minScore = 0;
  let _currentGigId = null;

  // DOM refs
  let $list, $refreshBtn, $statsBtn, $statsPanel, $statsbody,
      $newBadge, $proposalDrawer, $proposalText, $proposalLoading,
      $proposalTitle, $proposalClose, $proposalCopy, $proposalApplied;

  function _esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _platformBadge(platform) {
    const map = {
      'upwork':             { label: 'Upwork',         color: '#14a800' },
      'reddit_forhire':     { label: 'r/forhire',      color: '#ff4500' },
      'reddit_slavelabour': { label: 'r/slavelabour',  color: '#ff4500' },
      'pph':                { label: 'PPH',             color: '#f7971e' },
      'freelancer':         { label: 'Freelancer',      color: '#0066cc' },
    };
    const p = map[platform] || { label: platform, color: '#888' };
    return `<span class="gigs-platform-badge" style="background:${p.color}22;color:${p.color};border-color:${p.color}44">${_esc(p.label)}</span>`;
  }

  function _scoreBar(score) {
    const color = score >= 70 ? '#22c55e' : score >= 40 ? '#f59e0b' : '#6b7280';
    return `<div class="gigs-score-bar-wrap" title="Relevance score: ${score}/100">
      <div class="gigs-score-bar" style="width:${score}%;background:${color}"></div>
      <span class="gigs-score-label" style="color:${color}">${score}</span>
    </div>`;
  }

  function _renderGigCard(gig) {
    const timeAgo = gig.posted_at ? _timeAgo(gig.posted_at) : '';
    return `
      <div class="gigs-card" data-id="${_esc(gig.id)}" data-status="${_esc(gig.status)}">
        <div class="gigs-card-head">
          ${_platformBadge(gig.platform)}
          ${timeAgo ? `<span class="gigs-time">${_esc(timeAgo)}</span>` : ''}
          <span class="gigs-status-chip gigs-status-${_esc(gig.status)}">${_esc(gig.status)}</span>
        </div>
        <div class="gigs-card-title">
          <a href="${_esc(gig.url)}" target="_blank" rel="noopener">${_esc(gig.title)}</a>
        </div>
        ${gig.budget ? `<div class="gigs-budget">💰 ${_esc(gig.budget)}</div>` : ''}
        ${_scoreBar(gig.score)}
        <div class="gigs-card-desc">${_esc((gig.description||'').substring(0,200))}…</div>
        <div class="gigs-card-actions">
          <button class="gigs-btn gigs-btn--primary gigs-btn--sm" onclick="GigsView.openProposal('${_esc(gig.id)}')">✍️ Draft Proposal</button>
          ${gig.status === 'new' ? `
            <button class="gigs-btn gigs-btn--success gigs-btn--sm" onclick="GigsView.setStatus('${_esc(gig.id)}','applied')">📤 Applied</button>
            <button class="gigs-btn gigs-btn--ghost gigs-btn--sm" onclick="GigsView.setStatus('${_esc(gig.id)}','skipped')">🚫 Skip</button>
          ` : ''}
          ${gig.status === 'applied' ? `
            <button class="gigs-btn gigs-btn--success gigs-btn--sm" onclick="GigsView.setStatus('${_esc(gig.id)}','won')">🏆 Won</button>
            <button class="gigs-btn gigs-btn--danger gigs-btn--sm" onclick="GigsView.setStatus('${_esc(gig.id)}','lost')">❌ Lost</button>
          ` : ''}
        </div>
      </div>`;
  }

  function _timeAgo(isoStr) {
    if (!isoStr) return '';
    const diff = Date.now() - new Date(isoStr).getTime();
    const h = Math.floor(diff / 3600000);
    if (h < 1) return 'just now';
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h/24)}d ago`;
  }

  async function _loadGigs() {
    if (!$list) return;
    try {
      const params = new URLSearchParams({ status: _currentFilter, min_score: _minScore });
      const r = await fetch(`/api/gigs/list?${params}`);
      if (!r.ok) return;
      const { gigs, counts } = await r.json();

      const newCount = counts?.new || 0;
      if ($newBadge) {
        $newBadge.textContent = `${newCount} NEW`;
        $newBadge.hidden = newCount === 0;
      }

      if (!gigs || !gigs.length) {
        $list.innerHTML = `<div class="gigs-empty">
          No gigs found${_currentFilter !== 'all' ? ` with status "${_currentFilter}"` : ''}.
          <br><button class="gigs-btn gigs-btn--primary" style="margin-top:1rem" onclick="GigsView.refresh()">🔄 Refresh now</button>
        </div>`;
        return;
      }

      $list.innerHTML = gigs.map(_renderGigCard).join('');
    } catch(e) {
      $list.innerHTML = `<div class="gigs-empty gigs-error">Failed to load: ${_esc(String(e))}</div>`;
    }
  }

  async function _loadStats() {
    if (!$statsbody) return;
    try {
      const r = await fetch('/api/gigs/stats');
      if (!r.ok) return;
      const d = await r.json();
      $statsbody.innerHTML = `
        <div class="gigs-stats-grid">
          <div class="gigs-stat"><div class="gigs-stat-val">${d.total||0}</div><div class="gigs-stat-lbl">Total</div></div>
          <div class="gigs-stat"><div class="gigs-stat-val" style="color:#22c55e">${(d.by_status||{}).won||0}</div><div class="gigs-stat-lbl">Won</div></div>
          <div class="gigs-stat"><div class="gigs-stat-val" style="color:#f59e0b">${(d.by_status||{}).applied||0}</div><div class="gigs-stat-lbl">Applied</div></div>
          <div class="gigs-stat"><div class="gigs-stat-val">${Math.round(d.avg_score||0)}</div><div class="gigs-stat-lbl">Avg Score</div></div>
        </div>
        ${d.top_gigs?.length ? `<div style="margin-top:1rem;font-size:0.8rem;color:var(--text-dim)">Top opportunities: ${d.top_gigs.map(g=>`<strong>${_esc(g.title?.substring(0,40)||'')}</strong>`).join(', ')}</div>` : ''}`;
    } catch {}
  }

  async function mount(root) {
    if (_mounted) return;
    _mounted = true;
    const r = await fetch('/modules/gigs/view.html');
    root.innerHTML = await r.text();

    $list            = document.getElementById('gigs-list');
    $refreshBtn      = document.getElementById('gigs-refresh-btn');
    $statsBtn        = document.getElementById('gigs-stats-btn');
    $statsPanel      = document.getElementById('gigs-stats-panel');
    $statsbody       = document.getElementById('gigs-stats-body');
    $newBadge        = document.getElementById('gigs-new-badge');
    $proposalDrawer  = document.getElementById('gigs-proposal-drawer');
    $proposalText    = document.getElementById('gigs-proposal-text');
    $proposalLoading = document.getElementById('gigs-proposal-loading');
    $proposalTitle   = document.getElementById('gigs-proposal-gig-title');
    $proposalClose   = document.getElementById('gigs-proposal-close');
    $proposalCopy    = document.getElementById('gigs-proposal-copy');
    $proposalApplied = document.getElementById('gigs-proposal-applied');

    $refreshBtn?.addEventListener('click', () => GigsView.refresh());
    $statsBtn?.addEventListener('click', () => {
      if (!$statsPanel) return;
      $statsPanel.hidden = !$statsPanel.hidden;
      if (!$statsPanel.hidden) _loadStats();
    });
    $proposalClose?.addEventListener('click', () => { if ($proposalDrawer) $proposalDrawer.hidden = true; });
    $proposalCopy?.addEventListener('click', () => {
      navigator.clipboard.writeText($proposalText?.value || '').then(() => {
        if ($proposalCopy) { $proposalCopy.textContent = '✅ Copied!'; setTimeout(() => { $proposalCopy.textContent = '📋 Copy to clipboard'; }, 2000); }
      });
    });
    $proposalApplied?.addEventListener('click', () => {
      if (_currentGigId) GigsView.setStatus(_currentGigId, 'applied');
      if ($proposalDrawer) $proposalDrawer.hidden = true;
    });

    document.querySelectorAll('.gigs-filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.gigs-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        _currentFilter = btn.dataset.status || 'all';
        _loadGigs();
      });
    });

    _loadGigs();
  }

  function unmount() {
    _mounted = false;
    _currentFilter = 'all';
    _minScore = 0;
    _currentGigId = null;
  }

  window.GigsView = {
    mount, unmount,

    async refresh() {
      if ($refreshBtn) { $refreshBtn.disabled = true; $refreshBtn.textContent = '⏳ Refreshing…'; }
      try {
        const r = await fetch('/api/gigs/refresh', { method: 'POST' });
        const d = await r.json();
        if (d.added > 0 && window.Shell) window.Shell.toast(`Found ${d.added} new gig(s)!`, 'success');
        _loadGigs();
      } catch(e) {
        if (window.Shell) window.Shell.toast('Refresh failed: ' + e.message, 'error');
      } finally {
        if ($refreshBtn) { $refreshBtn.disabled = false; $refreshBtn.textContent = '🔄 Refresh'; }
      }
    },

    async openProposal(gigId) {
      _currentGigId = gigId;
      if (!$proposalDrawer) return;
      $proposalDrawer.hidden = false;
      if ($proposalLoading) $proposalLoading.hidden = false;
      if ($proposalText) { $proposalText.hidden = true; $proposalText.value = ''; }

      try {
        const r = await fetch(`/api/gigs/${gigId}`);
        if (r.ok) {
          const gig = await r.json();
          if ($proposalTitle) $proposalTitle.textContent = gig.title || '';
          if (gig.proposal_draft) {
            if ($proposalText) { $proposalText.value = gig.proposal_draft; $proposalText.hidden = false; }
            if ($proposalLoading) $proposalLoading.hidden = true;
            return;
          }
        }
        const dr = await fetch(`/api/gigs/${gigId}/draft`, { method: 'POST' });
        if (!dr.ok) throw new Error(`HTTP ${dr.status}`);
        const { proposal } = await dr.json();
        if ($proposalText) { $proposalText.value = proposal; $proposalText.hidden = false; }
        if ($proposalLoading) $proposalLoading.hidden = true;
      } catch(e) {
        if ($proposalLoading) $proposalLoading.textContent = `❌ Failed: ${e.message}`;
      }
    },

    async setStatus(gigId, status) {
      try {
        await fetch(`/api/gigs/${gigId}/status`, {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ status }),
        });
        _loadGigs();
      } catch {}
    },

    setMinScore(val) {
      _minScore = parseInt(val) || 0;
      const lbl = document.getElementById('gigs-min-score-label');
      if (lbl) lbl.textContent = `${_minScore}+`;
      _loadGigs();
    },
  };
})();
