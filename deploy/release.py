"""Allowlisted, reproducible release archives; no secrets or runtime data."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile


FILES = (
    "app.py", "sync.py", "crypto_sync.py", "refresh.py", "requirements.txt", "Dockerfile", ".dockerignore",
    "compose.yaml", "compose.production.yaml", ".env.example", "README.md", "templates/index.html",
    "tests/__init__.py", "tests/test_app.py", "tests/test_sync.py", "tests/test_crypto_sync.py", "tests/test_refresh.py",
    "tests/check_production.py", "tests/check_deploy.py", "deploy/setup.sh", "deploy/update.sh",
    "deploy/manage.py", "deploy/release.py", "deploy/data.py", "deploy/server.env.example", "deploy/Caddyfile.stocks",
    "deploy/ROLLOUT.md", "docs/deployment.md", "docs/PLAN_deployment.md",
)
LIMIT = 8 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_file(path, owner=None):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError("Expected a private regular file")
    if owner is not None and info.st_uid != owner:
        raise ValueError("Unexpected file owner")
    return path.read_bytes()


def identifier(files):
    return digest(json.dumps(files, sort_keys=True).encode())[:16]


def prepare(source, output):
    output.mkdir(mode=0o700, exist_ok=False)
    files, payload = {}, {}
    for name in FILES:
        path = source / name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.resolve() != source.resolve() / name:
            raise ValueError("Missing or unsafe release file: " + name)
        payload[name] = path.read_bytes()
        files[name] = digest(payload[name])
    if sum(map(len, payload.values())) > LIMIT:
        raise ValueError("Release exceeds size limit")
    archive = output / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in sorted(payload.items()):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            tar.addfile(entry, io.BytesIO(data))
    archive.chmod(0o600)
    manifest = {"release": identifier(files), "files": files, "archive_sha256": digest(archive.read_bytes())}
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (output / "manifest.json").chmod(0o600)
    return unpack(output)


def unpack(stage):
    manifest = json.loads(private_file(stage / "manifest.json"))
    validate_manifest(manifest)

    archive = private_file(stage / "release.tar.gz")
    if len(archive) > LIMIT or digest(archive) != manifest["archive_sha256"]:
        raise ValueError("Release archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        entries = tar.getmembers()
        if len(entries) != len(FILES) or {e.name for e in entries} != set(FILES):
            raise ValueError("Unexpected archive entries")
        if any(not e.isfile() or e.size < 0 for e in entries) or sum(e.size for e in entries) > LIMIT:
            raise ValueError("Unsafe archive member")
        payload = {e.name: tar.extractfile(e).read() for e in entries}
    if any(digest(data) != manifest["files"][name] for name, data in payload.items()):
        raise ValueError("Release file checksum mismatch")
    code = stage / "code"
    code.mkdir(mode=0o700, exist_ok=False)
    for name, data in payload.items():
        path = code / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o644)  # Non-secret source must be readable by the image's app UID.
    for path in code.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    return manifest


def validate_manifest(manifest):
    if set(manifest) != {"release", "files", "archive_sha256"} or set(manifest["files"]) != set(FILES):
        raise ValueError("Unexpected release manifest")
    if manifest["release"] != identifier(manifest["files"]):
        raise ValueError("Invalid release identity")
    if any(not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
           for value in [manifest["archive_sha256"], *manifest["files"].values()]):
        raise ValueError("Invalid manifest digest")


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) != 2:
        raise SystemExit("Usage: release.py PRIVATE_STAGE_DIRECTORY")
    print(unpack(Path(sys.argv[1]))["release"])
