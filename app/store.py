"""Durable per-architecture stable index store.

Semantics
---------
* The service starts from a preloaded dual-architecture OCI index found in the
  read-only image layout (``index.json``, optionally pointing at a nested index
  blob).
* A release verifies one architecture outside any commit lock, then swaps only
  that architecture's descriptor while preserving the other architecture's
  descriptor *as of commit time*.
* The swap is guarded by an on-disk file lock and compare-and-swap on the
  target platform's previous digest.  A stale ``expectedPreviousDigest`` yields
  a conflict and nothing is written.
* The whole index is persisted atomically (temp file, fsync, rename) so the
  last successful release remains queryable after a failed release or restart.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import PRIMARY_REGISTRY, Settings
from .oci import (
    INDEX_MEDIA_TYPES,
    OCI_INDEX_MEDIA_TYPE,
    LayoutError,
    OciError,
    OciLayout,
    VerifiedRelease,
    validate_digest,
)

STATE_FILENAME = "stable-index.json"
LOCK_FILENAME = "stable-index.lock"
STATE_SCHEMA_VERSION = 1


class ConflictError(Exception):
    """The target architecture's digest no longer matches the expectation."""

    def __init__(self, platform: str, expected: str, current: str | None) -> None:
        super().__init__(
            f"{platform} baseline changed: expected {expected}, current {current or 'missing'}"
        )
        self.platform = platform
        self.expected = expected
        self.current = current


class RegistryChangedError(Exception):
    """The effective registry generation changed after a release was verified."""

    def __init__(self, registry: str, generation: int) -> None:
        super().__init__(
            f"active registry changed; now {registry!r} at generation {generation}"
        )
        self.registry = registry
        self.generation = generation


class StableCoverageError(Exception):
    """Switch validation does not cover the stable index read at commit time."""

    def __init__(
        self,
        validated: dict[str, str],
        current: dict[str, str],
        revision: int,
    ) -> None:
        super().__init__(
            "validated stable digests no longer cover commit-time stable: "
            f"validated={validated}, current={current}"
        )
        self.validated = dict(validated)
        self.current = dict(current)
        self.revision = revision


class StoreError(Exception):
    """Persisted state could not be read or written."""


@dataclass(frozen=True)
class StoreSnapshot:
    index: dict[str, Any]
    revision: int
    updated_at: str | None
    etag: str
    persisted: bool
    registry: str = PRIMARY_REGISTRY
    registry_generation: int = 0
    registry_updated_at: str | None = None


@dataclass(frozen=True)
class SwitchResult:
    previous_registry: str
    registry: str
    generation: int
    changed: bool
    revision: int
    updated_at: str | None
    registry_updated_at: str | None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _etag_for(index: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(index)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"cannot parse {path}: {exc}") from exc


def _registry_fields(state: dict[str, Any]) -> tuple[str, int, str | None]:
    """Return (active registry name, generation, switched-at).

    State files written before registry switching existed carry no such keys;
    those old volumes must keep using the original (primary) layout.
    """
    registry = state.get("registry", PRIMARY_REGISTRY)
    if not isinstance(registry, str) or not registry:
        registry = PRIMARY_REGISTRY
    generation = state.get("registryGeneration", 0)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        generation = 0
    switched_at = state.get("registryUpdatedAt")
    if not isinstance(switched_at, str):
        switched_at = None
    return registry, generation, switched_at


def stable_digest_map(index: dict[str, Any]) -> dict[str, str]:
    """architecture -> manifest digest for a stable OCI index."""
    result: dict[str, str] = {}
    for desc in index.get("manifests") or []:
        if not isinstance(desc, dict):
            continue
        platform = desc.get("platform") or {}
        arch = platform.get("architecture")
        digest = desc.get("digest")
        if isinstance(arch, str) and isinstance(digest, str):
            result[arch] = digest
    return result


