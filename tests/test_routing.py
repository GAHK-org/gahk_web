"""The CGI fallback, the development server's .htaccess emulation, and their agreement with .htaccess."""

import re
from pathlib import Path

import pytest
from conftest import GANG, KOKKEN, Client, image, publish, rename_image, snapshot
from sqlalchemy.orm import Session

from gahk_web import routing
from gahk_web.config import Config
from gahk_web.devserver import make_dev_app

HTACCESS = Path(__file__).resolve().parents[1] / "public_html" / ".htaccess"


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
        # The prefix stays: intern.gahk.dk sends /intern/x on to /x itself (or, running the old
        # code, serves it).
        ("/intern", "https://intern.gahk.dk/intern"),
        ("/intern/", "https://intern.gahk.dk/intern/"),
        ("/intern/den-hurtige/", "https://intern.gahk.dk/intern/den-hurtige/"),
        ("/intern/admin/login", "https://intern.gahk.dk/intern/admin/login"),
        ("/nyintern", "https://intern.gahk.dk/nyintern"),
        ("/nyintern/x", "https://intern.gahk.dk/nyintern/x"),
        ("/admin", "https://intern.gahk.dk/admin"),
        ("/admin/roles", "https://intern.gahk.dk/admin/roles"),
        ("/django-admin/", "https://intern.gahk.dk/django-admin/"),
        ("/photo-album/1/", "https://intern.gahk.dk/photo-album/1/"),
        ("/media/cms/2026/x.jpg", "https://intern.gahk.dk/media/cms/2026/x.jpg"),
        ("/wiki/Forside", "https://intern.gahk.dk/wiki/Forside"),
        ("/wiki/index.php", "https://intern.gahk.dk/wiki/index.php"),
        ("/health", "https://intern.gahk.dk/health"),
        ("/healthz", "https://intern.gahk.dk/healthz"),
        ("/healthz/", "https://intern.gahk.dk/healthz"),
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


# The old wiki's own addresses: $wgArticlePath is /wiki/index.php?title=$1, and its pages load
# scripts and styles from /wiki/load.php. They end in .php, yet must be redirected, not refused.
WIKI_ADDRESSES = (
    ("/wiki/index.php", "title=Forside"),
    ("/wiki/index.php", "title=Speciel:Log_ind&returnto=Forside"),
    ("/wiki/index.php", ""),
    ("/wiki/load.php", "modules=startup&only=scripts&raw=1&skin=vector"),
    ("/wiki/api.php", "action=query&format=json"),
)


@pytest.mark.parametrize(("path", "query"), WIKI_ADDRESSES)
def test_the_wikis_php_addresses_go_to_intern(client: Client, dev: Client, path: str, query: str) -> None:
    target = "https://intern.gahk.dk" + path + (f"?{query}" if query else "")
    for answer in (dev.get(path, query=query), client.get(path, query=query)):
        assert (answer.status, answer.headers["Location"]) == (308, target)
    login = dev.post(path, query=query)  # the wiki's login form posts to index.php
    assert (login.status, login.headers["Location"]) == (308, target)


def test_htaccess_redirects_the_intranet_before_it_refuses_scripts() -> None:
    lines = rule_lines(HTACCESS.read_text())
    scripts = next(i for i, line in enumerate(lines) if line.endswith("- [F,L,NC]"))
    intern = [i for i, line in enumerate(lines) if "https://intern.gahk.dk/" in line and "[R=308,L]" in line]
    assert intern and max(intern) < scripts


def test_old_intranet_posts_keep_their_method_and_prefix(client: Client, dev: Client) -> None:
    moved = dev.post("/intern/den-hurtige/send", query="x=1")
    assert (moved.status, moved.headers["Location"]) == (
        308,
        "https://intern.gahk.dk/intern/den-hurtige/send?x=1",
    )
    moved = client.post("/nyintern/login")
    assert (moved.status, moved.headers["Location"]) == (308, "https://intern.gahk.dk/nyintern/login")


def test_not_intranet() -> None:
    assert routing.intern_redirect("/administration") is None
    assert routing.intern_redirect("/internat") is None
    assert routing.intern_redirect("/nyinternet") is None
    assert routing.intern_redirect("/healthy") is None
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


