# Edge stable release API

A FastAPI service that releases **one architecture at a time** into a
dual-architecture (linux/amd64 + linux/arm64) stable OCI index. Publishing an
amd64 fix never requires re-pushing arm64, and vice versa.

Image content is verified from a **local, read-only OCI image layout** — no
registry access, no network fetches.

## Architecture

```
        read-only mounts                 writable volume
┌──────────────┐   ┌────────────────────┐   ┌──────────────────────┐
│ layout-init  │──▶│ /oci-layout       │   │ /state               │
│ (one-shot,   │   │ /oci-layout-      │   │  stable-index.json   │
│ builds both) │   │   standby         │   │   + active registry  │
└──────────────┘   │  oci-layout       │   │  stable-index.lock   │
                   │  index.json       │   └──────────┬───────────┘
                   │  blobs/sha256/…   │              │
                   └────────┬───────────┘              │
                            │ verify active/candidate │ CAS commit
                   ┌────────▼───────────────────────────▼──────────┐
                   │ release-api (uvicorn, 2 workers)              │
                   │ GET /v1/stable   POST /v1/releases            │
                   │ GET /v1/registry POST /v1/registry/switch     │
                   └───────────────────────────────────────────────┘
```

`/oci-layout` is the primary library; `/oci-layout-standby` is the fixed
backup the operator can activate. Which one is effective is durable state, not
an env var, so every worker and restart agrees.

### Request flow for `POST /v1/releases`

1. Validate `platform` is `linux/amd64` or `linux/arm64` and both digests are
   well-formed `sha256:…`.
2. **Outside any lock**, verify the candidate manifest:
   - manifest blob exists and its bytes hash to the submitted digest,
     `schemaVersion` is 2, media type allowed;
   - config descriptor: media type, size, digest, and the config's own
     `os` / `architecture` must equal `linux` / the requested architecture;
   - every layer descriptor: media type, `size` (checked against the blob by
     `stat`, again while streaming, and on the final byte count) and digest,
     hashed in bounded chunks (`LAYER_CHUNK_BYTES`).
3. **Under an `flock` + compare-and-swap**, re-read the persisted index; if the
   target platform's digest differs from `expectedPreviousDigest` → `409` with
   the current digest, writing nothing. Otherwise replace only that platform's
   descriptor, keep the other exactly as it is, bump the revision, and persist.
4. Persist atomically (temp file → `fsync` → `rename` → directory `fsync`), so a
   reader or a crash only ever sees the whole old or whole new index.

Consequences:

* Layer scanning never holds the commit lock, so `GET /v1/stable` stays
  responsive and the other architecture can commit mid-scan.
* Failed verification (422) or a conflict (409) leaves the persisted index
  untouched; after a restart the last successful index is served.
* Two uvicorn workers (or two containers) sharing `/state` serialise commits
  through the file lock and each read the latest committed state.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/stable` | Full OCI index (`schemaVersion`, `mediaType`, both `manifests`). `ETag`/`304`. |
| `GET` | `/v1/stable/{arch}` | One platform descriptor (`amd64` or `arm64`). |
| `POST` | `/v1/releases` | Release one architecture. |
| `GET` | `/v1/releases` | Per-architecture last publication + currently active scans. |
| `GET` | `/v1/registry` | Effective registry, generation and configured layout targets. |
| `POST` | `/v1/registry/switch` | Validate current stable in the target layout and cut over. Body `{"target":"standby"}` (`PUT /v1/registry` is an alias). |
| `GET` | `/healthz` | Readiness, revision, persistence status, **current registry**, in-flight scans/switches. |
| `GET` | `/docs` | OpenAPI UI. |

## Switching to the standby image library

For maintenance the on-call operator cuts the service over to a second, fixed,
read-only OCI layout (`standby`, mounted at `/oci-layout-standby`). The standby
can be missing images that were never published to stable.

`POST /v1/registry/switch {"target":"standby"}`:

1. Reads current stable and **verifies both architectures in the target layout
   with the same rules as a release** — manifest digest/schema/media type,
   config platform, and every layer size + chunked digest. This runs without
   the commit lock, so queries and releases continue during the scan.
2. Atomically (same `flock` + state file) re-reads stable and activates the
   target only if the validated `arch → digest` map still **covers commit-time
   stable**. A release that committed meanwhile makes this fail with
   `409 conflict / stable_changed_during_validation`; the service stays on
   the current library and the operator retries.
3. On success only the durable `registry` / `registryGeneration` fields change.
   The stable index, its `ETag`, its revision and the published version are
   untouched — a switch is not a release.

A release that verified under registry generation *N* carries that generation
into its commit. If a switch committed generation *N+1* first, the generation
guard rejects the blind commit and the service **re-verifies the candidate in
the new registry**; only then does the compare-and-swap run. If that
re-validation fails (for example the standby lacks the candidate blob), the
release returns `409 conflict / registry_revalidation_failed` and writes
nothing. If it passes, the release commits normally.

The active registry is shared durable state in `/state/stable-index.json`, so
all uvicorn workers and restarted/new processes use the effective library. A
state volume from an older release without registry fields defaults to
`primary` (`OCI_LAYOUT_DIR`).

See **[`examples/registry-switch.md`](examples/registry-switch.md)** for the
full curl/JSON reference and
[`examples/demo_switch.py`](examples/demo_switch.py), which demonstrates both
commit orderings against a live server (`--case publish-first`,
`--case switch-first`, `--case happy`).

## Run with Docker Compose

```console
docker compose up -d --build
curl -s localhost:8000/v1/stable | jq .
docker compose exec release-api python /srv/examples/demo_concurrent.py
```

`docker-compose.yml`:

* `layout-init` builds two deterministic demo layouts: the full `oci-layout`
  volume (preloaded stable **v1** on both architectures, plus **v2** and
  **v3** candidate blobs and `demo-digests.json`) and the restricted
  `oci-layout-standby` volume (stable **v1** on both architectures plus only
  the arm64 **v2** candidate, with `demo-standby-digests.json`).
* `release-api` mounts both layouts **read-only** and `release-state`
  read-write. It runs two uvicorn workers to exercise the cross-process commit
  path. `LAYER_CHUNK_DELAY_MS=1` is a demo-only pause per 64 KiB chunk so the
  concurrency is visible on small demo layers; set it to `0` in production.
  The demo layer sizes are asymmetric on purpose (moderate stable v1 amd64
  layers, huge amd64 v2/v3 candidates) so both switch/commit race orderings in
  `examples/demo_switch.py` have wide, reliable windows.

```console
# the two commit-order races (reset the stack between cases):
docker compose exec release-api python /srv/examples/demo_switch.py --case publish-first
docker compose down -v && docker compose up -d --build
docker compose exec release-api python /srv/examples/demo_switch.py --case switch-first
```

Reset the demo (returns to the v1 baseline):

```console
docker compose down -v && docker compose up -d
```

## Local run without Docker

Requires Python 3.11+.

```console
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# primary full layout + restricted standby layout
python scripts/build_layout.py --pair \
  --out /tmp/oci-layout --meta /tmp/oci-layout/demo-digests.json \
  --standby-out /tmp/oci-layout-standby \
  --standby-meta /tmp/oci-layout-standby/demo-standby-digests.json

