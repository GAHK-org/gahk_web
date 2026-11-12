import copy
from typing import Any

import pytest
from conftest import snapshot

from gahk_web.snapshot import (
    SnapshotError,
    billeder_href,
    billeder_match,
    is_reserved,
    validate,
    validate_preview,
)


def test_valid_snapshot_passes() -> None:
    data = snapshot()
    assert validate(copy.deepcopy(data)) == data


def broken(**changes: object) -> dict[str, Any]:
    data = snapshot()
    for key, value in changes.items():
        data[key] = value
    return data


@pytest.mark.parametrize(
    "path",
    [
        "admin",
        "api",
        "begivenheder",
        "billeder",
        "static",
        "s",
        "intern/x",
        "404",
        "optagelse/ansoeg",
        "optagelse/fremlej",
        "optagelse/send_fremleje",
    ],
)
def test_reserved_paths_are_refused(path: str) -> None:
    data = snapshot()
    data["pages"][1]["path"] = path
    with pytest.raises(SnapshotError, match=r"reserved|not a valid page path"):
        validate(data)


def test_optagelse_itself_is_a_normal_page() -> None:
    assert not is_reserved("optagelse")
    assert is_reserved("optagelse/ansoeg")


@pytest.mark.parametrize(
    "path", ["Faciliteter", "a/b/c", "/kontakt", "kontakt/", "a--b", "a b", "æble", "x.html", "-a"]
)
def test_bad_paths_are_refused(path: str) -> None:
    data = snapshot()
    data["pages"][1]["path"] = path
    with pytest.raises(SnapshotError):
        validate(data)


def test_only_the_front_page_has_the_empty_path() -> None:
    data = snapshot()
    data["pages"][1]["path"] = ""
    with pytest.raises(SnapshotError):
        validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "/media/cms/2026/11/x.jpg",
        "/billeder/0123/x.jpg",
        "/billeder/0123456789abcdef/../x.jpg",
        "/billeder/0123456789abcdef/X.jpg",
        "/billeder/0123456789abcdef/x.png",  # extension does not match image/jpeg
        "https://example.com/x.jpg",
    ],
)
def test_bad_image_urls_are_refused(url: str) -> None:
    data = snapshot()
    data["images"][0]["url"] = url
    with pytest.raises(SnapshotError):
        validate(data)


SHA16 = "0123456789abcdef"


@pytest.mark.parametrize(
    "name",
    [
        "tovtrækning-2019.jpg",
        "kø_og_ål.jpeg",
        "æøå.png",
        "straße.gif",  # ß has no single upper-case letter, and needs none
        "fête_d-été.jpg",
        "ελληνικά.jpg",
        "日本語.png",
        "한국어.jpg",  # Hangul in its composed (NFC) form
        "٢٠٢٤.jpg",  # Arabic-Indic digits are decimal digits
        "a" * 251 + ".jpg",
        "ø" * 125 + ".jpg",  # 254 bytes
    ],
)
def test_image_names_keep_their_letters(name: str) -> None:
    assert billeder_match(f"/billeder/{SHA16}/{name}")


@pytest.mark.parametrize(
    "name",
    [
        "Tovtrækning.jpg",  # upper case
        "tovtrÆkning.jpg",
        "ǅ.jpg",  # title case
        "tovtra\u030akning.jpg",  # NFD: "a" plus a combining ring
        "\u1112\u1161\u11ab.jpg",  # NFD Hangul: three jamo, each a letter, for one syllable
        "x\u200d.jpg",  # a zero-width joiner
        "x².jpg",
        "½.jpg",
        "a b.jpg",
        "a\tb.jpg",
        "a\x00.jpg",
        "a%20b.jpg",  # names are text, not encoded URLs
        "a/b.jpg",
        "../x.jpg",
        "a..b.jpg",
        "a--b.jpg",
        ".x.jpg",
        "_x.jpg",
        "_960w.webp",  # gahk.dk's own variants
        "x-.jpg",
        "x.jpg\n",
        "a.cgi.jpg",  # Apache maps a handler by every extension: it would run these
        "a.py.jpg",
        "a.fcgi.jpg",
        "foto.php.jpg",
        "foto.shtml.jpg",
        "foto.phtml.png",
        "café.v2.webp",  # one extension only (intern turns a "." in a stem into "-")
        "ø" * 126 + ".jpg",  # 256 bytes: more than a file name may have
    ],
)
def test_bad_image_names_are_refused(name: str) -> None:
    assert not billeder_match(f"/billeder/{SHA16}/{name}")


def test_a_snapshot_ships_images_with_danish_names() -> None:
    data = snapshot()
    sha = data["images"][2]["sha256"]
    url = f"/billeder/{sha[:16]}/køkken-på-første.jpg"
    data["images"][2]["url"] = url
    data["pages"][2]["background"] = url
    data["legacy_images"]["/static/legacy/image/upload/køkken.jpg"] = url
    validate(data)


