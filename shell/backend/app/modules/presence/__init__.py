from registry import ModuleManifest
from .routes  import build_router


def init(app, deps):
    return build_router(deps)


manifest = ModuleManifest(
    id="presence",
    label="Presence",
    icon="📣",
    version="1.0.0",
    prefix="/api/presence",
    kind="native",
    description="Generate content, platform listings, and outreach for your OSINT freelance business.",
    init=init,
)