def test_old_image_addresses_with_the_old_sites_static_hash_follow_the_map(client: Client) -> None:
    """The old site also served its legacy images under ManifestStaticFilesStorage's hashed names."""
    publish(client)
    gang = image("gang.jpg", GANG)["url"]
    result = client.get("/static/legacy/image/upload/images/gang.b8dc291a4e43.jpg")
    assert (result.status, result.headers["Location"]) == (301, gang)
    result = client.get("/public/image/upload/images/gang.b8dc291a4e43.jpg")
    assert result.headers["Location"] == "/static/legacy/image/upload/images/gang.b8dc291a4e43.jpg"
    # Only a 12-hex-digit segment right before the extension is a hash.
    assert client.get("/static/legacy/image/upload/images/gang.b8dc291a4e4.jpg").status == 404
    assert client.get("/static/legacy/image/upload/images/gang.B8DC291A4E43.jpg").status == 404
    assert client.get("/static/legacy/image/upload/images/ukendt.b8dc291a4e43.jpg").status == 404


def test_old_image_addresses_with_other_letters_follow_the_map(client: Client) -> None:
    """Old addresses may have their letters spelled out or percent-encoded in the map; a browser
    always asks percent-encoded (NFC, or NFD from a Mac). The Location is percent-encoded too."""
    data = rename_image(snapshot(), "kokken.jpg", "køkken-på-første.jpg")
    kokken = image("køkken-på-første.jpg", KOKKEN)["url"]
    href = f"/billeder/{kokken.split('/')[2]}/k%C3%B8kken-p%C3%A5-f%C3%B8rste.jpg"
    data["legacy_images"] |= {
        "/static/legacy/image/upload/køkken.jpg": kokken,
        "/static/legacy/image/upload/s%C3%B8en.jpg": kokken,
        "/static/legacy/image/upload/tre_æbler.2019.jpg": kokken,
    }
    publish(client, data)
    for path in (
        "/static/legacy/image/upload/k%C3%B8kken.jpg",
        "/static/legacy/image/upload/s%C3%B8en.jpg",
        "/static/legacy/image/upload/tre_%C3%A6bler.2019.jpg",
        "/static/legacy/image/upload/k%C3%B8kken.b8dc291a4e43.jpg",  # the old site's hashed name
    ):
        result = client.get(path)
        assert (result.status, result.headers["Location"]) == (301, href), path
    # NFD (a Mac's "å": "a" plus a combining ring) finds the NFC key, and the other way round.
    data["legacy_images"] |= {
        "/static/legacy/image/upload/på.jpg": kokken,
        "/static/legacy/image/upload/ha%CC%8Ab.jpg": kokken,
    }
    publish(client, data | {"revision": "c" * 64})
    for path in ("/static/legacy/image/upload/pa%CC%8A.jpg", "/static/legacy/image/upload/h%C3%A5b.jpg"):
        result = client.get(path)
        assert (result.status, result.headers["Location"]) == (301, href), path
    assert client.get("/static/legacy/image/upload/kokken.jpg").status == 404
    result = client.get("/public/image/upload/k%C3%B8kken.jpg")
    assert (result.status, result.headers["Location"]) == (301, "/static/legacy/image/upload/k%C3%B8kken.jpg")


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


def test_htaccess_names_the_same_rules() -> None:
    text = HTACCESS.read_text()
    assert "DirectorySlash Off" in text
    assert "RewriteCond %{REQUEST_FILENAME}/index.html -f" in text
    assert "RewriteRule ^(.*)$ cgi-bin/gahk.fcgi/$1 [L,QSA]" in text
    assert "RewriteRule ^public/image/(.+)$ /static/legacy/image/$1 [R=301,L]" in text
    rules = re.findall(
        r"^\s*RewriteRule \^(\S+) https://intern\.gahk\.dk/\S* \[R=308,L\]$", text, re.MULTILINE
    )
    assert len(rules) == 5
    for prefix in (
        "intern|nyintern|admin|django-admin|wiki",
        "photo-album|media",
        "health",
        "listansoegninger|eksport",
        "showAnsoegning|setasreceived|kasser",
    ):
        assert any(prefix in rule for rule in rules), prefix
    assert "max-age=31536000, immutable" in text


def rule_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith(("Rewrite", "# Rewrite"))]


