from registry import ModuleManifest
from .routes import build_router


def init(app, deps):
    return build_router(deps)


manifest = ModuleManifest(
    id="quant",
    label="Quant",
    icon="📈",
    version="1.0.0",
    prefix="/api/quant",
    kind="native",
    description="Describe a trading strategy in plain English; Claude writes it as a "
                "NautilusTrader strategy and backtests it on historical data. Research only — "
                "no live trading.",
    init=init,
)
