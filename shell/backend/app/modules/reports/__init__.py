"""
Reports module — cross-source intelligence briefings.

Pulls from:
  • News module   (NewsArticle / NewsCountry nodes)
  • Fieldwork     (Case / Entity nodes — same Neo4j instance)

Generates structured Markdown reports via Ollama, stores them as
ReportDoc nodes in Neo4j for later retrieval.
"""
from registry import ModuleManifest

from .routes  import build_router
from .service import ReportsService


def init(app, deps):
    service = ReportsService(deps)
    return build_router(service)


manifest = ModuleManifest(
    id="reports",
    label="Reports",
    icon="📋",
    version="1.0.0",
    prefix="/api/reports",
    kind="native",
    description="Generate & archive AI-powered intelligence briefings from news + case data.",
    init=init,
)
