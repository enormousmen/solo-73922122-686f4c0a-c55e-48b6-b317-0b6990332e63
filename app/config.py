"""Typed configuration loaded from environment variables.

Only the environment is consulted so that the same image can run under Docker
Compose with a read-only bind mount and no secrets on the command line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ARCHITECTURES: tuple[str, ...] = ("amd64", "arm64")
PRIMARY_REGISTRY = "primary"
STANDBY_REGISTRY = "standby"
DEFAULT_STANDBY_LAYOUT = "/oci-layout-standby"


class ConfigError(ValueError):
    """Raised when the process environment is not usable."""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the release service."""

    oci_layout: Path
    state_dir: Path
    index_ref: str = "registry.example.com/edge/stable:latest"
    architectures: tuple[str, ...] = DEFAULT_ARCHITECTURES
    layer_chunk_bytes: int = 64 * 1024
    layer_chunk_delay: float = 0.0
    # Named, content-fixed read-only OCI layouts the operator may switch
    # between.  ``primary`` always equals ``oci_layout``; additional targets
    # come from OCI_STANDBY_LAYOUT_DIR / OCI_REGISTRY_LAYOUTS.  The active
    # target is *not* configuration: it lives in the shared state volume so
    # every worker and every restart observes the same effective registry.
    registry_dirs: dict[str, Path] = field(default_factory=dict)
    allowed_media_types: tuple[str, ...] = field(
        default_factory=lambda: (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )

    def layout_dir_for(self, name: str) -> Path | None:
        return self.registry_dirs.get(name)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        raw_layout = env.get("OCI_LAYOUT_DIR", "/oci-layout")
        raw_state = env.get("STATE_DIR", "/state")
        raw_archs = env.get("OCI_ARCHITECTURES", ",".join(DEFAULT_ARCHITECTURES))
        raw_ref = env.get("OCI_INDEX_REF", "registry.example.com/edge/stable:latest")
        raw_chunk = env.get("LAYER_CHUNK_BYTES", str(64 * 1024))
        raw_chunk_delay = env.get("LAYER_CHUNK_DELAY_MS", "0")
        raw_standby = env.get("OCI_STANDBY_LAYOUT_DIR", DEFAULT_STANDBY_LAYOUT)
        raw_extra_targets = env.get("OCI_REGISTRY_LAYOUTS", "")

        architectures = tuple(_split(raw_archs))
        if not architectures:
            raise ConfigError("OCI_ARCHITECTURES must contain at least one architecture")
        for arch in architectures:
            if "/" in arch or not arch:
                raise ConfigError(f"invalid architecture name: {arch!r}")
        if len(set(architectures)) != len(architectures):
            raise ConfigError("OCI_ARCHITECTURES contains duplicates")

        try:
            chunk = int(raw_chunk)
        except ValueError as exc:
            raise ConfigError("LAYER_CHUNK_BYTES must be an integer") from exc
        if chunk < 1 or chunk > 16 * 1024 * 1024:
            raise ConfigError("LAYER_CHUNK_BYTES must be between 1 and 16777216")

        try:
            chunk_delay_ms = float(raw_chunk_delay)
        except ValueError as exc:
            raise ConfigError("LAYER_CHUNK_DELAY_MS must be numeric") from exc
        if not 0 <= chunk_delay_ms <= 1000:
            raise ConfigError("LAYER_CHUNK_DELAY_MS must be between 0 and 1000")

        if not raw_ref.strip() or any(c.isspace() for c in raw_ref):
            raise ConfigError("OCI_INDEX_REF must be a non-empty reference without spaces")

        registry_dirs: dict[str, Path] = {PRIMARY_REGISTRY: Path(raw_layout)}
        if raw_standby.strip():
            registry_dirs[STANDBY_REGISTRY] = Path(raw_standby.strip())
        # Generic form: OCI_REGISTRY_LAYOUTS="mirror=/layouts/mirror,dr=/layouts/dr"
        for entry in _split(raw_extra_targets):
            if "=" not in entry:
                raise ConfigError(
                    "OCI_REGISTRY_LAYOUTS entries must be 'name=/path', got "
                    f"{entry!r}"
                )
            target_name, target_path = entry.split("=", 1)
            target_name = target_name.strip()
            target_path = target_path.strip()
            if not target_name or not target_path:
                raise ConfigError(
                    f"OCI_REGISTRY_LAYOUTS entry {entry!r} has an empty name or path"
                )
            if "/" in target_name or not all(
                ch.isalnum() or ch in "_-" for ch in target_name
            ):
                raise ConfigError(
                    f"invalid registry target name {target_name!r}; use [A-Za-z0-9_-]"
                )
            if target_name == PRIMARY_REGISTRY:
                raise ConfigError(
                    f"{PRIMARY_REGISTRY!r} registry target is fixed by OCI_LAYOUT_DIR"
                )
            registry_dirs[target_name] = Path(target_path)

        return cls(
            oci_layout=Path(raw_layout),
            state_dir=Path(raw_state),
            index_ref=raw_ref.strip(),
            architectures=architectures,
            layer_chunk_bytes=chunk,
            layer_chunk_delay=chunk_delay_ms / 1000.0,
            registry_dirs=registry_dirs,
        )

    def media_type_allowed(self, media_type: object) -> bool:
        return isinstance(media_type, str) and media_type in self.allowed_media_types
