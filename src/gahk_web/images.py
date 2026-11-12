"""The image cache: blobs from intern, published under their content-hashed URLs.

intern PUTs the bytes of each image the webhost lacks (PUT /api/images/<sha256>); they are kept as a
blob in state_dir/images/<sha256>. The public URL /billeder/<sha16>/<name> carries a name only the
snapshot knows, so publishing links (or copies) each blob to public_html/billeder/<sha16>/<name>
and records it in image_files. Those URLs never change content, so Apache serves them with a
one-year immutable cache.

After a successful publish, public copies the new snapshot does not use are deleted, and so are blobs
nothing uses, once they are an hour old (a blob PUT for a publish still in flight stays).

Each JPEG, PNG and WebP image also gets responsive WebP variants (variants.py), kept next to its public
copies as public_html/billeder/<sha16>/_<width>w.webp. A name that starts with "_" is never a
snapshot name, so a variant cannot collide with an image. The images row records the image's upright
size and which variant widths exist; pages link the variants instead of the original (render.py).
Variants are cache: they are made when intern PUTs the image (one image per request spreads the work),
and whatever is still missing is made, within a time budget, by the next publish, rebuild or
housekeeping pass. They go with their blob. An image that fails gets none, and pages show the
original; a publish never fails because of a variant.
"""

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import db
from .clock import now_utc
from .config import Config
from .snapshot import BILLEDER_RE, HEX64_RE, IMAGE_TYPES, MAX_IMAGE_BYTES, Snapshot

