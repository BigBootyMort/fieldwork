# Runi Shell

The shell is a thin chrome that hosts Fieldwork + future modules (News,
Calendar, Files, Earn) behind a single unified UI.

## Why a shell?

The existing Fieldwork app is ~10 k lines of backend Python and ~18 k lines
of frontend HTML/JS. A monolithic single-page app made sense when there was
one tool. As you add Calendar, News, File storage, an Earning workflow,
etc. that monolith becomes unworkable.

The shell:

- Owns top-level navigation, theming, command palette, toast notifications,
  user/auth state, and cross-module events
- Hosts each feature as a **module** — a self-contained package with its own
  backend routes (mounted under `/api/{module_id}/`) and its own frontend view
- Lets modules talk to each other through a small in-process event bus
  (`Shell.emit('case_opened', {id})` → News module highlights related stories)

## File layout

```
shell/
├── README.md                       you are here
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                 FastAPI bootloader
│       ├── registry.py             ModuleRegistry + ModuleManifest
│       ├── deps.py                 graph_db / http / bus / settings
│       └── modules/
│           ├── __init__.py
│           ├── fieldwork/          ← iframe wrapper around legacy app
│           │   └── __init__.py
│           ├── news/               (next to build)
│           ├── calendar/           (planned)
│           ├── files/              (planned)
│           └── earn/               (planned, narrow scope)
└── frontend/
    ├── Dockerfile
    ├── nginx.conf                  serves SPA + proxies /api/shell/
    ├── index.html                  shell chrome (header, nav, root, palette)
    ├── shell.js                    registry, switch(), bus, palette, toasts
    ├── style.css                   theme tokens + shell-level styles
    └── modules/
        ├── fieldwork/              (empty — iframe URL comes from backend manifest)
        ├── news/                   (next to build — native module)
        ├── calendar/
        ├── files/
        └── earn/
```

## How modules are wired

There are **two kinds** of modules:

### Native module (recommended for all new ones)

Lives entirely inside the shell SPA. Its frontend `manifest.js` calls
`Shell.register({...})` to declare itself. Its backend routes are mounted
on `shell-backend` under `/api/{module_id}/`. Shares JS context with the
shell — can use `Shell.api()`, `Shell.toast()`, `Shell.emit()`, etc.

### Iframe module (for legacy code only)

Loaded into an `<iframe>` so the legacy app needs zero refactor. Used right
now for **Fieldwork** because porting 18 k LOC of JS in one go is reckless.
Cross-frame talk via `postMessage` (the shell sends `shell:event` messages,
modules send back `module:event`).

## Run it

The shell adds two services to `docker-compose.yml`:

```bash
docker compose up -d --build shell-backend shell-frontend
```

Then open:

| URL                              | What you get                                |
|----------------------------------|---------------------------------------------|
| http://localhost:3001            | **Runi shell** (the new home page)          |
| http://localhost:3000            | Direct Fieldwork (legacy, still works)      |
| http://localhost:8002/api/shell/modules | Module registry as JSON                     |
| http://localhost:8002/docs       | Shell backend Swagger                       |

## Adding a new native module

1. Create the backend package:

   ```bash
   mkdir -p shell/backend/app/modules/news
   touch    shell/backend/app/modules/news/__init__.py
   ```

   In `__init__.py`:

   ```python
   from fastapi import APIRouter
   from ...registry import ModuleManifest

   def init(app, deps):
       router = APIRouter()

       @router.get("/feeds")
       async def list_feeds():
           ...

       return router

   manifest = ModuleManifest(
       id="news", label="News & Brief", icon="📰",
       version="0.1.0", prefix="/api/news",
       kind="native",
       init=init,
   )
   ```

2. Register it in `shell/backend/app/main.py`:

   ```python
   from modules.news import manifest as news_manifest
   registry.register(news_manifest)
   ```

3. nginx: nothing to do. `shell/frontend/nginx.conf` has a single catch-all
   `location /api/` that proxies every `/api/*` call to the backend, so a newly registered
   `/api/<id>/` route is reachable immediately — no per-module proxy rule.

4. Create the frontend manifest:

   ```bash
   mkdir -p shell/frontend/modules/news
   ```

   `shell/frontend/modules/news/manifest.js`:

   ```javascript
   Shell.register({
       id:    'news',
       label: 'News & Brief',
       icon:  '📰',
       kind:  'native',
       async mount(root) {
           const html = await fetch('/modules/news/view.html').then(r => r.text());
           root.innerHTML = html;
           // ... attach handlers, fetch data ...
       },
       async unmount() { /* cleanup */ },
       palette: [
           { icon: '📰', label: 'Generate morning brief', action: () => /* ... */ },
       ],
   });
   ```

5. Wire it into `shell/frontend/index.html`:

   ```html
   <script src="/modules/news/manifest.js"></script>
   ```

6. Rebuild and restart:

   ```bash
   docker compose up -d --build shell-backend shell-frontend
   ```

That's it. The new module shows up in the nav with no other code changes.

## Roadmap

- [x] Shell skeleton — Fieldwork loads as iframe module
- [x] **News module — choropleth map + LLM brief + AI assistant + TTS hooks**
- [ ] Calendar module — native; integrates with Fieldwork watchlist re-enrich
- [ ] Files module — extends Fieldwork Evidence Locker, adds general storage
- [ ] Earn module — one narrow workflow first, not a generic earn dashboard

## News module — first-use

After `docker compose up -d --build shell-backend shell-frontend`:

1. Open **http://localhost:3001** and click **📰 News & Brief** in the nav
2. Click **🔄 Poll feeds** — this fetches RSS from ~11 default sources (BBC,
   Reuters, AP via NPR mirror, Guardian, Al Jazeera, DW, Hacker News,
   Ars Technica, Krebs, BleepingComputer, The Record). Takes 10-30 seconds.
3. Watch the world map colour up. Hover any country for a top-headline tooltip.
4. Click a country to filter the article list to that country.
5. Click **🧠 Brief** to generate a Markdown morning brief with Ollama (20-60 s
   on first run).
6. Ask Runi questions at the bottom — "summarise the Ukraine items",
   "what's happening in tech?", etc. Toggle 🔈 TTS for spoken replies.

Heat scoring: `source_weight × topic_weight × exp(-age_h / 12)`. Topic
auto-bumps for war / disaster / crisis / security keywords.
