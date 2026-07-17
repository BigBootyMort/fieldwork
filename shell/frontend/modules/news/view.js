/* ─────────────────────────────────────────────────────────────────────────
 * News module — mount/unmount logic.
 *
 * Exposes NewsView.mount(root) and NewsView.unmount() which the module's
 * manifest.js wires into Shell.register().
 *
 * State is module-scoped (lives on window.NewsView) so a switch away and
 * back retains things like the selected country and the last brief.
 * ─────────────────────────────────────────────────────────────────────── */

window.NewsView = (function () {
    'use strict';

    // Country fill colour is chosen by the country's dominant topic.
    // Use vivid colours — dark ones like #7f1d1d are invisible on the
    // near-black (#06080c) map background even at 80 % opacity.
    const TOPIC_COLOURS = {
        war:        '#f87171',  // bright red
        disaster:   '#fb923c',  // orange-red
        crisis:     '#fbbf24',  // amber
        security:   '#e879f9',  // fuchsia / magenta
        world:      '#38bdf8',  // sky blue
        business:   '#34d399',  // emerald
        tech:       '#818cf8',  // indigo
        lifestyle:  '#a78bfa',  // violet
        sport:      '#4ade80',  // green
    };
    const TOPIC_DEFAULT = '#38bdf8';   // sky blue — generic 'world'

    // Hot-score → opacity stops.
    // Floor raised to 0.25 so even low-traffic countries are visible on
    // the dark background.
    const OPACITY_STOPS = [
        [100, 0.90],
        [ 50, 0.75],
        [ 25, 0.60],
        [ 10, 0.45],
        [  0, 0.25],
    ];

    // Module-scoped state
    const state = {
        map:             null,
        countriesLayer:  null,
        windowH:         12,
        selectedISO:     null,
        articles:        [],
        articlesByISO:   {},
        heat:            [],
        ttsOn:           false,
        ttsState:        'idle',   // 'idle' | 'playing' | 'paused'
        chatLog:         [],
        leafletReady:    false,
        worldGeo:        null,
        llmEngine:       null,     // 'claude' | 'ollama' | null — active brief engine
        storiesMode:     false,    // group article list into clustered stories
        watchlist:       [],
    };

    // ── Leaflet lazy-load ──────────────────────────────────────────────
    async function ensureLeaflet() {
        if (window.L && window.L.geoJSON) { state.leafletReady = true; return; }
        await new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = '/modules/news/vendor/leaflet/leaflet.js';
            s.crossOrigin = '';
            s.onload  = resolve;
            s.onerror = reject;
            document.head.appendChild(s);
        });
        state.leafletReady = true;
    }

    // Cached world GeoJSON (Natural Earth low-res, fetched once per session)
    async function ensureWorldGeo() {
        if (state.worldGeo) return state.worldGeo;
        // Vendored locally (Highcharts world.geo.json, ~190 KB, iso-a2 per feature)
        // so the map works offline and makes no external requests — see vendor/.
        const url = '/modules/news/vendor/world.geo.json';
        const res = await fetch(url);
        if (!res.ok) throw new Error('Failed to load world GeoJSON');
        state.worldGeo = await res.json();
        return state.worldGeo;
    }

    function colourForTopic(topic) {
        return TOPIC_COLOURS[topic] || TOPIC_DEFAULT;
    }

    function opacityForScore(score) {
        for (const [t, o] of OPACITY_STOPS) {
            if (score >= t) return o;
        }
        return 0;
    }

    // ── Map ────────────────────────────────────────────────────────────
    async function initMap() {
        await ensureLeaflet();

        if (state.map) {
            // Re-mounting into a (possibly differently sized) container.
            // Leaflet caches the container size from init; invalidateSize()
            // forces it to re-measure and re-draw the tiles correctly.
            state.map.invalidateSize();
            return;
        }

        // Give the browser two animation frames to finish layout before
        // Leaflet measures the container.  Without this the map div often
        // has 0 × 0 dimensions on first render and shows a blank pane.
        // rAF never fires while the tab isn't painting (backgrounded / headless),
        // so race it against a timeout — otherwise init would hang forever there.
        await new Promise(r => {
            let done = false;
            const finish = () => { if (!done) { done = true; r(); } };
            requestAnimationFrame(() => requestAnimationFrame(finish));
            setTimeout(finish, 300);
        });

        // Safety-net: if the container still reports no height, wait a bit
        // longer (happens when the CSS grid hasn't settled yet).
        const mapEl = document.getElementById('news-map');
        if (mapEl && mapEl.offsetHeight === 0) {
            await new Promise(r => setTimeout(r, 120));
        }

        state.map = L.map('news-map', {
            center: [20, 0],
            zoom:   2,
            minZoom: 2,
            maxZoom: 6,
            worldCopyJump: true,
            zoomControl: true,
            attributionControl: false,
        });
        // No external tile basemap — the choropleth polygons are the data and
        // render on the dark map background (#news-map bg in style.css). This
        // keeps the map fully offline / zero external requests for OPSEC.

        // ── Tooltip stuck-open fix ──────────────────────────────────────
        // Leaflet's layer-level mouseout doesn't always fire when the cursor
        // exits the map quickly (e.g. moving to the Morning Brief panel).
        // We catch this at two levels so sticky tooltips always get cleaned up.

        // 1. Native DOM mouseleave — fires reliably whenever the cursor leaves
        //    the map container div, even if Leaflet's synthetic event is missed.
        state.map.getContainer().addEventListener('mouseleave', _closeAllTooltips);

        // 2. Leaflet map-level mouseout — belt-and-suspenders for in-map cases
        //    (e.g. cursor drifts into ocean / tile area with no feature layer).
        state.map.on('mouseout', _closeAllTooltips);

        await renderChoropleth();
    }

    /** Close every tooltip currently open on the countries GeoJSON layer. */
    function _closeAllTooltips() {
        if (!state.countriesLayer) return;
        state.countriesLayer.eachLayer(layer => {
            try { layer.closeTooltip(); } catch (_) {}
        });
    }

    // Extract a 2-letter ISO code from a GeoJSON feature, trying every known
    // property name the geo-countries dataset has used across versions.
    // NOTE: the datasets/geo-countries dataset switched from ISO_A2 to
    //       ISO3166-1-Alpha-2 at some point — check that first.
    function isoFromFeature(props) {
        return props['ISO3166-1-Alpha-2']   // current geo-countries format
            || props.ISO_A2                  // old Natural Earth format
            || props.iso_a2
            || props.ISO_A2_EH              // Natural Earth extended
            || props.ISO2
            || props.iso2
            || props['iso-a2']              // Highcharts world.geo.json (vendored)
            || props['hc-a2']
            || null;
    }

    // Compute the base (non-hover) style for a GeoJSON feature.
    function baseStyle(feature, byISO) {
        const raw   = isoFromFeature(feature.properties);
        const iso   = raw ? raw.toUpperCase() : null;
        const entry = iso ? byISO[iso] : null;
        const score = entry?.hot_score || 0;
        const topic = entry?.dominant_topic || 'world';
        const sel   = state.selectedISO && iso === state.selectedISO;
        return {
            fillColor:   entry ? colourForTopic(topic) : 'transparent',
            fillOpacity: entry ? opacityForScore(score) : 0,
            weight:      sel ? 2.5 : 0.5,
            color:       sel ? '#38bdf8' : 'rgba(120,140,170,0.25)',
        };
    }

    // Build the tooltip HTML for a country feature.
    function buildTooltip(name, iso, entry, topic, score) {
        const tc = colourForTopic(topic);
        if (!entry) {
            return `<div class="ct-wrap ct-empty">
                <span class="ct-name">${escapeHtml(name || iso || '?')}</span>
                <span class="ct-no-stories">no stories</span>
            </div>`;
        }
        const cnt   = entry.article_count;
        const badge = `<span class="ct-badge" style="background:${tc}20;color:${tc}">${escapeHtml(topic)}</span>`;
        const hl    = entry.top_headline
            ? `<div class="ct-headline">${escapeHtml(
                entry.top_headline.length > 72
                    ? entry.top_headline.slice(0, 69) + '…'
                    : entry.top_headline)}</div>`
            : '';
        return `<div class="ct-wrap">
            <div class="ct-head">
                <span class="ct-name">${escapeHtml(name || iso || '?')}</span>
                ${badge}
            </div>
            <div class="ct-stats">
                <span class="ct-dot" style="background:${tc}"></span>
                ${cnt} article${cnt === 1 ? '' : 's'} &nbsp;·&nbsp; ⚡ ${score}
            </div>
            ${hl}
            <div class="ct-hint">click to filter articles →</div>
        </div>`;
    }

    async function renderChoropleth() {
        if (!state.map) return;
        if (state.countriesLayer) {
            // Close any open tooltip before tearing the layer down — removeLayer()
            // does NOT fire mouseout, so the tooltip would otherwise be orphaned.
            _closeAllTooltips();
            state.map.removeLayer(state.countriesLayer);
            state.countriesLayer = null;
        }

        let geo;
        try { geo = await ensureWorldGeo(); }
        catch (e) {
            Shell.toast('Could not load world map data — heatmap disabled', 'warning');
            return;
        }

        // Build a case-insensitive lookup so storage/GeoJSON casing differences
        // don't silently break the match (e.g. "ir" vs "IR").
        const byISO = {};
        for (const h of state.heat) {
            if (h.iso) {
                const key = h.iso.toUpperCase();
                byISO[key] = h;
            }
        }

        state.countriesLayer = L.geoJSON(geo, {
            style: feature => baseStyle(feature, byISO),
            onEachFeature: (feature, layer) => {
                const raw   = isoFromFeature(feature.properties);
                const iso   = raw ? raw.toUpperCase() : null;
                const name  = feature.properties.name
                    || feature.properties.ADMIN
                    || feature.properties.admin
                    || iso;
                const entry = iso ? byISO[iso] : null;
                const score = entry?.hot_score || 0;
                const topic = entry?.dominant_topic || 'world';

                // Tooltip
                layer.bindTooltip(
                    buildTooltip(name, iso, entry, topic, score),
                    { sticky: true, className: 'country-tooltip', offset: [8, 0] }
                );

                if (!iso) return;

                // Live hover highlight — no full re-render, just flip the style
                layer.on('mouseover', function () {
                    const sel = state.selectedISO === iso;
                    this.setStyle({
                        weight:      sel ? 3 : 2,
                        color:       entry ? colourForTopic(topic) : 'rgba(160,180,210,0.5)',
                        fillOpacity: entry
                            ? Math.min(0.95, opacityForScore(score) + 0.18)
                            : 0,
                    });
                    this.bringToFront();
                });
                layer.on('mouseout', function () {
                    state.countriesLayer?.resetStyle(this);
                });
                layer.on('click', () => selectCountry(iso, name));
            },
        }).addTo(state.map);
    }

    function selectCountry(iso, name) {
        state.selectedISO = iso;
        const el = document.getElementById('news-selected-country');
        const nm = document.getElementById('nsc-name');
        if (el && nm) {
            // Resolve the display name: prefer the passed name, then the heat data,
            // then the raw ISO code (e.g. when called from article country chips).
            const displayName = name
                || state.heat.find(h => h.iso === iso)?.name
                || iso;
            nm.textContent = `📍 ${displayName}`;
            el.classList.remove('hidden');
        }
        renderChoropleth();   // restyle to highlight border
        loadArticles();
    }

    function clearCountry() {
        state.selectedISO = null;
        document.getElementById('news-selected-country')?.classList.add('hidden');
        renderChoropleth();
        loadArticles();
    }

    // ── Data loading ───────────────────────────────────────────────────
    async function loadHeatmap() {
        try {
            const data = await Shell.api(`/api/news/heatmap?window=${state.windowH}`);
            state.heat = data.points || [];
            updateWindowStats();
            await renderChoropleth();
        } catch (e) {
            Shell.toast('Heatmap load failed: ' + e.message, 'error');
        }
    }

    async function loadArticles() {
        if (state.storiesMode) return loadStories();
        try {
            const q = new URLSearchParams({ window: state.windowH, limit: 60 });
            if (state.selectedISO) q.set('country', state.selectedISO);
            const data = await Shell.api(`/api/news/articles?${q}`);
            state.articles = data.articles || [];
            renderArticles();
        } catch (e) {
            Shell.toast('Articles load failed: ' + e.message, 'error');
        }
    }

    // Media-lean badge (colour-coded), with factual + ownership tooltip.
    function _leanClass(lean) {
        const l = (lean || '').toLowerCase();
        if (l.includes('far right')) return 'far-right';
        if (l.includes('right'))     return 'right';
        if (l.includes('far left'))  return 'far-left';
        if (l.includes('left'))      return 'left';
        if (l.includes('center'))    return 'center';
        return 'na';
    }
    function _biasBadge(b) {
        if (!b || b.lean === 'Unrated' || b.lean === 'N/A') return '';
        const state = /state|⚑/i.test(b.owner || '');
        const ai    = !!b.ai_estimated;
        const cls   = _leanClass(b.lean);
        const tip   = `${b.lean} · Factual: ${b.factual}\nOwner: ${(b.owner || '').replace(/⚑\s*/, '')}`
                    + (ai ? '\n(AI-estimated — not a curated rating)' : '');
        return `<span class="news-lean news-lean--${cls}${ai ? ' news-lean--ai' : ''}" title="${escapeHtml(tip)}">`
             + `${state ? '⚑ ' : ''}${escapeHtml(b.lean)}${ai ? ' ~' : ''}</span>`;
    }

    // Coverage-balance bar for a story cluster — political spread of outlets.
    function _coverageBar(cov) {
        if (!cov) return '';
        const b = cov.buckets || {};
        const total = (b.left || 0) + (b.center || 0) + (b.right || 0);
        if (total < 2) return '';   // only meaningful for multi-source stories
        const pctL = (b.left   / total * 100).toFixed(0);
        const pctC = (b.center / total * 100).toFixed(0);
        const pctR = (b.right  / total * 100).toFixed(0);
        const flag = cov.state_funded_present
            ? '<span class="news-cov-state" title="A state-funded / government-influenced outlet is among the sources">⚑ state media</span>'
            : '';
        return `<div class="news-coverage" title="${b.left||0} left · ${b.center||0} center · ${b.right||0} right">
            <div class="news-cov-bar">
                <span style="width:${pctL}%" class="news-cov-l"></span>
                <span style="width:${pctC}%" class="news-cov-c"></span>
                <span style="width:${pctR}%" class="news-cov-r"></span>
            </div>
            <span class="news-cov-label">${escapeHtml(cov.label || '')}</span>${flag}
        </div>`;
    }

    // Entity chips → click to investigate / watch
    function _entityChips(entities) {
        if (!entities || !entities.length) return '';
        return `<div class="news-entities">` + entities.slice(0, 6).map(e =>
            `<span class="news-entity-chip" title="Investigate &quot;${escapeHtml(e)}&quot;"
                   onclick="event.stopPropagation();NewsView.investigate('${escapeHtml(e).replace(/'/g,"\\'")}')"
            >🔬 ${escapeHtml(e)}</span>`).join('') + `</div>`;
    }

    function updateWindowStats() {
        const el = document.getElementById('news-window-stats');
        if (!el) return;
        const arts = state.heat.reduce((s, c) => s + c.article_count, 0);
        el.textContent = `${arts} articles · ${state.heat.length} countries`;
    }

    function renderArticles() {
        const list = document.getElementById('news-articles-list');
        const cnt  = document.getElementById('news-articles-count');
        if (!list) return;
        if (cnt) cnt.textContent = `(${state.articles.length})`;
        if (!state.articles.length) {
            list.innerHTML = `<div class="news-dim" style="padding:0.5rem 0.65rem;font-size:0.82rem">
                No articles in this window. Click <strong>🔄 Poll feeds</strong> to fetch.
            </div>`;
            return;
        }
        // Top importance value sets the colour scale's high end
        const maxImp = state.articles.reduce((m, a) => Math.max(m, a.importance || 0), 0.01);

        list.innerHTML = state.articles.map(a => {
            const time = formatRelative(a.published_at);
            const topicCls = ['war','disaster','crisis','security'].includes(a.topic) ? a.topic : '';
            const chips = (a.countries || []).slice(0, 4)
                .map(c => `<span class="news-country-chip"
                                  onclick="event.preventDefault();event.stopPropagation();
                                           if(window.NewsView)NewsView.selectCountry('${escapeHtml(c)}')"
                                  title="Filter to ${escapeHtml(c)}"
                                  style="cursor:pointer">${escapeHtml(c)}</span>`).join(' ');
            // Border colour reflects the topic; brightness reflects importance vs max
            const borderColour = TOPIC_COLOURS[a.topic] || TOPIC_DEFAULT;
            const impRel       = Math.min(1, (a.importance || 0) / maxImp);
            const borderAlpha  = (0.35 + impRel * 0.65).toFixed(2);
            return `<article class="news-article" data-aid="${escapeHtml(a.id)}"
                        style="border-left:3px solid ${borderColour};
                               border-left-color:${hexToRgba(borderColour, borderAlpha)}">
                <div class="news-article-title">
                    <a href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>
                </div>
                <div class="news-article-meta">
                    <span>${escapeHtml(a.bias?.outlet || a.source?.name || 'unknown')}</span>
                    ${_biasBadge(a.bias)}
                    <span>·</span>
                    <span>${time}</span>
                    ${a.topic ? `<span class="news-topic-chip ${topicCls}">${escapeHtml(a.topic)}</span>` : ''}
                    ${chips}
                    <button class="news-article-tts" data-aid="${escapeHtml(a.id)}"
                            title="Read article aloud">🔈</button>
                </div>
                ${a.summary ? `<div class="news-article-summary">${escapeHtml(a.summary)}</div>` : ''}
                ${_entityChips(a.entities)}
            </article>`;
        }).join('');

        list.querySelectorAll('.news-article-tts').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const id = btn.dataset.aid;
                const a  = state.articles.find(x => x.id === id);
                if (a) speak(`${a.title}. ${a.summary || ''}`);
            });
        });
    }

    // ── LLM status ────────────────────────────────────────────────────
    async function checkLLMStatus() {
        try {
            const data = await Shell.api('/api/news/llm-status');
            state.llmEngine = data.engine || null;
            // Show which engine will write the brief (proactively, in the header)
            const headBadge = document.getElementById('news-brief-engine');
            if (headBadge) headBadge.innerHTML = data.engine ? engineBadge(data.engine) : '';
            if (!data.ok) {
                const body = document.getElementById('news-brief-body');
                if (body) {
                    const hint = data.hint
                        ? `\`\`\`\n${data.hint}\n\`\`\``
                        : '';
                    body.innerHTML = renderMarkdown(
                        `⚠️ **No LLM ready** — add an Anthropic API key in **Agent → Settings** for Claude, `
                        + `or load a local model.\n\n${hint}`
                    );
                }
            }
        } catch (_) {
            // Non-fatal — backend might not be up yet; brief button will fail with a clear message anyway
        }
    }

    // ── Polling & brief ────────────────────────────────────────────────
    async function poll() {
        const btn = document.getElementById('news-poll-btn');
        if (btn) { btn.disabled = true; btn.textContent = '⏳ Polling…'; }
        try {
            const data = await Shell.api('/api/news/poll?sync=true', { method: 'POST' });
            const total = data.new_total ?? 0;
            Shell.toast(`Polled feeds — ${total} new articles`, 'success');
            await loadHeatmap();
            await loadArticles();
        } catch (e) {
            Shell.toast('Poll failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '🔄 Poll feeds'; }
        }
    }

    // Small badge that shows which engine produced the text.
    function engineBadge(engine) {
        if (engine === 'claude') {
            return `<span class="news-engine-badge claude" title="Generated by Claude (API)">✦ Claude</span>`;
        }
        if (engine === 'claude-code') {
            return `<span class="news-engine-badge claude" title="Generated by Claude via your subscription (Claude Code bridge)">✦ Claude Code</span>`;
        }
        if (engine === 'ollama') {
            return `<span class="news-engine-badge ollama" title="Generated locally by Ollama">⌂ Ollama</span>`;
        }
        return '';
    }

    async function generateBrief(force = false) {
        const body = document.getElementById('news-brief-body');
        const btn  = document.getElementById('news-brief-btn');
        // Reflect the active engine's expected latency.
        const waitMsg = {
            'claude':      ' with Claude',
            'claude-code': ' via Claude Code (your subscription)',
            'ollama':      ' — may take 20-60 s with the local LLM',
        }[state.llmEngine] || '';
        if (body) body.innerHTML = `<span class="news-dim">⏳ Generating brief${waitMsg}…</span>`;
        if (btn)  { btn.disabled = true; btn.textContent = '⏳…'; }
        try {
            const q = new URLSearchParams({ window: state.windowH });
            if (force) q.set('force', 'true');
            const data = await Shell.api(`/api/news/brief?${q}`);
            const md = data.brief || '_No brief returned._';
            if (data.engine) state.llmEngine = data.engine;
            // Stamp the engine badge into the brief card header
            const headBadge = document.getElementById('news-brief-engine');
            if (headBadge) headBadge.innerHTML = engineBadge(data.engine);
            if (body) body.innerHTML = renderMarkdown(md);
            if (state.ttsOn) speak(stripMarkdown(md));
        } catch (e) {
            if (body) body.innerHTML = `<span class="news-dim">Brief failed: ${escapeHtml(e.message)}</span>`;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '🧠 Brief'; }
        }
    }

    // ── Assistant ──────────────────────────────────────────────────────
    async function askAssistant(text) {
        const log = document.getElementById('news-assistant-log');
        if (!log) return;

        // Disable the send button while the request is in flight
        const sendBtn = document.getElementById('news-assistant-send');
        const inp     = document.getElementById('news-assistant-input');
        if (sendBtn) sendBtn.disabled = true;
        if (inp)     inp.disabled     = true;

        appendChat('user', text);

        // Thinking indicator — replaced when the response arrives
        const thinking = _appendThinking();

        const visible = state.articles.slice(0, 30).map(a => a.id);
        try {
            const data = await Shell.api('/api/news/ask', {
                method: 'POST',
                body: JSON.stringify({
                    message: text,
                    visible_article_ids: visible,
                    country: state.selectedISO,
                }),
            });
            thinking.remove();
            const reply = data.reply || '_No reply from Runi. Is Ollama running?_';
            appendChat('bot', reply);
            if (state.ttsOn) speak(stripMarkdown(reply));
        } catch (e) {
            thinking.remove();
            // Distinguish network errors from backend errors
            const msg = e.message.includes('Failed to fetch')
                ? 'Cannot reach the shell backend. Is it running?'
                : e.message;
            appendChat('bot', `⚠ **Error:** ${msg}`);
        } finally {
            if (sendBtn) sendBtn.disabled = false;
            if (inp)     { inp.disabled = false; inp.focus(); }
        }
    }

    /** Create and append a "thinking" bubble. Returns the element so caller can remove it. */
    function _appendThinking() {
        const log = document.getElementById('news-assistant-log');
        const div = document.createElement('div');
        div.className = 'na-msg bot na-thinking';

        const avatar = document.createElement('span');
        avatar.className = 'na-avatar';
        avatar.textContent = 'Runi';

        const dots = document.createElement('span');
        dots.className = 'na-dots';
        dots.innerHTML = '<span></span><span></span><span></span>';

        div.appendChild(avatar);
        div.appendChild(dots);

        if (log) { log.appendChild(div); log.scrollTop = log.scrollHeight; }
        return div;
    }

    function appendChat(role, text) {
        const log = document.getElementById('news-assistant-log');
        if (!log) return;

        const wrap = document.createElement('div');
        wrap.className = 'na-msg ' + role;

        if (role === 'bot') {
            // Bot: avatar label + markdown body in a flex layout
            const avatar = document.createElement('span');
            avatar.className = 'na-avatar';
            avatar.textContent = 'Runi';

            const body = document.createElement('div');
            body.className = 'na-body';
            body.innerHTML = renderMarkdown(text);

            wrap.appendChild(avatar);
            wrap.appendChild(body);
        } else {
            // User: plain text with a prompt symbol
            const avatar = document.createElement('span');
            avatar.className = 'na-avatar';
            avatar.textContent = 'You';

            const body = document.createElement('div');
            body.className = 'na-body';
            body.textContent = text;

            wrap.appendChild(avatar);
            wrap.appendChild(body);
        }

        log.appendChild(wrap);
        log.scrollTop = log.scrollHeight;
        state.chatLog.push({ role, text });
    }

    // ── TTS — Runi voice (Web Speech API) ────────────────────────────
    //
    // Priority order: female neural online voices first, then desktop female,
    // then gender-neutral fallbacks.  First match in the installed voice list wins.
    //
    const TTS_PREFERRED = [
        // ── Premium online neural (female) ─────────────────────────────
        'Microsoft Aria Online (Natural) - English (United States)',   // warm, natural
        'Microsoft Sonia Online (Natural) - English (United Kingdom)', // British female
        'Microsoft Jenny Online (Natural) - English (United States)',  // clear, articulate
        'Microsoft Mia Online (Natural) - English (United Kingdom)',   // UK female
        'Microsoft Libby Online (Natural) - English (United Kingdom)', // UK female alt
        // ── Desktop female (Windows) ────────────────────────────────────
        'Microsoft Zira - English (United States)',
        'Microsoft Hazel - English (United Kingdom)',
        // ── Google female ───────────────────────────────────────────────
        'Google UK English Female',
        // ── macOS / iOS female ──────────────────────────────────────────
        'Samantha',  // macOS default female
        'Karen',     // Australian female
        'Moira',     // Irish female
        'Tessa',     // South African female
        // ── Last resort ─────────────────────────────────────────────────
        'Google US English',
    ];

    let _ttsVoice = null;   // cached after first successful pick

    function _pickVoice() {
        if (_ttsVoice) return _ttsVoice;
        const voices = window.speechSynthesis?.getVoices() || [];
        if (!voices.length) return null;

        // 1. Try the priority list first
        for (const name of TTS_PREFERRED) {
            const v = voices.find(v => v.name === name);
            if (v) { _ttsVoice = v; return v; }
        }

        // 2. Any voice with "female" in the name (some platforms use that pattern)
        const femaleName = voices.find(v => /female/i.test(v.name) && v.lang?.startsWith('en'));
        if (femaleName) { _ttsVoice = femaleName; return femaleName; }

        // 3. Female-biased heuristic — avoid names that are obviously male
        const maleName = /\b(ryan|guy|david|mark|james|richard|george|daniel|tom|william|male)\b/i;
        const fallbacks = [
            voices.find(v => v.lang === 'en-GB' && !maleName.test(v.name)),
            voices.find(v => v.lang === 'en-US' && !maleName.test(v.name)),
            voices.find(v => v.lang?.startsWith('en') && !maleName.test(v.name)),
            voices.find(v => v.lang?.startsWith('en')),
            voices[0],
        ];
        _ttsVoice = fallbacks.find(Boolean) || null;
        return _ttsVoice;
    }

    // Chrome loads voices asynchronously — reset cache when the list arrives.
    if ('speechSynthesis' in window) {
        window.speechSynthesis.addEventListener('voiceschanged', () => {
            _ttsVoice = null;
        });
    }

    // Sync the pause / stop button states to the current TTS state.
    function _updateBriefControls() {
        const pauseBtn = document.getElementById('news-brief-pause');
        const stopBtn  = document.getElementById('news-brief-stop');
        if (!pauseBtn || !stopBtn) return;

        const active = state.ttsState !== 'idle';
        const paused = state.ttsState === 'paused';

        pauseBtn.disabled    = !active;
        pauseBtn.textContent = paused ? '▶' : '⏸';
        pauseBtn.title       = paused ? 'Resume' : 'Pause';
        stopBtn.disabled     = !active;
    }

    function speak(text) {
        if (!('speechSynthesis' in window) || !text) return;
        try {
            speechSynthesis.cancel();
            const u = new SpeechSynthesisUtterance(text);
            const voice = _pickVoice();
            if (voice) {
                u.voice = voice;
                u.lang  = voice.lang;
            } else {
                u.lang = 'en-GB';
            }
            // Runi voice profile — techy-female, clear and slightly elevated
            u.rate  = 0.94;   // slightly below default: dense news text needs breath room
            u.pitch = 1.15;   // slightly higher than neutral → feminine/techy character
            u.volume = 1.0;

            u.onstart = () => { state.ttsState = 'playing'; _updateBriefControls(); };
            u.onend   = () => { state.ttsState = 'idle';    _updateBriefControls(); };
            u.onerror = () => { state.ttsState = 'idle';    _updateBriefControls(); };

            state.ttsState = 'playing';
            _updateBriefControls();
            speechSynthesis.speak(u);
        } catch (e) {
            console.warn('Runi TTS failed', e);
            state.ttsState = 'idle';
            _updateBriefControls();
        }
    }

    function briefPause() {
        if (!('speechSynthesis' in window)) return;
        if (state.ttsState === 'playing') {
            speechSynthesis.pause();
            state.ttsState = 'paused';
            _updateBriefControls();
        } else if (state.ttsState === 'paused') {
            speechSynthesis.resume();
            state.ttsState = 'playing';
            _updateBriefControls();
        }
    }

    function briefStop() {
        if (!('speechSynthesis' in window)) return;
        speechSynthesis.cancel();
        state.ttsState = 'idle';
        _updateBriefControls();
    }

    function toggleTTS() {
        state.ttsOn = !state.ttsOn;
        const btn = document.getElementById('news-tts-toggle');
        if (btn) btn.classList.toggle('tts-on', state.ttsOn);
        Shell.toast('Runi voice ' + (state.ttsOn ? 'on' : 'off'), 'info', 1800);
        if (!state.ttsOn && 'speechSynthesis' in window) {
            speechSynthesis.cancel();
            state.ttsState = 'idle';
            _updateBriefControls();
        }
    }

    // ── Watchlist ──────────────────────────────────────────────────────
    function closeOverlays() {
        document.getElementById('news-watch-overlay')?.setAttribute('hidden', '');
        document.getElementById('news-invest-overlay')?.setAttribute('hidden', '');
        document.getElementById('news-retro-overlay')?.setAttribute('hidden', '');
    }

    // ── Time Machine (retrospective intelligence) ──────────────────────
    function openRetro() {
        document.getElementById('news-retro-overlay')?.removeAttribute('hidden');
        document.getElementById('news-retro-query')?.focus();
    }
    async function runRetro() {
        const query    = document.getElementById('news-retro-query')?.value.trim();
        const question = document.getElementById('news-retro-question')?.value.trim();
        const days     = parseInt(document.getElementById('news-retro-range')?.value, 10) || 30;
        const out      = document.getElementById('news-retro-results');
        if (!query) { document.getElementById('news-retro-query')?.focus(); return; }
        if (out) out.innerHTML = '<div class="news-dim" style="padding:1rem">🕰 Searching the archive and reconstructing — 20–60s…</div>';
        try {
            const d = await Shell.api('/api/news/retro', {
                method: 'POST',
                body: JSON.stringify({ query, question: question || null, days_back: days }),
            });
            if (!d.found) {
                if (out) out.innerHTML = `<div class="news-dim" style="padding:1rem">${escapeHtml(d.brief || 'No matches.')}</div>`;
                return;
            }
            const eng = d.engine ? `<span class="news-engine-badge ${d.engine.startsWith('claude') ? 'claude' : 'ollama'}">${escapeHtml(d.engine)}</span>` : '';
            const arts = (d.articles || []).slice().reverse().map(a =>
                `<div class="news-retro-src"><span class="news-dim">${escapeHtml((a.published_at||'').slice(0,10))}</span>
                 <a href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>
                 <span class="news-dim">· ${escapeHtml(a.source||'')}</span></div>`).join('');
            if (out) out.innerHTML = `
                <div class="news-dim" style="font-size:0.76rem;margin-bottom:0.5rem">
                    ${d.matched} matches · ${d.range.from} → ${d.range.to} (${d.range.days_back}d) ${eng}
                </div>
                <div class="news-brief-body">${renderMarkdown(d.brief || '')}</div>
                <details style="margin-top:0.75rem"><summary class="news-dim" style="cursor:pointer;font-size:0.8rem">📰 Sources used (${(d.articles||[]).length})</summary>
                    <div style="margin-top:0.4rem">${arts}</div></details>`;
            if (state.ttsOn) speak(stripMarkdown(d.brief || ''));
        } catch (e) {
            if (out) out.innerHTML = `<div class="news-dim" style="padding:1rem;color:var(--danger,#ff3b5c)">Failed: ${escapeHtml(e.message)}</div>`;
        }
    }
    function openWatchlist() {
        document.getElementById('news-watch-overlay')?.removeAttribute('hidden');
        loadWatchlist();
    }
    async function loadWatchlist() {
        try {
            const d = await Shell.api('/api/news/watchlist');
            state.watchlist = d.items || [];
            renderWatchlist();
        } catch (_) {}
    }
    function renderWatchlist() {
        const el = document.getElementById('news-watch-list');
        if (!el) return;
        if (!state.watchlist.length) {
            el.innerHTML = '<div class="news-dim" style="font-size:0.8rem;padding:0.5rem 0">No items yet.</div>';
            return;
        }
        const icon = { entity: '🏷', topic: '#', country: '📍' };
        el.innerHTML = state.watchlist.map(it => `
            <div class="news-watch-item">
                <span>${icon[it.kind] || '•'} <strong>${escapeHtml(it.value)}</strong>
                    <span class="news-dim" style="font-size:0.7rem">${it.kind}</span></span>
                <button class="news-icon-btn" title="Remove"
                        onclick="NewsView.removeWatch('${escapeHtml(it.id)}')">✕</button>
            </div>`).join('');
    }
    async function addWatch(e) {
        e.preventDefault();
        const kind = document.getElementById('news-watch-kind')?.value || 'entity';
        const value = document.getElementById('news-watch-value')?.value.trim();
        if (!value) return;
        try {
            await Shell.api('/api/news/watchlist', {
                method: 'POST', body: JSON.stringify({ kind, value }),
            });
            document.getElementById('news-watch-value').value = '';
            loadWatchlist();
            Shell.toast(`Watching ${value}`, 'success', 1500);
        } catch (err) { Shell.toast('Add failed: ' + err.message, 'error'); }
    }

    // ── Stories (clustered) ────────────────────────────────────────────
    function toggleStories() {
        state.storiesMode = !state.storiesMode;
        document.getElementById('news-stories-btn')?.classList.toggle('news-btn-primary', state.storiesMode);
        loadArticles();
    }
    async function loadStories() {
        const list = document.getElementById('news-articles-list');
        if (list) list.innerHTML = '<div class="news-dim" style="padding:0.5rem 0.65rem;font-size:0.82rem">⏳ Clustering stories…</div>';
        try {
            const q = new URLSearchParams({ window: state.windowH, limit: 60 });
            const d = await Shell.api(`/api/news/stories?${q}`);
            renderStories(d.stories || []);
        } catch (e) {
            if (list) list.innerHTML = `<div class="news-dim">Stories failed: ${escapeHtml(e.message)}</div>`;
        }
    }
    function renderStories(stories) {
        const list = document.getElementById('news-articles-list');
        const cnt  = document.getElementById('news-articles-count');
        if (!list) return;
        if (cnt) cnt.textContent = `(${stories.length} stories)`;
        if (!stories.length) { list.innerHTML = '<div class="news-dim" style="padding:0.5rem">No stories.</div>'; return; }
        list.innerHTML = stories.map(s => {
            const a = s.lead || {};
            const border = TOPIC_COLOURS[a.topic] || TOPIC_DEFAULT;
            const corr = s.size > 1
                ? `<span class="news-corrob" title="${escapeHtml(s.sources.join(', '))}">✓ ${s.size} sources</span>`
                : `<span class="news-corrob news-corrob--single">1 source</span>`;
            const others = (s.members || []).slice(1, 5).map(m =>
                `<a href="${escapeHtml(m.url)}" target="_blank" rel="noopener" class="news-story-alt">${escapeHtml(m.bias?.outlet || m.source?.name || '?')}</a>`).join('');
            return `<article class="news-article" style="border-left:3px solid ${border}">
                <div class="news-article-title">
                    <a href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>
                </div>
                <div class="news-article-meta">
                    <span>${escapeHtml(a.bias?.outlet || a.source?.name || '?')}</span>
                    ${_biasBadge(a.bias)}<span>·</span>
                    <span>${formatRelative(a.published_at)}</span>
                    ${corr}
                </div>
                ${_coverageBar(s.coverage)}
                ${others ? `<div class="news-story-alts">also: ${others}</div>` : ''}
                ${_entityChips(a.entities)}
            </article>`;
        }).join('');
    }

    // ── Investigate (hand entity to Fieldwork Orchestrator) ────────────
    async function investigate(target) {
        const ov   = document.getElementById('news-invest-overlay');
        const body = document.getElementById('news-invest-body');
        const title = document.getElementById('news-invest-title');
        if (title) title.textContent = `🔬 Investigating: ${target}`;
        if (body) body.innerHTML = '<div class="news-dim" style="padding:1rem">⏳ Running multi-tool investigation — 30–90s…</div>';
        ov?.removeAttribute('hidden');
        try {
            const d = await Shell.api('/api/news/investigate', {
                method: 'POST', body: JSON.stringify({ target, type: 'auto' }),
            });
            if (d.error) { if (body) body.innerHTML = `<div class="news-dim">Failed: ${escapeHtml(d.error)}</div>`; return; }
            const eng = d.engine ? `<span class="news-engine-badge ${d.engine.startsWith('claude') ? 'claude' : 'ollama'}">${d.engine}</span>` : '';
            if (body) body.innerHTML = `
                <div style="margin-bottom:0.5rem;font-size:0.78rem" class="news-dim">
                    type: <strong>${escapeHtml(d.type || '?')}</strong> · ${(d.tools_run || []).length} tools ${eng}
                </div>
                <div class="news-brief-body">${renderMarkdown(d.brief || '_No brief._')}</div>
                <div style="margin-top:0.75rem">
                    <button class="news-btn news-btn-primary" onclick="NewsView.watchEntity('${escapeHtml(target).replace(/'/g,"\\'")}')">🎯 Add to watchlist</button>
                </div>`;
        } catch (e) {
            if (body) body.innerHTML = `<div class="news-dim">Failed: ${escapeHtml(e.message)}</div>`;
        }
    }

    // ── Helpers ────────────────────────────────────────────────────────
    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g,
            m => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[m]));
    }

    function hexToRgba(hex, alpha) {
        const h = hex.replace('#', '');
        const r = parseInt(h.slice(0, 2), 16);
        const g = parseInt(h.slice(2, 4), 16);
        const b = parseInt(h.slice(4, 6), 16);
        return `rgba(${r},${g},${b},${alpha})`;
    }

    function renderMarkdown(md) {
        let h = escapeHtml(md);
        h = h.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        h = h.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
        h = h.replace(/^# (.+)$/gm,   '<h2>$1</h2>');
        h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        h = h.replace(/\*([^*\n]+)\*/g,   '<em>$1</em>');
        h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
        h = h.replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>');
        // Wrap consecutive <li> in <ul>
        h = h.replace(/(<li>.*?<\/li>(?:\s*<li>.*?<\/li>)*)/gs, '<ul>$1</ul>');
        // Paragraphs (anything not already a block)
        h = h.split(/\n{2,}/).map(p =>
            /^<(h\d|ul|ol|li|pre)/.test(p) ? p : `<p>${p.replace(/\n/g, '<br>')}</p>`
        ).join('');
        return h;
    }

    // Convert markdown to natural speech text with breathing pauses.
    // Rules:
    //   • Headings           → spoken text + ". " (period = natural pause)
    //   • Bullet / numbered  → sentence ending with ". "
    //   • "What you need to know" → explicit long pause via extra period
    //   • Abbreviations      → expanded so TTS pronounces them correctly
    //   • Inline code/bold   → plain text
    function stripMarkdown(md) {
        return String(md || '')
            // ── Structural pauses ───────────────────────────────────────
            // h1/h2/h3 headings → spoken phrase + definite stop pause
            .replace(/^#{1,6}\s+(.+)$/gm, (_, t) => t.trim() + '. ')
            // Horizontal rules → pause beat
            .replace(/^[-_*]{3,}$/gm, '. ')
            // Bullet / unordered list items → end each as a sentence
            .replace(/^[ \t]*[-*•]\s+(.+)$/gm, (_, t) => t.trim() + '. ')
            // Numbered list items
            .replace(/^[ \t]*\d+[.)]\s+(.+)$/gm, (_, t) => t.trim() + '. ')

            // ── Specific phrase pauses ──────────────────────────────────
            // "What you need to know" — needs an extra beat after it
            .replace(/What you need to know[.:!?]?/gi,
                     'What you need to know. ')
            // Common section dividers LLMs produce
            .replace(/(?:Key takeaways?|Summary|Overview|Headlines?|Top stories?)\s*:/gi,
                     (m) => m + '. ')

            // ── Abbreviation expansion ──────────────────────────────────
            .replace(/\bU\.S\.\b/g, 'the United States')
            .replace(/\bU\.K\.\b/g, 'the United Kingdom')
            .replace(/\bE\.U\.\b/g, 'the European Union')
            .replace(/\bU\.N\.\b/g, 'the United Nations')
            .replace(/\bNATO\b/g, 'NATO')   // acronym: TTS reads letter-by-letter anyway
            .replace(/\be\.g\.\b/gi, 'for example,')
            .replace(/\bi\.e\.\b/gi, 'that is,')
            .replace(/\betc\.\b/gi, 'and so on.')
            .replace(/\bvs\.\b/gi, 'versus')
            .replace(/\bDr\.\s/g, 'Doctor ')
            .replace(/\bMr\.\s/g, 'Mister ')
            .replace(/\bMrs\.\s/g, 'Missus ')
            .replace(/\bMs\.\s/g, 'Miss ')
            .replace(/\bProf\.\s/g, 'Professor ')
            .replace(/\bSt\.\s/g, 'Saint ')

            // ── Inline formatting → plain text ──────────────────────────
            .replace(/\*\*([^*]+)\*\*/g, '$1')
            .replace(/\*([^*]+)\*/g, '$1')
            .replace(/__([^_]+)__/g, '$1')
            .replace(/_([^_]+)_/g, '$1')
            .replace(/`([^`]+)`/g, '$1')
            // Links → link text only
            .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')

            // ── Cleanup ─────────────────────────────────────────────────
            .replace(/\n+/g, ' ')
            .replace(/\s+/g, ' ')
            // Collapse multiple consecutive punctuation
            .replace(/([.!?])\s*[.!?]+\s*/g, '$1 ')
            .trim();
    }

    function formatRelative(ts) {
        if (!ts) return '';
        const t = new Date(ts);
        if (isNaN(t)) return '';
        const diff = (Date.now() - t.getTime()) / 1000;
        if (diff <  60)         return 'just now';
        if (diff <  3600)       return `${Math.floor(diff / 60)} m ago`;
        if (diff <  86400)      return `${Math.floor(diff / 3600)} h ago`;
        return `${Math.floor(diff / 86400)} d ago`;
    }

    // ── Mount / unmount ────────────────────────────────────────────────
    async function mount(root) {
        // Inject the view HTML
        const html = await fetch('/modules/news/view.html').then(r => r.text());
        root.innerHTML = html;

        // Wire toolbar
        const winSel = document.getElementById('news-window');
        if (winSel) {
            winSel.value = String(state.windowH);
            winSel.addEventListener('change', async () => {
                state.windowH = parseInt(winSel.value, 10) || 12;
                await loadHeatmap();
                await loadArticles();
            });
        }
        document.getElementById('news-poll-btn') ?.addEventListener('click', poll);
        document.getElementById('news-brief-btn')?.addEventListener('click', () => generateBrief(true));
        document.getElementById('news-tts-toggle')?.addEventListener('click', toggleTTS);

        // Watchlist + stories + investigate
        document.getElementById('news-watch-btn')?.addEventListener('click', openWatchlist);
        document.getElementById('news-watch-close')?.addEventListener('click', closeOverlays);
        document.getElementById('news-retro-btn')?.addEventListener('click', openRetro);
        document.getElementById('news-retro-close')?.addEventListener('click', closeOverlays);
        document.getElementById('news-retro-go')?.addEventListener('click', runRetro);
        document.getElementById('news-stories-btn')?.addEventListener('click', toggleStories);
        document.getElementById('news-invest-close')?.addEventListener('click', closeOverlays);
        document.getElementById('news-watch-form')?.addEventListener('submit', addWatch);
        document.querySelectorAll('.news-overlay').forEach(ov =>
            ov.addEventListener('click', e => { if (e.target === ov) closeOverlays(); }));
        loadWatchlist();
        document.getElementById('news-brief-play') ?.addEventListener('click', () => {
            const body = document.getElementById('news-brief-body');
            if (body) speak(stripMarkdown(body.innerText || body.textContent || ''));
        });
        document.getElementById('news-brief-pause')?.addEventListener('click', briefPause);
        document.getElementById('news-brief-stop') ?.addEventListener('click', briefStop);
        _updateBriefControls();   // set initial disabled state
        document.getElementById('nsc-clear')?.addEventListener('click', clearCountry);

        // Assistant form
        const form = document.getElementById('news-assistant-form');
        const inp  = document.getElementById('news-assistant-input');
        if (form && inp) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                const txt = inp.value.trim();
                if (!txt) return;
                inp.value = '';
                askAssistant(txt);
            });
        }

        // Initial render: load map, heat + articles, and check LLM readiness
        await initMap();
        await Promise.all([loadHeatmap(), loadArticles(), checkLLMStatus()]);

        // Replay any chat from previous mount (uses same appendChat so structure is consistent)
        if (state.chatLog.length) {
            // appendChat pushes to chatLog — snapshot it and clear first to avoid double-push
            const snapshot = state.chatLog.slice();
            state.chatLog = [];
            snapshot.forEach(m => appendChat(m.role, m.text));
        }
        if (state.ttsOn) {
            document.getElementById('news-tts-toggle')?.classList.add('tts-on');
        }
    }

    function unmount() {
        // Leaflet doesn't tolerate detached DOM; release the map cleanly.
        if ('speechSynthesis' in window) speechSynthesis.cancel();
        if (state.map) {
            state.map.remove();
            state.map = null;
            state.countriesLayer = null;
        }
    }

    async function removeWatch(id) {
        try {
            await Shell.api(`/api/news/watchlist/${id}`, { method: 'DELETE' });
            loadWatchlist();
        } catch (_) {}
    }
    async function watchEntity(value) {
        try {
            await Shell.api('/api/news/watchlist', {
                method: 'POST', body: JSON.stringify({ kind: 'entity', value }),
            });
            Shell.toast(`Watching ${value}`, 'success', 1500);
            loadWatchlist();
        } catch (e) { Shell.toast('Add failed: ' + e.message, 'error'); }
    }

    return { mount, unmount, selectCountry, investigate, removeWatch, watchEntity };
})();