OCI_LAYOUT_DIR=/tmp/oci-layout \
OCI_STANDBY_LAYOUT_DIR=/tmp/oci-layout-standby \
STATE_DIR=/tmp/state \
  uvicorn app.main:create_app --factory --port 8000 --workers 2

# in another shell
python examples/demo_concurrent.py --meta /tmp/oci-layout/demo-digests.json
python examples/demo_switch.py --meta /tmp/oci-layout/demo-digests.json --case happy
```

Tests (70 cases: validation, conflicts, durability, in-process and
cross-process concurrency, registry switch validation/coverage/generation
races and restart compatibility, endpoints):

```console
python -m pytest -q
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCI_LAYOUT_DIR` | `/oci-layout` | Primary OCI image layout root (read-only). |
| `OCI_STANDBY_LAYOUT_DIR` | `/oci-layout-standby` | Standby layout registered as target `standby`. |
| `OCI_REGISTRY_LAYOUTS` | _(unset)_ | Extra named targets, `name=/path,name2=/path2`. |
| `STATE_DIR` | `/state` | Writable directory for `stable-index.json` (also stores active registry). |
| `OCI_INDEX_REF` | `registry.example.com/edge/stable:latest` | Reference recorded on the index. |
| `OCI_ARCHITECTURES` | `amd64,arm64` | Platforms the index must contain. |
| `LAYER_CHUNK_BYTES` | `65536` | Blob hashing chunk size (1 B – 16 MiB). |
| `LAYER_CHUNK_DELAY_MS` | `0` | Demo-only per-chunk pause (0–1000 ms). |

## Input layout expectations

`<layout>/oci-layout` must contain `imageLayoutVersion: 1.0.0`.
`<layout>/index.json` may either list platform descriptors directly, or hold a
single descriptor whose `mediaType` is an OCI/Docker **index** and whose blob is
the nested multi-arch index (the demo does the latter). The resolved index must:

* have `schemaVersion: 2` and an OCI/Docker index media type,
* contain exactly one `linux/<arch>` descriptor per configured architecture,
* reference manifest blobs that exist and whose configs declare the matching
  platform.

Anything else fails startup with `invalid_layout` rather than serving an index
the service cannot honour.

## What was exercised

The full test suite (70 cases), live one- and two-worker uvicorn processes,
and the concurrent/switch demo scripts were run against generated layouts in
this environment. The switch scenarios were verified for real over HTTP:

* `publish-first` → release `200`, switch `409 stable_changed_during_validation`,
  `/healthz` stays `primary`;
* `switch-first` → switch `200` (generation 1, revision 0), in-flight release
  `409 registry_revalidation_failed / blob_not_found`, stable unchanged;
* `happy` → clean switch, arm64 v2 published after verification in standby,
  switch back to `primary`;
* 20/20 `/healthz` reads across two workers reported `standby` after cut-over,
  and a freshly booted process on the same state volume came up `standby`.

Docker itself is not installed here, so `docker compose up` was not executed;
the Compose file and the `Dockerfile.layout` JSON `CMD` array are validated by
YAML/JSON parsing only, and the exact demo flows were run with local uvicorn
using the same environment variables and builder arguments.
