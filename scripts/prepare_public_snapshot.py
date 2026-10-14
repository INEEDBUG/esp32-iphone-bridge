"""Prepare a clean, allowlisted public snapshot from an immutable Git commit.

This tool only writes a new local staging directory and a caller-selected private
manifest. It never initializes Git, accesses remotes, or reads the worktree/private data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
from typing import Iterable


ROOT_FILES = {
    ".gitignore", "Start-PhoneLab.ps1", "LICENSE", "THIRD_PARTY_NOTICES.md",
}
PUBLIC_DOCS = {
    "docs/PUBLIC_README.md": ("README.md",),
    "docs/PUBLIC_README.zh-CN.md": ("README.zh-CN.md",),
    "docs/PUBLIC_PROGRESS.md": ("docs/PROGRESS.md",),
}
PUBLIC_ASSETS = {"docs/assets/phonelab-banner.svg"}
SOURCE_DIRS = {"phonelab", "firmware", "upstream", "scripts", "tests"}
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "build", "dist", "cache", "caches", "media", "artifacts", "private",
    "secrets", "credentials", "pairing-cache", "evidence", "logs", "screenshots", "frames",
}
EXCLUDED_SUFFIXES = {
    ".bin", ".elf", ".map", ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".7z", ".pdf", ".plist",
    ".pem", ".key", ".ppk", ".p12", ".pfx", ".cer", ".crt", ".der", ".mobileprovision",
    ".db", ".sqlite", ".sqlite3",
    ".log", ".pyc", ".class", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov",
    ".wav", ".mp3", ".pyd", ".whl",
}


class SnapshotError(RuntimeError):
    """The selected commit or destination does not satisfy the export contract."""


def validate_repo_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SnapshotError("repository tree contains an invalid path")
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or
            any(part in ("", ".", "..") for part in path.parts)):
        raise SnapshotError("repository tree contains a path traversal")
    return path


def _git(source: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SnapshotError("Git could not read the requested source commit") from exc


def _tree(source: Path, commit: str) -> list[tuple[str, str, str, str]]:
    raw = _git(source, "ls-tree", "-r", "-z", "--full-tree", commit)
    rows = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SnapshotError("Git tree contains an unsupported entry") from exc
        validate_repo_path(path)
        rows.append((mode, kind, oid, path))
    return rows


def _excluded_path(path: PurePosixPath) -> str | None:
    lower_parts = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    if any(part in EXCLUDED_DIRS for part in lower_parts):
        return "generated_or_binary_directory"
    if name in {".env", ".env.local", ".phonelab-local.json"} or name.startswith(".env."):
        return "local_configuration_or_environment_file"
    if name.endswith(".local.json"):
        return "local_configuration_file"
    if path.suffix.casefold() in EXCLUDED_SUFFIXES:
        return "binary_media_or_sensitive_extension"
    return None


def _target_paths(path: str, *, legacy_readme: bool = False) -> tuple[str, ...] | None:
    parsed = validate_repo_path(path)
    top = parsed.parts[0]
    if len(parsed.parts) == 1:
        if path in ROOT_FILES or (path.startswith("requirements") and path.endswith(".txt")):
            return (path,)
        return None
    if top in SOURCE_DIRS:
        return (path,)
    if path in PUBLIC_ASSETS:
        return (path,)
    if legacy_readme and path == "docs/PUBLIC_README.zh-CN.md":
        return ("README.zh-CN.md", "README.md")
    return PUBLIC_DOCS.get(path)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_locations(source: Path, destination: Path, manifest: Path) -> None:
    src = source.resolve(strict=True)
    dst = destination.resolve(strict=False)
    man = manifest.resolve(strict=False)
    if not (src / ".git").exists():
        raise SnapshotError("source must be a local Git worktree")
    if _is_within(dst, src) or _is_within(src, dst):
        raise SnapshotError("source and destination must be separate trees")
    if _is_within(man, dst) or _is_within(man, src):
        raise SnapshotError("private manifest must be outside source and destination")
    if dst.exists():
        if not dst.is_dir() or any(dst.iterdir()):
            raise SnapshotError("destination must be new or empty; existing contents are preserved")
    if man.exists():
        raise SnapshotError("manifest already exists; refusing to overwrite it")


def _resolve_commit(source: Path, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise SnapshotError("invalid source commit reference")
    resolved = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii", "strict").strip()
    if len(resolved) not in (40, 64) or any(ch not in "0123456789abcdefABCDEF" for ch in resolved):
        raise SnapshotError("Git did not resolve an immutable commit")
    return resolved.lower()


def _blob(source: Path, oid: str) -> bytes:
    return _git(source, "cat-file", "blob", oid)


def _sanitize_public_readme(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SnapshotError("public README is not valid UTF-8") from exc
    return text.replace("../LICENSE", "LICENSE").replace(
        "../THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md").encode("utf-8")


def _validate_text_blob(data: bytes) -> None:
    if b"\0" in data:
        raise SnapshotError("unexpected binary content in an allowlisted file")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SnapshotError("allowlisted file is not UTF-8 text") from exc
    if any(ord(char) < 32 and char not in "\t\n\r\f" for char in text):
        raise SnapshotError("unexpected binary control bytes in an allowlisted file")


def prepare_public_snapshot(source: Path | str, destination: Path | str,
                            ref: str, manifest: Path | str) -> dict:
    source = Path(source).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().resolve(strict=False)
    manifest = Path(manifest).expanduser().resolve(strict=False)
    _validate_locations(source, destination, manifest)
    commit = _resolve_commit(source, ref)
    tree = _tree(source, commit)
    # Preserve reproducible exports of older commits with a single public README.
    legacy_readme = not any(row[3] == "docs/PUBLIC_README.md" for row in tree)
    files: dict[str, dict] = {}
    exclusions: list[dict[str, str]] = []
    targets_seen: set[str] = set()
    pending: list[tuple[str, tuple[str, ...], bytes, str, str]] = []
    for mode, kind, oid, source_path in tree:
        parsed = validate_repo_path(source_path)
        excluded = _excluded_path(parsed)
        if excluded:
            exclusions.append({"path": source_path, "reason": excluded})
            continue
        targets = _target_paths(source_path, legacy_readme=legacy_readme)
        if targets is None:
            exclusions.append({"path": source_path, "reason": "outside_public_allowlist"})
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise SnapshotError("allowlisted entry is not a regular file")
        data = _blob(source, oid)
        _validate_text_blob(data)
        source_hash = hashlib.sha256(data).hexdigest()
        output_mode = mode
        if source_path in {"docs/PUBLIC_README.md", "docs/PUBLIC_README.zh-CN.md"}:
            data = _sanitize_public_readme(data)
        for target in targets:
            target_path = validate_repo_path(target)
            normalized = "/".join(target_path.parts).casefold()
            if normalized in targets_seen:
                raise SnapshotError("two source entries map to the same public path")
            targets_seen.add(normalized)
        pending.append((source_path, targets, data, source_hash, output_mode))

    required_sources = {"LICENSE", "THIRD_PARTY_NOTICES.md", *PUBLIC_DOCS.keys()}
    if legacy_readme:
        required_sources.remove("docs/PUBLIC_README.md")
    pending_sources = {item[0] for item in pending}
    if not required_sources.issubset(pending_sources):
        raise SnapshotError("required public license or public documentation is missing from the commit")

    # Preflight all source bytes and mappings before creating the staging directory.
    destination.mkdir(parents=True, exist_ok=True)
    for source_path, targets, data, source_hash, output_mode in pending:
        output_records = []
        for target in targets:
            output = destination.joinpath(*validate_repo_path(target).parts)
            if not _is_within(output.resolve(strict=False), destination):
                raise SnapshotError("mapped destination escaped the staging directory")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(data)
            if output_mode == "100755":
                output.chmod(0o755)
            output_records.append({"path": target, "sha256": hashlib.sha256(data).hexdigest()})
        files[source_path] = {"source_sha256": source_hash, "outputs": output_records}

    manifest_data = {
        "schema": 1,
        "source_commit": commit,
        "files": dict(sorted(files.items())),
        "exclusions": sorted(exclusions, key=lambda row: row["path"]),
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest_data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return {"source_commit": commit, "files_written": len(targets_seen),
            "excluded_entries": len(exclusions)}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--manifest", required=True, type=Path,
                        help="new private manifest path outside source and destination")
    args = parser.parse_args(argv)
    try:
        result = prepare_public_snapshot(args.source, args.destination, args.ref, args.manifest)
    except SnapshotError as exc:
        print(f"SNAPSHOT_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "PREPARED_LOCAL_SNAPSHOT", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
