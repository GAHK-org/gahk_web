import hashlib
import json
import os
import unicodedata
from pathlib import Path

from conftest import (
    GANG,
    IMAGE_BYTES,
    KOKKEN,
    Client,
    execute,
    image,
    jpeg,
    page,
    publish,
    rename_image,
    snapshot,
    upload_images,
)
from sqlalchemy.orm import Session

from gahk_web.config import Config


def test_publish_reports_missing_images_until_they_are_uploaded(client: Client) -> None:
    data = snapshot()
    missing = client.api("POST", "/api/images/missing", {"sha256": sorted(IMAGE_BYTES)})
    assert missing.status == 200
    assert sorted(missing.json()["missing"]) == sorted(IMAGE_BYTES)

    result = client.api("POST", "/api/publish", data)
    assert result.status == 409
    assert result.json()["error"] == "missing images"
    assert sorted(result.json()["missing"]) == sorted(IMAGE_BYTES)

    upload_images(client)
    assert client.api("POST", "/api/images/missing", {"sha256": sorted(IMAGE_BYTES)}).json() == {
        "missing": []
    }
    result = client.api("POST", "/api/publish", data)
    assert result.status == 200
    assert result.json()["revision"] == "a" * 64
    assert result.json()["pages_written"] > 0


def test_image_put_checks_hash_type_and_bytes(client: Client) -> None:
    sha = hashlib.sha256(GANG).hexdigest()
    other = hashlib.sha256(b"other").hexdigest()
    assert (
        client.api("PUT", f"/api/images/{other}", raw=GANG, headers={"Content-Type": "image/jpeg"}).status
        == 400
    )
    assert (
        client.api("PUT", f"/api/images/{sha}", raw=GANG, headers={"Content-Type": "image/svg+xml"}).status
        == 400
    )
    assert (
        client.api("PUT", f"/api/images/{sha}", raw=GANG, headers={"Content-Type": "image/png"}).status == 400
    )
    assert (
        client.api("PUT", f"/api/images/{sha[:63]}", raw=GANG, headers={"Content-Type": "image/jpeg"}).status
        == 400
    )
    assert (
        client.api("PUT", f"/api/images/{sha}", raw=GANG, headers={"Content-Type": "image/jpeg"}).status
        == 204
    )


def test_invalid_snapshot_is_refused_and_changes_nothing(client: Client, cfg: Config) -> None:
    publish(client)
    before = (cfg.public_html / "kontakt/index.html").read_text()
    data = snapshot()
    data["pages"][3]["path"] = "api"
    result = client.api("POST", "/api/publish", data)
    assert result.status == 400
    assert "reserved" in result.json()["error"]
    assert client.api("POST", "/api/publish", raw=b"not json").status == 400
    assert (cfg.public_html / "kontakt/index.html").read_text() == before


def test_publish_renders_every_file(client: Client, cfg: Config) -> None:
    publish(client)
    root = cfg.public_html
    for rel in (
        "index.html",
        "faciliteter/index.html",
        "faciliteter/kokken/index.html",
        "kontakt/index.html",
        "optagelse/index.html",
        "begivenheder/index.html",
        "optagelse/ansoeg/index.html",
        "optagelse/fremlej/index.html",
        "optagelse/ansoeg/success/index.html",
        "404.html",
        "503.html",
        "sitemap.xml",
        "robots.txt",
        "sw.js",
    ):
        assert (root / rel).is_file(), rel
    kokken = image("kokken.jpg", KOKKEN)["url"]
    assert (root / kokken.lstrip("/")).read_bytes() == KOKKEN
    assert not list(root.rglob(".tmp-*"))


def test_titles_are_escaped_and_bodies_are_not(client: Client, cfg: Config) -> None:
    publish(client)
    kontakt = (cfg.public_html / "kontakt/index.html").read_text()
    assert "<title>Kontakt &lt;os&gt; - G. A. Hagemanns Kollegium</title>" in kontakt
    assert "Kontakt <os>" not in kontakt
    assert "<p>Skriv til os</p>" in kontakt
    assert '<meta name="description" content="Sådan kontakter du os">' in kontakt
    assert '<link rel="canonical" href="https://gahk.dk/kontakt">' in kontakt
    home = (cfg.public_html / "index.html").read_text()
    assert "Ny &lt;nyhed&gt;" in home
    assert '<meta name="description" content="Traditionsrigt kollegium på Østerbro, København">' in home


