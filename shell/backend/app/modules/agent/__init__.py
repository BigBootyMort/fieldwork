from registry import ModuleManifest
from .routes  import build_router


def init(app, deps):
    return build_router(deps)


manifest = ModuleManifest(
    id="agent",
    label="Agent",
    icon="🤖",
    version="1.0.0",
    prefix="/api/agent",
    kind="native",
    description="Autonomous AI Agent — plan, execute, and monitor multi-step tasks.",
    init=init,
)
