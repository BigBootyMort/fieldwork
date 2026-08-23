# Demo & screenshot guide

How to capture the visuals for the README and a 60-second demo reel. Assumes the stack is
up (`docker compose up --build`) and reachable at **http://localhost:3001**.

## Screenshots to capture

Put PNGs in `docs/img/` and reference them from `README.md`.

| File | What to shoot | Why it sells |
|---|---|---|
| `img/01-shell-news.png` | Shell home — News module, choropleth world map lit up, article list | Shows breadth + a polished landing view |
| `img/02-investigation-brief.png` | A completed AI investigation: synthesized brief **plus** the coverage panel (`data / blind / no_findings`) | The core differentiator — AI + honesty about gaps |
| `img/03-graph.png` | The Neo4j-backed network graph with a multi-hop entity cluster | "One provenance graph" made concrete |
| `img/04-trading-desk.png` | Trading Desk → AI Analysis (price + SMA + Bollinger band, legend, tooltip) or an equity-curve backtest | Range: it's not just OSINT |
| `img/05-module-grid.png` | The module nav / command palette (Ctrl/Cmd+K) | Architecture at a glance |

**Capture tips:** shoot at 1440p, dark theme, a realistic-but-**synthetic** target (never a
real person you investigated). Trim the browser chrome. Keep each PNG < 500 KB (run through
`pngquant`).

## The 60-second demo reel

One continuous "single target → full brief" arc — that's the wow.

| Time | Beat | Speed |
|---|---|---|
| 0–8s | Shell home, module grid visible → hero shot | real-time |
| 8–20s | Type one synthetic target into an investigation, hit run | real-time |
| 20–40s | Fan-out: tools lighting up concurrently, the **coverage panel** filling in | 3–4× speed-ramp |
| 40–52s | The **AI-synthesized brief** renders → cut to the **graph view** it built | real-time |
| 52–60s | Flourish: Trading Desk plain-English → backtest chart, or the News choropleth | real-time |

Record silent + captioned; export as MP4 and a ≤ 15 MB GIF for the top of the README.

**Tools (Windows):** [ScreenToGif](https://www.screentogif.com/) for capture + speed-ramp +
GIF export. Or capture MP4 with OBS and convert with `gifski --fps 15 --width 1000 in.mp4 -o demo.gif`.

## Reset to a clean demo state

```bash
# stop + wipe volumes for a fresh graph (destroys local data — demo boxes only)
docker compose down -v
docker compose up --build
```
Seed a couple of synthetic personas via the Identity module first, so the graph and
investigation views aren't empty on camera.
