"""
Fieldwork module — wraps the existing OSINT app as an iframe-backed module.

Because Fieldwork already has its own FastAPI service running on port 8000,
this module's init() does NOT mount any routes on the shell backend.
It exists purely to register the manifest so the frontend nav shows Fieldwork.

Later, if Fieldwork is ported to live natively in-shell, the init() can
be expanded to mount its routes here under /api/fieldwork/.
"""
from registry import ModuleManifest   # /app is on sys.path (uvicorn WORKDIR)


def init(app, deps):
    """No-op — Fieldwork keeps its own backend at RUNI_API."""
    return None


manifest = ModuleManifest(
    id="fieldwork",
    label="Fieldwork OSINT",
    icon="🔍",
    version="1.0.0",
    prefix="/api/fieldwork",
    kind="iframe",
    url=None,           # frontend resolves the iframe src from settings.RUNI_FRONT
    description="OSINT investigation platform — graph, cases, enrichment, MITRE.",
    init=init,
)
