from __future__ import annotations

import hashlib
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


MANIFEST_FILENAME = "update-manifest.json"


def _iter_files(root: Path, exclude_names: set[str]) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in exclude_names:
            continue
        yield path


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_update_manifest(
    root: Path,
    *,
    version: str,
    platform_tag: str,
    variant: str = "",
    artifact_name: str | None = None,
    extra: dict | None = None,
) -> dict:
    root = root.resolve()
    exclude_names = {MANIFEST_FILENAME}
    files = []
    total_size = 0

    for path in _iter_files(root, exclude_names):
        rel_path = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_size += size
        files.append(
            {
                "path": rel_path,
                "size": size,
                "sha256": sha256_file(path),
            }
        )

    manifest = {
        "schema_version": 1,
        "version": version,
        "platform_tag": platform_tag,
        "variant": variant,
        "artifact_name": artifact_name
        or f"MaaNTE-{platform_tag}-{version}{variant}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "total_size": total_size,
        "files": files,
    }
    if extra:
        manifest.update(extra)
    return manifest


def write_update_manifest(
    root: Path,
    *,
    version: str,
    platform_tag: str,
    variant: str = "",
    artifact_name: str | None = None,
    extra: dict | None = None,
) -> Path:
    manifest = build_update_manifest(
        root,
        version=version,
        platform_tag=platform_tag,
        variant=variant,
        artifact_name=artifact_name,
        extra=extra,
    )
    manifest_path = root / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate MaaNTE update manifest")
    parser.add_argument("root")
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform-tag", required=True)
    parser.add_argument("--variant", default="")
    parser.add_argument("--artifact-name", default="")
    args = parser.parse_args()

    path = write_update_manifest(
        Path(args.root),
        version=args.version,
        platform_tag=args.platform_tag,
        variant=args.variant,
        artifact_name=args.artifact_name or None,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
