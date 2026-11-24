"""FastAPI release API for a dual-architecture stable OCI index."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .oci import (
    OCI_INDEX_MEDIA_TYPE,
    LayoutError,
    OciLayout,
    ReleaseValidationError,
    validate_digest,
    verify_release,
)
from .registry import RegistryLayouts
from .store import (
    ConflictError,
    RegistryChangedError,
    StableCoverageError,
    StableStore,
    StoreError,
    stable_digest_map,
)

logger = logging.getLogger("release-api")


class ReleaseRequest(BaseModel):
    platform: str = Field(
        ...,
        description="Target platform, exactly 'linux/amd64' or 'linux/arm64'.",
        examples=["linux/amd64"],
    )
    manifestDigest: str = Field(
        ...,
        description="Digest of the new single-platform image manifest.",
        examples=["sha256:" + "0" * 64],
    )
    expectedPreviousDigest: str = Field(
        ...,
        description="Digest the caller believes is currently stable for that platform.",
        examples=["sha256:" + "1" * 64],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "platform": "linux/amd64",
                    "manifestDigest": "sha256:" + "a" * 64,
                    "expectedPreviousDigest": "sha256:" + "b" * 64,
                }
            ]
        }
    }


class SwitchRegistryRequest(BaseModel):
    """Target registry for a maintenance cut-over.

    The canonical field is ``target`` (a configured name such as ``standby``);
    the aliases accommodate operators spelling the same intent differently.
    A configured layout path is accepted too, but arbitrary paths are not.
    """

    target: str | None = Field(
        default=None,
        description="Configured registry name to activate (e.g. 'standby' or 'primary').",
        examples=["standby"],
    )
    registry: str | None = None
    name: str | None = None
    registryName: str | None = None
    targetRegistry: str | None = None
    layout: str | None = None
    layoutDir: str | None = None
    ociLayout: str | None = None
    ociLayoutDir: str | None = None
    directory: str | None = None
    path: str | None = None

    model_config = {"extra": "ignore"}

    def requested_target(self) -> str | None:
        for value in (
            self.target,
            self.registry,
            self.name,
            self.registryName,
            self.targetRegistry,
            self.layout,
            self.layoutDir,
            self.ociLayout,
            self.ociLayoutDir,
            self.directory,
            self.path,
        ):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


class ActiveRelease:
    __slots__ = ("architecture", "started", "manifest_digest")

    def __init__(self, architecture: str, manifest_digest: str) -> None:
        self.architecture = architecture
        self.started = time.monotonic()
        self.manifest_digest = manifest_digest


class ActiveSwitch:
    __slots__ = ("target", "started")

    def __init__(self, target: str) -> None:
        self.target = target
        self.started = time.monotonic()


class ReleaseService:
    def __init__(self, settings: Settings, layout: OciLayout, store: StableStore) -> None:
        self.settings = settings
        self.layout = layout
        self.store = store
        self._active: dict[int, ActiveRelease] = {}
        self._active_switches: dict[int, ActiveSwitch] = {}
        self._active_lock = threading.Lock()
        self._next_token = 0

    def begin(self, architecture: str, manifest_digest: str) -> int:
        with self._active_lock:
            self._next_token += 1
            token = self._next_token
            self._active[token] = ActiveRelease(architecture, manifest_digest)
            return token

    def end(self, token: int) -> None:
        with self._active_lock:
            self._active.pop(token, None)

    def active(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._active_lock:
            return [
                {
                    "platform": f"linux/{item.architecture}",
                    "manifestDigest": item.manifest_digest,
                    "elapsedMs": round((now - item.started) * 1000, 1),
                }
                for item in self._active.values()
            ]

    def begin_switch(self, target: str) -> int:
        with self._active_lock:
            self._next_token += 1
            token = self._next_token
            self._active_switches[token] = ActiveSwitch(target)
            return token

    def end_switch(self, token: int) -> None:
        with self._active_lock:
            self._active_switches.pop(token, None)

    def active_switches(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._active_lock:
            return [
                {
                    "target": item.target,
                    "started": True,
                    "elapsedMs": round((now - item.started) * 1000, 1),
                }
                for item in self._active_switches.values()
            ]


def _error(status: int, code: str, message: str, **details: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def _parse_platform(raw: str, settings: Settings) -> str:
    if not isinstance(raw, str) or raw.count("/") != 1:
        raise ReleaseValidationError(
            "platform must be 'linux/<architecture>'", code="invalid_platform"
        )
    os_name, arch = raw.split("/", 1)
    if os_name != "linux":
        raise ReleaseValidationError(
            f"only linux platforms are supported, got {os_name!r}", code="invalid_platform"
        )
    if arch not in settings.architectures:
        raise ReleaseValidationError(
            f"architecture {arch!r} is not configured; expected one of "
            + ", ".join(settings.architectures),
            code="invalid_platform",
        )
    return arch


def create_app(
    settings: Settings | None = None,
    *,
    layout: OciLayout | None = None,
    store: StableStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    layout = layout or OciLayout(settings.oci_layout)
    store = store or StableStore(settings, layout)
    layouts = RegistryLayouts(settings, primary_layout=layout)
    service = ReleaseService(settings, layout, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        snapshot = store.initialize()
        logger.info(
            "loaded stable index ref=%s revision=%d persisted=%s platforms=%s",
            settings.index_ref,
            snapshot.revision,
            snapshot.persisted,
            [m.get("platform", {}) for m in snapshot.index.get("manifests", [])],
        )
        yield

    app = FastAPI(
        title="Edge Stable Release API",
        version=__version__,
        description=(
            "Publishes single-architecture image manifests into a dual-architecture "
            "OCI stable index. Verification reads a local, read-only OCI image layout."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.layout = layout
    app.state.layouts = layouts
    app.state.store = store
    app.state.service = service

    @app.get("/healthz", summary="Liveness and readiness")
    def healthz() -> dict[str, Any]:
        try:
            snapshot = store.snapshot()
        except StoreError as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "reason": str(exc)},
            )
        registry_path = layouts.path_for(snapshot.registry)
        return {
            "status": "ok",
            "ref": settings.index_ref,
            "revision": snapshot.revision,
            "persisted": snapshot.persisted,
            "architectures": list(settings.architectures),
            # Current effective image library.  ``activeRegistry`` is an alias
            # for tooling that prefers the explicit name.
            "registry": snapshot.registry,
            "activeRegistry": snapshot.registry,
            "registryGeneration": snapshot.registry_generation,
            "registryPath": str(registry_path) if registry_path is not None else None,
            "activeReleases": service.active(),
            "activeSwitches": service.active_switches(),
        }

    @app.get("/v1/registry", summary="Show the effective registry and configured targets")
    def get_registry() -> Any:
        try:
            snapshot = store.snapshot()
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        return {
            "registry": snapshot.registry,
            "activeRegistry": snapshot.registry,
            "registryGeneration": snapshot.registry_generation,
            "updatedAt": snapshot.registry_updated_at,
            "revision": snapshot.revision,
            "targets": layouts.target_info(snapshot.registry),
        }

    @app.get(
        "/v1/stable",
        summary="Return the full stable OCI index",
        response_description="The complete two-architecture OCI image index.",
    )
    def get_stable(request: Request) -> Any:
        try:
            snapshot = store.snapshot()
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        etag = f'"{snapshot.etag}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache",
            "X-Index-Revision": str(snapshot.revision),
        }
        if snapshot.updated_at:
            headers["X-Index-Updated-At"] = snapshot.updated_at
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(
            status_code=200,
            content=snapshot.index,
            headers=headers,
            media_type=OCI_INDEX_MEDIA_TYPE,
        )

    @app.get("/v1/stable/{platform_arch}", summary="Return one platform descriptor")
    def get_stable_platform(platform_arch: str) -> Any:
        try:
            arch = _parse_platform(f"linux/{platform_arch}", settings)
            snapshot = store.snapshot()
        except ReleaseValidationError as exc:
            return _error(404, exc.code, str(exc))
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        for desc in snapshot.index["manifests"]:
            if desc.get("platform", {}).get("architecture") == arch:
                return JSONResponse(
                    status_code=200,
                    content=desc,
                    headers={"X-Index-Revision": str(snapshot.revision)},
                )
        return _error(404, "platform_not_found", f"no descriptor for linux/{arch}")

    @app.get("/v1/releases", summary="Publication history per architecture")
    def get_releases() -> Any:
        try:
            snapshot, published = store.view()
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        return {
            "ref": settings.index_ref,
            "revision": snapshot.revision,
            "updatedAt": snapshot.updated_at,
            "published": published,
            "active": service.active(),
        }

    @app.post(
        "/v1/releases",
        summary="Release one architecture into the stable index",
        status_code=200,
    )
    def create_release(payload: ReleaseRequest) -> Any:
        try:
            arch = _parse_platform(payload.platform, settings)
            manifest_digest = validate_digest(
                payload.manifestDigest, what="manifestDigest"
            )
            expected = validate_digest(
                payload.expectedPreviousDigest, what="expectedPreviousDigest"
            )
        except ReleaseValidationError as exc:
            return _error(422, exc.code, str(exc))

        token = service.begin(arch, manifest_digest)
        try:
            # Resolve the effective registry from shared durable state, not
            # from process configuration: another worker may have switched.
            try:
                baseline = store.snapshot()
            except StoreError as exc:
                return _error(503, "state_unavailable", str(exc))
            generation = baseline.registry_generation
            try:
                active_layout = layouts.get(baseline.registry)
            except LayoutError as exc:
                return _error(
                    503,
                    "registry_unavailable",
                    str(exc),
                    registry=baseline.registry,
                )

            # Full verification happens outside the commit lock, so a slow
            # layer scan never blocks GET /v1/stable or the other architecture.
            try:
                verified = verify_release(active_layout, settings, arch, manifest_digest)
                if verified.platform != payload.platform:
                    return _error(
                        422,
                        "platform_mismatch",
                        f"verified platform {verified.platform} != requested {payload.platform}",
                    )
            except ReleaseValidationError as exc:
                return _error(422, exc.code, str(exc))

            try:
                # If a registry switch commits after our verification, the
                # generation guard rejects the commit and we re-verify the
                # candidate in the newly effective registry before retrying.
                while True:
                    try:
                        snapshot, record = store.commit(
                            verified,
                            expected,
                            expected_generation=generation,
                        )
                        break
                    except RegistryChangedError as changed:
                        logger.info(
                            "release %s hit registry generation change -> %s@%d; "
                            "revalidating in new registry",
                            manifest_digest,
                            changed.registry,
                            changed.generation,
                        )
                        try:
                            new_layout = layouts.get(changed.registry)
                            verified = verify_release(
                                new_layout, settings, arch, manifest_digest
                            )
                            if verified.platform != payload.platform:
                                raise ReleaseValidationError(
                                    f"verified platform {verified.platform} != "
                                    f"requested {payload.platform}",
                                    code="platform_mismatch",
                                )
                        except (ReleaseValidationError, LayoutError) as exc:
                            # A release that started before the cut-over cannot
                            # be proven in the new library: conflict, stable
                            # and registry left exactly as committed.
                            code = getattr(exc, "code", "registry_validation_failed")
                            return _error(
                                409,
                                "conflict",
                                "registry switched before this release committed "
                                "and the artifact does not validate in the new registry",
                                reason="registry_revalidation_failed",
                                registry=changed.registry,
                                registryGeneration=changed.generation,
                                causeCode=code,
                                cause=str(exc),
                            )
                        generation = changed.generation
            except ConflictError as exc:
                return _error(
                    409,
                    "conflict",
                    str(exc),
                    platform=exc.platform,
                    expectedPreviousDigest=exc.expected,
                    currentDigest=exc.current,
                )
            except StoreError as exc:
                return _error(503, "state_unavailable", str(exc))
        finally:
            service.end(token)

        return JSONResponse(
            status_code=200,
            content={
                "ref": settings.index_ref,
                "revision": snapshot.revision,
                "updatedAt": snapshot.updated_at,
                "registry": snapshot.registry,
                "registryGeneration": snapshot.registry_generation,
                "released": {
                    "platform": verified.platform,
                    "previousDigest": record["previousDigest"],
                    "digest": verified.manifest_digest,
                    "descriptor": verified.descriptor,
                    "verified": {
                        "configBytes": verified.config_bytes,
                        "layers": verified.layer_count,
                        "layerBytes": verified.layer_bytes,
                        "chunks": verified.chunks,
                        "elapsedMs": verified.elapsed_ms,
                    },
                },
                "stable": snapshot.index,
            },
            headers={
                "ETag": f'"{snapshot.etag}"',
                "X-Index-Revision": str(snapshot.revision),
            },
        )

    def _switch_registry(
        payload: SwitchRegistryRequest | None = None,
        target_query: str | None = Query(default=None, alias="target"),
        target_name: str | None = None,
    ) -> Any:
        raw_target = target_query or target_name or (
            payload.requested_target() if payload is not None else None
        )
        if raw_target is not None:
            raw_target = raw_target.strip() or None
        if raw_target is None:
            return _error(
                422,
                "invalid_request",
                "request must name a 'target' registry, e.g. {'target': 'standby'}",
                available=layouts.names(),
            )
        target = layouts.resolve_name(raw_target)
        if target is None:
            return _error(
                422,
                "unknown_registry_target",
                f"{raw_target!r} is not a configured registry target",
                available=layouts.names(),
            )

        token = service.begin_switch(target)
        try:
            try:
                snapshot = store.snapshot()
            except StoreError as exc:
                return _error(503, "state_unavailable", str(exc))

            digests = stable_digest_map(snapshot.index)
            missing = [arch for arch in settings.architectures if arch not in digests]
            if missing:
                return _error(
                    503,
                    "state_unavailable",
                    "stable index is missing configured architectures: "
                    + ", ".join(sorted(missing)),
                )

            try:
                target_layout = layouts.get(target)
            except LayoutError as exc:
                return _error(
                    503,
                    "registry_unavailable",
                    str(exc),
                    target=target,
                    targetPath=str(layouts.path_for(target)),
                )

            # Validate the *current* stable digests in the target library using
            # exactly the release rules.  No lock is held here, so queries and
            # releases keep flowing throughout the (possibly slow) scan.
            validated_digests: dict[str, str] = {}
            verification: dict[str, Any] = {}
            try:
                for arch in settings.architectures:
                    digest = digests[arch]
                    verified = verify_release(target_layout, settings, arch, digest)
                    validated_digests[arch] = verified.manifest_digest
                    verification[arch] = {
                        "digest": verified.manifest_digest,
                        "configBytes": verified.config_bytes,
                        "layers": verified.layer_count,
                        "layerBytes": verified.layer_bytes,
                        "chunks": verified.chunks,
                        "elapsedMs": verified.elapsed_ms,
                    }
            except ReleaseValidationError as exc:
                logger.warning(
                    "switch to %s rejected: stable validation failed (%s): %s",
                    target,
                    exc.code,
                    exc,
                )
                return _error(
                    422,
                    "registry_validation_failed",
                    "current stable does not validate in the target registry; "
                    "staying on the current registry",
                    target=target,
                    causeCode=exc.code,
                    cause=str(exc),
                    validated=verification,
                )

            try:
                result = store.commit_switch(target, validated_digests)
            except StableCoverageError as exc:
                logger.warning(
                    "switch to %s rejected: validation does not cover commit-time stable",
                    target,
                )
                return _error(
                    409,
                    "conflict",
                    "stable changed while the target registry was being validated; "
                    "validation no longer covers stable at commit time, switch rejected",
                    reason="stable_changed_during_validation",
                    target=target,
                    validatedDigests=exc.validated,
                    currentDigests=exc.current,
                    revision=exc.revision,
                )
            except StoreError as exc:
                return _error(503, "state_unavailable", str(exc))
        finally:
            service.end_switch(token)

        logger.info(
            "registry now %s (generation %d, changed=%s); stable revision still %d",
            result.registry,
            result.generation,
            result.changed,
            result.revision,
        )
        return {
            "switched": result.changed,
            "target": target,
            "registry": result.registry,
            "activeRegistry": result.registry,
            "previousRegistry": result.previous_registry,
            "registryGeneration": result.generation,
            "registryUpdatedAt": result.registry_updated_at,
            # Explicit proof that cut-over is not a release.
            "revision": result.revision,
            "stableRevision": result.revision,
            "stableUpdatedAt": result.updated_at,
            "indexChanged": False,
            "verification": verification,
        }

    # Canonical operator endpoint plus RESTful aliases.
    app.post("/v1/registry/switch", summary="Switch the effective image registry")(
        _switch_registry
    )
    app.post("/v1/registry/switch/{target_name}", include_in_schema=False)(
        _switch_registry
    )
    app.post("/v1/registries/switch", include_in_schema=False)(_switch_registry)
    app.post("/v1/registries", include_in_schema=False)(_switch_registry)
    app.put("/v1/registry", summary="Set the effective image registry")(_switch_registry)
    app.post("/v1/registry", include_in_schema=False)(_switch_registry)
    app.put("/v1/registries/current", include_in_schema=False)(_switch_registry)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            "request body failed validation",
            errors=list(exc.errors()),
        )

    return app


# Constructed by uvicorn's factory mode (``app.main:create_app --factory``) so
# importing this module never requires a mounted layout, which keeps tests and
# one-off tooling importable.
