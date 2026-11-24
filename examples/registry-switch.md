# Registry switch API — maintenance cut-over to the standby library

Before maintenance an on-call operator moves the release service from the
primary OCI layout to a second, fixed, read-only **standby** layout. This
document shows the API and the two commit-order races. A runnable script
drives both races: [`demo_switch.py`](./demo_switch.py).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Includes `registry` / `activeRegistry`, `registryGeneration`, `registryPath`, in-flight `activeReleases` and `activeSwitches`. |
| `GET` | `/v1/registry` | Effective registry plus all configured targets and mount readiness. |
| `POST` | `/v1/registry/switch` | Validate current stable in the target, then activate it. Body `{"target":"standby"}`. |
| `PUT` | `/v1/registry` | Alias of the switch endpoint. |

`target` is a configured name (`primary` or `standby`) or one of their
configured mount paths. Arbitrary filesystem paths are rejected. The request
fields `registry`, `name`, `registryName`, `targetRegistry`, `layout` and
`layoutDir` are accepted as aliases for `target`.

Configuration:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCI_LAYOUT_DIR` | `/oci-layout` | Primary layout (`primary`). |
| `OCI_STANDBY_LAYOUT_DIR` | `/oci-layout-standby` | Standby layout (`standby`). |
| `OCI_REGISTRY_LAYOUTS` | _(unset)_ | Extra targets, `name=/path,name2=/path2`. |

## Compose layout

`docker compose up -d --build` runs `layout-init` once, which builds two
deterministic content-fixed layouts:

* `oci-layout` volume → `/oci-layout` — full primary: stable **v1** on both
  architectures plus **v2/v3** candidates;
* `oci-layout-standby` volume → `/oci-layout-standby` — restricted backup:
  stable **v1** on both architectures and only the **arm64 v2** candidate.
  Unpublished images (amd64 v2/v3, arm64 v3) are intentionally absent.

Both are mounted **read-only** into `release-api`; the active registry name and
generation are persisted in the writable `release-state` volume next to
`stable-index.json`.

Reset everything to the fresh v1 baseline before re-running a scenario:

```console
docker compose down -v && docker compose up -d --build
```

## 1. Find the current library

```console
$ curl -s localhost:8000/healthz | jq '{status, revision, registry, registryGeneration}'
{
  "status": "ok",
  "revision": 0,
  "registry": "primary",
  "registryGeneration": 0
}

$ curl -s localhost:8000/v1/registry | jq '.targets[] | {name, path, active, layoutReady}'
{"name":"primary","path":"/oci-layout","active":true,"layoutReady":true}
{"name":"standby","path":"/oci-layout-standby","active":false,"layoutReady":true}
```

## 2. Clean switch

The handler reads current stable and runs the **existing release verification
rules** (`verify_release`: manifest digest/schema/media type, config platform,
every layer size and chunked digest) for both architectures against the target
layout. Only then does it atomically activate the target.

```console
$ curl -s -X POST localhost:8000/v1/registry/switch \
    -H 'content-type: application/json' \
    -d '{"target":"standby"}' | jq .
{
  "switched": true,
  "target": "standby",
  "registry": "standby",
  "previousRegistry": "primary",
  "registryGeneration": 1,
  "registryUpdatedAt": "2026-11-24T20:20:05.770Z",
  "revision": 0,
  "stableRevision": 0,
  "indexChanged": false,
  "verification": {
    "amd64": {"digest": "sha256:45d6f28e…", "layers": 2, "chunks": 258, "…": "…"},
    "arm64": {"digest": "sha256:cb165f4b…", "layers": 2, "chunks": 10,  "…": "…"}
  }
}
```

`revision` is still `0` and `indexChanged` is `false`: switching libraries is
not a release. The stable index, its `ETag`, its revision and the published
version annotations are untouched. `GET /healthz` now reports
`"registry":"standby"`. Every uvicorn worker re-reads that durable field per
request, and a restarted process adopts it.

Repeating the same switch is a no-op (`switched:false`, same generation);
switching back to `primary` performs a fresh validation and increments the
generation again.

## 3. Ordering A — the publish commits first, the switch is rejected

Run it against a fresh stack:

```console
$ docker compose exec release-api \
    python /srv/examples/demo_switch.py --case publish-first
```

Real output from a two-worker run (digests abbreviated):

```text
=== Case A: publish commits first -> switch is rejected ===
baseline ok: registry=primary revision=0 generation=0

1) starting switch to 'standby'; it re-verifies stable v1 in standby
   switch validation observed in /healthz: True
2) publishing arm64 v2 (tiny, present in primary) while scan runs
   arm64 v2 release: HTTP 200 in 35ms
3) switch response after 578ms: HTTP 409
   error.code=conflict reason=stable_changed_during_validation
   validated arm64: sha256:adb94625…   (v1)
   current   arm64: sha256:cb165f4b…   (v2, committed mid-validation)
