/* ─────────────────────────────────────────────────────────────────────────
 * Runi Shell — module registry, navigation, palette, cross-module bus.
 *
 * Modules register themselves with Shell.register({...}).
 * The shell handles nav, mounting, unmounting, and shared services.
 *
 * Two module kinds are supported:
 *   - "native": module mounts its DOM directly into module-root
 *               (shares JS context with shell — best for new modules)
 *   - "iframe": module is loaded into an <iframe> (no JS sharing — used
 *               for legacy Fieldwork during the transition)
 *
 * Cross-iframe communication is via postMessage with a tiny protocol.
 * ─────────────────────────────────────────────────────────────────────── */

(function () {
    'use strict';

    const SHELL_API = '/api/shell';

    /* ── State ─────────────────────────────────────────────────────────── */
    const modules  = new Map();   // id -> ModuleDef
    let   activeId = null;
    let   _config  = null;        // /api/shell/config response (lazy)
    const _bus     = new EventTarget();
    const _palette = []; // {id, label, hint, action, moduleId}

    /* ── Toast helper ──────────────────────────────────────────────────── */
    function toast(msg, kind = 'info', ttl = 3500) {
        const c = document.getElementById('shell-toasts');
        if (!c) return console.log(`[toast:${kind}]`, msg);
        const el = document.createElement('div');
        el.className = 'shell-toast ' + (kind || '');
        el.textContent = msg;
        c.appendChild(el);
        setTimeout(() => el.remove(), ttl);
    }

    /* ── Public Shell API (modules use this) ───────────────────────────── */
    const Shell = {
        /* Register a module. Idempotent — re-registering replaces. */
        register(def) {
            if (!def || !def.id) throw new Error('Shell.register: id required');
            modules.set(def.id, def);
            if (def.palette) {
                def.palette.forEach(item => _palette.push({ ...item, moduleId: def.id }));
            }
            _renderNav();
        },

        /* Activate a module by id. */
        async switch(modId) {
            const next = modules.get(modId);
            if (!next) {
                toast(`Unknown module: ${modId}`, 'error');
                return;
            }
            if (activeId === modId) return;

            const prev = modules.get(activeId);
            if (prev && typeof prev.unmount === 'function') {
                try { await prev.unmount(); }
                catch (e) { console.warn(`unmount ${prev.id} failed`, e); }
            }

            const root = document.getElementById('module-root');
            root.innerHTML = '';

            // Iframe kind — just embed
            if (next.kind === 'iframe') {
                const iframe = document.createElement('iframe');
                iframe.className = 'module-iframe';
                iframe.src       = next.url;
                iframe.name      = `module-${next.id}`;
                iframe.dataset.moduleId = next.id;
                root.appendChild(iframe);
                _attachIframeBridge(iframe, next);
            }
            // Native kind — call mount(root)
            else if (typeof next.mount === 'function') {
                try { await next.mount(root); }
                catch (e) {
                    root.innerHTML = `<div class="module-empty">
                        <div class="icon">⚠</div>
                        <div>Module <strong>${next.id}</strong> failed to mount:</div>
                        <code style="color:var(--danger);font-size:0.8rem">${(e && e.message) || e}</code>
                    </div>`;
                }
            }
            // No mount + no iframe url — show placeholder
            else {
                root.innerHTML = `<div class="module-empty">
                    <div class="icon">${next.icon || '📦'}</div>
                    <div><strong>${next.label || next.id}</strong> — not yet implemented</div>
                </div>`;
            }

            activeId = modId;
            // Don't persist iframe modules — they are never auto-restored on startup
            if (next.kind !== 'iframe') {
                localStorage.setItem('shell_last_module', modId);
                history.replaceState({}, '', '#' + modId);
            } else {
                history.replaceState({}, '', location.pathname);
            }
            _renderNav();
            _bus.dispatchEvent(new CustomEvent('shell:module_switched', { detail: { id: modId } }));
        },

        /* Cross-module event bus (string event name, payload object). */
        on(event, fn)   { _bus.addEventListener(event, fn); },
        off(event, fn)  { _bus.removeEventListener(event, fn); },
        emit(event, payload) { _bus.dispatchEvent(new CustomEvent(event, { detail: payload || {} })); },

        /* Shared services. */
        toast,

        /* ── Runi voice — shared TTS available to all modules ────────────
         *
         *  Shell.speak(text)          → read text aloud with Runi's voice
         *  Shell.speak(null)          → stop current speech
         *
         *  Voice priority: female neural online > desktop female > any female-
         *  sounding > any English.  Pitch 1.15, rate 0.94 — techy-feminine.
         * ──────────────────────────────────────────────────────────────── */
        speak: (() => {
            // Runi's voice. Primary: local Piper neural TTS via /api/voice/tts, played
            // through Web Audio so the avatar can tap real amplitude (Shell.voiceAnalyser).
            // Fallback: browser speechSynthesis if Piper is unreachable, so the assistant
            // keeps talking even when the voice containers are down.
            //   Shell.speak(text[, {voice, length_scale}])  → speak
            //   Shell.speak(null)                           → stop
            const VOICE    = 'en_GB-alba-medium'; // Runi — composed British female
            const ACCENT   = null;                // clean by default ('ru' available as an option)
            const STRENGTH = 'medium';            // accent intensity when ACCENT is set
            let ac = null, curSrc = null;

            // ── browser-TTS fallback (previous behaviour, trimmed) ──
            const PREFERRED = [
                'Microsoft Sonia Online (Natural) - English (United Kingdom)',
                'Microsoft Libby Online (Natural) - English (United Kingdom)',
                'Google UK English Female', 'Samantha', 'Karen', 'Moira', 'Tessa',
            ];
            const MALE_RE = /\b(ryan|guy|david|mark|james|richard|george|daniel|tom|william|male)\b/i;
            let _cached = null;
            function _pick() {
                if (_cached) return _cached;
                const voices = window.speechSynthesis?.getVoices() || [];
                if (!voices.length) return null;
                for (const n of PREFERRED) { const v = voices.find(v => v.name === n); if (v) return (_cached = v); }
                _cached = voices.find(v => /female/i.test(v.name) && v.lang?.startsWith('en'))
                       || voices.find(v => v.lang?.startsWith('en') && !MALE_RE.test(v.name)) || voices[0] || null;
                return _cached;
            }
            if ('speechSynthesis' in window) {
                window.speechSynthesis.addEventListener('voiceschanged', () => { _cached = null; });
            }
            function _fallback(text, onend) {
                if (!('speechSynthesis' in window)) { if (onend) onend(); return; }
                try {
                    const u = new SpeechSynthesisUtterance(text);
                    const v = _pick(); if (v) { u.voice = v; u.lang = v.lang; } else u.lang = 'en-GB';
                    u.rate = 0.98; u.pitch = 1.05;
                    if (onend) { u.onend = onend; u.onerror = onend; }
                    speechSynthesis.speak(u);
                } catch (e) { console.warn('Runi fallback TTS failed', e); if (onend) onend(); }
            }

            let seq = 0;                       // bumps to cancel any in-flight speech
            function stop() {
                seq++;
                try { if (curSrc) { curSrc.onended = null; curSrc.stop(); curSrc = null; } } catch (e) {}
                try { speechSynthesis.cancel(); } catch (e) {}
                Shell.voiceAnalyser = null;
            }

            // Split into sentence-ish chunks (~240 chars) so the first bit plays while
            // the rest still synthesize — cuts the "text now, voice a few seconds later" lag.
            function chunkText(text) {
                const parts = String(text).replace(/\s+/g, ' ').trim().match(/[^.!?…]+[.!?…]*/g) || [String(text)];
                const out = []; let buf = '';
                for (const p of parts) { if (buf && (buf + p).length > 240) { out.push(buf.trim()); buf = p; } else buf += p; }
                if (buf.trim()) out.push(buf.trim());
                return out.filter(Boolean);
            }

            async function ttsBuf(text, mySeq, params) {
                const r = await fetch('/api/voice/tts', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text, ...params }),
                });
                if (!r.ok) throw new Error('tts ' + r.status);
                const bytes = await r.arrayBuffer();
                if (mySeq !== seq) return null;
                return await ac.decodeAudioData(bytes);
            }

            async function speak(text, opts) {
                stop();
                if (!text) return;               // Shell.speak(null) → stop
                opts = opts || {};
                const mySeq = seq;
                const params = {
                    voice: opts.voice || VOICE,
                    accent: opts.accent !== undefined ? opts.accent : ACCENT,
                    strength: opts.strength || STRENGTH,
                    length_scale: opts.length_scale,
                };
                ac = ac || new (window.AudioContext || window.webkitAudioContext)();
                if (ac.state === 'suspended') { try { await ac.resume(); } catch (e) {} }
                const chunks = chunkText(text);
                const an = ac.createAnalyser(); an.fftSize = 256; an.connect(ac.destination);
                Shell.voiceAnalyser = an;        // avatar reads this while she speaks
                let started = false;
                let nextP = ttsBuf(chunks[0], mySeq, params).catch(() => null);
                for (let i = 0; i < chunks.length; i++) {
                    let buf; try { buf = await nextP; } catch (e) { buf = null; }
                    if (mySeq !== seq) return;                                  // cancelled
                    if (i + 1 < chunks.length) nextP = ttsBuf(chunks[i + 1], mySeq, params).catch(() => null);
                    if (!buf) {
                        if (i === 0) { if (opts.onstart) opts.onstart(); _fallback(String(text), opts.onend); return; }
                        continue;
                    }
                    await new Promise(res => {
                        if (mySeq !== seq) { res(); return; }
                        const src = ac.createBufferSource(); src.buffer = buf; src.connect(an);
                        src.onended = () => { if (curSrc === src) curSrc = null; res(); };
                        curSrc = src;
                        if (!started) { started = true; if (opts.onstart) opts.onstart(); }  // avatar → speaking on first audio
                        src.start();
                    });
                    if (mySeq !== seq) return;
                }
                if (mySeq === seq) { Shell.voiceAnalyser = null; if (opts.onend) opts.onend(); }
            }
            speak.stop   = stop;
            speak.pause  = () => { try { if (ac && ac.state === 'running')   ac.suspend(); } catch (e) {} try { speechSynthesis.pause();  } catch (e) {} };
            speak.resume = () => { try { if (ac && ac.state === 'suspended') ac.resume();  } catch (e) {} try { speechSynthesis.resume(); } catch (e) {} };
            return speak;
        })(),

        // Runi's ears — mic → local Whisper STT (/api/voice/stt). Audio never
        // leaves the machine. Returns a controller: call .stop() to end the take;
        // the transcript arrives via onResult. Toggle a mic button on it.
        //   const rec = Shell.listen(text => input.value = text);
        //   … later …  rec.stop();
        listen(onResult, onError) {
            let rec = null, stream = null, micCtx = null;
            const chunks = [];
            const _clearMic = () => {
                Shell.micAnalyser = null;
                try { if (micCtx) { micCtx.close(); micCtx = null; } } catch (e) {}
            };
            navigator.mediaDevices?.getUserMedia({ audio: true }).then(s => {
                stream = s;
                // Expose live mic amplitude for the avatar (local — never sent anywhere).
                try {
                    micCtx = new (window.AudioContext || window.webkitAudioContext)();
                    const src = micCtx.createMediaStreamSource(s);
                    const an = micCtx.createAnalyser(); an.fftSize = 256;
                    src.connect(an);
                    Shell.micAnalyser = an;
                } catch (e) {}
                rec = new MediaRecorder(s);
                rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
                rec.onstop = async () => {
                    _clearMic();
                    try { stream.getTracks().forEach(t => t.stop()); } catch (e) {}
                    try {
                        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
                        const fd = new FormData();
                        fd.append('file', blob, 'mic.webm');
                        const r = await fetch('/api/voice/stt', { method: 'POST', body: fd });
                        if (!r.ok) throw new Error('stt ' + r.status);
                        const d = await r.json();
                        onResult && onResult((d.text || '').trim());
                    } catch (e) { onError ? onError(e) : console.warn('Runi listen failed', e); }
                };
                rec.start();
            }).catch(e => { onError ? onError(e) : console.warn('mic unavailable', e); });
            return { stop() { try { if (rec && rec.state !== 'inactive') rec.stop(); } catch (e) {} } };
        },

        async api(path, opts) {
            // Path resolution rules:
            //   http(s)://…       → use as-is (external)
            //   /api/…            → use as-is (absolute API path — module endpoints)
            //   /…  or anything   → treat as shell-relative, prepend /api/shell
            let url;
            if (path.startsWith('http://') || path.startsWith('https://')) {
                url = path;
            } else if (path.startsWith('/api/')) {
                url = path;
            } else if (path.startsWith('/')) {
                url = SHELL_API + path;
            } else {
                url = SHELL_API + '/' + path;
            }
            const res = await fetch(url, {
                ...opts,
                headers: {
                    'Content-Type': 'application/json',
                    ...(opts && opts.headers || {}),
                },
            });
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
            const ct = res.headers.get('content-type') || '';
            return ct.includes('application/json') ? res.json() : res.text();
        },
        async config() {
            if (!_config) _config = await Shell.api('/config').catch(() => ({}));
            return _config;
        },
        activeModule: () => activeId,
        modules:      () => [...modules.values()],
    };

    /* ── Iframe ↔ Shell postMessage bridge ─────────────────────────────── */
    function _attachIframeBridge(iframe, mod) {
        // Outbound: when shell switches modules, ping any listening iframe
        const handler = (ev) => {
            if (!iframe.contentWindow) return;
            iframe.contentWindow.postMessage(
                { type: 'shell:event', event: ev.type, payload: ev.detail || {} },
                '*'
            );
        };
        _bus.addEventListener('shell:command', handler);
        iframe.addEventListener('load', () => {
            iframe.contentWindow?.postMessage({ type: 'shell:hello', moduleId: mod.id }, '*');
        });

        // Inbound: iframes can emit events back into the shell bus
        window.addEventListener('message', (ev) => {
            if (!ev.data || typeof ev.data !== 'object') return;
            if (ev.source !== iframe.contentWindow) return;
            if (ev.data.type === 'module:event' && ev.data.event) {
                _bus.dispatchEvent(new CustomEvent(ev.data.event, { detail: ev.data.payload || {} }));
            } else if (ev.data.type === 'module:toast') {
                toast(ev.data.message || '…', ev.data.kind || 'info');
            }
        });
    }

    /* ── Nav rendering ─────────────────────────────────────────────────── */
    function _renderNav() {
        const nav = document.getElementById('shell-nav');
        if (!nav) return;
        nav.innerHTML = [...modules.values()].map(m => `
            <button class="shell-nav-btn ${m.id === activeId ? 'active' : ''}"
                    data-mod-id="${m.id}"
                    title="${m.description || m.label}">
                <span>${m.icon || '📦'}</span>
                <span>${m.label || m.id}</span>
            </button>
        `).join('');
        nav.querySelectorAll('.shell-nav-btn').forEach(b => {
            b.addEventListener('click', () => Shell.switch(b.dataset.modId));
        });
    }

    /* ── Command palette ───────────────────────────────────────────────── */
    function _initPalette() {
        const overlay = document.getElementById('shell-palette-overlay');
        const input   = document.getElementById('shell-palette-input');
        const results = document.getElementById('shell-palette-results');
        if (!overlay || !input || !results) return;
        let selectedIdx = 0;

        const close = () => { overlay.classList.remove('active'); input.value = ''; };
        const open  = () => {
            overlay.classList.add('active');
            input.focus();
            render('');
        };

        const items = () => {
            // Module-switch entries
            const modItems = [...modules.values()].map(m => ({
                icon: m.icon, label: `Open ${m.label}`, hint: m.id,
                action: () => Shell.switch(m.id),
            }));
            return [...modItems, ..._palette.map(p => ({
                icon: p.icon || '⚡', label: p.label, hint: p.moduleId,
                action: p.action,
            }))];
        };

        const render = (q) => {
            const ql = q.trim().toLowerCase();
            const filtered = items().filter(i =>
                !ql || i.label.toLowerCase().includes(ql) || (i.hint || '').toLowerCase().includes(ql)
            );
            results.innerHTML = filtered.length === 0
                ? `<div class="shell-palette-item" style="color:var(--text-dim)">No matches.</div>`
                : filtered.map((i, idx) => `
                    <div class="shell-palette-item ${idx===selectedIdx?'selected':''}" data-idx="${idx}">
                        <span class="pi-icon">${i.icon}</span>
                        <span>${i.label}</span>
                        <span class="pi-hint">${i.hint || ''}</span>
                    </div>
                `).join('');
            results.querySelectorAll('.shell-palette-item[data-idx]').forEach(el => {
                el.addEventListener('click', () => {
                    const idx = parseInt(el.dataset.idx);
                    const target = filtered[idx];
                    if (target) { close(); target.action(); }
                });
            });
        };

        input.addEventListener('input', () => { selectedIdx = 0; render(input.value); });
        input.addEventListener('keydown', (e) => {
            const filtered = items().filter(i => {
                const ql = input.value.toLowerCase();
                return !ql || i.label.toLowerCase().includes(ql) || (i.hint || '').toLowerCase().includes(ql);
            });
            if (e.key === 'ArrowDown') { selectedIdx = Math.min(selectedIdx + 1, filtered.length - 1); render(input.value); }
            else if (e.key === 'ArrowUp') { selectedIdx = Math.max(selectedIdx - 1, 0); render(input.value); }
            else if (e.key === 'Enter') {
                const target = filtered[selectedIdx];
                if (target) { close(); target.action(); }
            } else if (e.key === 'Escape') close();
        });
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                if (overlay.classList.contains('active')) close(); else open();
            } else if (e.key === 'Escape' && overlay.classList.contains('active')) close();
        });

        document.getElementById('shell-palette-btn')?.addEventListener('click', open);
    }

    /* ── Boot ──────────────────────────────────────────────────────────── */
    async function _boot() {
        _initPalette();

        // Fetch module manifests from the shell backend and register iframe modules
        try {
            const data = await Shell.api('/modules');
            (data.modules || []).forEach(m => {
                if (m.kind === 'iframe' && m.url) {
                    // Iframe modules — register directly from the backend manifest
                    if (!modules.has(m.id)) {
                        Shell.register({
                            id:    m.id,
                            label: m.label,
                            icon:  m.icon,
                            description: m.description,
                            kind:  'iframe',
                            url:   m.url,
                        });
                    } else {
                        // Module file already registered an in-shell native version;
                        // patch the iframe URL in if absent.
                        const existing = modules.get(m.id);
                        if (!existing.url) existing.url = m.url;
                    }
                }
                // Native modules are expected to call Shell.register() themselves
                // from their own manifest.js loaded as a separate <script>.
            });
        } catch (e) {
            console.warn('Could not fetch /api/shell/modules:', e);
            toast('Shell backend unreachable — check shell-backend container', 'error', 6000);
        }

        // Pick startup module — native modules only.
        // Iframe modules (fieldwork) are never auto-restored: they write no hash
        // and no localStorage entry, so the shell always opens on a native tab.
        const fromHash    = location.hash.replace(/^#/, '');
        const last        = localStorage.getItem('shell_last_module');
        const hashMod     = fromHash ? modules.get(fromHash) : null;
        const lastMod     = last     ? modules.get(last)     : null;
        const firstNative = [...modules.values()].find(m => m.kind === 'native')?.id
                         || (modules.size ? [...modules.keys()][0] : null);
        const target = (hashMod && hashMod.kind === 'native') ? fromHash
                     : (lastMod && lastMod.kind === 'native') ? last
                     : firstNative;
        if (target) Shell.switch(target);
    }

    window.Shell = Shell;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _boot);
    } else {
        _boot();
    }
})();
