from registry import ModuleManifest
from .routes import build_router


def init(app, deps):
    return build_router(deps)


manifest = ModuleManifest(
    id="identity",
    label="Identity Forge",
    icon="🎭",
    version="1.0.0",
    prefix="/api/identity",
    kind="native",
    description="Generate synthetic OSINT cover personas (sock puppets) for authorized research.",
    init=init,
)