BLOB_GRACE = timedelta(hours=1)
# An image whose variants were started this often without an outcome (the process died making them)
# is given up on, so one bad image cannot make every later request try again.
MAX_VARIANT_ATTEMPTS = 3
VARIANT_NAME_RE = re.compile(r"^_[0-9]+w\.webp$")


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
    # intern only sends an image the webhost lacks, so its variants are made afresh as well.
    db.execute(
        con,
        "INSERT INTO images (sha256, content_type, size) VALUES (:sha, :type, :size)"
        " ON CONFLICT (sha256) DO UPDATE SET content_type = EXCLUDED.content_type,"
        " size = EXCLUDED.size, stored_at = now(), variant_widths = NULL, variant_note = NULL,"
        " variant_attempts = 0",
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


# --- responsive variants ---


def variant_rel_path(sha256: str, width: int) -> str:
    return f"billeder/{sha256[:16]}/_{width}w.webp"


@dataclass(frozen=True)
class Picture:
    """A published image with variants, as pages link it."""

    url: str  # the original's /billeder/ URL
    width: int  # upright size of the original, in pixels
    height: int
    variants: tuple[tuple[int, str], ...]  # (width, URL), narrowest first; never empty

    @property
    def src(self) -> str:
        return self.variants[-1][1]

    @property
    def srcset(self) -> str:
        return ", ".join(f"{url} {width}w" for width, url in self.variants)

    def at_least(self, width: int) -> str:
        """The narrowest variant at least this wide, else the widest."""
        return next((url for variant_width, url in self.variants if variant_width >= width), self.src)


def pictures(con: db.Connection, snapshot: Snapshot) -> dict[str, Picture]:
    """The snapshot's images that have variants, by their original URL."""
    shas = sorted({image["sha256"] for image in snapshot["images"]})
    if not shas:
        return {}
    rows = {
        row["sha256"]: row
        for row in db.query(
            con,
            "SELECT sha256, width, height, variant_widths FROM images WHERE sha256 = ANY(:shas)"
            " AND width IS NOT NULL AND height IS NOT NULL AND cardinality(variant_widths) > 0",
            shas=shas,
        )
    }
    out: dict[str, Picture] = {}
    for image in snapshot["images"]:
        row = rows.get(image["sha256"])
        if row is None:
            continue
        variants = tuple(
            (width, "/" + variant_rel_path(image["sha256"], width)) for width in sorted(row["variant_widths"])
        )
        out[image["url"]] = Picture(image["url"], row["width"], row["height"], variants)
    return out


def _variant_files_exist(cfg: Config, sha256: str, widths: Iterable[int]) -> bool:
    return all((cfg.public_html / variant_rel_path(sha256, width)).is_file() for width in widths)


def _remove_variant_files(cfg: Config, sha256: str) -> int:
    folder = cfg.public_html / "billeder" / sha256[:16]
    removed = 0
    with contextlib.suppress(OSError):
        for entry in folder.iterdir():
            if VARIANT_NAME_RE.match(entry.name):
                with contextlib.suppress(FileNotFoundError):
                    entry.unlink()
                    removed += 1
    with contextlib.suppress(OSError):
        folder.rmdir()  # only when nothing else is left in it
    return removed


def make_variants(con: db.Connection, cfg: Config, sha256: str) -> bool:
    """Make (or remake) one image's variants and record them. True when it now has some. Never
    raises for a bad image: that image just gets none."""
    from . import variants

    db.execute(
        con, "UPDATE images SET variant_attempts = variant_attempts + 1 WHERE sha256 = :sha", sha=sha256
    )
    try:
        made = variants.make(blob_path(cfg, sha256).read_bytes())
    except FileNotFoundError:
        return False  # intern PUTs it again when it asks what is missing
    except variants.NoVariants as exc:
        note = str(exc)
    except Exception as exc:  # a picture Pillow cannot read; never fail a publish over it
        note = f"failed: {type(exc).__name__}: {exc}"[:500]
    else:
        written: list[Path] = []
        try:
            for width, data in made.files:
                target = cfg.public_html / variant_rel_path(sha256, width)
                write_atomically(target, data)
                written.append(target)
        except OSError:  # a full disk: try again later
            for target in written:
                with contextlib.suppress(OSError):
                    target.unlink()
            return False
        db.execute(
            con,
            "UPDATE images SET width = :width, height = :height, variant_widths = CAST(:widths AS integer[]),"
            " variant_note = NULL, variant_attempts = 0 WHERE sha256 = :sha",
            width=made.width,
            height=made.height,
            widths=[width for width, _data in made.files],
            sha=sha256,
        )
        return True
    db.execute(
        con,
        "UPDATE images SET variant_widths = '{}', variant_note = :note WHERE sha256 = :sha",
        note=note,
        sha=sha256,
    )
    return False


@dataclass(frozen=True)
class VariantProgress:
    changed: int  # images whose links changed: variants made, or recorded ones found missing
    pending: int  # images still waiting for theirs (the time budget ran out)


def snapshot_shas(snapshot: Snapshot) -> list[str]:
    """The snapshot's images, the ones seen first on a page (backgrounds) first."""
    by_url = {image["url"]: image["sha256"] for image in snapshot["images"]}
    first = [snapshot["site"]["default_background"]] + [page["background"] for page in snapshot["pages"]]
    ordered = [by_url[url] for url in first if url in by_url]
    ordered += [image["sha256"] for image in snapshot["images"]]
    return list(dict.fromkeys(ordered))


def ensure_variants(
    con: db.Connection, cfg: Config, shas: Iterable[str], budget: float | None = None
) -> VariantProgress:
    """Make the variants these images lack, until `budget` seconds have passed (None: no limit).

    Recorded variants whose files have gone are forgotten first, so no page links a missing file;
    they are made again like any other. Images over the budget stay pending for a later pass."""
    shas = list(dict.fromkeys(shas))
    if not shas:
        return VariantProgress(0, 0)
    rows = {
        row["sha256"]: row
        for row in db.query(
            con,
            "SELECT sha256, variant_widths, variant_attempts FROM images WHERE sha256 = ANY(:shas)",
            shas=shas,
        )
    }
    deadline = None if budget is None else time.monotonic() + budget
    changed = pending = 0
    for sha in shas:
        row = rows.get(sha)
        if row is None:
            continue
        widths = row["variant_widths"]
        if widths is not None:
            if _variant_files_exist(cfg, sha, widths):
                continue
            db.execute(con, "UPDATE images SET variant_widths = NULL WHERE sha256 = :sha", sha=sha)
            changed += 1
        if row["variant_attempts"] >= MAX_VARIANT_ATTEMPTS:
            db.execute(
                con,
                "UPDATE images SET variant_widths = '{}', variant_note = :note WHERE sha256 = :sha",
                note=f"given up after {row['variant_attempts']} unfinished attempts",
                sha=sha,
            )
            continue
        if deadline is not None and time.monotonic() >= deadline:
            pending += 1
            continue
        if make_variants(con, cfg, sha):
            changed += 1
    return VariantProgress(changed, pending)


def collect_garbage(con: db.Connection, cfg: Config, snapshot: Snapshot) -> int:
    """Delete public copies and blobs the snapshot does not use. Returns the number of files removed."""
    used_paths = {image["url"].lstrip("/") for image in snapshot["images"]}
    used_shas = {image["sha256"] for image in snapshot["images"]}
    used_folders = {sha[:16] for sha in used_shas}
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
        if sha[:16] not in used_folders:
            removed += _remove_variant_files(cfg, sha)
        db.execute(con, "DELETE FROM images WHERE sha256 = :sha", sha=sha)
    return removed
