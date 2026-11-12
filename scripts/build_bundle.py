#!/usr/bin/env python3
"""Build the release bundle the webhost's updater installs: python scripts/build_bundle.py

    dist/gahk_web-<sha>.tar.gz      the release (layout below)
    dist/manifest.json              {"version", "bundle", "sha256", "size", "built_at", "python"}
    dist/manifest-<sha>.json        the same, kept beside the bundle so a pin can find it later
    dist/updater.py, gahk.cgi, htaccess   the three files of the one-time bootstrap (DEPLOY.md)

The bundle, with no absolute paths, no links and fixed owners, dates and modes:

    VERSION                         the git sha
    requirements.lock               the runtime dependencies from uv.lock: every version pinned, with
                                    the sha256 of every wheel for every platform
    selftest.py                     python3 -I selftest.py: may this release go live?
    bin/updater.py                  the self-updater (a copy of src/gahk_web/updater.py)
    src/gahk_web/                   the package; its static/ holds only static-manifest.json
    public_html/.htaccess
    public_html/cgi-bin/gahk.cgi    the CGI stub (POST /api/update, large bodies, the CGI-only switch)
    public_html/cgi-bin/gahk.fcgi   the FastCGI stub (everything else, in a persistent process)
    public_html/static/             site assets: css/js with content-hashed names, the rest as they are

The bundle holds no dependencies. The webhost's updater installs requirements.lock with pip for its
own python into <release>/vendor; pip picks the wheels for that python and checks their hashes. CI
installs the same file the same way on Python 3.13 and 3.14 and self-tests the release with it
(scripts/smoke_bundle.py). The build is reproducible: the same commit and uv.lock give the same
bytes. Needs uv on PATH.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "gahk_web"
HASHED_SUFFIXES = (".css", ".js")
SKIP_NAMES = ("__pycache__", ".DS_Store")
PYTHON_REQUIREMENT = ">=3.13"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class BuildError(Exception):
    pass


def run(*args: str, cwd: Path = ROOT, capture: bool = False) -> str:
    done = subprocess.run(args, cwd=cwd, check=False, text=True, capture_output=capture)  # noqa: S603
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip() if capture else ""
        raise BuildError(f"{' '.join(args)} failed ({done.returncode}) {detail}".strip())
    return done.stdout or ""


def git_version() -> tuple[str, int]:
    sha = run("git", "rev-parse", "HEAD", capture=True).strip()
    when = int(run("git", "show", "-s", "--format=%ct", "HEAD", capture=True).strip())
    if run("git", "status", "--porcelain", "--untracked-files=no", capture=True).strip():
        print(f"warning: the working tree has uncommitted changes; the bundle still says {sha}")
    return sha, when


def copy_tree(source: Path, target: Path, skip: tuple[str, ...] = ()) -> None:
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(part in SKIP_NAMES for part in rel.parts) or path.suffix == ".pyc":
            continue
        if rel.parts[0] in skip:
            continue
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)


def build_static(stage: Path) -> dict[str, str]:
    """public_html/static with hashed css/js names; returns {"css/site.css": "css/site.<hash>.css"}."""
    source = PACKAGE / "static"
    target = stage / "public_html" / "static"
    manifest: dict[str, str] = {}
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if not path.is_file() or any(part in SKIP_NAMES for part in rel.parts):
            continue
        if rel.name == "static-manifest.json":
            continue
        name = rel.name
        if path.suffix in HASHED_SUFFIXES:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            name = f"{path.stem}.{digest}{path.suffix}"
            manifest[rel.as_posix()] = rel.with_name(name).as_posix()
        dest = target / rel.with_name(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    data = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (target / "static-manifest.json").write_text(data, encoding="utf-8")
    package_static = stage / "src" / "gahk_web" / "static"
    package_static.mkdir(parents=True, exist_ok=True)
    (package_static / "static-manifest.json").write_text(data, encoding="utf-8")
    return manifest


def requirements_lock() -> str:
    """The runtime dependencies as pip reads them: pinned, hashed, environment markers kept.

    Exported to stdout, so the header names no temporary path and the file depends on uv.lock only."""
    lock = run(
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        capture=True,
    )
    if "--hash=sha256:" not in lock:
        raise BuildError("uv export gave no hashes")
    return lock


def stage_release(stage: Path, version: str) -> None:
    copy_tree(PACKAGE, stage / "src" / "gahk_web", skip=("static",))
    build_static(stage)
    for rel in (".htaccess", "cgi-bin/gahk.cgi", "cgi-bin/gahk.fcgi"):
        dest = stage / "public_html" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "public_html" / rel, dest)
    (stage / "bin").mkdir()
    shutil.copyfile(PACKAGE / "updater.py", stage / "bin" / "updater.py")
    shutil.copyfile(ROOT / "bundle" / "selftest.py", stage / "selftest.py")
    (stage / "VERSION").write_text(version + "\n", encoding="ascii")
    (stage / "requirements.lock").write_text(requirements_lock(), encoding="utf-8")


EXECUTABLES = {
    "selftest.py",
    "bin/updater.py",
    "public_html/cgi-bin/gahk.cgi",
    "public_html/cgi-bin/gahk.fcgi",
}


def write_tarball(stage: Path, target: Path, mtime: int) -> None:
    """A gzipped tar that depends only on the staged files: sorted, fixed owner, date and modes."""

    def info(rel: str, path: Path) -> tarfile.TarInfo:
        entry = tarfile.TarInfo(rel)
        entry.mtime = mtime
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = ""
        if path.is_dir():
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o755
        else:
            entry.size = path.stat().st_size
            entry.mode = 0o755 if rel in EXECUTABLES else 0o644
        return entry

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(stage.rglob("*"), key=lambda p: p.relative_to(stage).as_posix()):
            rel = path.relative_to(stage).as_posix()
            if any(part in SKIP_NAMES for part in path.relative_to(stage).parts) or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise BuildError(f"the staged release holds a symlink: {rel}")
            entry = info(rel, path)
            if entry.isdir():
                tar.addfile(entry)
            else:
                with path.open("rb") as handle:
                    tar.addfile(entry, handle)
    with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        gz.write(buffer.getvalue())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder (default dist/)")
    args = parser.parse_args(argv)

    version, committed_at = git_version()
    if not SHA_RE.match(version):
        raise BuildError(f"not a git sha: {version}")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("gahk_web-*.tar.gz"), *out.glob("manifest*.json")]:
        old.unlink()  # dist/ holds exactly one release, for CI's upload step
    with tempfile.TemporaryDirectory(prefix="gahk_web-bundle-") as tmp:
        stage = Path(tmp) / "release"
        stage.mkdir()
        stage_release(stage, version)

        bundle_name = f"gahk_web-{version}.tar.gz"
        bundle = out / bundle_name
        write_tarball(stage, bundle, int(os.environ.get("SOURCE_DATE_EPOCH", committed_at)))

    data = bundle.read_bytes()
    manifest = {
        "version": version,
        "bundle": bundle_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "python": PYTHON_REQUIREMENT,
    }
    text = json.dumps(manifest, indent=2) + "\n"
    (out / "manifest.json").write_text(text, encoding="utf-8")
    (out / f"manifest-{version}.json").write_text(text, encoding="utf-8")
    shutil.copyfile(PACKAGE / "updater.py", out / "updater.py")
    shutil.copyfile(ROOT / "public_html" / "cgi-bin" / "gahk.cgi", out / "gahk.cgi")
    shutil.copyfile(ROOT / "public_html" / ".htaccess", out / "htaccess")
    print(f"built {bundle} ({len(data)} bytes, sha256 {manifest['sha256']})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BuildError as exc:
        print(f"build_bundle: {exc}", file=sys.stderr)
        sys.exit(1)
