"""The CGI fallback, the development server's .htaccess emulation, and their agreement with .htaccess."""

import pytest
from conftest import GANG, Client, image, publish

from gahk_web import routing
from gahk_web.config import Config
from gahk_web.devserver import make_dev_app


def test_no_snapshot_yet_is_503(client: Client) -> None:
    result = client.get("/faciliteter")
    assert result.status == 503
    assert "Siden opdateres" in result.text
    assert result.headers["Retry-After"] == "300"


def test_unknown_path_is_the_404_page(client: Client) -> None:
    publish(client)
    result = client.get("/findes-ikke")
    assert result.status == 404
    assert "Siden findes ikke" in result.text
    assert '<a href="/kontakt">Kontakt</a>' in result.text  # the rendered 404.html, with the menu


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/intern", "https://intern.gahk.dk/"),
        ("/intern/", "https://intern.gahk.dk/"),
        ("/intern/den-hurtige/", "https://intern.gahk.dk/den-hurtige/"),
        ("/nyintern/x", "https://intern.gahk.dk/x"),
        ("/admin", "https://intern.gahk.dk/admin"),
        ("/admin/roles", "https://intern.gahk.dk/admin/roles"),
        ("/django-admin/", "https://intern.gahk.dk/django-admin/"),
        ("/photo-album/1/", "https://intern.gahk.dk/photo-album/1/"),
        ("/media/cms/2026/x.jpg", "https://intern.gahk.dk/media/cms/2026/x.jpg"),
        ("/wiki/Forside", "https://intern.gahk.dk/wiki/Forside"),
        ("/health", "https://intern.gahk.dk/health"),
        ("/optagelse/listansoegninger", "https://intern.gahk.dk/optagelse/listansoegninger"),
        ("/optagelse/eksport", "https://intern.gahk.dk/optagelse/eksport"),
        ("/optagelse/showAnsoegning/12", "https://intern.gahk.dk/optagelse/showAnsoegning/12"),
        ("/optagelse/kasser/12", "https://intern.gahk.dk/optagelse/kasser/12"),
    ],
)
def test_intranet_addresses_go_to_intern_with_308(client: Client, path: str, target: str) -> None:
    result = client.get(path, query="a=1")
    assert result.status == 308
    assert result.headers["Location"] == target + "?a=1"


def test_not_intranet() -> None:
    assert routing.intern_redirect("/administration") is None
    assert routing.intern_redirect("/internat") is None
    assert routing.intern_redirect("/optagelse/") is None


def test_old_image_addresses_follow_the_legacy_map(client: Client) -> None:
    publish(client)
    gang = image("gang.jpg", GANG)["url"]
    result = client.get("/public/image/upload/images/gang.jpg")
    assert result.status == 301
    assert result.headers["Location"] == "/static/legacy/image/upload/images/gang.jpg"
    result = client.get("/static/legacy/image/upload/images/gang.jpg")
    assert result.status == 301
    assert result.headers["Location"] == gang
    assert client.get("/static/legacy/image/upload/images/ukendt.jpg").status == 404


def test_slash_variants_redirect_to_the_canonical_form(client: Client) -> None:
    publish(client)
    result = client.get("/faciliteter/", query="x=1")
    assert result.status == 301
    assert result.headers["Location"] == "/faciliteter?x=1"
    result = client.get("/begivenheder")
    assert (result.status, result.headers["Location"]) == (301, "/begivenheder/")
    result = client.get("/optagelse")
    assert (result.status, result.headers["Location"]) == (301, "/optagelse/")
    # The canonical form itself is served (Apache normally does it; the app is the safety net).
    result = client.get("/faciliteter")
    assert result.status == 200
    assert "Om faciliteterne" in result.text


def test_old_page_addresses_redirect(client: Client) -> None:
    publish(client)
    result = client.get("/gammel-side")
    assert (result.status, result.headers["Location"]) == (301, "/kontakt")
    result = client.get("/gammel-side/")
    assert (result.status, result.headers["Location"]) == (301, "/kontakt")
    result = client.get("/optagelse/send_rundvisning")
    assert (result.status, result.headers["Location"]) == (301, "/optagelse/ansoeg")


def test_other_methods_are_refused(client: Client) -> None:
    publish(client)
    assert client.request("PUT", "/kontakt").status == 405
    assert client.post("/kontakt").status == 405


# --- the development server emulates .htaccess ---


@pytest.fixture
def dev(client: Client, cfg: Config) -> Client:
    return Client(make_dev_app(cfg, client.app))


def test_devserver_serves_pages_in_both_slash_forms(client: Client, dev: Client) -> None:
    publish(client)
    for path in (
        "/",
        "/faciliteter",
        "/faciliteter/",
        "/begivenheder/",
        "/begivenheder",
        "/optagelse/ansoeg",
    ):
        result = dev.get(path)
        assert result.status == 200, path
        assert result.headers["Content-Type"].startswith("text/html")


def test_devserver_serves_assets_and_images(client: Client, dev: Client) -> None:
    publish(client)
    assert dev.get("/static/css/site.css").status == 200
    assert dev.get("/favicon.ico").status == 200
    assert dev.get("/static/icons/icon-192x192.png").status == 200
    gang = image("gang.jpg", GANG)["url"]
    result = dev.get(gang)
    assert (result.status, result.body) == (200, GANG)
    assert dev.get("/robots.txt").text.startswith("User-agent: *")
    assert dev.get("/sw.js").headers["Content-Type"].startswith(("application/javascript", "text/javascript"))


def test_devserver_matches_production_routing(client: Client, dev: Client) -> None:
    publish(client)
    assert dev.get("/intern/x").headers["Location"] == "https://intern.gahk.dk/x"
    assert dev.get("/public/image/a.jpg").headers["Location"] == "/static/legacy/image/a.jpg"
    assert dev.get("/gammel-side").headers["Location"] == "/kontakt"
    assert dev.get("/findes-ikke").status == 404
    assert dev.get("/.htaccess").status == 403
    assert dev.get("/static/../../etc/passwd").status == 403
    # POSTs never hit a file, even where one exists.
    assert dev.post("/optagelse/ansoeg").status == 403  # no Origin: refused by the app, not served as a file
