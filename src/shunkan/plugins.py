"""Plugins: how a local edition adds routes, loops and screens.

The public package never imports a plugin. It discovers them by entry point
and hands each one a Host; a plugin that raises during registration is
reported and contributes nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

GROUP = "shunkan.plugins"


@dataclass
class View:
    id: str
    code: str
    label: str
    module: str
    symbol: bool = False
    group: str = "CONTEXT"
    blurb: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Host:
    name: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    routers: list = field(default_factory=list)
    loops: list[tuple[str, Callable]] = field(default_factory=list)
    statics: list[tuple[str, Path]] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    statuses: dict[str, Callable[[], dict]] = field(default_factory=dict)
    symbol_sources: list[Callable[[str, int], list[dict]]] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def router(self, r) -> None:
        self.routers.append(r)

    def loop(self, name: str, factory: Callable) -> None:
        self.loops.append((name, factory))

    def static(self, directory) -> None:
        path = Path(directory)
        if not path.is_dir():
            raise FileNotFoundError(f"static directory not found: {path}")
        self.statics.append((self.name, path))

    def view(self, id: str, code: str, label: str, module: str, symbol: bool = False,
             group: str = "CONTEXT", blurb: str = "") -> None:
        self.views.append(View(id, code, label, module, symbol, group, blurb))

    def status(self, key: str, fn: Callable[[], dict]) -> None:
        self.statuses[key] = fn

    def symbols(self, fn: Callable[[str, int], list[dict]]) -> None:
        self.symbol_sources.append(fn)

    def _merge(self, other: "Host") -> None:
        self.routers += other.routers
        self.loops += other.loops
        self.statics += other.statics
        self.views += other.views
        self.statuses.update(other.statuses)
        self.symbol_sources += other.symbol_sources


def _candidates(plugins):
    if plugins is None:
        return [(ep.name, ep.load) for ep in entry_points(group=GROUP)]
    return [(getattr(p, "__name__", type(p).__name__), (lambda p=p: p)) for p in plugins]


def load(plugins=None, context: dict | None = None) -> Host:
    host = Host(context=dict(context or {}))
    for name, get in _candidates(plugins):
        staged = Host(name=name, context=host.context)
        try:
            get().register(staged)
        except Exception as exc:
            host.failed[name] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        host._merge(staged)
        host.loaded.append(name)
    return host