4) /healthz registry=primary (unchanged), generation=0
   arm64 v2 stayed published; switch did not touch stable

Case A result: PUBLISH WINS, SWITCH REJECTED, still on primary
```

The switch validated `{amd64:v1, arm64:v1}`. At its commit point the store
re-reads stable under the file lock and finds arm64 at v2, so the validation
result no longer covers commit-time stable:

```json
{
  "error": {
    "code": "conflict",
    "message": "stable changed while the target registry was being validated; …",
    "details": {
      "reason": "stable_changed_during_validation",
      "target": "standby",
      "validatedDigests": {"amd64": "sha256:798460f5…", "arm64": "sha256:adb94625…"},
      "currentDigests":   {"amd64": "sha256:798460f5…", "arm64": "sha256:cb165f4b…"},
      "revision": 1
    }
  }
}
```

The operator simply retries the switch; it then validates the new stable
(including the newly published image) in standby. During the whole validation
`GET /v1/stable` and `POST /v1/releases` keep working — no lock is held while
hashing layers.

## 4. Ordering B — the switch commits first, the in-flight publish conflicts

Reset the stack, then:

```console
$ docker compose exec release-api \
    python /srv/examples/demo_switch.py --case switch-first
```

```text
=== Case B: switch commits first -> in-flight publish conflicts ===
baseline ok: registry=primary revision=0 generation=0

1) starting slow amd64 v2 release (verifies huge layers in primary)
   release scan observed in /healthz: True
2) switching to standby while the release is mid-verification
   switch: HTTP 200 in 573ms
   registry=standby generation=1 stable revision kept at 0
3) in-flight amd64 release finishes: HTTP 409 after 2152ms
   reason=registry_revalidation_failed registry=standby causeCode=blob_not_found
4) /healthz registry=standby revision=0
   stable still points at v1; the failed release wrote nothing

Case B result: SWITCH WINS, OLD PUBLISH CONFLICTS, now on standby
```

The release was verified against generation 0 (`primary`). Its commit carries
that generation; the store's generation guard sees generation 1 and asks the
handler to re-verify the **same candidate in standby**. The demo standby omits
amd64 v2 (an unpublished image), so re-verification fails and the API returns
`409`:

```json
{
  "error": {
    "code": "conflict",
    "details": {
      "reason": "registry_revalidation_failed",
      "registry": "standby",
      "registryGeneration": 1,
      "causeCode": "blob_not_found",
      "cause": "manifest blob not found in local layout: sha256:de954dbc…"
    }
  }
}
```

If the candidate **does** validate in standby (for example the mirrored arm64
v2), the retried commit succeeds against generation 1. The automated tests
cover both branches deterministically.

A third scripted case, `--case happy`, performs a clean switch, publishes the
mirrored arm64 v2 from standby, and switches back to `primary`.

## 5. Target content validation failure

If stable cannot be verified in the target library (missing blob, digest or
size mismatch, wrong config platform, corrupt layer), the switch is rejected
with `422 registry_validation_failed` and the current registry stays active:

```json
{
  "error": {
    "code": "registry_validation_failed",
    "details": {
      "target": "standby",
      "causeCode": "blob_not_found",
      "cause": "manifest blob not found in local layout: sha256:abac9b40…",
      "validated": {}
    }
  }
}
```

A target mount that cannot even be opened (missing `oci-layout` marker, …)
returns `503 registry_unavailable`. Neither failure writes registry state.

## 6. Restart and old state volumes

The active registry lives in `/state/stable-index.json`:

```json
{ "schemaVersion": 1, "revision": 0,
  "registry": "standby", "registryGeneration": 1,
  "registryUpdatedAt": "2026-11-24T20:20:05.770Z" }
```

A new process or worker re-reads this on each request, so after
`docker compose restart release-api` (or a container recreation) `/healthz`
still reports `standby`. A state volume from before this feature has no
`registry` keys; it defaults to `"registry":"primary"` — the original
`OCI_LAYOUT_DIR` — at generation 0, with its stable index and revision intact.

## Status / error summary

| Status | `error.code` / `details.reason` | Meaning |
| --- | --- | --- |
| `200` | — | Switch applied (or same-target no-op). |
| `409` | `conflict` / `stable_changed_during_validation` | A release committed during validation; re-validate and retry. |
| `409` | `conflict` / `registry_revalidation_failed` | Switch won the race and the in-flight release does not validate in the new registry. |
| `422` | `unknown_registry_target` | Target name/path is not configured. |
| `422` | `registry_validation_failed` (`causeCode` carries the OCI rule) | Current stable is not satisfiable in the target. |
| `503` | `registry_unavailable` | Target layout is not mounted/openable. |
| `503` | `state_unavailable` | Shared state volume cannot be read/written. |