def test_htaccess_runs_the_stubs_only_through_its_own_rewrites() -> None:
    lines = rule_lines(HTACCESS.read_text())
    refuse = lines.index("RewriteRule ^cgi-bin(?:/|$) - [F,L]")
    assert lines[refuse - 1] == "RewriteCond %{ENV:REDIRECT_STATUS} ^$"  # only the browser's request
    assert lines[refuse + 1] == r"RewriteRule ^cgi-bin/gahk\.f?cgi(?:/|$) - [L]"  # the second round
    assert lines[refuse + 2] == "RewriteRule ^cgi-bin(?:/|$) - [F,L]"  # anything else in cgi-bin
    pattern = r"(?:^|/)[^/]*\.(?:py|cgi|fcgi|php\d?|phtml|phar|pl|shtml)(?:[./]|$)"
    scripts = lines.index(f"RewriteRule {pattern} - [F,L,NC]")
    assert refuse < scripts < lines.index("RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$")
    assert routing._SCRIPT_RE.pattern == pattern
    # The stubs themselves may run; every other Python file is denied outright.
    files_match = re.search(r'<FilesMatch "([^"]+)">', HTACCESS.read_text())
    assert files_match and "py|" in files_match.group(1) and "cgi" not in files_match.group(1)


def test_htaccess_sends_updates_and_large_bodies_to_cgi_and_the_rest_to_fastcgi() -> None:
    lines = rule_lines(HTACCESS.read_text())
    tail = lines[lines.index("RewriteRule ^api/update$ cgi-bin/gahk.cgi/api/update [L,QSA]") :]
    assert tail == [
        "RewriteRule ^api/update$ cgi-bin/gahk.cgi/api/update [L,QSA]",
        "RewriteRule ^(api/(?:publish|rebuild))$ cgi-bin/gahk.cgi/$1 [L,QSA]",
        "RewriteCond %{HTTP:Content-Length} ^[0-9]{6,}$",
        "RewriteRule ^(.*)$ cgi-bin/gahk.cgi/$1 [L,QSA]",
        "RewriteCond %{ENV:GAHK_CGI_ONLY} =1",
        "RewriteRule ^(.*)$ cgi-bin/gahk.cgi/$1 [L,QSA]",
        "RewriteRule ^(.*)$ cgi-bin/gahk.fcgi/$1 [L,QSA]",
    ]
    assert routing._LARGE_BODY_RE.pattern == "^[0-9]{6,}$"
    assert routing.UPDATE_PATH == "/api/update"
    assert routing.LONG_PATHS == {"/api/publish", "/api/rebuild"}


def test_the_cgi_only_switch_is_in_the_local_block_and_off() -> None:
    text = HTACCESS.read_text()
    block = text[text.index("# BEGIN LOCAL") : text.index("# END LOCAL")]
    assert "\n# RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n" in block  # commented out: FastCGI by default
    # It comes before every other rule, so later RewriteConds see the variable in the same round.
    assert text.index("# RewriteRule ^ - [E=GAHK_CGI_ONLY:1]") < text.index("RewriteEngine On")
    from gahk_web import updater

    assert not updater.cgi_only(text.encode())
    assert updater.cgi_only(text.replace("# RewriteRule ^ - [E=", "RewriteRule ^ - [E=").encode())


@pytest.mark.parametrize(
    ("path", "content_length", "cgi_only", "stub"),
    [
        ("/api/update", "2", False, "gahk.cgi"),
        ("/api/update/", "2", False, "gahk.fcgi"),
        ("/api/publish", "99999", False, "gahk.cgi"),  # may outlast mod_fcgid's FcgidIOTimeout
        ("/api/publish", "100000", False, "gahk.cgi"),
        ("/api/rebuild", "2", False, "gahk.cgi"),
        ("/api/rebuild/", "2", False, "gahk.fcgi"),
        ("/api/preview", "99999", False, "gahk.fcgi"),
        ("/api/status", "", False, "gahk.fcgi"),
        ("/api/images/" + "a" * 64, "2400000", False, "gahk.cgi"),
        ("/s", "", False, "gahk.fcgi"),
        ("/optagelse/ansoeg", "512", False, "gahk.fcgi"),
        ("/findes-ikke", "", True, "gahk.cgi"),
    ],
)
def test_which_stub_answers(path: str, content_length: str, cgi_only: bool, stub: str) -> None:
    assert routing.stub_for(path, content_length, cgi_only=cgi_only) == stub


