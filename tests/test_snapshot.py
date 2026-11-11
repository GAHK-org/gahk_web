import copy
from typing import Any

import pytest
from conftest import snapshot

from gahk_web.snapshot import SnapshotError, is_reserved, validate, validate_preview


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
