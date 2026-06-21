"""
Markets module — stock & crypto price proxy.

Proxies Yahoo Finance (equities) server-side to avoid browser CORS issues.
Crypto is fetched client-side from CoinGecko (CORS-open free API).
"""
from registry import ModuleManifest
from .routes  import build_router


def init(app, deps):
    return build_router(deps)


manifest = ModuleManifest(
    id="markets",
    label="Markets",
    icon="📈",
    version="1.0.0",
    prefix="/api/markets",
    kind="native",
    description="Live stock & crypto prices with sparklines and market news.",
    init=init,
)
