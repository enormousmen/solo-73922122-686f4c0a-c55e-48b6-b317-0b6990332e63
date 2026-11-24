"""Registry cut-over: validation, optimistic coverage, and commit races.

These tests pin the maintenance switch semantics:

* switching validates the current stable manifests/configs/layers in the
  target layout using the ordinary release rules;
* a switch whose validation no longer covers commit-time stable is rejected
  (409) and the current registry stays effective;
* switch does not change the stable index or its revision;
* a release that started before the cut-over must re-validate in the new
  registry before committing, otherwise it gets a 409;
* the effective registry is durable shared state, so a second app/worker and
  a restart observe it, while old state files default to ``primary``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.oci import OciLayout, VerifiedRelease
from app.store import RegistryChangedError, StableStore
from tests.conftest import make_client, stable_by_arch
from scripts.build_layout import build_demo_layout


SMALL_SIZES = {
    "v1": {"amd64": 64 * 1024, "arm64": 8 * 1024},
    "v2": {"amd64": 128 * 1024, "arm64": 8 * 1024},
    "v3": {"amd64": 128 * 1024, "arm64": 8 * 1024},
}


def _body(meta: dict, version: str, arch: str, previous: str) -> dict:
    return {
        "platform": f"linux/{arch}",
        "manifestDigest": meta["versions"][version][arch]["digest"],
        "expectedPreviousDigest": meta["versions"][previous][arch]["digest"],
    }


@pytest.fixture
def switch_env(tmp_path: Path):
    primary_dir = tmp_path / "primary"
    min_dir = tmp_path / "standby-min"
    v2_dir = tmp_path / "standby-v2"
    broken_dir = tmp_path / "standby-broken"
    state_dir = tmp_path / "state"

    primary_meta = build_demo_layout(
        primary_dir, tmp_path / "primary.json", payload_sizes=SMALL_SIZES
    )
    # Same builder parameters -> identical v1 digests to the primary.
    build_demo_layout(
        min_dir, tmp_path / "min.json", versions=("v1",), payload_sizes=SMALL_SIZES
    )
    # Also mirrors both v2 candidates, so revalidation-after-switch can pass.
    v2_meta = build_demo_layout(
        v2_dir, tmp_path / "v2.json", versions=("v1", "v2"), payload_sizes=SMALL_SIZES
    )
    broken_dir.mkdir()

    def make_settings(standby_dir: Path = min_dir, **extra: str) -> Settings:
        env = {
            "OCI_LAYOUT_DIR": str(primary_dir),
            "OCI_STANDBY_LAYOUT_DIR": str(standby_dir),
            "STATE_DIR": str(state_dir),
            "OCI_ARCHITECTURES": "amd64,arm64",
            "LAYER_CHUNK_BYTES": "1024",
        }
        env.update(extra)
        return Settings.from_env(env)

    return SimpleNamespace(
        tmp=tmp_path,
        primary_dir=primary_dir,
        min_dir=min_dir,
        v2_dir=v2_dir,
        broken_dir=broken_dir,
        state_dir=state_dir,
        meta=primary_meta,
        v2_meta=v2_meta,
        make_settings=make_settings,
    )


# ---------------------------------------------------------------------------
# Discovery / defaults
# ---------------------------------------------------------------------------


def test_healthz_reports_primary_by_default(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        health = client.get("/healthz").json()
    assert health["registry"] == "primary"
    assert health["activeRegistry"] == "primary"
    assert health["registryGeneration"] == 0
    assert health["activeSwitches"] == []


def test_registry_endpoint_lists_configured_targets(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        body = client.get("/v1/registry").json()
    assert body["registry"] == "primary"
    targets = {t["name"]: t for t in body["targets"]}
    assert targets["primary"]["active"] is True
    assert targets["standby"]["active"] is False
    assert targets["standby"]["layoutReady"] is True


def test_unknown_target_rejected(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        response = client.post("/v1/registry/switch", json={"target": "dr-site"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_registry_target"
    assert response.json()["error"]["details"]["available"] == ["primary", "standby"]


def test_missing_target_rejected(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        response = client.post("/v1/registry/switch", json={})
    assert response.status_code == 422
    assert "standby" in response.json()["error"]["details"]["available"]


def test_target_can_be_a_configured_path_or_alias_field(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        by_path = client.post(
            "/v1/registry/switch", json={"target": str(switch_env.min_dir)}
        )
        assert by_path.status_code == 200, by_path.text
        assert by_path.json()["registry"] == "standby"

        # Idempotent: same target does not bump the generation.
        again = client.put("/v1/registry", json={"registryName": "standby"})
        assert again.status_code == 200
        assert again.json()["switched"] is False
        assert again.json()["registryGeneration"] == 1


# ---------------------------------------------------------------------------
# Successful switch and persistence
# ---------------------------------------------------------------------------


def test_successful_switch_persists_and_survives_restart(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        response = client.post("/v1/registry/switch", json={"target": "standby"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["switched"] is True
        assert body["previousRegistry"] == "primary"
        assert body["registry"] == "standby"
        assert body["registryGeneration"] == 1
        assert body["revision"] == 0
        assert set(body["verification"]) == {"amd64", "arm64"}

        health = client.get("/healthz").json()
        assert health["registry"] == "standby"
        assert health["registryGeneration"] == 1

    # A brand new app instance + store over the same state dir (new worker or
    # restarted container) must use the switched library.
    with make_client(switch_env.make_settings()) as restarted:
        health = restarted.get("/healthz").json()
        assert health["registry"] == "standby"
        assert health["registryGeneration"] == 1
        assert restarted.get("/v1/registry").json()["registry"] == "standby"


def test_switch_does_not_change_index_revision_or_etag(switch_env):
    # standby-v2 mirrors v2 so the post-release stable validates there too.
    settings = switch_env.make_settings(standby_dir=switch_env.v2_dir)
    with make_client(settings) as client:
        released = client.post(
            "/v1/releases",
            json=_body(switch_env.meta, "v2", "amd64", "v1"),
        )
        assert released.status_code == 200, released.text
        before = client.get("/v1/stable")
        before_json = before.json()
        before_etag = before.headers["etag"]
        assert before.headers["x-index-revision"] == "1"

        switched = client.post("/v1/registry/switch", json={"target": "standby"})
        assert switched.status_code == 200, switched.text
        assert switched.json()["revision"] == 1
        assert switched.json()["indexChanged"] is False

        after = client.get("/v1/stable")
        assert after.json() == before_json
        assert after.headers["etag"] == before_etag
        assert after.headers["x-index-revision"] == "1"

    state = json.loads((switch_env.state_dir / "stable-index.json").read_text())
    assert state["revision"] == 1
    assert state["registry"] == "standby"
    assert state["registryGeneration"] == 1


def test_can_switch_back_to_primary(switch_env):
    settings = switch_env.make_settings(standby_dir=switch_env.v2_dir)
    with make_client(settings) as client:
        assert client.post(
            "/v1/registry/switch", json={"target": "standby"}
        ).status_code == 200
        back = client.post("/v1/registry/switch", json={"target": "primary"})
        assert back.status_code == 200
        body = back.json()
        assert body["switched"] is True
        assert body["previousRegistry"] == "standby"
        assert body["registry"] == "primary"
        # Each real change bumps the generation monotonically.
        assert body["registryGeneration"] == 2
        assert client.get("/healthz").json()["registry"] == "primary"


# ---------------------------------------------------------------------------
# Validation failures keep the current library
# ---------------------------------------------------------------------------


def test_switch_rejected_when_target_misses_stable_blob(switch_env):
    meta = switch_env.meta
    arm_v1_hex = meta["versions"]["v1"]["arm64"]["digest"].split(":", 1)[1]
    (switch_env.min_dir / "blobs" / "sha256" / arm_v1_hex).unlink()

    settings = switch_env.make_settings()
    with make_client(settings) as client:
        response = client.post("/v1/registry/switch", json={"target": "standby"})
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "registry_validation_failed"
        assert error["details"]["causeCode"] == "blob_not_found"

        assert client.get("/healthz").json()["registry"] == "primary"

    state = json.loads((switch_env.state_dir / "stable-index.json").read_text())
    assert state.get("registry", "primary") == "primary"
    assert state.get("registryGeneration", 0) == 0


def test_switch_to_unmounted_target_is_unavailable(switch_env):
    settings = switch_env.make_settings(standby_dir=switch_env.broken_dir)
    with make_client(settings) as client:
        response = client.post("/v1/registry/switch", json={"target": "standby"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "registry_unavailable"
        assert client.get("/healthz").json()["registry"] == "primary"


# ---------------------------------------------------------------------------
# Publish-first: the switch validation goes stale and is rejected
# ---------------------------------------------------------------------------


def test_publish_committing_first_rejects_switch(switch_env, monkeypatch):
    settings = switch_env.make_settings()
    entered = threading.Event()
    proceed = threading.Event()
    original = StableStore.commit_switch

    def gated_commit_switch(self, target, validated_digests):
        # Park after full validation but before the atomic coverage commit.
        entered.set()
        assert proceed.wait(timeout=10)
        return original(self, target, validated_digests)

    monkeypatch.setattr(StableStore, "commit_switch", gated_commit_switch)

    with make_client(settings) as client:
        switch_result: dict[str, object] = {}

        def do_switch() -> None:
            switch_result["response"] = client.post(
                "/v1/registry/switch", json={"target": "standby"}
            )

        switch_thread = threading.Thread(target=do_switch)
        switch_thread.start()
        assert entered.wait(timeout=10), "switch never reached commit"

        # Queries keep working while the switch is parked at commit.
        assert client.get("/v1/stable").status_code == 200

        # The fast arm64 release commits while the switch is in flight.
        release = client.post(
            "/v1/releases", json=_body(switch_env.meta, "v2", "arm64", "v1")
        )
        assert release.status_code == 200, release.text

        proceed.set()
        switch_thread.join(timeout=15)
        response = switch_result["response"]
        assert response.status_code == 409, response.text
        details = response.json()["error"]["details"]
        assert details["reason"] == "stable_changed_during_validation"
        assert details["currentDigests"]["arm64"] == (
            switch_env.meta["versions"]["v2"]["arm64"]["digest"]
        )
        assert details["validatedDigests"]["arm64"] == (
            switch_env.meta["versions"]["v1"]["arm64"]["digest"]
        )

        health = client.get("/healthz").json()
        assert health["registry"] == "primary"
        by_arch = stable_by_arch(client.get("/v1/stable").json())
        assert by_arch["arm64"]["digest"] == (
            switch_env.meta["versions"]["v2"]["arm64"]["digest"]
        )
        assert by_arch["amd64"]["digest"] == (
            switch_env.meta["versions"]["v1"]["amd64"]["digest"]
        )


def test_queries_and_publish_run_during_switch_scan(switch_env, monkeypatch):
    """Block the switch mid-verification and prove the service is not stalled."""
    import app.main as main_module

    settings = switch_env.make_settings()
    scan_entered = threading.Event()
    release_scan = threading.Event()
    original_verify = main_module.verify_release
    standby_root = switch_env.min_dir

    def blocking_verify(layout, cfg, arch, digest):
        if (
            Path(layout.root) == standby_root
            and arch == "amd64"
            and not scan_entered.is_set()
        ):
            scan_entered.set()
            assert release_scan.wait(timeout=10)
        return original_verify(layout, cfg, arch, digest)

    monkeypatch.setattr(main_module, "verify_release", blocking_verify)

    with make_client(settings) as client:
        switch_result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: switch_result.update(
                response=client.post(
                    "/v1/registry/switch", json={"target": "standby"}
                )
            )
        )
        thread.start()
        assert scan_entered.wait(timeout=10)

        started = time.monotonic()
        stable = client.get("/v1/stable")
        assert stable.status_code == 200
        assert time.monotonic() - started < 2.0

        release = client.post(
            "/v1/releases", json=_body(switch_env.meta, "v2", "arm64", "v1")
        )
        assert release.status_code == 200, release.text

        release_scan.set()
        thread.join(timeout=15)
        assert switch_result["response"].status_code == 409
        assert client.get("/healthz").json()["registry"] == "primary"


# ---------------------------------------------------------------------------
# Switch-first: the in-flight publish must re-validate in the new library
# ---------------------------------------------------------------------------


def _gate_first_commit(monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()
    state = {"calls": 0}
    call_lock = threading.Lock()
    original = StableStore.commit

    def gated(self, verified, expected, *, expected_generation=None):
        with call_lock:
            state["calls"] += 1
            call_number = state["calls"]
        if call_number == 1:
            entered.set()
            assert proceed.wait(timeout=10)
        return original(
            self,
            verified,
            expected,
            expected_generation=expected_generation,
        )

    monkeypatch.setattr(StableStore, "commit", gated)
    return entered, proceed, state


def test_switch_first_revalidation_missing_in_standby_conflicts(
    switch_env, monkeypatch
):
    # min standby has v1 only -> the amd64 v2 candidate is absent there.
    settings = switch_env.make_settings(standby_dir=switch_env.min_dir)
    entered, proceed, calls = _gate_first_commit(monkeypatch)

    with make_client(settings) as client:
        outcome: dict[str, object] = {}

        def publish() -> None:
            outcome["response"] = client.post(
                "/v1/releases",
                json=_body(switch_env.meta, "v2", "amd64", "v1"),
            )

        publish_thread = threading.Thread(target=publish)
        publish_thread.start()
        assert entered.wait(timeout=10), "publish never reached commit"

        # The switch validates unchanged v1 in standby and commits first.
        switch_response = client.post(
            "/v1/registry/switch", json={"target": "standby"}
        )
        assert switch_response.status_code == 200, switch_response.text
        assert switch_response.json()["registryGeneration"] == 1

        proceed.set()
        publish_thread.join(timeout=20)

        response = outcome["response"]
        assert response.status_code == 409, response.text
        details = response.json()["error"]["details"]
        assert details["reason"] == "registry_revalidation_failed"
        assert details["registry"] == "standby"
        assert details["causeCode"] == "blob_not_found"

        # Nothing about stable moved: switch is not a release and the publish
        # was rejected, so revision stays 0 and digests stay at v1.
        stable = client.get("/v1/stable")
        assert stable.headers["x-index-revision"] == "0"
        by_arch = stable_by_arch(stable.json())
        assert by_arch["amd64"]["digest"] == (
            switch_env.meta["versions"]["v1"]["amd64"]["digest"]
        )
        assert client.get("/healthz").json()["registry"] == "standby"


def test_switch_first_revalidation_passing_in_standby_commits(
    switch_env, monkeypatch
):
    # v2 standby mirrors the amd64 v2 candidate, so re-validation succeeds.
    settings = switch_env.make_settings(standby_dir=switch_env.v2_dir)
    entered, proceed, calls = _gate_first_commit(monkeypatch)

    with make_client(settings) as client:
        outcome: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: outcome.update(
                response=client.post(
                    "/v1/releases",
                    json=_body(switch_env.meta, "v2", "amd64", "v1"),
                )
            )
        )
        thread.start()
        assert entered.wait(timeout=10)

        switch_response = client.post(
            "/v1/registry/switch", json={"target": "standby"}
        )
        assert switch_response.status_code == 200

        proceed.set()
        thread.join(timeout=20)

        response = outcome["response"]
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["registry"] == "standby"
        assert body["registryGeneration"] == 1
        assert body["revision"] == 1
        assert calls["calls"] == 2  # generation rejection + successful retry

        by_arch = stable_by_arch(client.get("/v1/stable").json())
        assert by_arch["amd64"]["digest"] == (
            switch_env.meta["versions"]["v2"]["amd64"]["digest"]
        )


# ---------------------------------------------------------------------------
# Cross-worker shared state and old-volume compatibility
# ---------------------------------------------------------------------------


def test_second_store_worker_observes_switch_and_generation_guard(switch_env):
    settings = switch_env.make_settings()
    primary_layout = OciLayout(settings.oci_layout)
    worker_one = StableStore(settings, primary_layout)
    worker_one.initialize()
    worker_two = StableStore(settings, OciLayout(settings.oci_layout))

    validated = {
        arch: switch_env.meta["versions"]["v1"][arch]["digest"]
        for arch in ("amd64", "arm64")
    }
    result = worker_one.commit_switch("standby", validated)
    assert result.generation == 1

    observed = worker_two.snapshot()
    assert observed.registry == "standby"
    assert observed.registry_generation == 1

    # A commit verified under generation 0 on worker two must be rejected.
    fake = VerifiedRelease(
        platform="linux/amd64",
        architecture="amd64",
        os="linux",
        manifest_digest=validated["amd64"],
        descriptor={"digest": validated["amd64"]},
        config_bytes=1,
        layer_count=1,
        layer_bytes=1,
        chunks=1,
        elapsed_ms=0.0,
    )
    with pytest.raises(RegistryChangedError):
        worker_two.commit(
            fake,
            validated["amd64"],
            expected_generation=0,
        )


def test_old_state_file_without_registry_defaults_to_primary(switch_env):
    settings = switch_env.make_settings()
    with make_client(settings) as client:
        released = client.post(
            "/v1/releases", json=_body(switch_env.meta, "v2", "amd64", "v1")
        )
        assert released.status_code == 200

    state_path = switch_env.state_dir / "stable-index.json"
    state = json.loads(state_path.read_text())
    state.pop("registry", None)
    state.pop("registryGeneration", None)
    state.pop("registryUpdatedAt", None)
    state_path.write_text(json.dumps(state))

    with make_client(
        switch_env.make_settings(standby_dir=switch_env.v2_dir)
    ) as restarted:
        health = restarted.get("/healthz").json()
        assert health["registry"] == "primary"
        assert health["registryGeneration"] == 0
        # Release history/index from the old volume are untouched.
        assert health["revision"] == 1
        by_arch = stable_by_arch(restarted.get("/v1/stable").json())
        assert by_arch["amd64"]["digest"] == (
            switch_env.meta["versions"]["v2"]["amd64"]["digest"]
        )
        # And switching from that compatibility default works once the target
        # contains the now-stable v2 content.
        switched = restarted.post(
            "/v1/registry/switch", json={"target": "standby"}
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["registryGeneration"] == 1
