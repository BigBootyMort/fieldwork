/* Agent module view — window.AgentView = { mount, unmount } */
(function () {
  'use strict';

  // ── Domain templates ───────────────────────────────────────────────────────
  const DOMAIN_META = {
    dev: {
      label: 'Software Dev',
      pill:  '💻 Dev',
      color: '#3b82f6',
      examples: [
        'Clone https://github.com/OWNER/REPO, scan for SQL-injection vulnerabilities, write a bug report',
        'Write a Python FastAPI CRUD endpoint for a User model with JWT auth, save to api.py',
        'Audit the code in workspace/app.py for security issues and suggest hardened versions',
        'Create a shell script that automates Docker image build, test, and push to registry',
        'Find all open issues in the repo OWNER/REPO tagged "bug" and summarise them',
      ],
    },
    freelance: {
      label: 'Freelance',
      pill:  '💼 Freelance',
      color: '#8b5cf6',
      examples: [
        'Research the top 5 competitors to [company] and write a 500-word competitive analysis',
        'Draft a professional cover letter for a Senior DevOps Engineer role at [company]',
        'Write a detailed project proposal for a mobile e-commerce app (timeline + budget)',
        'Create a market research summary on AI-powered OSINT tools for a client report',
        'Summarise the content at [URL] and produce a structured executive briefing',
      ],
    },
    trading: {
      label: 'Trading',
      pill:  '📈 Trading',
      color: '#22c55e',
      examples: [
        'Fetch AAPL, TSLA, and NVDA prices, analyse momentum vs SMA20, identify the strongest buy signal',
        'Analyse BTC-USD and ETH-USD — compute trend strength and propose a position with stop-loss',
        'Review the macro picture for gold (GC=F) and silver (SI=F) and suggest a pairs trade',
        'Scan MSFT, GOOGL, META, AMZN for oversold conditions and generate entry/exit targets',
        'Fetch SPY and QQQ — identify market regime (bull/bear/range) and propose a hedge',
      ],
    },
  };

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    taskId:           null,
    status:           'idle',
    domain:           null,
    evtSource:        null,
    logCount:         0,
    currentIntercept: null,
    stepMap:          {},
  };

  let _mounted = false;

  // ── DOM refs ───────────────────────────────────────────────────────────────
  let $log, $badge, $steps, $stopBtn, $runBtn, $objective,
      $interceptPanel, $interceptCat, $interceptReason, $interceptStepDesc,
      $interceptNotes, $interceptApprove, $interceptReject,
      $planSummary, $logCount, $historyList, $clearBtn, $historyRefresh,
      $examples, $examplesList, $domainBadge,
      $capGitHub, $capAlpaca, $capOllama,
      $tradeCard, $tradeSide, $tradeSymbol, $tradeQty,
      $tradeType, $tradeTif, $tradeMode, $tradeMktCtx,
      $settingsOverlay, $settingsBody, $settingsSave, $settingsStatus,
      $settingsBtn, $settingsClose;

  // ── Helpers ────────────────────────────────────────────────────────────────

  function el(id) { return document.getElementById(id); }

  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _levelClass(level) {
    const l = (level || '').toLowerCase().replace(/\s+\d+$/, '');
    const map = {
      recon: 'recon', plan: 'plan', step: 'step', step_done: 'step_done',
      output: 'output', intercept: 'intercept', approved: 'approved',
      rejected: 'rejected', complete: 'complete', error: 'error', stop: 'stop',
    };
    return `agent-level--${map[l] || 'default'}`;
  }

  function _statusBadgeClass(s) {
    return {
      idle: 'agent-badge--idle', queued: 'agent-badge--queued',
      running: 'agent-badge--running', intercepted: 'agent-badge--intercepted',
      complete: 'agent-badge--complete', error: 'agent-badge--error',
      stopped: 'agent-badge--stopped',
    }[s] || 'agent-badge--idle';
  }

  function _statusText(s) {
    return { idle:'IDLE', queued:'QUEUED', running:'RUNNING',
             intercepted:'INTERCEPTED', complete:'COMPLETE',
             error:'ERROR', stopped:'STOPPED' }[s] || s.toUpperCase();
  }

  function _stepIcon(ss) {
    return { idle:'○', running:'⏳', done:'✓', error:'✕',
             intercepted:'⚠️', skipped:'⤵' }[ss] || '○';
  }

  function _nowStr() {
    return new Date().toTimeString().slice(0, 8);
  }

  // ── Status badge + button states ───────────────────────────────────────────

  function _updateBadge(status) {
    state.status = status;
    if (!$badge) return;
    $badge.className = `agent-badge ${_statusBadgeClass(status)}`;
    $badge.textContent = _statusText(status);
    const busy = ['running','intercepted','queued'].includes(status);
    if ($stopBtn) $stopBtn.disabled = !busy;
    if ($runBtn)  $runBtn.disabled  =  busy;
  }

  // ── Domain selector ────────────────────────────────────────────────────────

  function _selectDomain(domain) {
    state.domain = domain;
    const meta = DOMAIN_META[domain];

    // Highlight the clicked card
    document.querySelectorAll('.agent-domain-card').forEach(c => {
      c.classList.toggle('active', c.dataset.domain === domain);
      if (c.dataset.domain === domain && meta) {
        c.style.setProperty('--dom-color', meta.color);
      }
    });

    // Domain pill on the label
    if ($domainBadge && meta) {
      $domainBadge.textContent = meta.pill;
      $domainBadge.style.background = meta.color + '22';
      $domainBadge.style.color      = meta.color;
      $domainBadge.style.borderColor= meta.color + '55';
      $domainBadge.hidden = false;
    }

    // Render example prompts
    if ($examples && $examplesList && meta) {
      $examplesList.innerHTML = '';
      meta.examples.forEach(ex => {
        const btn = document.createElement('button');
        btn.className = 'agent-example-btn';
        btn.textContent = ex;
        btn.addEventListener('click', () => {
          if ($objective) {
            $objective.value = ex;
            $examples.hidden = true;
            $objective.focus();
          }
        });
        $examplesList.appendChild(btn);
      });
      $examples.hidden = false;
    }

    // Pre-focus objective
    $objective?.focus();
  }

  // ── Capability chips ───────────────────────────────────────────────────────

  async function _loadCapabilities() {
    try {
      const r = await fetch('/api/agent/capabilities');
      if (!r.ok) return;
      const caps = await r.json();

      if ($capGitHub) {
        $capGitHub.classList.toggle('agent-cap--ok',  caps.github);
        $capGitHub.classList.toggle('agent-cap--off', !caps.github);
        $capGitHub.title = caps.github ? 'GitHub: connected' : 'GitHub: no token set';
      }
      if ($capAlpaca) {
        const ok = caps.alpaca;
        $capAlpaca.classList.toggle('agent-cap--ok',   ok);
        $capAlpaca.classList.toggle('agent-cap--off', !ok);
        $capAlpaca.title = ok
          ? `Alpaca: ${caps.alpaca_mode} mode`
          : 'Alpaca: not configured';
        if (caps.alpaca_mode === 'live') $capAlpaca.classList.add('agent-cap--live');
      }
      if ($capOllama) {
        $capOllama.classList.add('agent-cap--ok');
        $capOllama.title = `Ollama: ${caps.ollama_model}`;
      }
    } catch {}
  }

  // ── Log rendering ──────────────────────────────────────────────────────────

  function appendLog(entry) {
    if (!$log) return;
    const empty = $log.querySelector('.agent-log-empty');
    if (empty) empty.remove();

    const level = entry.level || '';
    const row   = document.createElement('div');

    if (entry.market_data) {
      // Render as a compact price table
      row.className = 'agent-log-entry agent-log-entry--market';
      const rows = (entry.message || '').split('\n').filter(Boolean);
      row.innerHTML = `
        <span class="agent-log-ts">${_esc(entry.ts || '')}</span>
        <div class="agent-log-market-wrap">
          <div class="agent-log-level ${_levelClass(level)}">${_esc(level)}</div>
          <div class="agent-log-market">${
            rows.map(line => {
              const cls = line.includes('+') ? 'up' : line.includes('-') ? 'dn' : '';
              return `<div class="agent-mkt-row agent-mkt-row--${cls}">${_esc(line)}</div>`;
            }).join('')
          }</div>
        </div>`;
    } else if (entry.code_output) {
      // Code-style output block
      row.className = 'agent-log-entry agent-log-entry--code';
      row.innerHTML = `
        <span class="agent-log-ts">${_esc(entry.ts || '')}</span>
        <div class="agent-log-code-wrap">
          <span class="agent-log-level ${_levelClass(level)}">${_esc(level)}</span>
          <pre class="agent-log-code">${_esc(entry.message || '')}</pre>
        </div>`;
    } else {
      row.className = 'agent-log-entry';
      row.innerHTML = `
        <span class="agent-log-ts">${_esc(entry.ts || '')}</span>
        <span class="agent-log-level ${_levelClass(level)}">${_esc(level)}</span>
        <span class="agent-log-msg">${_esc(entry.message || '')}</span>`;
    }

    $log.appendChild(row);

    // Side-effects on special events
    if (entry.plan)                               _renderPlan(entry.plan);
    if (level === 'INTERCEPT' && entry.intercept) _showIntercept(entry.intercept);
    if (['APPROVED','REJECTED','COMPLETE','ERROR','STOP'].includes(level)) _hideIntercept();
    if (['RUNNING','INTERCEPTED'].includes(level.toUpperCase())) {
      _updateBadge(level.toLowerCase());
    }
    if (level === 'COMPLETE') _updateBadge('complete');
    if (level === 'ERROR')    _updateBadge('error');
    _handleStepEvent(entry);

    state.logCount++;
    if ($logCount) $logCount.textContent = `${state.logCount} lines`;
    $log.scrollTop = $log.scrollHeight;
  }

  // ── Plan rendering ─────────────────────────────────────────────────────────

  function _renderPlan(plan) {
    if (!$steps || !plan || !Array.isArray(plan.steps)) return;
    $steps.innerHTML = '';
    state.stepMap = {};
    if ($planSummary) $planSummary.textContent = plan.summary || '';

    const ATYPE_ICONS = {
      bash: '⬛', web_request: '🌐', analysis: '🧠', report: '📄',
      file_write: '✏️', file_read: '📖', code_exec: '⚙️', git_op: '🌿',
      github_api: '🐙', market_data: '📊', trade_order: '💰',
    };

    plan.steps.forEach(step => {
      const li = document.createElement('li');
      li.className = 'agent-step';
      li.dataset.stepId = step.id;
      const icon = ATYPE_ICONS[step.action_type] || '○';
      li.innerHTML = `
        <span class="agent-step-icon">○</span>
        <div class="agent-step-body">
          <div class="agent-step-num">Step ${_esc(String(step.id))}</div>
          <div class="agent-step-desc">${_esc(step.description || '')}</div>
          <span class="agent-step-type">${icon} ${_esc(step.action_type || '')}</span>
          ${step.requires_intercept
            ? '<span class="agent-step-type agent-step-type--intercept">⚠ intercept</span>'
            : ''}
        </div>`;
      $steps.appendChild(li);
      state.stepMap[step.id] = li;
    });
  }

  function _setStepState(stepId, stepStatus) {
    const li = state.stepMap[stepId];
    if (!li) return;
    li.classList.remove('agent-step--running','agent-step--done','agent-step--error',
                        'agent-step--intercepted','agent-step--skipped');
    if (stepStatus !== 'idle') li.classList.add(`agent-step--${stepStatus}`);
    const iconEl = li.querySelector('.agent-step-icon');
    if (iconEl) iconEl.textContent = _stepIcon(stepStatus);
  }

  function _handleStepEvent(entry) {
    const level  = entry.level || '';
    const stepId = entry.step_id;
    if (!stepId) return;
    if (level.startsWith('STEP ') && !level.includes('DONE')) _setStepState(stepId, 'running');
    if (level === 'STEP_DONE')  _setStepState(stepId, 'done');
    if (level === 'INTERCEPT')  _setStepState(stepId, 'intercepted');
    if (level === 'APPROVED')   _setStepState(stepId, 'running');
    if (level === 'REJECTED')   _setStepState(stepId, 'skipped');
    if (level === 'ERROR')      _setStepState(stepId, 'error');
  }

  // ── Intercept panel ────────────────────────────────────────────────────────

  function _showIntercept(idata) {
    state.currentIntercept = idata;
    if (!$interceptPanel) return;
    $interceptPanel.hidden = false;

    if ($interceptCat)       $interceptCat.textContent       = idata.category || 'UNKNOWN';
    if ($interceptReason)    $interceptReason.textContent    = idata.reason   || '';
    if ($interceptStepDesc)  $interceptStepDesc.textContent  = idata.step?.description || '';
    if ($interceptNotes)     $interceptNotes.value           = '';

    // Trade card
    const trade = idata.trade || {};
    if ($tradeCard && Object.keys(trade).length && idata.category === 'FINANCIAL_TRANSACTION') {
      const side = (trade.side || 'buy').toUpperCase();
      if ($tradeSide) {
        $tradeSide.textContent = side;
        $tradeSide.className   = `agent-trade-side agent-trade-side--${side.toLowerCase()}`;
      }
      if ($tradeSymbol)  $tradeSymbol.textContent  = trade.symbol   || '';
      if ($tradeQty)     $tradeQty.textContent      = `× ${trade.qty || '?'}`;
      if ($tradeType)    $tradeType.textContent     = (trade.type || 'market').toUpperCase();
      if ($tradeTif)     $tradeTif.textContent      = trade.time_in_force || 'day';
      if ($tradeMode) {
        const isLive = idata.alpaca_mode === 'live';
        $tradeMode.textContent = isLive ? '🔴 LIVE MONEY' : '📄 PAPER TRADING';
        $tradeMode.className   = `agent-trade-val ${isLive ? 'agent-trade-live' : 'agent-trade-paper'}`;
      }
      if ($tradeMktCtx && idata.market_ctx) {
        $tradeMktCtx.textContent = idata.market_ctx;
        $tradeMktCtx.hidden = false;
      } else if ($tradeMktCtx) {
        $tradeMktCtx.hidden = true;
      }
      $tradeCard.hidden = false;
    } else if ($tradeCard) {
      $tradeCard.hidden = true;
    }
  }

  function _hideIntercept() {
    state.currentIntercept = null;
    if ($interceptPanel) $interceptPanel.hidden = true;
    if ($tradeCard)      $tradeCard.hidden      = true;
    if ($interceptNotes) $interceptNotes.value  = '';
  }

  async function _respondIntercept(approved) {
    if (!state.taskId || !state.currentIntercept) return;
    const notes = $interceptNotes?.value?.trim() || null;
    try {
      await fetch(`/api/agent/tasks/${state.taskId}/intercept/respond`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ approved, notes }),
      });
    } catch (e) {
      console.error('Agent intercept respond failed:', e);
    }
    _hideIntercept();
  }

  // ── SSE stream ─────────────────────────────────────────────────────────────

  function _connectStream(taskId) {
    if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
    const es = new EventSource(`/api/agent/tasks/${taskId}/stream`);
    state.evtSource = es;

    es.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === 'done') {
        es.close(); state.evtSource = null;
        _fetchTaskStatus(taskId);
        _loadHistory();
        return;
      }
      if (msg.type === 'heartbeat') return;
      appendLog(msg);
    };
    es.onerror = () => {};
  }

  async function _fetchTaskStatus(taskId) {
    try {
      const r = await fetch(`/api/agent/tasks/${taskId}`);
      if (!r.ok) return;
      const t = await r.json();
      _updateBadge(t.status || 'idle');
    } catch {}
  }

  // ── Run a new task ─────────────────────────────────────────────────────────

  async function _runTask() {
    const objective = $objective?.value?.trim();
    if (!objective) { $objective?.focus(); return; }
    if (['running','intercepted','queued'].includes(state.status)) return;

    _clearLog();
    _hideIntercept();
    _clearPlan();
    state.logCount = 0;
    if ($logCount) $logCount.textContent = '';
    if ($examples) $examples.hidden = true;

    try {
      const r = await fetch('/api/agent/tasks', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ objective, domain: state.domain }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      state.taskId = data.task_id;
      _updateBadge('queued');
      _connectStream(state.taskId);
      _loadHistory();
    } catch (e) {
      appendLog({ ts: _nowStr(), level: 'ERROR', message: `Failed to create task: ${e}` });
    }
  }

  function _clearLog() {
    if (!$log) return;
    $log.innerHTML = '<div class="agent-log-empty">No task running. Select a domain or describe an objective above.</div>';
    state.logCount = 0;
    if ($logCount) $logCount.textContent = '';
  }

  function _clearPlan() {
    if (!$steps) return;
    state.stepMap = {};
    $steps.innerHTML = '<li class="agent-step-placeholder">Plan will appear once the LLM generates it…</li>';
    if ($planSummary) $planSummary.textContent = '';
  }

  // ── Stop task ──────────────────────────────────────────────────────────────

  async function _stopTask() {
    if (!state.taskId) return;
    try {
      await fetch(`/api/agent/tasks/${state.taskId}/stop`, { method: 'POST' });
    } catch {}
    if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
    _updateBadge('stopped');
    _hideIntercept();
    _loadHistory();
  }

  // ── Task history ───────────────────────────────────────────────────────────

  async function _loadHistory() {
    if (!$historyList) return;
    try {
      const r = await fetch('/api/agent/tasks');
      if (!r.ok) return;
      const { tasks } = await r.json();
      _renderHistory(tasks || []);
    } catch {}
  }

  function _renderHistory(tasks) {
    if (!$historyList) return;
    if (!tasks.length) {
      $historyList.innerHTML = '<li class="agent-history-empty">No tasks yet.</li>';
      return;
    }
    $historyList.innerHTML = '';
    const DOMAIN_ICONS = { dev:'💻', freelance:'💼', trading:'📈' };
    tasks.slice(0, 30).forEach(t => {
      const li = document.createElement('li');
      li.className = `agent-history-item${t.id === state.taskId ? ' active' : ''}`;
      const dicon = DOMAIN_ICONS[t.domain] || '';
      li.innerHTML = `
        <span class="agent-history-dot agent-history-dot--${t.status}"></span>
        <span class="agent-history-obj" title="${_esc(t.objective)}">${dicon ? dicon + ' ' : ''}${_esc(t.objective)}</span>
        <span class="agent-history-ts">${_esc(t.created_at || '')}</span>`;
      li.addEventListener('click', () => _resumeTask(t.id));
      $historyList.appendChild(li);
    });
  }

  async function _resumeTask(taskId) {
    if (taskId === state.taskId && state.evtSource) return;
    if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
    _clearLog();
    _clearPlan();
    _hideIntercept();
    state.taskId = taskId;

    try {
      const r = await fetch(`/api/agent/tasks/${taskId}`);
      if (r.ok) {
        const t = await r.json();
        _updateBadge(t.status || 'idle');
        if (t.plan) _renderPlan(t.plan);
        if (t.objective && $objective) $objective.value = t.objective;
        if (t.domain) _selectDomain(t.domain);
      }
    } catch {}

    _connectStream(taskId);
    _loadHistory();
  }

  // ── Settings drawer ────────────────────────────────────────────────────────

  const GROUP_ORDER = ['GitHub', 'Alpaca Trading'];
  const GROUP_ICONS = { 'GitHub': '🐙', 'Alpaca Trading': '📈' };
  const GROUP_LINKS = {
    'GitHub':        { label: 'Create token ↗', url: 'https://github.com/settings/tokens/new?scopes=repo,read:org' },
    'Alpaca Trading':{ label: 'Open dashboard ↗', url: 'https://app.alpaca.markets' },
  };

  async function _openSettings() {
    if (!$settingsOverlay) return;
    $settingsOverlay.hidden = false;
    await _loadSettings();
  }

  function _closeSettings() {
    if ($settingsOverlay) $settingsOverlay.hidden = true;
    if ($settingsStatus)  $settingsStatus.textContent = '';
    if ($settingsSave)    $settingsSave.disabled = true;
  }

  async function _loadSettings() {
    if (!$settingsBody) return;
    $settingsBody.innerHTML = '<div class="agent-settings-loading">Loading…</div>';
    try {
      const r = await fetch('/api/agent/settings');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const { settings } = await r.json();
      _renderSettings(settings);
    } catch (e) {
      $settingsBody.innerHTML = `<div class="agent-settings-error">Failed to load settings: ${_esc(String(e))}</div>`;
    }
  }

  function _renderSettings(items) {
    if (!$settingsBody) return;
    $settingsBody.innerHTML = '';

    // Group items
    const groups = {};
    items.forEach(item => {
      if (!groups[item.group]) groups[item.group] = [];
      groups[item.group].push(item);
    });

    const orderedGroups = [...GROUP_ORDER, ...Object.keys(groups).filter(g => !GROUP_ORDER.includes(g))];

    orderedGroups.forEach(groupName => {
      const groupItems = groups[groupName];
      if (!groupItems) return;

      const section = document.createElement('div');
      section.className = 'agent-settings-group';

      const link = GROUP_LINKS[groupName];
      section.innerHTML = `
        <div class="agent-settings-group-head">
          <span class="agent-settings-group-icon">${GROUP_ICONS[groupName] || '🔧'}</span>
          <span class="agent-settings-group-name">${_esc(groupName)}</span>
          ${link ? `<a class="agent-settings-group-link" href="${_esc(link.url)}" target="_blank" rel="noopener">${_esc(link.label)}</a>` : ''}
        </div>`;

      groupItems.forEach(item => {
        const row = document.createElement('div');
        row.className = 'agent-settings-row';
        const isLive = item.key === 'ALPACA_BASE_URL' && item.display && !item.display.includes('paper');

        row.innerHTML = `
          <div class="agent-settings-row-top">
            <span class="agent-settings-label">${_esc(item.label)}</span>
            <span class="agent-settings-badge ${item.set ? 'agent-settings-badge--set' : 'agent-settings-badge--unset'}">
              ${item.set ? '🟢 configured' : '⚪ not set'}
            </span>
            ${item.set ? `<button class="agent-settings-clear" data-key="${_esc(item.key)}" title="Clear this value">✕</button>` : ''}
          </div>
          ${item.set && item.kind === 'password' ? `<div class="agent-settings-masked">${_esc(item.display)}</div>` : ''}
          <input
            class="agent-settings-input"
            data-key="${_esc(item.key)}"
            type="${item.kind === 'password' ? 'password' : 'text'}"
            placeholder="${item.set ? (item.kind === 'password' ? 'Paste new value to replace…' : '') : 'Paste value…'}"
            value="${item.kind !== 'password' ? _esc(item.display || item.default || '') : ''}"
            autocomplete="off"
            spellcheck="false"
          />
          <div class="agent-settings-note">
            ${_esc(item.note)}
            ${isLive ? '<span class="agent-settings-live-warn">⚠️ Live money mode!</span>' : ''}
          </div>`;

        // "Clear" button
        row.querySelector('.agent-settings-clear')?.addEventListener('click', async (e) => {
          const key = e.currentTarget.dataset.key;
          try {
            await fetch(`/api/agent/settings/${key}`, { method: 'DELETE' });
            await _loadSettings();
            await _loadCapabilities();
            if ($settingsStatus) {
              $settingsStatus.textContent = `${key} cleared.`;
              setTimeout(() => { if ($settingsStatus) $settingsStatus.textContent = ''; }, 3000);
            }
          } catch {}
        });

        // Warn live mode on ALPACA_BASE_URL change
        if (item.key === 'ALPACA_BASE_URL') {
          const inp = row.querySelector('.agent-settings-input');
          inp?.addEventListener('input', () => {
            const noteEl = row.querySelector('.agent-settings-note');
            if (noteEl) {
              const isLiveNow = inp.value && !inp.value.includes('paper');
              const existing = noteEl.querySelector('.agent-settings-live-warn');
              if (isLiveNow && !existing) {
                noteEl.insertAdjacentHTML('beforeend', '<span class="agent-settings-live-warn">⚠️ Live money mode!</span>');
              } else if (!isLiveNow && existing) {
                existing.remove();
              }
            }
          });
        }

        section.appendChild(row);
      });

      $settingsBody.appendChild(section);
    });

    // Ollama info (read-only, always at the bottom)
    const ollamaDiv = document.createElement('div');
    ollamaDiv.className = 'agent-settings-group agent-settings-group--info';
    ollamaDiv.innerHTML = `
      <div class="agent-settings-group-head">
        <span class="agent-settings-group-icon">🤖</span>
        <span class="agent-settings-group-name">Ollama LLM</span>
      </div>
      <div class="agent-settings-row">
        <div class="agent-settings-note">
          Model is set via <code>OLLAMA_MODEL</code> in docker-compose.yml.
          The endpoint is set via <code>OLLAMA_URL</code>. These take effect on container restart.
        </div>
      </div>`;
    $settingsBody.appendChild(ollamaDiv);

    // Wire up input change detection
    $settingsBody.querySelectorAll('.agent-settings-input').forEach(inp => {
      inp.addEventListener('input', () => {
        const anyChanged = [...$settingsBody.querySelectorAll('.agent-settings-input')]
          .some(i => i.value.trim());
        if ($settingsSave) $settingsSave.disabled = !anyChanged;
      });
    });
  }

  async function _saveSettings() {
    if (!$settingsBody || !$settingsSave) return;
    const updates = {};
    $settingsBody.querySelectorAll('.agent-settings-input').forEach(inp => {
      const v = inp.value.trim();
      if (v) updates[inp.dataset.key] = v;
    });
    if (!Object.keys(updates).length) return;

    $settingsSave.disabled   = true;
    $settingsSave.textContent = '⏳ Saving…';
    if ($settingsStatus) $settingsStatus.textContent = '';

    try {
      const r = await fetch('/api/agent/settings', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ updates }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if ($settingsStatus) {
        $settingsStatus.textContent = `✓ ${d.updated.length} setting(s) saved.`;
        $settingsStatus.className = 'agent-settings-status agent-settings-status--ok';
        setTimeout(() => { if ($settingsStatus) { $settingsStatus.textContent=''; $settingsStatus.className='agent-settings-status'; } }, 4000);
      }
      await _loadSettings();
      await _loadCapabilities();
    } catch (e) {
      if ($settingsStatus) {
        $settingsStatus.textContent = `✗ Save failed: ${e}`;
        $settingsStatus.className = 'agent-settings-status agent-settings-status--err';
      }
    }
    $settingsSave.textContent = '💾 Save changes';
    $settingsSave.disabled    = true;
  }

  // ── Mount / unmount ────────────────────────────────────────────────────────

  async function mount(root) {
    if (_mounted) return;
    _mounted = true;

    const r = await fetch('/modules/agent/view.html');
    root.innerHTML = await r.text();

    $log              = el('agent-log');
    $badge            = el('agent-status-badge');
    $steps            = el('agent-steps');
    $stopBtn          = el('agent-stop-btn');
    $runBtn           = el('agent-run-btn');
    $objective        = el('agent-objective');
    $interceptPanel   = el('agent-intercept-panel');
    $interceptCat     = el('agent-intercept-cat');
    $interceptReason  = el('agent-intercept-reason');
    $interceptStepDesc= el('agent-intercept-step-desc');
    $interceptNotes   = el('agent-intercept-notes');
    $interceptApprove = el('agent-intercept-approve');
    $interceptReject  = el('agent-intercept-reject');
    $planSummary      = el('agent-plan-summary');
    $logCount         = el('agent-log-count');
    $historyList      = el('agent-history-list');
    $clearBtn         = el('agent-clear-btn');
    $historyRefresh   = el('agent-history-refresh');
    $examples         = el('agent-examples');
    $examplesList     = el('agent-examples-list');
    $domainBadge      = el('agent-domain-badge');
    $capGitHub        = el('agent-cap-github');
    $capAlpaca        = el('agent-cap-alpaca');
    $capOllama        = el('agent-cap-ollama');
    $tradeCard        = el('agent-trade-card');
    $tradeSide        = el('agent-trade-side');
    $tradeSymbol      = el('agent-trade-symbol');
    $tradeQty         = el('agent-trade-qty');
    $tradeType        = el('agent-trade-type');
    $tradeTif         = el('agent-trade-tif');
    $tradeMode        = el('agent-trade-mode');
    $tradeMktCtx      = el('agent-trade-mktctx');
    $settingsOverlay  = el('agent-settings-overlay');
    $settingsBody     = el('agent-settings-body');
    $settingsSave     = el('agent-settings-save');
    $settingsStatus   = el('agent-settings-status');
    $settingsBtn      = el('agent-settings-btn');
    $settingsClose    = el('agent-settings-close');

    // Domain cards
    document.querySelectorAll('.agent-domain-card').forEach(card => {
      card.addEventListener('click', () => _selectDomain(card.dataset.domain));
    });

    // Hide examples when user starts typing
    $objective?.addEventListener('input', () => {
      if ($objective.value.trim() && $examples) $examples.hidden = true;
    });

    $runBtn?.addEventListener('click', _runTask);
    $stopBtn?.addEventListener('click', _stopTask);
    $clearBtn?.addEventListener('click', _clearLog);
    $historyRefresh?.addEventListener('click', _loadHistory);
    $interceptApprove?.addEventListener('click', () => _respondIntercept(true));
    $interceptReject?.addEventListener('click',  () => _respondIntercept(false));
    $settingsBtn?.addEventListener('click', _openSettings);
    $settingsClose?.addEventListener('click', _closeSettings);
    $settingsSave?.addEventListener('click', _saveSettings);
    // Close drawer on overlay background click
    $settingsOverlay?.addEventListener('click', e => {
      if (e.target === $settingsOverlay) _closeSettings();
    });

    $objective?.addEventListener('keydown', e => {
      if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); _runTask(); }
    });

    _updateBadge('idle');
    _loadCapabilities();
    _loadHistory();
  }

  function unmount() {
    if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
    _mounted = false;
    state.taskId = null;
    state.status = 'idle';
    state.domain = null;
    state.logCount = 0;
    state.stepMap = {};
    state.currentIntercept = null;
  }

  window.AgentView = { mount, unmount };
})();
