# Investigation eval harness

A small, dependency-free harness that measures the quality of Runi-OS's AI **synthesis**
layer — the step where an LLM turns collected OSINT evidence into an intelligence brief.
It treats the synthesizer like any other RAG system and scores three things:

| Metric | Question it answers | How |
|---|---|---|
| **Grounding** | Of the concrete entities the brief asserts (emails, domains, IPs, names, orgs), what fraction are actually present in the collected evidence? | precision of extracted entities vs. evidence |
| **Hallucinations** | How many planted "trap" facts — things that never appear in the evidence — leaked into the brief? | exact-match trap detection |
| **Consistency** | Across N runs on the same target, how stable is the set of entities surfaced? | mean pairwise Jaccard of entity sets |

Entity extraction is deliberately simple and transparent (regex over emails / domains /
IPs / ETH addresses / proper-noun phrases) so every score is explainable and reproducible.
No third-party packages — standard library only.

## Run it

```bash
cd eval

# 1. Prove the harness works — deterministic, no network, no cost (use in CI):
python run_eval.py --selftest

# 2. Score the bundled reference briefs and write a report (default, offline):
python run_eval.py --offline        # -> report.md, report.json

# 3. Score the REAL running system (costs LLM credits, hits external sources).
#    Point it at a benign, synthetic, or authorized target:
python run_eval.py --live --target acme-robotics.io --type domain --runs 3
```

The stack must be up for `--live` (`docker compose up`); it calls
`POST /investigate/orchestrate` on the legacy backend (`RUNI_LEGACY_API`, default
`http://localhost:8000`), scores the returned `brief` against the collected `results`,
and reports run-to-run consistency.

## Fixtures

`fixtures/*.json` are **fully synthetic** cases (no real people or infrastructure). Each
carries the evidence a sweep would collect, a set of forbidden "trap" facts, and two
reference briefs — a faithful `good` one and a hallucinating `bad` one — so `--selftest`
can prove the metrics actually discriminate between them.

Add a case by dropping a new JSON in `fixtures/` with the same shape.

## What `--selftest` guarantees

For every fixture: the `good` brief grounds ≥ 0.85 with **zero** trap leakage; the `bad`
brief scores strictly lower grounding **and** trips ≥ 1 trap; identical runs score
consistency 1.0 while good-vs-bad scores < 1.0. If any of those breaks, it exits non-zero.
