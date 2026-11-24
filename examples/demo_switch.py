#!/usr/bin/env python3
"""Registry cut-over race demos over plain HTTP.

Runs against a live service (default http://127.0.0.1:8000) started with the
two mounted demo layouts (see docker-compose.yml).  Every case expects a
**fresh v1 baseline**; reset between cases with::

    docker compose down -v && docker compose up -d --build

Two commit orderings are demonstrated:

* ``publish-first``  - an arm64 release commits while the switch is still
  validating stable in the standby library.  The switch's validation no
  longer covers commit-time stable, so the switch is rejected (409) and the
  service keeps using the primary library.

* ``switch-first``   - a slow amd64 release has verified in the primary
  library but has not committed when the switch commits.  The release must
  re-validate in standby; the demo standby deliberately lacks the amd64 v2
  candidate, so the release gets a 409 conflict and nothing is written.

* ``happy``          - a clean cut-over succeeds, /healthz reports standby,
  and the arm64 v2 candidate mirrored into standby can then be released.

Timing windows come from the deliberately asymmetric demo layer sizes and
LAYER_CHUNK_DELAY_MS; /healthz active* fields are polled as a best-effort
start signal (with multiple uvicorn workers a poll can land on another
worker, hence the generous windows).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_META = Path("/oci-layout/demo-digests.json")


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read() or b"null")
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read() or b"null")
        status = exc.code
    return status, payload, time.monotonic() - started


def by_arch(index: dict[str, Any]) -> dict[str, str]:
    return {
        manifest["platform"]["architecture"]: manifest["digest"]
        for manifest in index["manifests"]
    }


def release(base: str, meta: dict[str, Any], version: str, arch: str, previous: str):
    return call(
        base,
        "POST",
        "/v1/releases",
        {
            "platform": f"linux/{arch}",
            "manifestDigest": meta["versions"][version][arch]["digest"],
            "expectedPreviousDigest": meta["versions"][previous][arch]["digest"],
        },
    )


def wait_for_active(base: str, kind: str, needle: str, timeout: float = 3.0) -> bool:
    """Best-effort wait until /healthz shows the requested in-flight item."""
    key = "activeSwitches" if kind == "switch" else "activeReleases"
    field = "target" if kind == "switch" else "platform"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, health, _ = call(base, "GET", "/healthz")
        if status == 200:
            for item in health.get(key, []):
                if needle in str(item.get(field, "")):
                    return True
        time.sleep(0.01)
    return False


def check_fresh_baseline(base: str, meta: dict[str, Any]) -> dict[str, str] | None:
    status, health, _ = call(base, "GET", "/healthz")
    if status != 200 or health.get("registry") != "primary":
        print("! service is not on the primary registry:", status, health)
        return None
    status, stable, _ = call(base, "GET", "/v1/stable")
    if status != 200:
        print("! cannot read /v1/stable:", status, stable)
        return None
    digests = by_arch(stable)
    expected = {
        arch: meta["versions"]["v1"][arch]["digest"] for arch in ("amd64", "arm64")
    }
    if digests != expected:
        print("! stable is not at the fresh v1 baseline:")
        print("  live:    ", digests)
        print("  expected:", expected)
        print("  reset with: docker compose down -v && docker compose up -d")
        return None
    print(
        f"baseline ok: registry=primary revision={health.get('revision')} "
        f"generation={health.get('registryGeneration')}"
    )
    return digests


def case_publish_first(base: str, meta: dict[str, Any]) -> int:
    print("\n=== Case A: publish commits first -> switch is rejected ===")
    if check_fresh_baseline(base, meta) is None:
        return 2

    switch_result: dict[str, tuple[int, Any, float]] = {}

    def do_switch() -> None:
        switch_result["value"] = call(
            base, "POST", "/v1/registry/switch", {"target": "standby"}
        )

    print("\n1) starting switch to 'standby'; it re-verifies stable v1 in standby")
    switch_thread = threading.Thread(target=do_switch)
    switch_thread.start()
    seen = wait_for_active(base, "switch", "standby")
    print(f"   switch validation observed in /healthz: {seen}")
    # Small grace so the scan is definitely underway even if polling missed
    # this worker behind the uvicorn socket.
    time.sleep(0.05)

    print("2) publishing arm64 v2 (tiny, present in primary) while scan runs")
    rel_status, rel_body, rel_ms = release(base, meta, "v2", "arm64", "v1")
    print(f"   arm64 v2 release: HTTP {rel_status} in {rel_ms*1000:.0f}ms")
    assert rel_status == 200, rel_body

    switch_thread.join(timeout=120)
    sw_status, sw_body, sw_ms = switch_result["value"]
    print(f"3) switch response after {sw_ms*1000:.0f}ms: HTTP {sw_status}")
    assert sw_status == 409, sw_body
    error = sw_body["error"]
    print(f"   error.code={error['code']} reason={error['details']['reason']}")
    print(f"   validated arm64: {error['details']['validatedDigests'].get('arm64')}")
    print(f"   current   arm64: {error['details']['currentDigests'].get('arm64')}")
    assert error["details"]["reason"] == "stable_changed_during_validation"

    health = call(base, "GET", "/healthz")[1]
    print(f"4) /healthz registry={health['registry']} (unchanged), "
          f"generation={health['registryGeneration']}")
    assert health["registry"] == "primary"
    stable = by_arch(call(base, "GET", "/v1/stable")[1])
    assert stable["arm64"] == meta["versions"]["v2"]["arm64"]["digest"]
    print("   arm64 v2 stayed published; switch did not touch stable")
    print("\nCase A result: PUBLISH WINS, SWITCH REJECTED, still on primary")
    return 0


def case_switch_first(base: str, meta: dict[str, Any]) -> int:
    print("\n=== Case B: switch commits first -> in-flight publish conflicts ===")
    if check_fresh_baseline(base, meta) is None:
        return 2

    release_result: dict[str, tuple[int, Any, float]] = {}

    def do_release() -> None:
        release_result["value"] = release(base, meta, "v2", "amd64", "v1")

    print("\n1) starting slow amd64 v2 release (verifies huge layers in primary)")
    release_thread = threading.Thread(target=do_release)
    release_thread.start()
    seen = wait_for_active(base, "release", "amd64")
    print(f"   release scan observed in /healthz: {seen}")
    time.sleep(0.05)

    print("2) switching to standby while the release is mid-verification")
    sw_status, sw_body, sw_ms = call(
        base, "POST", "/v1/registry/switch", {"target": "standby"}
    )
    print(f"   switch: HTTP {sw_status} in {sw_ms*1000:.0f}ms")
    assert sw_status == 200, sw_body
    print(
        f"   registry={sw_body['registry']} generation={sw_body['registryGeneration']} "
        f"stable revision kept at {sw_body['revision']}"
    )
    assert sw_body["revision"] == 0

    release_thread.join(timeout=120)
    rel_status, rel_body, rel_ms = release_result["value"]
    print(f"3) in-flight amd64 release finishes: HTTP {rel_status} after {rel_ms*1000:.0f}ms")
    assert rel_status == 409, rel_body
    details = rel_body["error"]["details"]
    print(f"   reason={details['reason']} registry={details['registry']} "
          f"causeCode={details['causeCode']}")
    assert details["reason"] == "registry_revalidation_failed"
    assert details["causeCode"] == "blob_not_found"

    health = call(base, "GET", "/healthz")[1]
    stable_resp = call(base, "GET", "/v1/stable")
    # healthz revision carries the same index revision as X-Index-Revision.
    print(f"4) /healthz registry={health['registry']} revision={health['revision']}")
    assert health["registry"] == "standby"
    assert health["revision"] == 0
    digests = by_arch(stable_resp[1])
    assert digests["amd64"] == meta["versions"]["v1"]["amd64"]["digest"]
    print("   stable still points at v1; the failed release wrote nothing")
    print("\nCase B result: SWITCH WINS, OLD PUBLISH CONFLICTS, now on standby")
    return 0


def case_happy(base: str, meta: dict[str, Any]) -> int:
    print("\n=== Case C: clean switch and a publish verified in standby ===")
    if check_fresh_baseline(base, meta) is None:
        return 2

    print("\n1) POST /v1/registry/switch {target: standby}")
    status, body, ms = call(base, "POST", "/v1/registry/switch", {"target": "standby"})
    print(f"   HTTP {status} in {ms*1000:.0f}ms switched={body.get('switched')} "
          f"generation={body.get('registryGeneration')} revision={body.get('revision')}")
    assert status == 200, body
    assert body["revision"] == 0

    health = call(base, "GET", "/healthz")[1]
    print(f"2) /healthz registry={health['registry']} path={health['registryPath']}")
    assert health["registry"] == "standby"

    print("3) release arm64 v2; its blobs were mirrored into standby")
    rel_status, rel_body, _ = release(base, meta, "v2", "arm64", "v1")
    print(f"   HTTP {rel_status} revision={rel_body.get('revision')} "
          f"registry={rel_body.get('registry')}")
    assert rel_status == 200, rel_body

    print("4) switching back to primary (validates arm64 v2 there too)")
    back_status, back_body, _ = call(
        base, "POST", "/v1/registry/switch", {"target": "primary"}
    )
    print(f"   HTTP {back_status} registry={back_body.get('registry')} "
          f"generation={back_body.get('registryGeneration')}")
    assert back_status == 200, back_body
    assert call(base, "GET", "/healthz")[1]["registry"] == "primary"
    print("\nCase C result: CLEAN CUT-OVER, PUBLISH ON STANDBY, SWITCH BACK OK")
    return 0


CASES = {
    "publish-first": case_publish_first,
    "switch-first": case_switch_first,
    "happy": case_happy,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument(
        "--case",
        choices=sorted(CASES),
        default="publish-first",
        help="which ordering scenario to drive (fresh v1 state required)",
    )
    args = parser.parse_args()
    meta = json.loads(args.meta.read_text())
    return CASES[args.case](args.base, meta)


if __name__ == "__main__":
    raise SystemExit(main())
