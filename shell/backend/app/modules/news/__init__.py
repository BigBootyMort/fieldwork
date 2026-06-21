"""News module — RSS ingest + choropleth heatmap + AI brief & assistant."""
import asyncio
import logging

from registry  import ModuleManifest    # /app is on sys.path (uvicorn WORKDIR)
from .service  import NewsService
from .routes   import build_router

log = logging.getLogger("news")


def init(app, deps):
    service = NewsService(deps)
    # Warm the brief on startup (poll + generate) in the background so it's
    # ready when the user first opens News. Runs inside the lifespan loop.
    try:
        asyncio.create_task(service.warm_start())
    except RuntimeError:
        log.debug("no running loop at init — skipping news warm-start")
    return build_router(service)


manifest = ModuleManifest(
    id="news",
    label="News & Brief",
    icon="📰",
    version="0.1.0",
    prefix="/api/news",
    kind="native",   # lives inside the shell SPA (not iframe)
    description="World news choropleth + LLM brief + AI assistant.",
    init=init,
)
