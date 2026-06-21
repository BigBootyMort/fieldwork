from registry import ModuleManifest
from .routes  import build_router


def init(app, deps):
    return build_router(deps)


manifest = ModuleManifest(
    id="gigs",
    label="Gig Hunter",
    icon="💼",
    version="1.0.0",
    prefix="/api/gigs",
    kind="native",
    description="Monitor freelance platforms for OSINT & research gigs.",
    init=init,
)
