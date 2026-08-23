---
name: Bug report
about: Something in the stack isn't behaving
title: "[bug] "
labels: bug
---

**What happened**
A clear description of the bug.

**Expected**
What you expected instead.

**Repro**
1. …
2. …

**Which surface**
- [ ] Shell module (which: …)
- [ ] Legacy Fieldwork app
- [ ] A crawler / enrichment source (which: …)
- [ ] Investigation / AI synthesis
- [ ] Graph / Neo4j
- [ ] Infra / docker-compose

**Environment**
- OS / Docker version:
- Stack state: `docker compose ps` output (redact anything sensitive)

**Logs**
Relevant `docker compose logs <service>` (scrub API keys, targets, and personal data).
