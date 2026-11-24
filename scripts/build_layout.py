"""Build deterministic OCI image layouts for demos and tests.

Produces a real ``oci-layout`` directory:

    <root>/oci-layout
    <root>/index.json          -> points at a nested multi-arch index blob
    <root>/blobs/sha256/<hex>  -> configs, layers, manifests, index

Everything is deterministic for a given architecture/version/seed so the demo
metadata file always matches the blobs on disk.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import random
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_layer(arch: str, version: str, index: int, payload_bytes: int) -> tuple[bytes, str]:
    """Return (gzipped layer bytes, uncompressed diff_id)."""
    rng = random.Random(f"{arch}/{version}/{index}")
    payload = rng.randbytes(payload_bytes)

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in (
            ("etc/os-release", f"ID=edge\nVERSION={version}\nARCH={arch}\n".encode()),
            ("payload.bin", payload),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    tar_bytes = tar_buffer.getvalue()

    gz_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buffer, mode="wb", mtime=0, filename="") as gz:
        gz.write(tar_bytes)
    return gz_buffer.getvalue(), sha256_digest(tar_bytes)


@dataclass
class LayoutBuilder:
    root: Path
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        (self.root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)

    def write_blob(self, data: bytes) -> tuple[str, int]:
        digest = sha256_digest(data)
        path = self.root / "blobs" / "sha256" / digest.split(":", 1)[1]
        if not path.exists():
            path.write_bytes(data)
        return digest, len(data)

    def add_image(
        self,
        *,
        arch: str,
        version: str,
        layers: int = 2,
        payload_bytes: int = 512 * 1024,
        variant: str | None = None,
    ) -> dict[str, Any]:
        layer_descs: list[dict[str, Any]] = []
        diff_ids: list[str] = []
        for position in range(layers):
            blob, diff_id = build_layer(arch, version, position, payload_bytes)
            digest, size = self.write_blob(blob)
            layer_descs.append(
                {"mediaType": OCI_LAYER_GZIP, "digest": digest, "size": size}
            )
            diff_ids.append(diff_id)

        config: dict[str, Any] = {
            "architecture": arch,
            "os": "linux",
            "created": "1970-01-01T00:00:00Z",
            "config": {
                "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
                "Labels": {"org.opencontainers.image.version": version},
                "Cmd": ["/usr/local/bin/edge-agent"],
            },
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
            "history": [
                {"created": "1970-01-01T00:00:00Z", "created_by": f"demo {version}"}
            ],
        }
        if variant:
            config["variant"] = variant
        config_bytes = canonical_json(config)
        config_digest, config_size = self.write_blob(config_bytes)

        manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": OCI_CONFIG,
                "digest": config_digest,
                "size": config_size,
            },
            "layers": layer_descs,
            "annotations": {
                "org.opencontainers.image.version": version,
                "org.opencontainers.image.ref.name": f"{arch}-{version}",
            },
        }
        manifest_bytes = canonical_json(manifest)
        manifest_digest, manifest_size = self.write_blob(manifest_bytes)

        platform: dict[str, str] = {"architecture": arch, "os": "linux"}
        if variant:
            platform["variant"] = variant
        descriptor = {
            "mediaType": OCI_MANIFEST,
            "digest": manifest_digest,
            "size": manifest_size,
            "platform": platform,
            "annotations": {
                "org.opencontainers.image.version": version,
            },
        }
        self.manifests[f"{arch}/{version}"] = descriptor
        return descriptor

    def write_index(
        self,
        *,
        preloaded: list[dict[str, Any]],
        ref_name: str,
        nested: bool = True,
    ) -> dict[str, Any]:
        index = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": preloaded,
            "annotations": {
                "org.opencontainers.image.ref.name": ref_name,
                "org.opencontainers.image.version": "stable",
            },
        }
        index_bytes = canonical_json(index)
        index_digest, index_size = self.write_blob(index_bytes)

        (self.root / "oci-layout").write_text(
            json.dumps({"imageLayoutVersion": "1.0.0"}, indent=2) + "\n",
            encoding="utf-8",
        )
        if nested:
            layout_index = {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": [
                    {
                        "mediaType": OCI_INDEX,
                        "digest": index_digest,
                        "size": index_size,
                        "annotations": {"org.opencontainers.image.ref.name": ref_name},
                    }
                ],
            }
        else:
            layout_index = index
        (self.root / "index.json").write_text(
            json.dumps(layout_index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {
            "indexDigest": index_digest,
            "indexSize": index_size,
            "index": index,
        }


def build_demo_layout(
    out_dir: Path,
    meta_path: Path | None = None,
    *,
    amd64_payload_bytes: int = 512 * 1024,
    arm64_payload_bytes: int = 512 * 1024,
    layers: int = 2,
    versions: tuple[str, ...] = ("v1", "v2", "v3"),
    payload_sizes: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Layout with stable v1 preloaded plus candidate v2/v3 manifests.

    v1 is what the service starts with on both architectures.  v2 and v3
    manifests are already present as blobs so the API can verify them without
    a registry, exactly like an air-gapped edge rollout.

    amd64 layers can be made deliberately larger than arm64 so the concurrent
    demo has a slow scan on one architecture and a fast one on the other.

    ``payload_sizes`` may override the payload size of any
    ``{version: {arch: bytes}}`` combination; omitted entries fall back to the
    arch-wide ``*_payload_bytes`` argument.
    """
    builder = LayoutBuilder(out_dir)
    base_payloads = {"amd64": amd64_payload_bytes, "arm64": arm64_payload_bytes}
    descriptors: dict[str, dict[str, dict[str, Any]]] = {}
    variants = {"amd64": None, "arm64": "v8"}
    for version in versions:
        descriptors[version] = {}
        for arch in ("amd64", "arm64"):
            payload_bytes = (
                (payload_sizes or {}).get(version, {}).get(arch, base_payloads[arch])
            )
            descriptors[version][arch] = builder.add_image(
                arch=arch,
                version=version,
                variant=variants[arch],
                layers=layers,
                payload_bytes=payload_bytes,
            )

    ref = "registry.example.com/edge/stable:latest"
    preloaded = [descriptors["v1"]["amd64"], descriptors["v1"]["arm64"]]
    written = builder.write_index(preloaded=preloaded, ref_name=ref, nested=True)

    meta = {
        "ref": ref,
        "preloadedVersion": "v1",
        "versions": descriptors,
        "indexDigest": written["indexDigest"],
        "layout": str(out_dir),
    }
    if meta_path is not None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return meta


