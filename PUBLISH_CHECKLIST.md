# Publishing Runi-OS — go-public checklist

Status: repo is **private**. This branch (`showcase-prep`) is being prepared so you can
flip visibility to public later without a scramble. This file is a maintainer note —
**delete it before/after going public** if you don't want it in the showcase.

---

## ✅ Already done (on `showcase-prep`)

- **Secrets audit** — no API keys in any tracked file; runtime secret files
  (`runtime_api_keys.json`, `key_verifications.json`, `agent_config.json`, `.env`) were
  **never committed** (verified across full history). `.env.example` is a safe template
  (all keys blank/placeholder).
- **Machine-local / junk removed** — `.mcp.json` (leaked an absolute host path),
  `AERIS10_BUILD_PLAN.md` (unrelated), and two stray `ruvector.db` binaries that slipped
  in before the gitignore rule. `.mcp.json` is now gitignored.
- **PII scrubbed from the working tree** — operator handle removed from `CLAUDE.md` and
  `docs/kb/architecture.md`.
- **LICENSE** — MIT added (⚠️ replace the `Runi-OS` placeholder with your legal name).
- **README** — public showcase rewrite (badges, Mermaid architecture, comparison table).
- **Rebrand (visible surfaces)** — see below.

## 🎨 Rebrand: Fieldwork → Runi-OS

**Done (everything a viewer sees):**
- Browser titles — `frontend/index.html`, `shell/frontend/index.html`
- In-app header logos — legacy app (`// RUNI-OS OSINT`) and shell (`// RUNI-OS`)
- `.env.example` header + `SEC_USER_AGENT`
- `README.md` (full)

**Deliberately kept (renaming these unattended would break the running stack):**
| Identifier | Where | Why kept |
|---|---|---|
| `FIELDWORK_API`, `FIELDWORK_FRONT` env vars | code + compose (21 refs) | read by `deps.py`, both `main.py`, the fieldwork module, news/reports — a partial rename = `None` at runtime |
| `container_name: fieldwork-*` | `docker-compose.yml` | cosmetic; referenced by `docker exec/logs` in docs |
| module `id="fieldwork"` | shell registry | changing the id breaks route registration + the iframe module |
| `.claude/skills/fieldwork-*` filenames | dev tooling | internal, not user-facing |

**To finish the deep rename later (do it with the stack UP so you can verify):**
```bash
# 1. rename the env vars everywhere, then rebuild + smoke-test
git grep -l 'FIELDWORK_' | xargs sed -i 's/FIELDWORK_API/RUNI_API/g; s/FIELDWORK_FRONT/RUNI_FRONT/g'
# 2. rename container_names (cosmetic)
sed -i 's/container_name: fieldwork-/container_name: runi-/' docker-compose.yml
docker compose up -d --build         # verify all 16 services + the legacy iframe still load
```
Leave the `fieldwork` **module id** alone unless you also migrate its frontend manifest + routes.

## ⚠️ Git history still contains

The working tree is clean, but **history** (old commits) still holds: the operator handle,
the `C:\Users\…` path from `.mcp.json`, and the removed `ruvector.db` blobs. If you flip
**this same repo** to public, those remain visible in the commit log.

**Options before going public:**
- **Simplest — squash to a clean initial commit** (loses granular history, which is fine for a showcase):
  ```bash
  git checkout --orphan public-main showcase-prep
  git commit -m "Runi-OS — initial public release"
  # push public-main as the new default branch
  ```
- **Surgical — rewrite just the offending paths** with [`git filter-repo`](https://github.com/newren/git-filter-repo):
  ```bash
  git filter-repo --path .mcp.json --path shell/frontend/ruvector.db \
                  --path shell/host-bridge/ruvector.db --invert-paths
  ```

## 📋 Final human steps (when you decide to publish)

- [ ] Set your real name in `LICENSE`.
- [ ] Capture screenshots into `docs/img/` and wire them into the README (see `docs/DEMO.md`).
- [ ] Decide on git-history handling (above).
- [ ] (Optional) rename the GitHub repo `fieldwork` → `runi-os` and update the clone URL in the README.
- [ ] Flip repo visibility to **public**.