@pytest.mark.parametrize(
    "path",
    [
        "/x.py",
        "/fcgitest/test.fcgi",
        "/fcgitest/test.fcgi/extra",
        "/install.cgi",
        "/A.FCGI",
        "/billeder/0123456789abcdef/a.cgi.jpg",
        "/x.Py",
        "/billeder/0123456789abcdef/foto.php.jpg",
        "/billeder/0123456789abcdef/foto.PHP5.jpg",
        "/billeder/0123456789abcdef/foto.shtml.jpg",
        "/x.phtml",
        "/x.phar",
        "/cgi.pl",
    ],
)
def test_scripts_are_refused(path: str) -> None:
    assert routing.is_script(path)


@pytest.mark.parametrize(
    "path",
    ["/faciliteter", "/static/css/site.1a2b3c4d.css", "/pycon", "/a.pyc", "/a.plan", "/php", "/a.phpx"],
)
def test_other_paths_are_not_scripts(path: str) -> None:
    assert not routing.is_script(path)


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


def test_fonts_may_be_fetched_from_any_origin(client: Client, dev: Client) -> None:
    """intern's preview shows this site's HTML on intern.gahk.dk, and browsers fetch fonts with CORS:
    without the header the preview fell back to other fonts (found in the full-system e2e)."""
    text = HTACCESS.read_text()
    block = re.search(
        r'<If "%\{REQUEST_FILENAME\} =~ m#(\S+)#">\s*Header set Access-Control-Allow-Origin "\*"', text
    )
    assert block is not None and block.group(1) == routing.FONT_RE.pattern.lstrip("^")
    assert text.count("Access-Control-Allow-Origin") == 1  # fonts only
    for font in ("/static/fonts/ubuntu-400.woff2", "/static/fonts/julius-sans-one-400.woff2"):
        assert routing.is_font(font)
        result = dev.get(font)
        assert result.status == 200, font
        assert result.headers["Access-Control-Allow-Origin"] == "*"
    for other in (
        "/static/css/site.css",
        "/static/icons/icon-192x192.png",
        "/static/fonts/",
        "/fonts/x.woff2",
    ):
        assert not routing.is_font(other)
    assert "Access-Control-Allow-Origin" not in dev.get("/static/css/site.css").headers


def test_devserver_serves_images_with_other_letters(client: Client, dev: Client) -> None:
    publish(client, rename_image(snapshot(), "kokken.jpg", "tovtrækning-2019.jpg"))
    folder = image("kokken.jpg", KOKKEN)["url"].rsplit("/", 1)[0]
    result = dev.get(f"{folder}/tovtr%C3%A6kning-2019.jpg")
    assert (result.status, result.body) == (200, KOKKEN)
    assert dev.get(f"{folder}/tovtra%CC%8Akning-2019.jpg").status == 404  # NFD is another name
    page = dev.get("/faciliteter/kokken").text
    assert f"{folder}/tovtr%C3%A6kning-2019.jpg" in page


def test_devserver_matches_production_routing(client: Client, dev: Client) -> None:
    publish(client)
    assert dev.get("/intern/x").headers["Location"] == "https://intern.gahk.dk/intern/x"
    assert dev.get("/public/image/a.jpg").headers["Location"] == "/static/legacy/image/a.jpg"
    assert dev.get("/gammel-side").headers["Location"] == "/kontakt"
    assert dev.get("/findes-ikke").status == 404
    assert dev.get("/.htaccess").status == 403
    assert dev.get("/fcgitest/test.fcgi").status == 403  # a test left by hand is never run
    assert dev.get("/install.cgi").status == 403
    assert dev.get("/install.py").status == 403
    assert dev.get("/cgi-bin/gahk.fcgi/api/health").status == 403  # the stubs only through rewrites
    assert dev.get("/cgi-bin/gahk.cgi/api/update").status == 403
    assert dev.get("/static/../../etc/passwd").status == 403
    # POSTs never hit a file, even where one exists.
    assert dev.post("/optagelse/ansoeg").status == 403  # no Origin: refused by the app, not served as a file


def test_devserver_answers_as_the_stub_would(cfg: Config, con: Session) -> None:
    dev = Client(make_dev_app(cfg))
    assert dev.get("/api/health").headers["X-Gahk-Runner"] == "fastcgi"
    big = dev.request("GET", "/api/health", b"x" * 100_000)
    assert big.headers["X-Gahk-Runner"] == "cgi"  # too large for mod_fcgid
    cgi_only = Client(make_dev_app(cfg, cgi_only=True))
    assert cgi_only.get("/api/health").headers["X-Gahk-Runner"] == "cgi"