def build_standby_layout(
    out_dir: Path,
    meta_path: Path | None = None,
    *,
    layers: int = 2,
    payload_sizes: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Build the restricted standby library mounted at /oci-layout-standby.

    Content is deterministic with the same builder parameters, so its v1
    manifests/configs/layers have *exactly the same digests* as the primary
    layout's v1 and validate the persisted stable index.  It intentionally
    contains only what a standby needs before maintenance:

    * v1 for both architectures (the deployed stable baseline),
    * arm64 v2 as one mirrored post-maintenance candidate,

    and deliberately lacks amd64 v2/v3 and arm64 v3 — unpublished images do
    not have to exist in the backup library.
    """
    sizes = payload_sizes or {}

    def size(version: str, arch: str, default: int) -> int:
        return sizes.get(version, {}).get(arch, default)

    builder = LayoutBuilder(out_dir)
    descriptors: dict[str, dict[str, dict[str, Any]]] = {"v1": {}, "v2": {}}
    for arch in ("amd64", "arm64"):
        descriptors["v1"][arch] = builder.add_image(
            arch=arch,
            version="v1",
            variant="v8" if arch == "arm64" else None,
            layers=layers,
            payload_bytes=size("v1", arch, 512 * 1024),
        )
    # One mirrored candidate so a clean cut-over can still publish arm64 v2.
    descriptors["v2"]["arm64"] = builder.add_image(
        arch="arm64",
        version="v2",
        variant="v8",
        layers=layers,
        payload_bytes=size("v2", "arm64", 512 * 1024),
    )

    ref = "registry.example.com/edge/stable:latest"
    preloaded = [descriptors["v1"]["amd64"], descriptors["v1"]["arm64"]]
    written = builder.write_index(preloaded=preloaded, ref_name=ref, nested=True)
    meta = {
        "ref": ref,
        "preloadedVersion": "v1",
        "versions": descriptors,
        "indexDigest": written["indexDigest"],
        "layout": str(out_dir),
        "note": "standby library: v1 both architectures plus arm64 v2 only",
    }
    if meta_path is not None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return meta


def build_demo_pair(
    *,
    primary_out: Path,
    primary_meta: Path,
    standby_out: Path,
    standby_meta: Path,
    layers: int = 2,
    payload_sizes: dict[str, dict[str, int]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the two fixed layouts mounted by Docker Compose."""
    primary = build_demo_layout(
        primary_out,
        primary_meta,
        layers=layers,
        payload_sizes=payload_sizes,
    )
    standby = build_standby_layout(
        standby_out,
        standby_meta,
        layers=layers,
        payload_sizes=payload_sizes,
    )
    return primary, standby


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="oci-layout", help="output layout directory")
    parser.add_argument(
        "--meta", default="examples/demo-digests.json", help="where to write digest metadata"
    )
    parser.add_argument(
        "--amd64-payload-bytes",
        type=int,
        default=512 * 1024,
        help="random payload per amd64 layer for every version (bigger = slower verification)",
    )
    parser.add_argument(
        "--arm64-payload-bytes",
        type=int,
        default=512 * 1024,
        help="random payload per arm64 layer for every version",
    )
    parser.add_argument(
        "--v1-amd64-payload-bytes",
        type=int,
        default=None,
        help="override amd64 v1 payload (the switch-validation scan)",
    )
    parser.add_argument(
        "--v1-arm64-payload-bytes",
        type=int,
        default=None,
        help="override arm64 v1 payload",
    )
    parser.add_argument(
        "--candidate-amd64-payload-bytes",
        type=int,
        default=None,
        help="override amd64 v2/v3 payload (the slow in-flight release scan)",
    )
    parser.add_argument(
        "--candidate-arm64-payload-bytes",
        type=int,
        default=None,
        help="override arm64 v2/v3 payload",
    )
    parser.add_argument("--layers", type=int, default=2, help="layers per image")
    parser.add_argument(
        "--pair",
        action="store_true",
        help="also build the restricted standby layout for registry-switch demos",
    )
    parser.add_argument("--standby-out", default="/oci-layout-standby")
    parser.add_argument(
        "--standby-meta", default="/oci-layout-standby/demo-standby-digests.json"
    )
    args = parser.parse_args(argv)

    candidate_amd = (
        args.candidate_amd64_payload_bytes
        if args.candidate_amd64_payload_bytes is not None
        else args.amd64_payload_bytes
    )
    candidate_arm = (
        args.candidate_arm64_payload_bytes
        if args.candidate_arm64_payload_bytes is not None
        else args.arm64_payload_bytes
    )
    v1_amd = (
        args.v1_amd64_payload_bytes
        if args.v1_amd64_payload_bytes is not None
        else args.amd64_payload_bytes
    )
    v1_arm = (
        args.v1_arm64_payload_bytes
        if args.v1_arm64_payload_bytes is not None
        else args.arm64_payload_bytes
    )
    payload_sizes = {
        "v1": {"amd64": v1_amd, "arm64": v1_arm},
        "v2": {"amd64": candidate_amd, "arm64": candidate_arm},
        "v3": {"amd64": candidate_amd, "arm64": candidate_arm},
    }

    if args.pair:
        primary, standby = build_demo_pair(
            primary_out=Path(args.out),
            primary_meta=Path(args.meta),
            standby_out=Path(args.standby_out),
            standby_meta=Path(args.standby_meta),
            layers=args.layers,
            payload_sizes=payload_sizes,
        )
        print(json.dumps(primary["versions"], indent=2, sort_keys=True))
        print(
            f"wrote primary layout to {args.out} ({args.meta}) and standby layout "
            f"to {args.standby_out} ({args.standby_meta})"
        )
        print("standby versions:", sorted(standby["versions"]))
        return 0

    meta = build_demo_layout(
        Path(args.out),
        Path(args.meta),
        layers=args.layers,
        payload_sizes=payload_sizes,
    )
    print(json.dumps(meta["versions"], indent=2, sort_keys=True))
    print(f"wrote layout to {args.out} and metadata to {args.meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
