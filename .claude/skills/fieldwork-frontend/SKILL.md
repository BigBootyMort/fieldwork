---
name: fieldwork-frontend
description: >-
  Navigate and safely edit the legacy Fieldwork OSINT frontend, which is a single
  ~18k-line file at frontend/index.html (styles, markup, and all JS inline). Use this
  skill whenever the task touches the Fieldwork investigation UI or its "OSINT Tools"
  tab — adding or fixing a tool panel, a search/identify/profile/graph/map mode, tweaking
  the cyberpunk theme, or debugging a broken button/handler. Triggers include mentions of
  "fieldwork frontend", "OSINT tab/tools", "index.html", switchMode/switchToolTab, the
  Look Up / Search Graph / Build Profile / Connections modes, or any UI flaw in the
  investigation app (as opposed to the Runi Shell markets/news/agent modules, which live
  under shell/frontend/).
---

# Fieldwork legacy frontend

The entire investigation UI is one file: `frontend/index.html` (~18k lines) — CSS in a
`<style>` block, HTML markup, then all JavaScript in a trailing `<script>`. There is no
build step and no framework. nginx serves it read-only-mounted, so **edits are live on a
hard refresh (Ctrl+Shift+R)** — no rebuild needed. It's loaded both directly (port 3000)
and as an iframe inside the Runi Shell.

Because it's one giant file, **never read it top-to-bottom**. Use Grep to jump to the
relevant symbol, read a focused window, and edit surgically.

## Orientation: how to find things fast

- **A whole screen ("mode")**: markup lives in `<div id="<mode>-view" class="tab-content">`;
  the nav button is `data-mode="<mode>"`. Grep `id="<mode>-view"` and `switchMode`.
- **An OSINT tool**: tab button is `<div class="tab" data-tool="<id>">` inside `#tools-tabs`;
  its panel is `<div class="tab-content" id="tools-<id>">`; its handler is a JS function,
  usually `run<Something>()`. Grep the `data-tool` id and the panel id together.
- **A behavior/handler**: Grep the `onclick="…"` string or the function name.
- **A style**: Grep the class name in the `<style>` block (there are two theme blocks —
  see Theming below).

## Navigation model

`switchMode(mode)` (grep `function switchMode`) is the single entry point that swaps
screens: it toggles the `active` class on `#<mode>-view`, updates nav highlights, fires a
glitch animation, and runs per-mode init (`initMap`, `initGraph`, `showCasesList`, re-
activating the last tools sub-tab, …). `currentMode` holds the active mode.

Modes: `identify` (Look Up), `find` (Search Graph), `profile` (Build Profile),
`pivot` (Connections), `monitor` (Watch & Alert), `tools` (OSINT Tools),
`ai` (AI Assistant), `timeline`, `dashboard`, `graph` (Network Graph),
`map` (Geo Map), `cases`.

Inside the `tools` mode, `switchToolTab(tabId)` swaps the ~40 tool sub-panels and tracks
`currentToolTab`. To add a mode-level init side-effect, add a branch in `switchMode`; for a
tool that needs setup on first view, branch in `switchToolTab`.

## Shared helpers — always reuse these (grep to confirm signatures)

- `apiFetch(path, options={})` — the fetch wrapper. Prefixes `API`
  (`http://localhost:8000`), sets JSON content-type when there's a body, and **parses
  FastAPI's `detail` error shape** (string, or `[{loc,msg}]` validation arrays) into a
  clean `Error`. Always call the backend through this, never raw `fetch`, so errors surface
  consistently.
- `esc(str)` — HTML-escapes `& < > " '`. **Every piece of server/user data interpolated
  into innerHTML must be wrapped in `esc()`.** This is the app's only XSS defense; the tool
  panels render attacker-controlled OSINT data (usernames, org names, banners), so an
  unescaped field is a real injection bug, not a nicety.
- `setLoading(el, msg)` — spinner + message into `el`.
- `showError(el, msg)` — red error box into `el` (already escapes).
- `showToast(msg, type)` — transient bottom-right banner; `type` = `success|error|info`.
- `spin()` — returns the inline spinner span.

## The standard tool-handler shape

Every tool handler follows the same rhythm — match it so the UI behaves consistently:

```js
async function runThing() {
    const val = document.getElementById('thing-input').value.trim();
    if (!val) { alert('Enter a value'); return; }
    const el = document.getElementById('thing-results');
    el.style.display = 'block';
    setLoading(el, 'Looking up…');
    try {
        const d = await apiFetch(`/enrich/thing/${encodeURIComponent(val)}`);
        if (!d.found) { el.innerHTML = `<div class="text-dim">${esc(d.reason || 'No data')}</div>`; return; }
        el.innerHTML = `
          <div class="finding-item">
            <div class="finding-type">Result — ${esc(val)}</div>
            <div class="finding-content">${esc(d.something)}</div>
          </div>`;
    } catch (e) { showError(el, e.message); }
}
```

Note: backend enrichment responses use `{ "found": bool, "reason": str, ... }` — render the
`reason` when `found` is false rather than treating it as an error.

## Markup + class conventions

Reuse existing classes instead of inventing styles:
- Result rows: `finding-item` > `finding-type` (label) + `finding-content` (body).
- Cards: `card` > `card-title` (with an emoji `icon` span).
- Inputs: `input-group` > `label` + `input`; buttons `btn btn-primary` / `btn btn-outline`.
- Muted text: `text-dim`, `text-small`. Result containers start `style="display:none"` and
  are flipped to `block` by the handler.

## Theming (there are TWO theme blocks)

Colors come from CSS custom properties (`var(--bg)`, `--surface`, `--primary`, `--text`,
`--text-dim`, `--border`, `--danger`, `--success`, `--info`). There are **two `:root`
blocks** defining the same variables (an amber theme ~line 19 and a cyan theme ~line 3231)
— if a color change doesn't take, you're likely editing the inactive palette; grep the
variable name and check which block is live. Prefer changing a `var(--…)` value over
hardcoding hex so both light/dark and the two themes stay coherent.

Gotcha already fixed once: native `<select>` popups rendered light-gray-on-white until
`color-scheme: dark` + a `select option { background: var(--surface) }` rule were added to
the base `input, select, textarea` rule. Keep that in mind for any new dropdowns.

## Editing discipline

- Match the surrounding code's idiom (inline `onclick`, template-literal innerHTML, the
  helper set above). Don't introduce a framework, a bundler, or a new fetch pattern.
- One file means edits are easy to make unique — include enough context in Edit
  `old_string` to disambiguate (there are many similar tool panels).
- The repo uses `core.autocrlf input`; keep line endings LF. Edit with UTF-8 tooling.
- After a change, a hard refresh shows it. To actually confirm a fix, drive the UI (the
  `run` / `verify` skills) rather than only eyeballing the diff.
- **No external CDNs — vendor assets locally.** This is a localhost-only, OPSEC-sensitive
  tool; a `<script src="https://cdn…">`, external font, or map-tile URL leaks usage to that
  host and breaks offline. Third-party libs, fonts, and the world GeoJSON are vendored under
  `frontend/static/vendor/` and `static/fonts/` and served by nginx. Add new assets there,
  not from a CDN. (Opt-in `target="_blank"` "view on OSM/Google Maps" links are user-clicked
  and fine.)

## Related

- Adding a *new* end-to-end OSINT tool (backend crawler + endpoint + this UI) is its own
  workflow — see the `fieldwork-osint-tool` skill.
- Architecture, endpoints, and the LLM chain are documented in `docs/kb/` — consult those
  for backend/service facts.
