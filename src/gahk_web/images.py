"""The image cache: blobs from intern, published under their content-hashed URLs.

intern PUTs the bytes of each image the webhost lacks (PUT /api/images/<sha256>); they are kept as a
blob in state_dir/images/<sha256>. The public URL /billeder/<sha16>/<name> carries a name only the
snapshot knows, so publishing links (or copies) each blob to public_html/billeder/<sha16>/<name>
and records it in image_files. Those URLs never change content, so Apache serves them with a
one-year immutable cache.

After a successful publish, public copies the new snapshot does not use are deleted, and so are blobs
nothing uses, once they are an hour old (a blob PUT for a publish still in flight stays).
"""

import contextlib
import hashlib
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from . import db
from .clock import now_utc
from .config import Config
from .snapshot import BILLEDER_RE, HEX64_RE, IMAGE_TYPES, MAX_IMAGE_BYTES, Snapshot

BLOB_GRACE = timedelta(hours=1)


class ImageError(ValueError):
    pass


def blob_dir(cfg: Config) -> Path:
    return cfg.state_dir / "images"


def blob_path(cfg: Config, sha256: str) -> Path:
    return blob_dir(cfg) / sha256


def _looks_like(content_type: str, body: bytes) -> bool:
    if content_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return body.startswith((b"GIF87a", b"GIF89a"))
    return body[:4] == b"RIFF" and body[8:12] == b"WEBP"


def write_atomically(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def store(con: db.Connection, cfg: Config, sha256: str, body: bytes, content_type: str) -> None:
    """Keep the bytes of one image. Raises ImageError for anything but a matching, allowed image."""
    content_type = content_type.split(";", 1)[0].strip().lower()
    if not HEX64_RE.match(sha256):
        raise ImageError("the path must end in a lower-case sha256")
    if len(body) > MAX_IMAGE_BYTES:
        raise ImageError("image larger than 15 MB")
    if content_type not in IMAGE_TYPES:
        raise ImageError(f"content type {content_type!r} is not allowed")
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ImageError("sha256 of the body does not match the path")
    if not _looks_like(content_type, body):
        raise ImageError(f"the bytes are not {content_type}")
    write_atomically(blob_path(cfg, sha256), body)
    db.execute(
        con,
        "INSERT INTO images (sha256, content_type, size) VALUES (:sha, :type, :size)"
        " ON CONFLICT (sha256) DO UPDATE SET content_type = EXCLUDED.content_type,"
        " size = EXCLUDED.size, stored_at = now()",
        sha=sha256,
        type=content_type,
        size=len(body),
    )


def missing(cfg: Config, shas: list[str]) -> list[str]:
    """The given hashes we hold no blob for, in the order given."""
    return [sha for sha in dict.fromkeys(shas) if not blob_path(cfg, sha).is_file()]


def missing_for(cfg: Config, snapshot: Snapshot) -> list[str]:
    return missing(cfg, [image["sha256"] for image in snapshot["images"]])


def _publish_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    os.close(fd)
    temporary = Path(tmp)
    temporary.unlink()
    try:
        try:
            temporary.hardlink_to(source)  # same filesystem: no second copy of the bytes
        except OSError:
            shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        temporary.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def materialize(con: db.Connection, cfg: Config, snapshot: Snapshot) -> None:
    """Make every image URL of the snapshot exist in public_html. Call after missing_for() is empty."""
    known = {
        row["rel_path"]: row["sha256"] for row in db.query(con, "SELECT rel_path, sha256 FROM image_files")
    }
    for image in snapshot["images"]:
        rel_path = image["url"].lstrip("/")
        target = cfg.public_html / rel_path
        if known.get(rel_path) == image["sha256"] and target.is_file():
            continue
        _publish_copy(blob_path(cfg, image["sha256"]), target)
        db.execute(
            con,
            "INSERT INTO image_files (rel_path, sha256) VALUES (:path, :sha)"
            " ON CONFLICT (rel_path) DO UPDATE SET sha256 = EXCLUDED.sha256",
            path=rel_path,
            sha=image["sha256"],
        )


def collect_garbage(con: db.Connection, cfg: Config, snapshot: Snapshot) -> int:
    """Delete public copies and blobs the snapshot does not use. Returns the number of files removed."""
    used_paths = {image["url"].lstrip("/") for image in snapshot["images"]}
    used_shas = {image["sha256"] for image in snapshot["images"]}
    removed = 0
    for row in db.query(con, "SELECT rel_path FROM image_files"):
        rel_path = row["rel_path"]
        if rel_path in used_paths or not BILLEDER_RE.match("/" + rel_path):
            continue
        target = cfg.public_html / rel_path
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
            removed += 1
        with contextlib.suppress(OSError):
            target.parent.rmdir()  # only succeeds when the <sha16> folder is now empty
        db.execute(con, "DELETE FROM image_files WHERE rel_path = :path", path=rel_path)

    cutoff = now_utc() - BLOB_GRACE
    for row in db.query(con, "SELECT sha256, stored_at FROM images"):
        sha = row["sha256"]
        if sha in used_shas or row["stored_at"] > cutoff:
            continue
        with contextlib.suppress(FileNotFoundError):
            blob_path(cfg, sha).unlink()
            removed += 1
        db.execute(con, "DELETE FROM images WHERE sha256 = :sha", sha=sha)
    return removed
