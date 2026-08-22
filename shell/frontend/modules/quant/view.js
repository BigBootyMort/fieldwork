(function () {
  'use strict';

  let _code = null;          // current generated strategy code (null = builtin ema_cross)
  let _lastCfg = null;       // last backtest config (for saving)
  let _lastMetrics = null;
  let _library = [];

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  async function _api(path, opts) {
    const r = await fetch('/api/quant' + path, opts);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // ── engine status ──────────────────────────────────────────────────────────
  async function _loadEngine() {
    const chip = $('quant-engine-chip');
    try {
      const d = await _api('/engine');
      if (d.ok) {
        chip.textContent = `engine ✓ nautilus ${d.nautilus}` + (d.llm_key ? '' : ' · no LLM key');
        chip.className = 'quant-chip quant-chip--ok';
      } else {
        chip.textContent = 'engine offline';
        chip.className = 'quant-chip quant-chip--err';
      }
    } catch {
      chip.textContent = 'engine offline';
      chip.className = 'quant-chip quant-chip--err';
    }
  }

  // ── composer ────────────────────────────────────────────────────────────────
  async function _generate() {
    const prompt = $('quant-prompt').value.trim();
    if (!prompt) { $('quant-validate').textContent = 'Enter a description first.'; return; }
    const btn = $('quant-gen-btn'), vd = $('quant-validate');
    btn.disabled = true; btn.textContent = '✨ Generating…'; vd.textContent = '';
    try {
      const g = await _api('/generate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt }),
      });
      if (!g.ok) { vd.textContent = '✗ ' + (g.reason || 'generation failed'); vd.className = 'quant-validate quant-bad'; return; }
      _code = g.code;
      const pre = $('quant-code'); pre.textContent = g.code; pre.hidden = false;
      // auto-validate
      const v = await _api('/validate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: g.code }),
      });
      if (v.valid) { vd.textContent = '✓ valid — ready to backtest'; vd.className = 'quant-validate quant-good'; }
      else { vd.textContent = '✗ invalid: ' + (v.reason || ''); vd.className = 'quant-validate quant-bad'; }
    } catch (e) {
      vd.textContent = '✗ ' + e.message; vd.className = 'quant-validate quant-bad';
    } finally {
      btn.disabled = false; btn.textContent = '✨ Generate';
    }
  }

  function _useTemplate() {
    _code = null;
    $('quant-code').hidden = true;
    const vd = $('quant-validate');
    vd.textContent = '✓ using built-in EMA-cross (10/30) — ready to backtest';
    vd.className = 'quant-validate quant-good';
  }

  // ── backtest ─────────────────────────────────────────────────────────────────
  async function _runBacktest() {
    const symbol = $('quant-symbol').value;
    const interval = $('quant-interval').value;
    const limit = parseInt($('quant-limit').value, 10) || 500;
    const body = { symbol, interval, limit };
    if (_code) body.code = _code;
    else body.params = { fast: 10, slow: 30, trade_size: '0.10' };

    const st = $('quant-bt-status'), btn = $('quant-run-btn');
    btn.disabled = true; btn.textContent = '▶ Running…';
    st.textContent = `Fetching ${symbol}/USDT ${interval} and running NautilusTrader… (can take ~30s)`;
    $('quant-metrics').hidden = true; $('quant-curve').hidden = true; $('quant-save-row').hidden = true;
    try {
      const d = await _api('/backtest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!d.ok) { st.textContent = '✗ ' + (d.reason || 'backtest failed'); return; }
      _lastMetrics = d;
      _lastCfg = { symbol, interval, code: _code, params: body.params };
      st.textContent = `Done — ${d.bars} bars, ${d.num_trades} trade(s).`;
      _renderMetrics(d);
      _drawCurve(d.equity_curve || []);
      $('quant-save-row').hidden = false;
    } catch (e) {
      st.textContent = '✗ ' + e.message;
    } finally {
      btn.disabled = false; btn.textContent = '▶ Run backtest';
    }
  }

  function _fmt(v, kind) {
    if (v === null || v === undefined) return '—';
    if (kind === 'pct') return (v).toFixed(2) + '%';
    if (kind === 'pct100') return (v * 100).toFixed(1) + '%';
    if (kind === 'usd') return (v >= 0 ? '+' : '') + '$' + v.toFixed(2);
    if (kind === 'num') return (typeof v === 'number' ? v.toFixed(2) : v);
    return v;
  }

  function _tile(label, val, cls) {
    return `<div class="quant-tile"><div class="qt-k">${label}</div><div class="qt-v ${cls || ''}">${val}</div></div>`;
  }

  function _renderMetrics(d) {
    const el = $('quant-metrics'); el.hidden = false;
    const pnlCls = (d.total_pnl || 0) >= 0 ? 'qt-pos' : 'qt-neg';
    el.innerHTML =
      _tile('Trades', d.num_trades ?? 0) +
      _tile('Net PnL', _fmt(d.total_pnl, 'usd'), pnlCls) +
      _tile('Return', _fmt(d.total_return_pct, 'pct'), pnlCls) +
      _tile('Win rate', _fmt(d.win_rate, 'pct100')) +
      _tile('Sharpe', _fmt(d.sharpe, 'num')) +
      _tile('Sortino', _fmt(d.sortino, 'num')) +
      _tile('Profit factor', _fmt(d.profit_factor, 'num')) +
      _tile('Expectancy', _fmt(d.expectancy, 'usd'));
  }

  function _drawCurve(points) {
    const cv = $('quant-curve');
    if (!points || points.length < 2) { cv.hidden = true; return; }
    cv.hidden = false;
    const w = cv.clientWidth || 600, h = 150;
    cv.width = w; cv.height = h;
    const ctx = cv.getContext('2d');
    const ys = points.map(p => p.equity);
    const min = Math.min(...ys), max = Math.max(...ys), span = (max - min) || 1;
    const x = (i) => (i / (points.length - 1)) * (w - 8) + 4;
    const y = (v) => h - 8 - ((v - min) / span) * (h - 16);
    ctx.clearRect(0, 0, w, h);
    // zero-ish baseline (starting equity = first point)
    const up = ys[ys.length - 1] >= ys[0];
    const col = up ? '#2ee6a6' : '#ff4d6d';
    // area
    ctx.beginPath(); ctx.moveTo(x(0), y(ys[0]));
    points.forEach((p, i) => ctx.lineTo(x(i), y(p.equity)));
    ctx.lineTo(x(points.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath();
    ctx.fillStyle = col + '22'; ctx.fill();
    // line
    ctx.beginPath(); ctx.moveTo(x(0), y(ys[0]));
    points.forEach((p, i) => ctx.lineTo(x(i), y(p.equity)));
    ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.stroke();
  }

  // ── library ──────────────────────────────────────────────────────────────────
  async function _loadLibrary() {
    try {
      const d = await _api('/strategies');
      _library = d.strategies || [];
    } catch { _library = []; }
    const el = $('quant-lib');
    if (!_library.length) { el.innerHTML = '<div class="quant-empty">No saved strategies yet.</div>'; return; }
    el.innerHTML = _library.map(s => {
      const m = s.metrics || {};
      const ret = (m.total_return_pct != null) ? m.total_return_pct.toFixed(2) + '%' : '—';
      const cls = (m.total_pnl || 0) >= 0 ? 'qt-pos' : 'qt-neg';
      return `<div class="quant-lib-item">
        <div class="qli-main">
          <div class="qli-name">${esc(s.name)}</div>
          <div class="qli-meta">${esc(s.symbol)}/${esc(s.interval)} · <span class="${cls}">${ret}</span> · ${(m.num_trades ?? 0)} trades</div>
        </div>
        <div class="qli-actions">
          <button class="quant-mini" onclick="QuantView.loadStrategy('${esc(s.id)}')" title="Load into composer">↥</button>
          <button class="quant-mini" onclick="QuantView.deleteStrategy('${esc(s.id)}')" title="Delete">✕</button>
        </div>
      </div>`;
    }).join('');
  }

  async function _save() {
    const name = $('quant-save-name').value.trim();
    if (!name) { $('quant-save-name').focus(); return; }
    if (!_lastCfg) return;
    await _api('/strategies', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, code: _lastCfg.code || '', symbol: _lastCfg.symbol, interval: _lastCfg.interval,
        params: _lastCfg.params || null, metrics: _lastMetrics || null,
      }),
    });
    $('quant-save-name').value = '';
    Shell.toast('Strategy saved', 'success');
    _loadLibrary();
  }

  // ── mount ────────────────────────────────────────────────────────────────────
  async function mount(root) {
    const r = await fetch('/modules/quant/view.html');
    root.innerHTML = await r.text();

    $('quant-gen-btn').addEventListener('click', _generate);
    $('quant-template-btn').addEventListener('click', _useTemplate);
    $('quant-run-btn').addEventListener('click', _runBacktest);
    $('quant-save-btn').addEventListener('click', _save);
    $('quant-lib-refresh').addEventListener('click', _loadLibrary);

    _loadEngine();
    _loadLibrary();
  }

  function unmount() { /* nothing persistent to tear down */ }

  window.QuantView = {
    mount,
    unmount,
    loadStrategy(id) {
      const s = _library.find(x => x.id === id);
      if (!s) return;
      if (s.code) {
        _code = s.code;
        const pre = $('quant-code'); pre.textContent = s.code; pre.hidden = false;
        $('quant-validate').textContent = '✓ loaded from library';
        $('quant-validate').className = 'quant-validate quant-good';
      } else { _useTemplate(); }
      if (s.prompt) $('quant-prompt').value = s.prompt;
      $('quant-symbol').value = s.symbol || 'BTC';
      $('quant-interval').value = s.interval || '1h';
    },
    async deleteStrategy(id) {
      await _api('/strategies/' + id, { method: 'DELETE' });
      _loadLibrary();
    },
  };
})();
