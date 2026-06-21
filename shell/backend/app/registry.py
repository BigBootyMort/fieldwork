"""
Shell module registry.

A module is a self-contained feature area (Fieldwork, Calendar, News, …).
Each module ships a ModuleManifest describing itself + an init() callable
that the shell invokes at boot to register routes / background tasks.

The registry is intentionally tiny — modules are plain Python packages
under shell/backend/app/modules/{module_id}/. There is no plugin loader
magic; importing the package and calling its `manifest` is enough.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing      import Callable, List, Optional
from fastapi     import FastAPI


@dataclass
class ModuleManifest:
    id:           str                       # "fieldwork" | "news" | "calendar" | …
    label:        str                       # display name
    icon:         str                       # single emoji shown in the nav
    version:      str                       # semver — modules can declare compat
    prefix:       str                       # mount prefix for module's API routes
    # ── Rendering hints for the frontend shell ──────────────────────────────
    kind:         str = "native"            # "native" | "iframe" | "external"
    url:          Optional[str] = None      # iframe src or external URL
    # ── Backend wiring ──────────────────────────────────────────────────────
    init:         Optional[Callable] = None # (app, deps) -> Optional[APIRouter]
    dependencies: List[str] = field(default_factory=list)
    description:  str = ""

    def to_json(self) -> dict:
        """Serialisable manifest for the frontend's /api/shell/modules call."""
        return {
            "id":           self.id,
            "label":        self.label,
            "icon":         self.icon,
            "version":      self.version,
            "prefix":       self.prefix,
            "kind":         self.kind,
            "url":          self.url,
            "description":  self.description,
            "dependencies": self.dependencies,
        }


class ModuleRegistry:
    """Holds the set of registered modules and bootstraps them into FastAPI."""

    def __init__(self) -> None:
        self._mods: dict[str, ModuleManifest] = {}

    def register(self, manifest: ModuleManifest) -> None:
        if manifest.id in self._mods:
            raise ValueError(f"duplicate module: {manifest.id}")
        self._mods[manifest.id] = manifest

    def get(self, mod_id: str) -> Optional[ModuleManifest]:
        return self._mods.get(mod_id)

    def all(self) -> list[ModuleManifest]:
        return list(self._mods.values())

    def list_json(self) -> list[dict]:
        return [m.to_json() for m in self._mods.values()]

    def bootstrap(self, app: FastAPI, deps: dict) -> None:
        """Topologically initialise modules, mounting each one's router."""
        ordered = self._topo_sorted()
        for m in ordered:
            if not m.init:
                continue
            router = m.init(app, deps)
            if router is not None:
                app.include_router(router, prefix=m.prefix, tags=[m.id])

    # ── internals ───────────────────────────────────────────────────────────
    def _topo_sorted(self) -> list[ModuleManifest]:
        """Stable topological sort — modules with no deps come first."""
        ordered: list[ModuleManifest] = []
        seen:    set[str]             = set()
        visiting: set[str]            = set()

        def visit(m: ModuleManifest) -> None:
            if m.id in seen:
                return
            if m.id in visiting:
                raise RuntimeError(f"dependency cycle involving {m.id}")
            visiting.add(m.id)
            for dep_id in m.dependencies:
                dep = self._mods.get(dep_id)
                if dep is None:
                    raise RuntimeError(f"{m.id} depends on missing module: {dep_id}")
                visit(dep)
            visiting.discard(m.id)
            seen.add(m.id)
            ordered.append(m)

        for m in self._mods.values():
            visit(m)
        return ordered
