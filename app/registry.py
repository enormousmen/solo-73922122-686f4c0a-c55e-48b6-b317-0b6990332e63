"""Named, read-only OCI layout targets and active-registry resolution.

The *active* registry name is durable state owned by :class:`~app.store.StableStore`
(shared ``/state`` volume).  This module only maps a target name to its fixed
on-disk :class:`~app.oci.OciLayout` and caches the opened layout per process.
Because every request re-reads the durable active name, all uvicorn workers and
restarted processes converge on the same effective registry without any
in-process gossip.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .config import PRIMARY_REGISTRY, Settings
from .oci import LayoutError, OciLayout


def layout_ready(path: Path) -> bool:
    """Cheap mount check: marker, index.json and blob directory are present."""
    try:
        root = Path(path)
        return (
            (root / "oci-layout").is_file()
            and (root / "index.json").is_file()
            and (root / "blobs" / "sha256").is_dir()
        )
    except OSError:
        return False


class RegistryLayouts:
    """Resolves configured registry names to cached read-only OCI layouts."""

    def __init__(
        self,
        settings: Settings,
        *,
        primary_layout: OciLayout | None = None,
    ) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._layouts: dict[str, OciLayout] = {}
        if primary_layout is not None:
            self._layouts[PRIMARY_REGISTRY] = primary_layout

    def names(self) -> list[str]:
        return list(self.settings.registry_dirs.keys())

    def path_for(self, name: str) -> Path | None:
        return self.settings.registry_dirs.get(name)

    def resolve_name(self, raw: str) -> str | None:
        """Map an operator-supplied name or configured path to a target name."""
        candidate = raw.strip()
        if not candidate:
            return None
        if candidate in self.settings.registry_dirs:
            return candidate
        try:
            supplied = Path(candidate).resolve()
        except OSError:
            return None
        for name, path in self.settings.registry_dirs.items():
            try:
                if supplied == path.resolve():
                    return name
            except OSError:
                continue
        return None

    def get(self, name: str) -> OciLayout:
        """Return an opened layout for ``name``.

        Construction is lazy so an absent optional standby mount never breaks
        service startup.  :class:`LayoutError` propagates for a missing/broken
        target.
        """
        with self._lock:
            cached = self._layouts.get(name)
            if cached is not None:
                return cached
            path = self.settings.registry_dirs.get(name)
            if path is None:
                raise LayoutError(f"unknown registry target {name!r}", code="unknown_registry_target")
            layout = OciLayout(path)
            self._layouts[name] = layout
            return layout

    def target_info(self, active_name: str) -> list[dict[str, object]]:
        info: list[dict[str, object]] = []
        for name, path in self.settings.registry_dirs.items():
            info.append(
                {
                    "name": name,
                    "path": str(path),
                    "active": name == active_name,
                    "layoutReady": layout_ready(path),
                }
            )
        return info