def test_menu_and_canonical_follow_the_snapshot(client: Client, cfg: Config) -> None:
    publish(client)
    page_html = (cfg.public_html / "faciliteter/index.html").read_text()
    assert '<a href="/kontakt">Kontakt</a>' in page_html
    assert '<a href="/faciliteter" aria-current="page">Faciliteter</a>' in page_html
    optagelse = (cfg.public_html / "optagelse/index.html").read_text()
    assert '<link rel="canonical" href="https://gahk.dk/optagelse/">' in optagelse
    sitemap = (cfg.public_html / "sitemap.xml").read_text()
    assert "<loc>https://gahk.dk/faciliteter/kokken</loc>" in sitemap
    assert "<loc>https://gahk.dk/begivenheder/</loc>" in sitemap


def test_section_sidebar_only_for_sections_with_two_pages(client: Client, cfg: Config) -> None:
    publish(client)
    kokken = (cfg.public_html / "faciliteter/kokken/index.html").read_text()
    assert 'class="section-nav"' in kokken
    assert '<a href="/faciliteter">Faciliteter</a>' in kokken
    assert '<a href="/faciliteter/kokken" class="is-current" aria-current="page">Køkkenet</a>' in kokken
    assert (
        "section-nav"
        not in (cfg.public_html / "kontakt/index.html").read_text().split("<main")[0].split("</header>")[1]
    )


def test_backgrounds_default_and_plain(client: Client, cfg: Config) -> None:
    data = snapshot()
    publish(client, data)
    gang = image("gang.jpg", GANG)["url"]
    kontakt = (cfg.public_html / "kontakt/index.html").read_text()
    assert f"background-image:url('{gang}')" in kontakt
    home = (cfg.public_html / "index.html").read_text()
    assert image("hero.jpg", jpeg("hero"))["url"] in home
    data["site"]["default_background"] = ""
    data["revision"] = "b" * 64
    publish(client, data)
    kontakt = (cfg.public_html / "kontakt/index.html").read_text()
    assert "page-stage--plain" in kontakt
    assert "hero--plain" in (cfg.public_html / "begivenheder/index.html").read_text()


def test_home_lists_only_upcoming_events(client: Client, cfg: Config) -> None:
    publish(client)
    home = (cfg.public_html / "index.html").read_text()
    assert "Julefrokost" in home
    assert "Idag-fest" in home
    assert "Sommerfest" not in home
    archive = (cfg.public_html / "begivenheder/index.html").read_text()
    assert archive.index("Kommende begivenheder") < archive.index("Julefrokost")
    assert archive.index("Tidligere begivenheder") < archive.index("Sommerfest")
    assert archive.index("Ny &lt;nyhed&gt;") < archive.index("Gammel nyhed")


def test_republish_removes_stale_pages_only(client: Client, cfg: Config) -> None:
    publish(client)
    (cfg.public_html / "static").mkdir()
    (cfg.public_html / "static/site.css").write_text("x")
    (cfg.public_html / ".htaccess").write_text("x")
    (cfg.public_html / "keep/index.html").parent.mkdir()
    (cfg.public_html / "keep/index.html").write_text("not ours")

    data = snapshot(revision="b" * 64)
    data["pages"] = [p for p in data["pages"] if p["id"] != 3]  # faciliteter/kokken goes
    data["pages"].append(page(6, "kollegielivet", "Kollegielivet"))
    publish(client, data)

    assert not (cfg.public_html / "faciliteter/kokken").exists()
    assert (cfg.public_html / "faciliteter/index.html").is_file()
    assert (cfg.public_html / "kollegielivet/index.html").is_file()
    assert (cfg.public_html / "static/site.css").is_file()
    assert (cfg.public_html / ".htaccess").is_file()
    assert (cfg.public_html / "keep/index.html").is_file()


