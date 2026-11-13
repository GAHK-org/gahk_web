"""The image cache: blobs from intern, published under their content-hashed URLs.

intern PUTs the bytes of each image the webhost lacks (PUT /api/images/<sha256>); they are kept as a
blob in state_dir/images/<sha256>. The public URL /images/<sha16>/<name> carries a name only the
snapshot knows, so publishing links (or copies) each blob to public_html/images/<sha16>/<name>
and records it in image_files. The name keeps its letters (tovtrækning.jpg): it is written in UTF-8
(NFC, as the snapshot has it), which is what Apache finds for the percent-encoded address a browser
asks for (/images/<sha16>/tovtr%C3%A6kning.jpg). Those URLs never change content, so Apache serves them with a
one-year immutable cache.

After a successful publish, public copies the new snapshot does not use are deleted, and so are blobs
nothing uses, once they are an hour old (a blob PUT for a publish still in flight stays).

Each JPEG, PNG and WebP image also gets responsive WebP variants (variants.py), kept next to its public
copies as public_html/images/<sha16>/_<width>w.webp. A name that starts with "_" is never a
snapshot name, so a variant cannot collide with an image. The images row records the image's upright
size and which variant widths exist; pages link the variants instead of the original (render.py).
Variants are cache: they are made when intern PUTs the image (one image per request spreads the work),
and whatever is still missing is made, within a time budget, by the next publish, rebuild or
housekeeping pass. A publish that stops using an image deletes its variants at once, together with
its public copies; only the blob waits out the grace hour. An image that fails gets none, and pages
show the original; a publish never fails because of a variant. An attempt that never finished (the
process was killed) is retried only by housekeeping and the command line, never by a request intern
waits for, and an image is given up after MAX_VARIANT_ATTEMPTS of them.
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

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .clock import now_utc
from .config import Config
from .models import Image, ImageFile
from .snapshot import HEX64_RE, IMAGE_TYPES, MAX_IMAGE_BYTES, Snapshot, image_url_match

BLOB_GRACE = timedelta(hours=1)
# An image whose variants were started this often without an outcome (the process died making them)
# is given up on, so one bad image cannot make every later request try again.
MAX_VARIANT_ATTEMPTS = 3
VARIANT_NAME_RE = re.compile(r"^_[0-9]+w\.webp$")


class ImageError(ValueError):
    pass


def public_file(cfg: Config, rel_path: str) -> Path:
    """public_html/<rel_path> with its name in UTF-8 on disk, whatever locale the process has (a
    CGI process may get none, and Python's file names follow the locale): Apache maps a request's
    percent-decoded bytes to a file name as they are, and pages encode names from UTF-8."""
    return cfg.public_html / os.fsdecode(rel_path.encode("utf-8"))


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


def store(session: Session, cfg: Config, sha256: str, body: bytes, content_type: str) -> None:
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
    row = insert(Image).values(sha256=sha256, content_type=content_type, size=len(body))
    session.execute(
        row.on_conflict_do_update(
            index_elements=[Image.sha256],
            set_={
                Image.content_type: row.excluded.content_type,
                Image.size: row.excluded.size,
                Image.stored_at: func.now(),
                Image.variant_widths: None,
                Image.variant_note: None,
                Image.variant_attempts: 0,
            },
        )
    )
    session.commit()


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


def materialize(session: Session, cfg: Config, snapshot: Snapshot) -> None:
    """Make every image URL of the snapshot exist in public_html. Call after missing_for() is empty."""
    known = {
        row.rel_path: row.sha256 for row in session.execute(select(ImageFile.rel_path, ImageFile.sha256))
    }
    for image in snapshot["images"]:
        rel_path = image["url"].lstrip("/")
        target = public_file(cfg, rel_path)
        if known.get(rel_path) == image["sha256"] and target.is_file():
            continue
        _publish_copy(blob_path(cfg, image["sha256"]), target)
        row = insert(ImageFile).values(rel_path=rel_path, sha256=image["sha256"])
        session.execute(
            row.on_conflict_do_update(
                index_elements=[ImageFile.rel_path], set_={ImageFile.sha256: row.excluded.sha256}
            )
        )
        session.commit()


# --- responsive variants ---


def variant_rel_path(sha256: str, width: int) -> str:
    return f"images/{sha256[:16]}/_{width}w.webp"


@dataclass(frozen=True)
class Picture:
    """A published image with variants, as pages link it."""

    url: str  # the original's /images/ URL
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


def pictures(session: Session, snapshot: Snapshot) -> dict[str, Picture]:
    """The snapshot's images that have variants, by their original URL."""
    shas = sorted({image["sha256"] for image in snapshot["images"]})
    if not shas:
        return {}
    rows = {
        sha: (width, height, widths)
        for sha, width, height, widths in session.execute(
            select(Image.sha256, Image.width, Image.height, Image.variant_widths).where(
                Image.sha256.in_(shas),
                Image.width.is_not(None),
                Image.height.is_not(None),
                func.cardinality(Image.variant_widths) > 0,
            )
        )
        if width is not None and height is not None and widths  # what the query asked for
    }
    out: dict[str, Picture] = {}
    for image in snapshot["images"]:
        row = rows.get(image["sha256"])
        if row is None:
            continue
        width, height, widths = row
        variants = tuple((w, "/" + variant_rel_path(image["sha256"], w)) for w in sorted(widths))
        out[image["url"]] = Picture(image["url"], width, height, variants)
    return out


def _variant_files_exist(cfg: Config, sha256: str, widths: Iterable[int]) -> bool:
    return all((cfg.public_html / variant_rel_path(sha256, width)).is_file() for width in widths)


def _remove_variant_files(cfg: Config, sha256: str) -> int:
    folder = cfg.public_html / "images" / sha256[:16]
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


def _remove_files(paths: Iterable[Path]) -> None:
    folders: set[Path] = set()
    for path in paths:
        folders.add(path.parent)
        with contextlib.suppress(OSError):
            path.unlink()
    for folder in folders:
        with contextlib.suppress(OSError):
            folder.rmdir()  # only when nothing else is left in it


def _uncount_attempt(session: Session, sha256: str) -> None:
    """Take back an attempt that ended without an outcome for a reason that is not the image's (a
    missing blob, a full disk). Only a process that died making them counts toward giving up."""
    session.execute(
        update(Image)
        .where(Image.sha256 == sha256)
        .values(variant_attempts=func.greatest(Image.variant_attempts - 1, 0))
    )
    session.commit()


def make_variants(session: Session, cfg: Config, sha256: str) -> bool:
    """Make (or remake) one image's variants and record them. True when it now has some. Never
    raises for a bad image: that image just gets none."""
    from . import variants

    # Counted (and committed) before the work, so a process killed while making them (memory) still counts.
    session.execute(
        update(Image).where(Image.sha256 == sha256).values(variant_attempts=Image.variant_attempts + 1)
    )
    session.commit()
    try:
        made = variants.make(blob_path(cfg, sha256).read_bytes())
    except FileNotFoundError:
        _uncount_attempt(session, sha256)
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
        except OSError:  # a full disk or quota: not the image's fault, so try again later
            _remove_files(written)
            _uncount_attempt(session, sha256)
            return False
        recorded = session.scalar(
            update(Image)
            .where(Image.sha256 == sha256)
            .values(
                width=made.width,
                height=made.height,
                variant_widths=[width for width, _data in made.files],
                variant_note=None,
                variant_attempts=0,
            )
            .returning(Image.sha256)
        )
        session.commit()
        if recorded is None:
            # A publish collected the image while these were made (this runs outside the render
            # lock): no row is left that could ever delete them, so they go now.
            _remove_files(written)
            return False
        return True
    session.execute(update(Image).where(Image.sha256 == sha256).values(variant_widths=[], variant_note=note))
    session.commit()
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
    session: Session,
    cfg: Config,
    shas: Iterable[str],
    budget: float | None = None,
    *,
    retry: bool = True,
) -> VariantProgress:
    """Make the variants these images lack, until `budget` seconds have passed (None: no limit).

    Recorded variants whose files have gone are forgotten first, so no page links a missing file;
    they are made again like any other. Images over the budget stay pending for a later pass.

    With retry=False, an image whose earlier attempt never finished (the process was killed, maybe
    for its memory) is left pending: the requests intern waits for (PUT, publish, rebuild) pass that
    False, so such an image cannot fail them again. Housekeeping and the command line retry it."""
    shas = list(dict.fromkeys(shas))
    if not shas:
        return VariantProgress(0, 0)
    rows = {
        sha: (widths, attempts)
        for sha, widths, attempts in session.execute(
            select(Image.sha256, Image.variant_widths, Image.variant_attempts).where(Image.sha256.in_(shas))
        )
    }
    deadline = None if budget is None else time.monotonic() + budget
    changed = pending = 0
    for sha in shas:
        row = rows.get(sha)
        if row is None:
            continue
        widths, attempts = row
        if widths is not None:
            if _variant_files_exist(cfg, sha, widths):
                continue
            session.execute(update(Image).where(Image.sha256 == sha).values(variant_widths=None))
            session.commit()
            changed += 1
        if attempts >= MAX_VARIANT_ATTEMPTS:
            session.execute(
                update(Image)
                .where(Image.sha256 == sha)
                .values(variant_widths=[], variant_note=f"given up after {attempts} unfinished attempts")
            )
            session.commit()
            continue
        if (not retry and attempts > 0) or (deadline is not None and time.monotonic() >= deadline):
            pending += 1
            continue
        if make_variants(session, cfg, sha):
            changed += 1
    return VariantProgress(changed, pending)


