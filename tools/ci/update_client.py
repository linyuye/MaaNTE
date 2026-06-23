from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_RELEASE_OWNER = "1bananachicken"
DEFAULT_RELEASE_REPO = "MaaNTE"


@dataclass
class UpdateSource:
    manifest_url: str | None = None
    release_api: str | None = None


def _http_get_json(url: str, timeout: int = 20) -> dict:
    request = Request(url, headers={"Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_download(url: str, dest: Path, timeout: int = 60) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "MaaNTE-updater"})
    with urlopen(request, timeout=timeout) as response, dest.open("wb") as f:
        shutil.copyfileobj(response, f)


def _sha256_file(path: Path) -> str:
    import hashlib

    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _latest_release_asset(release_api: str, asset_hint: str | None = None) -> tuple[str, dict]:
    release = _http_get_json(release_api)
    assets = release.get("assets", [])
    if not assets:
        raise RuntimeError("release has no assets")

    if asset_hint:
        for asset in assets:
            if asset_hint in asset.get("name", ""):
                return release.get("tag_name", ""), asset

    preferred_suffixes = (".zip", ".tar.gz", ".tgz")
    for suffix in preferred_suffixes:
        for asset in assets:
            if asset.get("name", "").endswith(suffix):
                return release.get("tag_name", ""), asset

    return release.get("tag_name", ""), assets[0]


def _resolve_source(args: argparse.Namespace) -> UpdateSource:
    manifest_url = args.manifest_url or os.getenv("MAANTE_UPDATE_MANIFEST_URL")
    release_api = args.release_api or os.getenv("MAANTE_RELEASE_API")
    if not release_api:
        release_api = (
            f"https://api.github.com/repos/{args.owner}/{args.repo}/releases/latest"
        )
    return UpdateSource(manifest_url=manifest_url, release_api=release_api)


def _iter_manifest_files(manifest: dict) -> Iterable[dict]:
    for item in manifest.get("files", []):
        if isinstance(item, dict) and item.get("path"):
            yield item


def _default_target_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_processes(pids: Iterable[int], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    live_pids = {pid for pid in pids if pid > 0}
    while live_pids and time.monotonic() < deadline:
        live_pids = {pid for pid in live_pids if _process_exists(pid)}
        if live_pids:
            time.sleep(1)
    if live_pids:
        raise TimeoutError(f"processes still running: {sorted(live_pids)}")


def _restart_program(path: str, cwd: Path) -> None:
    if not path:
        return
    import subprocess

    restart_path = Path(path)
    if not restart_path.is_absolute():
        restart_path = cwd / restart_path
    if not restart_path.exists():
        raise FileNotFoundError(restart_path)
    subprocess.Popen(
        [str(restart_path)],
        cwd=str(cwd),
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _download_manifest(source: UpdateSource) -> tuple[dict, str]:
    if source.manifest_url:
        manifest = _http_get_json(source.manifest_url)
        return manifest, source.manifest_url

    release = _http_get_json(source.release_api or "")
    assets = release.get("assets", [])
    for asset in assets:
        asset_name = asset.get("name", "")
        if "update-manifest" in asset_name:
            manifest_url = asset.get("browser_download_url")
            if not manifest_url:
                break
            return _http_get_json(manifest_url), manifest_url
    raise RuntimeError("cannot find update-manifest.json in release assets")


def _download_release_asset(source: UpdateSource, asset_hint: str | None = None) -> tuple[dict, Path, str]:
    release = _http_get_json(source.release_api or "")
    assets = release.get("assets", [])
    if not assets:
        raise RuntimeError("release has no assets")

    asset = None
    if asset_hint:
        for candidate in assets:
            if asset_hint in candidate.get("name", ""):
                asset = candidate
                break
    if asset is None:
        for candidate in assets:
            name = candidate.get("name", "")
            if name.endswith(".zip") or name.endswith(".tar.gz") or name.endswith(".tgz"):
                asset = candidate
                break
    if asset is None:
        raise RuntimeError("cannot find downloadable release archive")

    asset_url = asset.get("browser_download_url")
    if not asset_url:
        raise RuntimeError("release asset missing browser_download_url")

    temp_dir = Path(tempfile.mkdtemp(prefix="maante-release-"))
    archive_path = temp_dir / asset.get("name", "release-asset")
    _http_download(asset_url, archive_path)
    return release, archive_path, asset.get("name", "")


def _extract_archive(archive_path: Path, extract_dir: Path) -> None:
    if archive_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
        return

    if archive_path.name.endswith(".tar.gz") or archive_path.name.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(extract_dir)
        return

    raise RuntimeError(f"unsupported archive type: {archive_path.name}")


def update_from_manifest(
    manifest: dict,
    *,
    target_root: Path,
    source_dir: Path | None = None,
    base_url: str = "",
    dry_run: bool = False,
) -> list[str]:
    changed: list[str] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="maante-update-"))
    locked_paths: set[str] = set()
    try:
        locked_paths.add(Path(sys.executable).resolve().relative_to(target_root.resolve()).as_posix())
    except ValueError:
        pass
    try:
        for file_info in _iter_manifest_files(manifest):
            rel_path = file_info["path"]
            if rel_path in locked_paths:
                continue
            target_path = target_root / rel_path

            current_hash = None
            if target_path.exists():
                current_hash = _sha256_file(target_path)
                if current_hash == file_info.get("sha256"):
                    continue

            changed.append(rel_path)
            if dry_run:
                continue

            temp_file = temp_dir / rel_path
            if source_dir is not None:
                source_file = source_dir / rel_path
                if not source_file.exists():
                    raise FileNotFoundError(source_file)
                temp_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, temp_file)
            else:
                download_url = urljoin(base_url, rel_path)
                _http_download(download_url, temp_file)

            downloaded_hash = _sha256_file(temp_file)
            expected_hash = file_info.get("sha256")
            if expected_hash and downloaded_hash != expected_hash:
                raise RuntimeError(f"hash mismatch for {rel_path}")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_file), str(target_path))

        return changed
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="MaaNTE update client")
    parser.add_argument("--owner", default=DEFAULT_RELEASE_OWNER)
    parser.add_argument("--repo", default=DEFAULT_RELEASE_REPO)
    parser.add_argument("--manifest-url", default="")
    parser.add_argument("--release-api", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--target-root", default="")
    parser.add_argument("--asset-hint", default="")
    parser.add_argument("--wait-pid", action="append", type=int, default=[])
    parser.add_argument("--wait-timeout", type=int, default=300)
    parser.add_argument("--restart", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = _resolve_source(args)
    target_root = Path(args.target_root) if args.target_root else _default_target_root()
    if args.wait_pid:
        _wait_for_processes(args.wait_pid, args.wait_timeout)

    manifest, manifest_source_url = _download_manifest(source)

    source_dir = None
    base_url = args.base_url or manifest.get("base_url") or ""
    if base_url:
        pass
    elif Path(urlparse(manifest_source_url).path).name == "update-manifest.json":
        base_url = manifest_source_url.rsplit("/", 1)[0] + "/"
    else:
        _, archive_path, archive_name = _download_release_asset(source, args.asset_hint or manifest.get("artifact_name", ""))
        stage_dir = Path(tempfile.mkdtemp(prefix="maante-stage-"))
        _extract_archive(archive_path, stage_dir)
        source_dir = stage_dir
        changed = update_from_manifest(
            manifest,
            target_root=target_root,
            source_dir=source_dir,
            dry_run=args.dry_run,
        )
        print(json.dumps({"changed": changed, "count": len(changed), "asset": archive_name}, ensure_ascii=False))
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(archive_path.parent, ignore_errors=True)
        _restart_program(args.restart, target_root)
        return 0

    changed = update_from_manifest(
        manifest,
        target_root=target_root,
        base_url=base_url,
        dry_run=args.dry_run,
    )

    print(json.dumps({"changed": changed, "count": len(changed)}, ensure_ascii=False))
    _restart_program(args.restart, target_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