def test_unused_images_are_collected_after_publish(client: Client, cfg: Config, con: Session) -> None:
    publish(client)
    kokken = image("kokken.jpg", KOKKEN)
    data = snapshot(revision="c" * 64)
    data["pages"][2]["background"] = ""
    data["pages"][2]["body"] = "<p>Ingen billeder</p>"
    data["images"] = [img for img in data["images"] if img["sha256"] != kokken["sha256"]]
    publish(client, data)
    assert not (cfg.public_html / kokken["url"].lstrip("/")).exists()
    assert not (cfg.public_html / kokken["url"].lstrip("/")).parent.exists()
    # The blob stays for an hour (a publish in flight may still need it).
    assert (cfg.state_dir / "images" / kokken["sha256"]).is_file()
    execute(con, "UPDATE images SET stored_at = now() - interval '2 hours'")
    again = snapshot(revision="d" * 64) | {"images": data["images"], "pages": data["pages"]}
    assert client.api("POST", "/api/publish", again).status == 200
    assert not (cfg.state_dir / "images" / kokken["sha256"]).exists()


def test_image_files_keep_their_letters(client: Client, cfg: Config) -> None:
    """A name with Danish letters is written as it is, in UTF-8 and NFC: the bytes Apache looks for
    when a browser asks for the percent-encoded address."""
    name = "køkken-på-første_sal-v2.jpg"
    assert unicodedata.is_normalized("NFC", name)
    publish(client, rename_image(snapshot(), "kokken.jpg", name))
    folder = os.fsencode(cfg.public_html / "images" / image(name, KOKKEN)["sha256"][:16])
    assert os.listdir(folder) == [name.encode("utf-8")]  # noqa: PTH208 - the name as bytes on disk
    assert (cfg.public_html / image(name, KOKKEN)["url"].lstrip("/")).read_bytes() == KOKKEN
    # The next publish names it as before: the copy under the old name goes.
    publish(client, snapshot(revision="c" * 64))
    assert os.listdir(folder) == [b"kokken.jpg"]  # noqa: PTH208 - the name as bytes on disk


def test_status_reports_revision_and_outbox(client: Client, cfg: Config) -> None:
    result = client.api("GET", "/api/status").json()
    assert result["revision"] is None
    assert result["outbox"] == {"applications": 0, "visit_days": 0}
    assert result["update"]["last_result"] is None
    publish(client)
    cfg.state_dir.mkdir(exist_ok=True)
    (cfg.state_dir / "update.json").write_text(json.dumps({"last_result": "ok", "available_version": "abc"}))
    result = client.api("GET", "/api/status").json()
    assert result["revision"] == "a" * 64
    assert result["version"] == "dev"
    assert result["rendered_at"]
    assert result["update"]["available_version"] == "abc"
    assert client.get("/api/health").json()["revision"] == "a" * 64


def test_rebuild_rerenders_from_the_cache(client: Client, cfg: Config) -> None:
    assert client.api("POST", "/api/rebuild", {}).status == 409
    publish(client)
    (cfg.public_html / "kontakt/index.html").unlink()
    result = client.api("POST", "/api/rebuild", {})
    assert result.status == 200
    assert result.json()["pages_written"] == 1
    assert (cfg.public_html / "kontakt/index.html").is_file()


def test_update_in_a_checkout_is_refused(client: Client) -> None:
    result = client.api("POST", "/api/update", {})
    assert result.status == 409
    assert result.json()["version"] == "dev"


def test_unknown_api_paths(client: Client) -> None:
    assert client.api("GET", "/api/nope").status == 404
    assert client.api("GET", "/api/publish").status == 405


def test_render_writes_only_changed_files(client: Client, cfg: Config) -> None:
    publish(client)
    target: Path = cfg.public_html / "kontakt/index.html"
    mtime = target.stat().st_mtime_ns
    result = client.api("POST", "/api/rebuild", {})
    assert result.json()["pages_written"] == 0
    assert target.stat().st_mtime_ns == mtime


def test_edit_links_follow_the_protocol(client: Client, cfg: Config) -> None:
    """docs/protocol.md "The edit link": the cookie name and intern's edit addresses."""
    publish(client)
    pages = {
        "index.html": "https://intern.gahk.dk/hjemmeside/sider/1/",
        "faciliteter/kokken/index.html": "https://intern.gahk.dk/hjemmeside/sider/3/",
        "begivenheder/index.html": "https://intern.gahk.dk/hjemmeside/",
    }
    for name, edit_url in pages.items():
        html = (cfg.public_html / name).read_text()
        assert f'<a id="rediger" class="editor-link" href="{edit_url}" hidden>Rediger siden</a>' in html
        assert "gahk_editor=1" in html
