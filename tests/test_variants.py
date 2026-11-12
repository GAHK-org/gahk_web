"""Responsive WebP variants: how they are made, kept, linked and cleaned up."""

import hashlib
import io
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from conftest import GANG, Client, image, page, publish, snapshot
from PIL import Image, ImageCms

from gahk_web import api, db, images, variants
from gahk_web.config import Config

GPS = 0x8825
ORIENTATION = 0x0112


def photo(
    width: int,
    height: int,
    *,
    kind: str = "JPEG",
    mode: str = "RGB",
    gps: bool = False,
    orientation: int | None = None,
    icc: bool = False,
) -> bytes:
    """A real picture: a gradient (so it compresses like a photo, not like a flat colour)."""
    gradient = Image.linear_gradient("L").resize((width, height))
    picture = Image.merge("RGB", (gradient, gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT), gradient))
    if mode == "RGBA":
        picture = picture.convert("RGBA")
        picture.putalpha(gradient)
    elif mode != "RGB":
        picture = picture.convert(mode)
    exif = Image.Exif()
    if gps:
        exif[GPS] = {1: "N", 2: (55.0, 41.0, 59.0), 3: "E", 4: (12.0, 34.0, 56.0)}
        exif[0x010F] = "Kamerafabrik"  # Make
    if orientation:
        exif[ORIENTATION] = orientation
    options: dict[str, Any] = {"exif": exif.tobytes()} if len(exif) else {}
    if icc:
        options["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    if kind == "JPEG":
        options["xmp"] = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'
    buffer = io.BytesIO()
    picture.save(buffer, kind, **options)
    return buffer.getvalue()


def riff_chunks(data: bytes) -> list[bytes]:
    """The chunk ids of a WebP file (EXIF, XMP and ICCP would be metadata)."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ids, offset = [], 12
    while offset + 8 <= len(data):
        chunk, size = data[offset : offset + 4], struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        ids.append(chunk)
        offset += 8 + size + (size & 1)
    return ids


def assert_bare_webp(data: bytes, size: tuple[int, int]) -> None:
    assert not {b"EXIF", b"XMP ", b"ICCP"} & set(riff_chunks(data))
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == size
        assert not decoded.getexif()
        assert not {"exif", "xmp", "icc_profile"} & set(decoded.info)


# --- making variants ---


@pytest.mark.parametrize(
    ("width", "expected"),
    [
        (300, (300,)),
        (480, (480,)),
        (800, (480, 800)),
        (1600, (480, 960, 1600)),
        (2000, (480, 960, 1600, 2000)),
        (2400, (480, 960, 1600, 2400)),
        (4800, (480, 960, 1600, 2400)),
    ],
)
def test_variant_widths_never_scale_up(width: int, expected: tuple[int, ...]) -> None:
    assert variants.widths_for(width) == expected


def test_variants_are_webp_without_any_metadata() -> None:
    original = photo(2000, 1000, gps=True, icc=True)
    with Image.open(io.BytesIO(original)) as check:  # the test picture really carries it all
        assert GPS in check.getexif() and "icc_profile" in check.info and "xmp" in check.info

    made = variants.make(original)

    assert (made.width, made.height) == (2000, 1000)
    assert [width for width, _data in made.files] == [480, 960, 1600, 2000]
    for width, data in made.files:
        assert_bare_webp(data, (width, width // 2))
        assert b"Kamerafabrik" not in data
    assert sum(len(data) for _width, data in made.files[:-1]) < len(original)


def test_variants_are_turned_upright() -> None:
    made = variants.make(photo(600, 400, orientation=6))
    assert (made.width, made.height) == (400, 600)
    [(width, data)] = made.files
    assert width == 400
    assert_bare_webp(data, (400, 600))


def test_a_big_photo_is_decoded_reduced_and_capped_at_2400() -> None:
    made = variants.make(photo(4800, 3200))
    assert (made.width, made.height) == (4800, 3200)
    assert [(width, Image.open(io.BytesIO(data)).size) for width, data in made.files] == [
        (480, (480, 320)),
        (960, (960, 640)),
        (1600, (1600, 1067)),
        (2400, (2400, 1600)),
    ]


def test_transparency_is_kept() -> None:
    made = variants.make(photo(800, 600, kind="PNG", mode="RGBA"))
    for _width, data in made.files:
        with Image.open(io.BytesIO(data)) as decoded:
            assert decoded.mode == "RGBA"
            assert decoded.getextrema()[3][0] < 50  # still partly see-through


@pytest.mark.parametrize("mode", ["L", "P", "CMYK"])
def test_other_colour_modes_become_rgb(mode: str) -> None:
    kind = "JPEG" if mode == "CMYK" else "PNG"
    made = variants.make(photo(500, 300, kind=kind, mode=mode))
    with Image.open(io.BytesIO(made.files[-1][1])) as decoded:
        assert decoded.mode == "RGB"


def tiny_png_header(width: int, height: int) -> bytes:
    """A PNG that claims to be huge: a few bytes on the wire, gigabytes once decoded."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def test_decompression_bombs_get_no_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(variants.NoVariants, match="pixels"):
        variants.make(tiny_png_header(40000, 40000))
    monkeypatch.setattr(variants, "MAX_PIXELS", 100 * 100 - 1)
    with pytest.raises(variants.NoVariants, match="pixels"):
        variants.make(photo(100, 100, kind="PNG"))


def test_gifs_animations_and_broken_files_get_no_variants() -> None:
    gif = io.BytesIO()
    Image.new("P", (10, 10)).save(gif, "GIF")
    with pytest.raises(variants.NoVariants, match="not a JPEG"):
        variants.make(gif.getvalue())

    animation = io.BytesIO()
    frames = [Image.new("RGB", (10, 10), colour) for colour in ("red", "blue")]
    frames[0].save(animation, "WEBP", save_all=True, append_images=frames[1:])
    with pytest.raises(variants.NoVariants, match="animation"):
        variants.make(animation.getvalue())

    with pytest.raises(OSError, match="truncated"):
        variants.make(photo(800, 600)[:2000])


# --- keeping and publishing them ---

PHOTO = photo(2000, 1000, gps=True)
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
BROKEN = photo(800, 600)[:2000]
BROKEN_SHA = hashlib.sha256(BROKEN).hexdigest()


def put(client: Client, data: bytes, content_type: str = "image/jpeg") -> None:
    sha = hashlib.sha256(data).hexdigest()
    result = client.api("PUT", f"/api/images/{sha}", raw=data, headers={"Content-Type": content_type})
    assert result.status == 204, result.text


def variant_files(cfg: Config, sha: str) -> list[str]:
    folder = cfg.public_html / "billeder" / sha[:16]
    return sorted(entry.name for entry in folder.glob("_*")) if folder.is_dir() else []


def image_row(con: db.Connection, sha: str) -> dict[str, Any]:
    row = db.query_one(
        con,
        "SELECT width, height, variant_widths, variant_note, variant_attempts FROM images WHERE sha256 = :sha",
        sha=sha,
    )
    assert row is not None
    return row


def photo_snapshot(**overrides: object) -> dict[str, Any]:
    """The test snapshot with the real photo in a page body, as that page's background and the
    front page's, and the broken one in a news item."""
    data = snapshot(**overrides)
    gallery, broken = image("galleri.jpg", PHOTO), image("broken.jpg", BROKEN)
    data["images"] += [gallery, broken]
    data["pages"][0]["background"] = gallery["url"]
    data["pages"][0]["body"] = f'<p><img src="{gallery["url"]}" alt="Forsiden"></p>'
    data["pages"].append(
        page(
            6,
            "galleri",
            "Galleri",
            f'<p>Se her</p><p><img alt="" src="{gallery["url"]}" style="width: 420px; height: 210px;">'
            f'<img src="{broken["url"]}"></p><script>alert(1)</script>',
            background=gallery["url"],
        )
    )
    return data


def test_a_put_makes_the_variants(client: Client, cfg: Config, con: db.Connection) -> None:
    put(client, PHOTO)

    assert variant_files(cfg, PHOTO_SHA) == ["_1600w.webp", "_2000w.webp", "_480w.webp", "_960w.webp"]
    row = image_row(con, PHOTO_SHA)
    assert (row["width"], row["height"], row["variant_widths"]) == (2000, 1000, [480, 960, 1600, 2000])
    for name in variant_files(cfg, PHOTO_SHA):
        data = (cfg.public_html / "billeder" / PHOTO_SHA[:16] / name).read_bytes()
        assert_bare_webp(data, Image.open(io.BytesIO(data)).size)


def test_a_broken_image_is_kept_without_variants(client: Client, cfg: Config, con: db.Connection) -> None:
    put(client, BROKEN)
    assert variant_files(cfg, BROKEN_SHA) == []
    row = image_row(con, BROKEN_SHA)
    assert row["variant_widths"] == []
    assert row["variant_note"].startswith("failed: OSError")
    assert images.blob_path(cfg, BROKEN_SHA).read_bytes() == BROKEN


def test_pages_link_the_variants(client: Client, cfg: Config) -> None:
    put(client, PHOTO)
    put(client, BROKEN)
    publish(client, photo_snapshot())
    gallery = image("galleri.jpg", PHOTO)["url"]
    broken = image("broken.jpg", BROKEN)["url"]
    folder = f"/billeder/{PHOTO_SHA[:16]}"

    html = (cfg.public_html / "galleri/index.html").read_text()
    assert (
        f'<img src="{folder}/_2000w.webp" alt="" style="width: 420px; height: 210px;" width="2000"'
        f' height="1000" srcset="{folder}/_480w.webp 480w, {folder}/_960w.webp 960w,'
        f' {folder}/_1600w.webp 1600w, {folder}/_2000w.webp 2000w" sizes="(min-width: 468px) 420px,'
        ' calc(100vw - 3rem)" loading="lazy" decoding="async">'
    ) in html
    assert f'<img src="{broken}">' in html  # no variants: the original, as it was
    assert "<script>alert" not in html  # sanitised again
    assert gallery not in html.split("<body>")[1]  # the original is no longer linked from the page
    assert f".page-stage--photo{{background-image:url('{folder}/_2000w.webp')}}" in html
    assert (cfg.public_html / gallery.lstrip("/")).read_bytes() == PHOTO  # still there for old links

    home = (cfg.public_html / "index.html").read_text()
    assert f'srcset="{folder}/_480w.webp 480w' in home and 'sizes="100vw"' in home
    body = home.split('class="prose-gahk"')[1]
    assert 'alt="Forsiden"' in body and "loading=" not in body.split("</div>")[0]  # first: eager

    kontakt = (cfg.public_html / "kontakt/index.html").read_text()  # the default background
    assert f"background-image:url('{image('gang.jpg', GANG)['url']}')" in kontakt  # no variants


def test_a_phone_gets_a_narrower_background(client: Client, cfg: Config) -> None:
    big = photo(3000, 2000)
    for data_bytes in (big, PHOTO, BROKEN):
        put(client, data_bytes)
    data = photo_snapshot()
    picture = image("stor.jpg", big)
    data["images"].append(picture)
    data["pages"][-1]["background"] = picture["url"]
    publish(client, data)
    folder = f"/billeder/{picture['sha256'][:16]}"
    html = (cfg.public_html / "galleri/index.html").read_text()
    assert f"background-image:url('{folder}/_2400w.webp')" in html
    assert (
        f"@media (max-width:640px){{.page-stage--photo{{background-image:url('{folder}/_1600w.webp')}}}}"
        in html
    )


def test_missing_variant_files_are_made_again(client: Client, cfg: Config, con: db.Connection) -> None:
    put(client, PHOTO)
    put(client, BROKEN)
    (cfg.public_html / "billeder" / PHOTO_SHA[:16] / "_960w.webp").unlink()
    publish(client, photo_snapshot())
    assert "_960w.webp" in variant_files(cfg, PHOTO_SHA)
    assert image_row(con, PHOTO_SHA)["variant_widths"] == [480, 960, 1600, 2000]


def test_variants_a_publish_had_no_time_for_come_with_housekeeping(
    client: Client, cfg: Config, con: db.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(client, PHOTO)
    put(client, BROKEN)
    for name in variant_files(cfg, PHOTO_SHA):  # as if stored by a release without variants
        (cfg.public_html / "billeder" / PHOTO_SHA[:16] / name).unlink()
    db.execute(con, "UPDATE images SET variant_widths = NULL, variant_attempts = 0")
    monkeypatch.setattr(api, "PUBLISH_VARIANT_BUDGET", 0.0)

    publish(client, photo_snapshot())
    page_file = cfg.public_html / "galleri/index.html"
    assert "srcset" not in page_file.read_text()  # the original for now
    assert image_row(con, PHOTO_SHA)["variant_widths"] is None

    assert client.get("/findes-ikke").status == 404  # a visitor waiting for an answer: no time for it
    assert image_row(con, PHOTO_SHA)["variant_widths"] is None
    beacon = client.post("/s", b"/galleri", headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
    assert beacon.status == 204  # the visit beacon, which nobody waits for, does a little upkeep
    assert image_row(con, PHOTO_SHA)["variant_widths"] == [480, 960, 1600, 2000]
    assert "srcset" in page_file.read_text()  # and the site was rendered again


def test_an_image_that_keeps_dying_is_given_up(client: Client, cfg: Config, con: db.Connection) -> None:
    put(client, PHOTO)
    db.execute(con, "UPDATE images SET variant_widths = NULL, variant_attempts = 3")
    assert images.ensure_variants(con, cfg, [PHOTO_SHA]) == images.VariantProgress(0, 0)
    row = image_row(con, PHOTO_SHA)
    assert row["variant_widths"] == []
    assert "given up" in row["variant_note"]


def test_variants_go_with_their_image(client: Client, cfg: Config, con: db.Connection) -> None:
    put(client, PHOTO)
    put(client, BROKEN)
    publish(client, photo_snapshot())
    folder = cfg.public_html / "billeder" / PHOTO_SHA[:16]
    assert variant_files(cfg, PHOTO_SHA)

    later = snapshot(revision="b" * 64)
    publish(client, later)
    assert not folder.exists()  # taken down: the variants go with the public copy, at once
    assert images.blob_path(cfg, PHOTO_SHA).exists()  # the blob's grace hour: a publish in flight may want it
    row = image_row(con, PHOTO_SHA)
    assert (row["variant_widths"], row["variant_attempts"]) == (None, 0)  # nothing links them any more

    publish(client, photo_snapshot(revision="c" * 64))  # back again within the hour: made again
    assert variant_files(cfg, PHOTO_SHA) == ["_1600w.webp", "_2000w.webp", "_480w.webp", "_960w.webp"]
    assert "srcset" in (cfg.public_html / "galleri/index.html").read_text()

    db.execute(con, "UPDATE images SET stored_at = now() - interval '2 hours'")
    publish(client, snapshot(revision="d" * 64))
    assert not folder.exists()
    assert not images.blob_path(cfg, PHOTO_SHA).exists()


def test_variants_of_an_image_collected_meanwhile_are_removed(
    cfg: Config, con: db.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    images.store(con, cfg, PHOTO_SHA, PHOTO, "image/jpeg")
    make = variants.make

    def collected_while_making(data: bytes) -> variants.Variants:
        made = make(data)
        db.execute(con, "DELETE FROM images WHERE sha256 = :sha", sha=PHOTO_SHA)  # a publish's GC
        return made

    monkeypatch.setattr(variants, "make", collected_while_making)
    assert not images.make_variants(con, cfg, PHOTO_SHA)
    assert not (cfg.public_html / "billeder" / PHOTO_SHA[:16]).exists()  # no orphans left behind


def test_a_full_disk_does_not_count_toward_giving_up(
    cfg: Config, con: db.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    images.store(con, cfg, PHOTO_SHA, PHOTO, "image/jpeg")
    writes = 0

    def disk_full(target: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes % 2 == 0:  # the second variant of each attempt
            raise OSError(28, "No space left on device")
        write_atomically(target, data)

    write_atomically = images.write_atomically
    monkeypatch.setattr(images, "write_atomically", disk_full)
    for _attempt in range(images.MAX_VARIANT_ATTEMPTS + 1):
        assert images.ensure_variants(con, cfg, [PHOTO_SHA]) == images.VariantProgress(0, 0)
        assert variant_files(cfg, PHOTO_SHA) == []  # what was written is taken back
    row = image_row(con, PHOTO_SHA)
    assert (row["variant_widths"], row["variant_note"], row["variant_attempts"]) == (None, None, 0)

    monkeypatch.setattr(images, "write_atomically", write_atomically)
    assert images.ensure_variants(con, cfg, [PHOTO_SHA]) == images.VariantProgress(1, 0)
    assert image_row(con, PHOTO_SHA)["variant_widths"] == [480, 960, 1600, 2000]


def test_a_preview_keeps_intern_images_and_is_sanitised(client: Client) -> None:
    put(client, PHOTO)
    put(client, BROKEN)
    publish(client, photo_snapshot())
    gallery = image("galleri.jpg", PHOTO)["url"]
    item = page(
        7,
        "ny",
        "Ny",
        f'<p><img src="/media/cms/ny.jpg" alt=""><img src="{gallery}" alt=""></p>'
        '<p onclick="x()"><a href="javascript:x()">x</a></p>',
        background="/media/cms/baggrund.jpg",
    )
    result = client.api(
        "POST", "/api/preview", {"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"}
    )
    assert result.status == 200
    html = result.text
    assert '<img src="https://intern.gahk.dk/media/cms/ny.jpg" alt="">' in html  # untouched
    assert "background-image:url('https://intern.gahk.dk/media/cms/baggrund.jpg')" in html
    assert f'srcset="/billeder/{PHOTO_SHA[:16]}/_480w.webp 480w' in html  # a published picture
    assert "onclick" not in html and "javascript:" not in html


def test_variant_names_never_collide_with_image_names() -> None:
    assert images.VARIANT_NAME_RE.match("_480w.webp")
    assert not images.BILLEDER_RE.match(f"/{images.variant_rel_path(PHOTO_SHA, 480)}")
    assert Path(images.variant_rel_path(PHOTO_SHA, 480)).parent.name == PHOTO_SHA[:16]
