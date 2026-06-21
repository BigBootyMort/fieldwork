"""News module — RSS ingest + choropleth heatmap + AI brief & assistant."""
from registry  import ModuleManifest    # /app is on sys.path (uvicorn WORKDIR)
from .service  import NewsService
from .routes   import build_router


def init(app, deps):
    service = NewsService(deps)
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