class StableStore:
    def __init__(self, settings: Settings, layout: OciLayout) -> None:
        self.settings = settings
        self.layout = layout
        self.state_path = settings.state_dir / STATE_FILENAME
        self.lock_path = settings.state_dir / LOCK_FILENAME
        self._fallback: dict[str, Any] | None = None
        self._memory_lock = threading.Lock()
        try:
            settings.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Served read-only: the preloaded index still answers, but releases
            # fail loudly at commit time instead of silently not persisting.
            pass

    # -- startup -----------------------------------------------------------
    def initialize(self) -> StoreSnapshot:
        """Load persisted state, or seed state from the preloaded layout."""
        if self.state_path.exists():
            snapshot = self._snapshot_from_state(self._load_state_file())
        else:
            snapshot = self._seed_if_absent()
        with self._memory_lock:
            self._fallback = snapshot.index
        return snapshot

    def _seed_if_absent(self) -> StoreSnapshot:
        """Seed revision 0 from the layout exactly once, even across workers.

        The existence check is repeated under the cross-process lock so two
        workers booting together cannot have a late one clobber a release that
        the other already committed.
        """
        try:
            with self._file_lock():
                if self.state_path.exists():
                    # Another worker already seeded or released; it wins.
                    return self._snapshot_from_state(self._load_state_file())
                # A broken layout is a configuration error and must propagate:
                # it is not swallowed by the state-dir fallback below because
                # load_preloaded_index raises LayoutError, not StoreError/OSError.
                index = self.load_preloaded_index()
                state = {
                    "schemaVersion": STATE_SCHEMA_VERSION,
                    "ref": self.settings.index_ref,
                    "revision": 0,
                    "updatedAt": None,
                    "published": {},
                    # A freshly seeded volume starts on the original layout.
                    "registry": PRIMARY_REGISTRY,
                    "registryGeneration": 0,
                    "registryUpdatedAt": None,
                    "index": index,
                }
                try:
                    self._write_state(state)
                except StoreError:
                    # Read-only state dir: serve the preloaded index in memory.
                    return StoreSnapshot(
                        index=index,
                        revision=0,
                        updated_at=None,
                        etag=_etag_for(index),
                        persisted=False,
                    )
                return StoreSnapshot(
                    index=index,
                    revision=0,
                    updated_at=None,
                    etag=_etag_for(index),
                    persisted=True,
                )
        except (OSError, StoreError):
            # State dir truly unusable (read-only mount): serve the preloaded
            # index from memory, but still surface layout errors.
            index = self.load_preloaded_index()
            return StoreSnapshot(
                index=index,
                revision=0,
                updated_at=None,
                etag=_etag_for(index),
                persisted=False,
            )

    def load_preloaded_index(self) -> dict[str, Any]:
        raw = _read_json(self.layout.root / "index.json")
        try:
            index = self._unwrap_index(raw)
            self._validate_index(index, check_blobs=True)
        except OciError as exc:
            raise LayoutError(f"invalid preloaded stable index: {exc}") from exc
        return index

    def _unwrap_index(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise LayoutError("layout index.json must be a JSON object")
        if raw.get("schemaVersion") != 2:
            raise LayoutError("layout index.json must have schemaVersion 2")
        manifests = raw.get("manifests")
        if not isinstance(manifests, list) or not manifests:
            raise LayoutError("layout index.json must contain manifests")
        if all(isinstance(m, dict) and m.get("platform") for m in manifests):
            return copy.deepcopy(raw)
        # Common OCI layout shape: a single descriptor pointing at an index blob.
        if len(manifests) == 1 and isinstance(manifests[0], dict):
            desc = manifests[0]
            media_type = desc.get("mediaType")
            if media_type in INDEX_MEDIA_TYPES and not desc.get("platform"):
                digest = validate_digest(desc.get("digest"), what="nested index digest")
                data = self.layout.read_blob(
                    digest, max_bytes=8 * 1024 * 1024, what="nested index"
                )
                try:
                    nested = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LayoutError(f"nested index is not valid JSON: {exc}") from exc
                if not isinstance(nested, dict):
                    raise LayoutError("nested index must be a JSON object")
                return nested
        raise LayoutError(
            "layout index.json must either list platform descriptors or point at an index blob"
        )

    def _validate_index(self, index: dict[str, Any], *, check_blobs: bool) -> None:
        if index.get("schemaVersion") != 2:
            raise LayoutError("stable index schemaVersion must be 2")
        media_type = index.get("mediaType")
        if media_type not in INDEX_MEDIA_TYPES:
            raise LayoutError(f"stable index has unsupported mediaType: {media_type!r}")
        manifests = index.get("manifests")
        if not isinstance(manifests, list) or not manifests:
            raise LayoutError("stable index must contain manifests")

        seen: dict[str, dict[str, Any]] = {}
        for position, desc in enumerate(manifests):
            if not isinstance(desc, dict):
                raise LayoutError(f"stable index manifest[{position}] must be an object")
            digest = validate_digest(
                desc.get("digest"), what=f"stable index manifest[{position}] digest"
            )
            size = desc.get("size")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise LayoutError(
                    f"stable index manifest[{position}] size must be a non-negative integer"
                )
            if not self.settings.media_type_allowed(desc.get("mediaType")):
                raise LayoutError(
                    f"stable index manifest[{position}] has unsupported mediaType"
                )
            if not isinstance(desc.get("annotations", {}), dict):
                raise LayoutError(
                    f"stable index manifest[{position}] annotations must be an object"
                )
            platform = desc.get("platform")
            if not isinstance(platform, dict):
                raise LayoutError(f"stable index manifest[{position}] lacks a platform object")
            os_name = platform.get("os")
            arch = platform.get("architecture")
            if os_name != "linux" or not isinstance(arch, str) or not arch:
                raise LayoutError(
                    f"stable index manifest[{position}] platform must be linux/<arch>"
                )
            if arch not in self.settings.architectures:
                raise LayoutError(
                    f"stable index contains unconfigured architecture {arch}"
                )
            if arch in seen:
                raise LayoutError(f"stable index contains duplicate platform {arch}")
            if check_blobs:
                if not self.layout.blob_exists(digest):
                    raise LayoutError(
                        f"stable index references manifest {digest} missing from the layout"
                    )
                self._check_descriptor_matches_config(desc, digest, size, arch)
            seen[arch] = desc

        missing = set(self.settings.architectures) - set(seen)
        if missing:
            raise LayoutError(
                "stable index is missing configured architectures: "
                + ", ".join(sorted(missing))
            )

    def _check_descriptor_matches_config(
        self, desc: dict[str, Any], digest: str, size: int, arch: str
    ) -> None:
        """Cheap consistency check: manifest blob, its config, and platform."""
        data = self.layout.read_blob(digest, max_bytes=8 * 1024 * 1024, what="manifest")
        if len(data) != size:
            raise LayoutError(f"stable index size for {digest} does not match blob")
        try:
            manifest = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LayoutError(f"manifest {digest} is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
            raise LayoutError(f"manifest {digest} lacks a config descriptor")
        config_desc = manifest["config"]
        config_digest = validate_digest(
            config_desc.get("digest"), what=f"manifest {digest} config digest"
        )
        config_data = self.layout.read_blob(
            config_digest, max_bytes=4 * 1024 * 1024, what="config"
        )
        try:
            config = json.loads(config_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LayoutError(f"config {config_digest} is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise LayoutError(f"config {config_digest} must be a JSON object")
        if config.get("os") != "linux" or config.get("architecture") != arch:
            raise LayoutError(
                f"stable index descriptor for {arch} points at config platform "
                f"{config.get('os')}/{config.get('architecture')}"
            )

    # -- persistence -------------------------------------------------------
    def _load_state_file(self) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if not isinstance(state, dict):
            raise StoreError(f"{self.state_path} must contain a JSON object")
        if state.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise StoreError(f"{self.state_path} has unsupported schemaVersion")
        index = state.get("index")
        if not isinstance(index, dict):
            raise StoreError(f"{self.state_path} lacks an index document")
        try:
            # Structure and platform coverage only: the state file is the source
            # of truth for the last success and must stay readable even if the
            # read-only layout changes underneath a running deployment.
            self._validate_index(index, check_blobs=False)
        except OciError as exc:
            raise StoreError(
                f"{self.state_path} holds an invalid stable index: {exc}"
            ) from exc
        revision = state.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise StoreError(f"{self.state_path} has invalid revision")
        return state

    def _ensure_state_dir(self) -> None:
        try:
            self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"state directory is not writable: {exc}") from exc

    def _write_state(self, state: dict[str, Any]) -> None:
        payload = _canonical_json(state)
        self._ensure_state_dir()
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{STATE_FILENAME}.", dir=self.settings.state_dir
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.state_path)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
            dir_fd = os.open(self.settings.state_dir, os.O_RDONLY)
            try:
                # Best effort: the data file is already fsynced and renamed, so a
                # filesystem that refuses directory fsync must not turn a
                # successful commit into an error the caller would retry.
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise StoreError(f"cannot persist stable index: {exc}") from exc

    def _snapshot_from_state(self, state: dict[str, Any]) -> StoreSnapshot:
        index = state["index"]
        registry, generation, switched_at = _registry_fields(state)
        return StoreSnapshot(
            index=copy.deepcopy(index),
            revision=state["revision"],
            updated_at=state.get("updatedAt"),
            etag=_etag_for(index),
            persisted=True,
            registry=registry,
            registry_generation=generation,
            registry_updated_at=switched_at,
        )

    def _fallback_snapshot(self) -> StoreSnapshot:
        with self._memory_lock:
            index = self._fallback
        if index is None:
            raise StoreError("stable index is not initialized")
        return StoreSnapshot(
            index=copy.deepcopy(index),
            revision=0,
            updated_at=None,
            etag=_etag_for(index),
            persisted=False,
            registry=PRIMARY_REGISTRY,
            registry_generation=0,
            registry_updated_at=None,
        )

    def snapshot(self) -> StoreSnapshot:
        """Return the latest successfully persisted index.

        Reading the small state file on each call means multiple worker
        processes and a restarting container all observe the same last success;
        the on-disk rename is atomic so no read lock is required.
        """
        if self.state_path.exists():
            return self._snapshot_from_state(self._load_state_file())
        return self._fallback_snapshot()

    def view(self) -> tuple[StoreSnapshot, dict[str, Any]]:
        """Snapshot plus publication history from one read of the state file."""
        if self.state_path.exists():
            state = self._load_state_file()
            published = state.get("published")
            return (
                self._snapshot_from_state(state),
                dict(published) if isinstance(published, dict) else {},
            )
        return self._fallback_snapshot(), {}

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self._ensure_state_dir()
        try:
            handle = open(self.lock_path, "a+b")
        except OSError as exc:
            raise StoreError(f"cannot open state lock: {exc}") from exc
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _find_platform(index: dict[str, Any], architecture: str) -> int | None:
        for position, desc in enumerate(index["manifests"]):
            platform = desc.get("platform") or {}
            if platform.get("os") == "linux" and platform.get("architecture") == architecture:
                return position
        return None

    def commit(
        self,
        verified: VerifiedRelease,
        expected_previous_digest: str,
        *,
        expected_generation: int | None = None,
    ) -> tuple[StoreSnapshot, dict[str, Any]]:
        """Cas-swap one architecture's descriptor and persist atomically.

        When ``expected_generation`` is given it must equal the durable active
        registry generation inside the same lock; otherwise
        :class:`RegistryChangedError` is raised before any compare-and-swap so
        the caller can re-verify the candidate against the new registry.
        """
        expected_previous_digest = validate_digest(
            expected_previous_digest, what="expectedPreviousDigest"
        )
        with self._file_lock():
            # Re-read inside the lock so a concurrent writer (even in another
            # process) always forms the baseline for the compare-and-swap.
            state = self._load_state_file()
            registry, generation, registry_updated_at = _registry_fields(state)
            if (
                expected_generation is not None
                and generation != expected_generation
            ):
                raise RegistryChangedError(registry, generation)
            current = state["index"]
            position = self._find_platform(current, verified.architecture)
            if position is None:
                raise ConflictError(
                    verified.platform, expected_previous_digest, None
                )
            previous = current["manifests"][position].get("digest")
            if previous != expected_previous_digest:
                raise ConflictError(
                    verified.platform, expected_previous_digest, previous
                )

            new_index = copy.deepcopy(current)
            new_index["manifests"][position] = copy.deepcopy(verified.descriptor)
            new_index["annotations"] = {
                **(new_index.get("annotations") or {}),
                "org.opencontainers.image.ref.name": self.settings.index_ref,
            }
            new_index.setdefault("mediaType", OCI_INDEX_MEDIA_TYPE)
            new_index["schemaVersion"] = 2

            revision = state["revision"] + 1
            updated_at = _utc_now()
            published = dict(state.get("published") or {})
            published[verified.architecture] = {
                "digest": verified.manifest_digest,
                "previousDigest": previous,
                "revision": revision,
                "updatedAt": updated_at,
            }
            new_state = {
                "schemaVersion": STATE_SCHEMA_VERSION,
                "ref": self.settings.index_ref,
                "revision": revision,
                "updatedAt": updated_at,
                "published": published,
                "index": new_index,
                # A release never changes which registry is effective.
                "registry": registry,
                "registryGeneration": generation,
                "registryUpdatedAt": registry_updated_at,
            }
            self._write_state(new_state)
            snapshot = self._snapshot_from_state(new_state)
            with self._memory_lock:
                self._fallback = snapshot.index
            return snapshot, published[verified.architecture]

    def commit_switch(
        self,
        target_registry: str,
        validated_digests: dict[str, str],
    ) -> SwitchResult:
        """Atomically activate ``target_registry`` if validation still covers stable.

        The stable index, its revision and ``updatedAt`` are carried through
        untouched: switching libraries is not a release.  Only the registry
        fields change (and ``registryGeneration`` when the name changes).
        """
        with self._file_lock():
            state = self._load_state_file()
            current_digests = stable_digest_map(state["index"])
            if dict(validated_digests) != current_digests:
                raise StableCoverageError(
                    validated=validated_digests,
                    current=current_digests,
                    revision=state["revision"],
                )

            previous_registry, generation, switched_at = _registry_fields(state)
            changed = previous_registry != target_registry
            if changed:
                generation += 1
                switched_at = _utc_now()

            new_state = dict(state)
            new_state["registry"] = target_registry
            new_state["registryGeneration"] = generation
            new_state["registryUpdatedAt"] = switched_at
            # Explicitly preserve release fields even if an old state file
            # lacked the registry keys (dict(state) already references them).
            self._write_state(new_state)
            return SwitchResult(
                previous_registry=previous_registry,
                registry=target_registry,
                generation=generation,
                changed=changed,
                revision=state["revision"],
                updated_at=state.get("updatedAt"),
                registry_updated_at=switched_at,
            )