def test_image_hrefs_are_percent_encoded_from_utf8() -> None:
    assert (
        billeder_href(f"/billeder/{SHA16}/tovtrækning-2019.jpg")
        == f"/billeder/{SHA16}/tovtr%C3%A6kning-2019.jpg"
    )
    assert billeder_href(f"/billeder/{SHA16}/a_b.c-d.jpg") == f"/billeder/{SHA16}/a_b.c-d.jpg"
    assert billeder_href(f"/billeder/{SHA16}/_960w.webp") == f"/billeder/{SHA16}/_960w.webp"
    # intern's /media/ names arrive encoded already: never encoded twice.
    assert billeder_href("/media/cms/k%C3%B8kken.jpg") == "/media/cms/k%C3%B8kken.jpg"


def test_image_url_hash_must_match_the_sha() -> None:
    data = snapshot()
    data["images"][0]["url"] = "/billeder/0000000000000000/gang.jpg"
    with pytest.raises(SnapshotError, match="does not match"):
        validate(data)


def test_backgrounds_must_be_shipped_images() -> None:
    data = snapshot()
    data["pages"][1]["background"] = "/static/legacy/image/upload/images/gang.jpg"
    with pytest.raises(SnapshotError, match="not an image this snapshot ships"):
        validate(data)
    data = snapshot()
    data["site"]["default_background"] = "/billeder/0123456789abcdef/other.jpg"
    with pytest.raises(SnapshotError, match="default_background"):
        validate(data)


def test_legacy_image_map_is_checked() -> None:
    data = snapshot()
    data["legacy_images"] = {"/public/image/x.jpg": data["images"][0]["url"]}
    with pytest.raises(SnapshotError, match="legacy_images"):
        validate(data)
    data["legacy_images"] = {"/static/legacy/image/x.jpg": "/billeder/0123456789abcdef/nope.jpg"}
    with pytest.raises(SnapshotError, match="legacy_images"):
        validate(data)
    data["legacy_images"] = {
        "/static/legacy/image/upload/images/nyhedshistorie GAH.png": data["images"][0]["url"]
    }
    validate(data)


def test_shape_drift_is_refused() -> None:
    with pytest.raises(SnapshotError, match="unknown extra"):
        validate(broken(extra=1))
    data = snapshot()
    del data["menu"]
    with pytest.raises(SnapshotError, match="missing menu"):
        validate(data)
    with pytest.raises(SnapshotError, match="format"):
        validate(broken(format=2))
    data = snapshot()
    data["pages"][1]["id"] = True
    with pytest.raises(SnapshotError):
        validate(data)
    with pytest.raises(SnapshotError, match="redirects"):
        validate(broken(redirects={"gammel": "findes-ikke"}))
    data = snapshot()
    data["menu"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(SnapshotError):
        validate(data)


def test_sizes_are_bounded() -> None:
    data = snapshot()
    data["pages"][1]["title"] = "x" * 256
    with pytest.raises(SnapshotError, match="longer than"):
        validate(data)
    data = snapshot()
    data["images"][0]["size"] = 16 * 1024 * 1024
    with pytest.raises(SnapshotError, match="size"):
        validate(data)


def test_preview_may_use_intern_media() -> None:
    item = snapshot()["pages"][1] | {"background": "/media/cms/2026/11/hal.jpg"}
    preview = validate_preview({"kind": "page", "item": item, "media_base": "https://intern.gahk.dk/"})
    assert preview["media_base"] == "https://intern.gahk.dk"
    with pytest.raises(SnapshotError):
        validate_preview({"kind": "page", "item": item, "media_base": "javascript:x"})
    with pytest.raises(SnapshotError):
        validate_preview({"kind": "other", "item": item, "media_base": "https://intern.gahk.dk"})


@pytest.mark.parametrize(
    "url",
    [
        "/media/cms/2026/11/k%C3%B8kken.jpg",  # intern sends quote(name, safe="/")
        "/media/cms/K%C3%B8kken%20og%20bad.JPG",
        "/media/cms/a~b_c-d.e.png",
        "/media/cms/%FF.jpg",  # not UTF-8: a broken picture, but nothing dangerous
    ],
)
def test_preview_media_names_may_be_percent_encoded(url: str) -> None:
    item = snapshot()["pages"][1] | {"background": url}
    preview = validate_preview({"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"})
    assert preview["item"]["background"] == url


@pytest.mark.parametrize(
    "url",
    [
        "/media/other/x.jpg",  # only intern's CMS library
        "/media/cms",
        "/media/cms/",
        "/media/cms//x.jpg",
        "/media/cms/../x.jpg",
        "/media/cms/a/./x.jpg",
        "/media/cms/%2e%2e/x.jpg",
        "/media/cms/a%2Fb.jpg",
        "/media/cms/a%5Cb.jpg",
        "/media/cms/a%0Ab.jpg",
        "/media/cms/a%7Fb.jpg",
        "/media/cms/a%2.jpg",
        "/media/cms/køkken.jpg",  # raw non-ASCII: intern encodes it
        "/media/cms/a b.jpg",
        "/media/cms/x.jpg?y=1",
        "/media/cms/x'.jpg",
    ],
)
def test_preview_media_urls_are_checked(url: str) -> None:
    item = snapshot()["pages"][1] | {"background": url}
    with pytest.raises(SnapshotError):
        validate_preview({"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"})
