#!/usr/bin/env python3
"""
Runi-OS investigation eval runner.

Measures the AI synthesis layer for faithfulness (grounding), hallucination leakage,
and run-to-run consistency. Three modes:

  --selftest   Deterministic. Scores the bundled good/bad reference briefs and asserts
               the metrics discriminate between them. No network, no LLM. Use in CI.

  --offline    Default. Scores the 'good' reference brief in each fixture against its
               evidence and prints/writes a report. No network, no LLM, no cost.

  --live       Calls the RUNNING investigation API for real. Costs LLM credits and hits
               external OSINT sources — run it yourself against a *benign* target.
                 python run_eval.py --live --target acme-robotics.io --type domain --runs 3

Reports are written to eval/report.md and eval/report.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scorer  # noqa: E402

# Windows consoles default to cp1252 and mangle the report's em-dash/middot; force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
FIXTURES = sorted(glob.glob(str(HERE / "fixtures" / "*.json")))


def load_fixtures() -> list[dict]:
    out = []
    for p in FIXTURES:
        with open(p, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def evidence_text(fx: dict) -> str:
    return "\n".join(str(v) for v in fx.get("evidence", {}).values())


# ── selftest ─────────────────────────────────────────────────────────────────
def run_selftest() -> int:
    fixtures = load_fixtures()
    failures = []
    for fx in fixtures:
        ev = evidence_text(fx)
        traps = fx.get("forbidden_traps", [])
        good = fx["offline_briefs"]["good"]
        bad = fx["offline_briefs"]["bad"]
        gs = scorer.score_case(good, ev, traps)
        bs = scorer.score_case(bad, ev, traps)
        cid = fx["id"]

        def check(cond, msg):
            if not cond:
                failures.append(f"[{cid}] {msg}")

        check(gs["grounding"] >= 0.85, f"good grounding too low: {gs['grounding']}")
        check(gs["hallucinations"] == 0, f"good leaked traps: {gs['hallucinated_facts']}")
        check(bs["grounding"] < gs["grounding"], "bad grounding not below good")
        check(bs["hallucinations"] >= 1, "bad brief did not trip any trap")
        # consistency: identical runs -> 1.0; good-vs-bad -> < 1.0
        same = scorer.consistency_score([good, good])["score"]
        diff = scorer.consistency_score([good, bad])["score"]
        check(same == 1.0, f"identical-run consistency != 1.0 ({same})")
        check(diff < 1.0, f"good-vs-bad consistency not < 1.0 ({diff})")

        print(f"  {cid:28s} good g={gs['grounding']:.2f} h={gs['hallucinations']}  "
              f"bad g={bs['grounding']:.2f} h={bs['hallucinations']}")

    if failures:
        print("\nSELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nSELFTEST PASSED ({len(fixtures)} fixtures)")
    return 0


# ── offline ──────────────────────────────────────────────────────────────────
def run_offline() -> int:
    fixtures = load_fixtures()
    rows = []
    for fx in fixtures:
        ev = evidence_text(fx)
        res = scorer.score_case(fx["offline_briefs"]["good"], ev, fx.get("forbidden_traps", []))
        rows.append({"case": fx["id"], "target": fx["target"], **res})
    write_report("offline (reference 'good' briefs)", rows)
    return 0


# ── live ─────────────────────────────────────────────────────────────────────
def run_live(target: str, ttype: str, runs: int, api: str) -> int:
    import urllib.request

    briefs, all_ev = [], []
    for i in range(runs):
        payload = json.dumps({"target": target, "type": ttype, "persist": False}).encode()
        req = urllib.request.Request(
            api.rstrip("/") + "/investigate/orchestrate",
            data=payload, headers={"Content-Type": "application/json"},
        )
        print(f"  run {i + 1}/{runs} -> {target} ({ttype}) ...", flush=True)
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        briefs.append(data.get("brief", ""))
        all_ev.append(json.dumps(data.get("results", {}), default=str))

    evidence = "\n".join(all_ev)
    rows = [{
        "case": f"live:{target}",
        "target": target,
        **scorer.score_case(briefs[-1], evidence, []),
        "consistency": scorer.consistency_score(briefs)["score"],
    }]
    write_report(f"live ({runs} runs, {target})", rows)
    return 0


# ── report ───────────────────────────────────────────────────────────────────
def write_report(mode: str, rows: list[dict]) -> None:
    cols = ["case", "target", "grounding", "grounded_entities", "hallucinations"]
    if any("consistency" in r for r in rows):
        cols.append("consistency")

    lines = [f"# Runi-OS investigation eval — {mode}", "",
             "| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")

    n = len(rows)
    avg_g = round(sum(r["grounding"] for r in rows) / n, 4) if n else 0
    tot_h = sum(r["hallucinations"] for r in rows)
    lines += ["", f"**Mean grounding:** {avg_g}  ·  **Total hallucinations:** {tot_h}  "
              f"·  **Cases:** {n}"]

    md = "\n".join(lines)
    (HERE / "report.md").write_text(md, encoding="utf-8")
    (HERE / "report.json").write_text(
        json.dumps({"mode": mode, "mean_grounding": avg_g,
                    "total_hallucinations": tot_h, "rows": rows}, indent=2),
        encoding="utf-8")
    print("\n" + md + f"\n\nWrote {HERE / 'report.md'} and report.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="Runi-OS investigation eval")
    ap.add_argument("--selftest", action="store_true", help="assert metrics discriminate good vs bad (CI)")
    ap.add_argument("--offline", action="store_true", help="score bundled reference briefs (default)")
    ap.add_argument("--live", action="store_true", help="call the running investigation API (costs credits)")
    ap.add_argument("--target", help="live: target to investigate")
    ap.add_argument("--type", default="auto", help="live: target type (default auto)")
    ap.add_argument("--runs", type=int, default=3, help="live: repetitions for consistency")
    ap.add_argument("--api", default=os.getenv("RUNI_LEGACY_API", "http://localhost:8000"),
                    help="live: legacy backend base URL")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest()
    if args.live:
        if not args.target:
            ap.error("--live requires --target")
        return run_live(args.target, args.type, args.runs, args.api)
    return run_offline()


if __name__ == "__main__":
    raise SystemExit(main())