def collect_garbage(session: Session, cfg: Config, snapshot: Snapshot) -> int:
    """Delete public copies and blobs the snapshot does not use. Returns the number of files removed."""
    used_paths = {image["url"].lstrip("/") for image in snapshot["images"]}
    used_shas = {image["sha256"] for image in snapshot["images"]}
    used_folders = {sha[:16] for sha in used_shas}
    removed = 0
    for rel_path in session.scalars(select(ImageFile.rel_path)).all():
        if rel_path in used_paths or not image_url_match("/" + rel_path):
            continue
        target = public_file(cfg, rel_path)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
            removed += 1
        with contextlib.suppress(OSError):
            target.parent.rmdir()  # only succeeds when the <sha16> folder is now empty
        session.execute(delete(ImageFile).where(ImageFile.rel_path == rel_path))
        session.commit()

    cutoff = now_utc() - BLOB_GRACE
    stored = session.execute(select(Image.sha256, Image.stored_at, Image.variant_widths)).all()
    for sha, stored_at, widths in stored:
        if sha in used_shas:
            continue
        # An image taken down loses its public variants at once, like its public copies above. Only
        # the blob waits out the grace hour. Forgetting the widths keeps a publish in flight, whose
        # variants were checked before it took the lock, from linking the deleted files: it shows
        # the original, and the next pass makes the variants again.
        if sha[:16] not in used_folders:
            removed += _remove_variant_files(cfg, sha)
        if stored_at > cutoff:
            if widths is not None:
                session.execute(
                    update(Image)
                    .where(Image.sha256 == sha)
                    .values(variant_widths=None, variant_note=None, variant_attempts=0)
                )
                session.commit()
            continue
        with contextlib.suppress(FileNotFoundError):
            blob_path(cfg, sha).unlink()
            removed += 1
        session.execute(delete(Image).where(Image.sha256 == sha))
        session.commit()
    return removed
