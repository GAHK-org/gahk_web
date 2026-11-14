"""The CGI fallback, the development server's .htaccess emulation, and their agreement with .htaccess."""

import importlib.util
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from conftest import HALLWAY, KITCHEN, Client, image, publish, rename_image, snapshot
from sqlalchemy.orm import Session

from gahk_web import routing
from gahk_web.config import Config
from gahk_web.devserver import make_dev_app
from gahk_web.render import STATIC_DIR

ROOT = Path(__file__).resolve().parents[1]
HTACCESS = ROOT / "public_html" / ".htaccess"


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
    methods = ["POST", "PUT", "DELETE"]  # every method; a GET or HEAD of /intern… gets the old app's page
    if routing.old_app_page("GET", path) is None:
        methods += ["GET", "HEAD"]
    for method in methods:
        result = client.request(method, path, query="a=1")
        assert (result.status, result.headers["Location"]) == (308, target + "?a=1"), method


# --- the old intranet's installed app (Den Hurtige, scope /intern/): a page, not a redirect ---

OLD_APP_PATHS = ("/intern", "/intern/", "/intern/den-hurtige/", "/intern/admin/login", "/intern/x.php")


def old_app_page() -> bytes:
    return (STATIC_DIR / routing.OLD_APP_PAGE.removeprefix("/static/")).read_bytes()


@pytest.mark.parametrize("path", OLD_APP_PATHS)
def test_a_get_of_the_old_intranet_gets_the_old_apps_page(client: Client, dev: Client, path: str) -> None:
    """Before anything is published, too: the page is a file of the release."""
    for app in (dev, client):
        for query in ("", "x=1&y=%C3%A6"):
            result = app.get(path, query=query)
            assert (result.status, result.body) == (200, old_app_page()), (path, query)
            assert result.headers["Content-Type"] == "text/html; charset=utf-8"
            assert "Location" not in result.headers
        head = app.request("HEAD", path)
        assert (head.status, head.body) == (200, b"")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_other_methods_to_the_old_intranet_still_go_to_intern(
    client: Client, dev: Client, method: str
) -> None:
    for app in (dev, client):
        for path in OLD_APP_PATHS:
            result = app.request(method, path, query="a=1")
            target = f"https://intern.gahk.dk{path}?a=1"
            assert (result.status, result.headers["Location"]) == (308, target), (method, path)


def test_the_old_apps_page_is_only_for_intern() -> None:
    assert routing.old_app_page("GET", "/intern/den-hurtige/") == routing.OLD_APP_PAGE
    assert routing.old_app_page("HEAD", "/intern") == routing.OLD_APP_PAGE
    assert routing.old_app_page("POST", "/intern/den-hurtige/") is None
    for path in ("/nyintern", "/nyintern/x", "/internat", "/intern.php", "/admin", "/wiki/", "/x/intern/"):
        assert routing.old_app_page("GET", path) is None, path


def test_the_old_apps_page() -> None:
    """Inside an installed app it asks for the new one; in a browser it sends the visitor on to the
    same address on intern.gahk.dk, as the 308 would (the prefix kept)."""
    page = old_app_page().decode("utf-8")
    assert '<meta name="robots" content="noindex">' in page
    assert "navigator.standalone === true" in page
    assert '"(display-mode: " + mode + ")"' in page
    assert (
        'var target = "https://intern.gahk.dk" + location.pathname + location.search + location.hash;' in page
    )
    assert "location.replace(target);" in page
    assert '<a class="btn" href="https://intern.gahk.dk/install">' in page
    assert "GAHK Intern" in page
    assert "<noscript>" in page and 'href="https://intern.gahk.dk/"' in page
    # Nothing from elsewhere, and only the release's unhashed files (the stylesheet's name is hashed).
    assert "<script src" not in page and "<link" not in page
    for asset in re.findall(r'(?:src|url\()="?(/static/[^")]+)', page):
        assert (STATIC_DIR / asset.removeprefix("/static/")).is_file(), asset
        assert not re.search(r"\.[0-9a-f]{8,}\.", asset), asset


def test_the_old_apps_page_is_in_the_bundle(tmp_path: Path) -> None:
    """build_bundle copies the package's static/ into public_html/static/ (installed by the updater);
    the page keeps its name, so .htaccess can name it."""
    spec = importlib.util.spec_from_file_location("build_bundle", ROOT / "scripts" / "build_bundle.py")
    assert spec and spec.loader
    build_bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_bundle)
    manifest = build_bundle.build_static(tmp_path)
    installed = tmp_path / "public_html" / routing.OLD_APP_PAGE.lstrip("/")
    assert installed.read_bytes() == old_app_page()
    assert "intern-moved.html" not in manifest  # not renamed with a hash


def test_htaccess_serves_the_old_apps_page_for_a_get_of_intern() -> None:
    lines = rule_lines(HTACCESS.read_text())
    rule = f"RewriteRule ^{routing._OLD_APP_RE.pattern.removeprefix('^/')} {routing.OLD_APP_PAGE.lstrip('/')} [L]"
    index = lines.index(rule)
    assert lines[index - 1] == "RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$"
    redirects = [
        i for i, line in enumerate(lines) if "https://intern.gahk.dk/" in line and "[R=308,L]" in line
    ]
    scripts = next(i for i, line in enumerate(lines) if line.endswith("- [F,L,NC]"))
    assert index < min(redirects) and index < scripts
    # Its second round finds the file: nothing before the files' own rule refuses or redirects it.
    path = routing.OLD_APP_PAGE
    assert not routing.is_hidden(path) and not routing.is_stub(path) and not routing.is_script(path)
    assert routing.intern_redirect(path) is None and routing.old_asset_redirect(path) is None


# --- the old wiki: gahk.dk/wiki/ moved to wiki.gahk.dk, where MediaWiki runs at the root ---

WIKI = "https://wiki.gahk.dk"
# (path, query, target without the query)
WIKI_ADDRESSES = (
    # Its front page.
    ("/wiki", "", f"{WIKI}/"),
    ("/wiki/", "", f"{WIKI}/"),
    ("/wiki/", "a=1", f"{WIKI}/"),
    # The root's scripts and folders lose the /wiki prefix. They end in .php, yet are redirected,
    # not refused: the old wiki's pages were /wiki/index.php?title=…, and they load /wiki/load.php.
    ("/wiki/index.php", "", f"{WIKI}/index.php"),
    ("/wiki/index.php", "title=Forside", f"{WIKI}/index.php"),
    ("/wiki/index.php", "title=Speciel:Log_ind&returnto=Forside", f"{WIKI}/index.php"),
    ("/wiki/index.php", "title=K%C3%A6lderen&action=edit", f"{WIKI}/index.php"),
    ("/wiki/index.php/Forside", "", f"{WIKI}/index.php/Forside"),
    ("/wiki/index.php/K%C3%A6lderen", "action=history", f"{WIKI}/index.php/K%C3%A6lderen"),
    ("/wiki/load.php", "modules=startup&only=scripts&raw=1&skin=vector", f"{WIKI}/load.php"),
    ("/wiki/api.php", "action=query&format=json", f"{WIKI}/api.php"),
    ("/wiki/rest.php/v1/page/Forside", "", f"{WIKI}/rest.php/v1/page/Forside"),
    ("/wiki/opensearch_desc.php", "", f"{WIKI}/opensearch_desc.php"),
    ("/wiki/thumb.php", "f=Hus.jpg&width=180", f"{WIKI}/thumb.php"),
    ("/wiki/img_auth.php/a/ab/Hus.jpg", "", f"{WIKI}/img_auth.php/a/ab/Hus.jpg"),
    ("/wiki/images/a/ab/K%C3%B8kken.jpg", "", f"{WIKI}/images/a/ab/K%C3%B8kken.jpg"),
    ("/wiki/images/thumb/a/ab/Hus.jpg/180px-Hus.jpg", "", f"{WIKI}/images/thumb/a/ab/Hus.jpg/180px-Hus.jpg"),
    ("/wiki/skins/Vector/resources/x.svg", "", f"{WIKI}/skins/Vector/resources/x.svg"),
    (
        "/wiki/resources/assets/poweredby_mediawiki.svg",
        "",
        f"{WIKI}/resources/assets/poweredby_mediawiki.svg",
    ),
    ("/wiki/extensions/x/y.js", "", f"{WIKI}/extensions/x/y.js"),
    # Anything else is a page's address: /wiki/<title> there too.
    ("/wiki/Forside", "", f"{WIKI}/wiki/Forside"),
    ("/wiki/Forside", "action=edit", f"{WIKI}/wiki/Forside"),
    ("/wiki/K%C3%A6lderen", "", f"{WIKI}/wiki/K%C3%A6lderen"),
    ("/wiki/Fil:K%C3%B8kken.jpg", "", f"{WIKI}/wiki/Fil:K%C3%B8kken.jpg"),
    ("/wiki/Kategori:Huset/Underside", "", f"{WIKI}/wiki/Kategori:Huset/Underside"),
    ("/wiki/Images", "", f"{WIKI}/wiki/Images"),  # a page: the folder is lower-case
    ("/wiki/index.php5", "", f"{WIKI}/wiki/index.php5"),
    # Encoded as Apache encodes them: what would end the path stays encoded, nothing twice. (Apache
    # 2.4.60 and later refuse a "%3F" in any path a rule rewrites: 403 there, as on the whole site.)
    ("/wiki/Hvad_er_GAHK%3F", "x=1", f"{WIKI}/wiki/Hvad_er_GAHK%3F"),
    ("/wiki/100%25", "", f"{WIKI}/wiki/100%25"),
    ("/wiki/A%23b", "", f"{WIKI}/wiki/A%23b"),
    ("/wiki/A%20b", "", f"{WIKI}/wiki/A%20b"),
    ("/wiki/A&b+c=d'(e)", "", f"{WIKI}/wiki/A&b+c=d'(e)"),
)


@pytest.mark.parametrize(("path", "query", "target"), WIKI_ADDRESSES)
def test_the_old_wikis_addresses_go_to_wiki_gahk_dk(
    client: Client, dev: Client, path: str, query: str, target: str
) -> None:
    location = target + (f"?{query}" if query else "")
    for method in ("GET", "HEAD", "POST", "PUT"):  # 308 keeps the method: its login form POSTs
        for app in (dev, client):
            answer = app.request(method, path, query=query)
            assert (answer.status, answer.headers["Location"]) == (308, location), (method, path)


@pytest.mark.parametrize("path", ["/wikipedia", "/wiki.php", "/Wiki/Forside", "/x/wiki/Forside", "/wikis/"])
def test_not_the_wiki(path: str) -> None:
    assert routing.wiki_redirect(path) is None
    assert routing.intern_redirect(path) is None


def htaccess_wiki_rules() -> list[tuple[str, str]]:
    return re.findall(
        r"^\s*RewriteRule \^(wiki\S*) (https://wiki\.gahk\.dk/\S*) \[R=308,L\]$",
        HTACCESS.read_text(),
        re.MULTILINE,
    )


def test_htaccess_redirects_the_wiki_as_routing_does() -> None:
    """Apply .htaccess's wiki rules (their regexes are Python's too) to the decoded path, as
    mod_rewrite does, and compare with routing.wiki_redirect (which encodes as mod_rewrite does)."""
    rules = htaccess_wiki_rules()
    assert len(rules) == len(routing._WIKI_RULES) == 3
    for path, _query, target in WIKI_ADDRESSES:
        decoded = unquote(path)
        found = [(re.match("^" + pattern, decoded.lstrip("/")), to) for pattern, to in rules]
        match, to = next(((m, to) for m, to in found if m), (None, ""))
        assert match is not None, path
        expanded = to.replace("$1", match.group(1)) if "$1" in to else to
        assert expanded == unquote(target), path
    # The intranet's rule no longer takes the wiki.
    assert routing.intern_redirect("/wiki/Forside") is None


def test_htaccess_redirects_the_intranet_and_the_wiki_before_it_refuses_scripts() -> None:
    lines = rule_lines(HTACCESS.read_text())
    scripts = next(i for i, line in enumerate(lines) if line.endswith("- [F,L,NC]"))
    redirects = [
        i
        for i, line in enumerate(lines)
        if ("https://intern.gahk.dk/" in line or "https://wiki.gahk.dk/" in line) and "[R=308,L]" in line
    ]
    assert len(redirects) == 8 and max(redirects) < scripts


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
    hallway = image("gang.jpg", HALLWAY)["url"]
    # Both old forms go straight to /images/, never by way of another /legacy/ address.
    for path in ("/public/image/upload/images/gang.jpg", "/static/legacy/image/upload/images/gang.jpg"):
        result = client.get(path)
        assert (result.status, result.headers["Location"]) == (301, hallway), path
    assert client.get("/public/image/upload/images/ukendt.jpg").status == 404
    assert client.get("/static/legacy/image/upload/images/ukendt.jpg").status == 404


def test_old_image_addresses_with_the_old_sites_static_hash_follow_the_map(client: Client) -> None:
    """The old site also served its legacy images under ManifestStaticFilesStorage's hashed names."""
    publish(client)
    hallway = image("gang.jpg", HALLWAY)["url"]
    result = client.get("/static/legacy/image/upload/images/gang.b8dc291a4e43.jpg")
    assert (result.status, result.headers["Location"]) == (301, hallway)
    result = client.get("/public/image/upload/images/gang.b8dc291a4e43.jpg")
    assert (result.status, result.headers["Location"]) == (301, hallway)
    # Only a 12-hex-digit segment right before the extension is a hash.
    assert client.get("/static/legacy/image/upload/images/gang.b8dc291a4e4.jpg").status == 404
    assert client.get("/static/legacy/image/upload/images/gang.B8DC291A4E43.jpg").status == 404
    assert client.get("/static/legacy/image/upload/images/ukendt.b8dc291a4e43.jpg").status == 404


def test_old_image_addresses_with_other_letters_follow_the_map(client: Client) -> None:
    """Old addresses may have their letters spelled out or percent-encoded in the map; a browser
    always asks percent-encoded (NFC, or NFD from a Mac). The Location is percent-encoded too."""
    data = rename_image(snapshot(), "kokken.jpg", "køkken-på-første.jpg")
    kitchen = image("køkken-på-første.jpg", KITCHEN)["url"]
    href = f"/images/{kitchen.split('/')[2]}/k%C3%B8kken-p%C3%A5-f%C3%B8rste.jpg"
    data["legacy_images"] |= {
        "/static/legacy/image/upload/køkken.jpg": kitchen,
        "/static/legacy/image/upload/s%C3%B8en.jpg": kitchen,
        "/static/legacy/image/upload/tre_æbler.2019.jpg": kitchen,
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
        "/static/legacy/image/upload/på.jpg": kitchen,
        "/static/legacy/image/upload/ha%CC%8Ab.jpg": kitchen,
    }
    publish(client, data | {"revision": "c" * 64})
    for path in ("/static/legacy/image/upload/pa%CC%8A.jpg", "/static/legacy/image/upload/h%C3%A5b.jpg"):
        result = client.get(path)
        assert (result.status, result.headers["Location"]) == (301, href), path
    assert client.get("/static/legacy/image/upload/kokken.jpg").status == 404
    result = client.get("/public/image/upload/k%C3%B8kken.jpg")
    assert (result.status, result.headers["Location"]) == (301, href)


def test_the_app_serves_a_page_in_either_slash_form(client: Client) -> None:
    """Apache serves both forms from the file; should it hand one to the app (a host whose index
    rule misses the slashed form), the app serves the same file instead of redirecting. The old site
    answered 200 in both forms too. The canonical link names the form the menu uses."""
    publish(client)
    for path, canonical in (
        ("/faciliteter", "/faciliteter"),
        ("/faciliteter/", "/faciliteter"),
        ("/faciliteter/kokken/", "/faciliteter/kokken"),
        ("/begivenheder", "/begivenheder/"),
        ("/begivenheder/", "/begivenheder/"),
        ("/optagelse", "/optagelse/"),
        ("/optagelse/ansoeg/", "/optagelse/ansoeg"),
    ):
        result = client.get(path, query="x=1")
        assert result.status == 200, path
        assert f'<link rel="canonical" href="https://gahk.dk{canonical}">' in result.text, path
    assert "Om faciliteterne" in client.get("/faciliteter/").text


def test_old_page_addresses_redirect(client: Client) -> None:
    publish(client)
    result = client.get("/gammel-side")
    assert (result.status, result.headers["Location"]) == (301, "/kontakt")
    result = client.get("/gammel-side/")
    assert (result.status, result.headers["Location"]) == (301, "/kontakt")
    result = client.get("/optagelse/send_rundvisning")
    assert (result.status, result.headers["Location"]) == (301, "/optagelse/ansoeg")


def test_the_front_pages_old_address_answers_with_the_front_page(
    client: Client, dev: Client, cfg: Config
) -> None:
    """The old site served the front page at /velkommen too (page 1's slug); intern publishes it as a
    redirect to the front page. It keeps answering 200, with the front page's canonical link."""
    data = snapshot()
    data["redirects"] |= {"velkommen": "", "forside": ""}
    publish(client, data)
    front = (cfg.public_html / "index.html").read_text()
    assert (cfg.public_html / "velkommen" / "index.html").read_text() == front
    assert '<link rel="canonical" href="https://gahk.dk/">' in front
    for path in ("/velkommen", "/velkommen/"):
        for answer in (dev.get(path, query="x=1"), client.get(path, query="x=1")):
            assert (answer.status, answer.text) == (200, front), path
    assert "/velkommen" not in client.get("/sitemap.xml").text
    # Its views count as the front page's: the page names its own address in the beacon.
    assert 'navigator.sendBeacon("/s", "/")' in front
    # Only that fixed alias: any other redirect to the front page stays a redirect.
    assert not (cfg.public_html / "forside").exists()
    result = client.get("/forside")
    assert (result.status, result.headers["Location"]) == (301, "/")
    # When the snapshot stops redirecting it to the front page, the copy goes.
    data["redirects"] = {"velkommen": "kontakt"}
    publish(client, data | {"revision": "e" * 64})
    assert not (cfg.public_html / "velkommen").exists()
    result = client.get("/velkommen")
    assert (result.status, result.headers["Location"]) == (301, "/kontakt")


def test_other_methods_are_refused(client: Client) -> None:
    publish(client)
    assert client.request("PUT", "/kontakt").status == 405
    assert client.post("/kontakt").status == 405


def test_htaccess_names_the_same_rules() -> None:
    text = HTACCESS.read_text()
    assert "DirectorySlash Off" in text
    assert "RewriteCond %{REQUEST_FILENAME}/index.html -f" in text
    assert "RewriteRule ^(.*)$ cgi-bin/gahk.fcgi/$1 [L,QSA]" in text
    assert not any("public/image" in line for line in text.splitlines() if "RewriteRule" in line)
    assert (
        "RewriteRule ^static/legacy/image/elements/lamassu(?:\\.b8dc291a4e43)?\\.svg$ /static/img/lamassu.svg"
        " [R=301,L]" in text
    )
    rules = re.findall(
        r"^\s*RewriteRule \^(\S+) https://intern\.gahk\.dk/\S* \[R=308,L\]$", text, re.MULTILINE
    )
    assert len(rules) == 5
    for prefix in (
        "intern|nyintern|admin|django-admin",
        "photo-album|media",
        "health",
        "listansoegninger|eksport",
        "showAnsoegning|setasreceived|kasser",
    ):
        assert any(prefix in rule for rule in rules), prefix
    assert not any("wiki" in rule for rule in rules)  # wiki.gahk.dk's own rules
    assert "max-age=31536000, immutable" in text


def test_htaccess_encodes_the_path_again_when_it_sends_www_and_http_to_https() -> None:
    """%{REQUEST_URI} is decoded: with NE, /a%20b on www.gahk.dk went to "https://gahk.dk/a b", and a
    Danish letter went out as raw UTF-8 (found by the URL parity probe of the old site's pictures)."""
    lines = rule_lines(HTACCESS.read_text())
    canonical = [line for line in lines if "%{REQUEST_URI}" in line]
    assert canonical == [
        "RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L]",
        "RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]",
    ]


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
    files = lines.index("RewriteCond %{REQUEST_FILENAME} -f") - 1
    assert lines[files] == "RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$"
    assert refuse < scripts < files
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
        "/images/0123456789abcdef/a.cgi.jpg",
        "/x.Py",
        "/images/0123456789abcdef/foto.php.jpg",
        "/images/0123456789abcdef/foto.PHP5.jpg",
        "/images/0123456789abcdef/foto.shtml.jpg",
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
    hallway = image("gang.jpg", HALLWAY)["url"]
    result = dev.get(hallway)
    assert (result.status, result.body) == (200, HALLWAY)
    assert dev.get("/robots.txt").text.startswith("User-agent: *")
    assert dev.get("/sw.js").headers["Content-Type"].startswith(("application/javascript", "text/javascript"))


def test_htaccess_serves_the_same_aliases() -> None:
    rules = re.findall(r"^\s*RewriteRule \^(\S+)\$ (static/\S+) \[L\]$", HTACCESS.read_text(), re.MULTILINE)
    rules = [(pattern, to) for pattern, to in rules if to != routing.OLD_APP_PAGE.lstrip("/")]
    assert len(rules) == 2
    for path, target in routing.ALIASES.items():
        assert [to for pattern, to in rules if re.fullmatch(pattern, path.lstrip("/"))] == [
            target.lstrip("/")
        ]
    for pattern, target in rules:
        assert (STATIC_DIR / target.removeprefix("static/")).is_file(), target


def test_the_old_sites_logo_redirects_to_the_sites_own(client: Client, dev: Client) -> None:
    """The logo and favicon of every page of the old site; other sites may link it. Nothing is served
    at an address with /legacy/ in it: the old addresses redirect to the site's own copy."""
    publish(client)
    logo = (STATIC_DIR / "img" / "lamassu.svg").read_bytes()
    assert logo.startswith(b"<?xml") and b"<svg" in logo
    for path in routing.OLD_LOGO_PATHS:
        for app in (dev, client):
            result = app.get(path)
            assert (result.status, result.headers["Location"]) == (301, routing.LOGO_PATH), path
    result = dev.get(routing.LOGO_PATH)
    assert (result.status, result.body) == (200, logo)
    assert result.headers["Content-Type"] == "image/svg+xml"


@pytest.mark.parametrize(
    "path",
    [
        "/static/manifest.json",
        "/static/icons/icon-512x512.png",
        "/static/icons/icon-512x512-maskable.png",
        "/static/dist/app.css",
        "/static/dist/app.2d95e49102ae.css",
        "/static/dist/chunks/den-hurtige.js",
        "/static/legacy/image/elements/logo.png",
        "/static/legacy/image/elements/gahk-kiosk.jpg",
        "/static/legacy/image/intern/sal1.png",
    ],
)
def test_the_old_intranets_static_files_go_to_intern(client: Client, dev: Client, path: str) -> None:
    """The old gahk.dk served them for the intranet; intern.gahk.dk serves them at the same path."""
    publish(client)
    for answer in (dev.get(path, query="v=2"), client.get(path, query="v=2")):
        assert (answer.status, answer.headers["Location"]) == (301, f"https://intern.gahk.dk{path}?v=2")


def test_this_sites_own_static_files_win_over_intern(client: Client, dev: Client) -> None:
    publish(client)
    assert dev.get("/static/icons/icon-192x192.png").status == 200
    assert dev.get("/static/icons/badge-72x72.png").status == 200
    assert dev.get("/static/css/findes-ikke.css").status == 404
    assert dev.get("/static/legacy/image/upload/images/ukendt.jpg").status == 404  # the picture map's
    assert routing.intern_static_redirect("/static/icons/") is None
    assert routing.intern_static_redirect("/static/manifest.json/x") is None
    assert routing.intern_static_redirect("/static/legacy/image/upload/images/gang.jpg") is None


def test_htaccess_sends_the_old_intranets_static_files_to_intern_after_its_own_files() -> None:
    lines = rule_lines(HTACCESS.read_text())
    rule = next(line for line in lines if "https://intern.gahk.dk/$1 [R=301,L]" in line)
    pattern = rule.split()[1]
    assert pattern == "^" + routing._INTERN_STATIC_RE.pattern.removeprefix("^/")
    assert lines.index("RewriteRule ^(.*?)/?$ $1/index.html [L]") < lines.index(rule)
    assert lines.index(rule) < lines.index("RewriteRule ^api/update$ cgi-bin/gahk.cgi/api/update [L,QSA]")


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
    folder = image("kokken.jpg", KITCHEN)["url"].rsplit("/", 1)[0]
    result = dev.get(f"{folder}/tovtr%C3%A6kning-2019.jpg")
    assert (result.status, result.body) == (200, KITCHEN)
    assert dev.get(f"{folder}/tovtra%CC%8Akning-2019.jpg").status == 404  # NFD is another name
    page = dev.get("/faciliteter/kokken").text
    assert f"{folder}/tovtr%C3%A6kning-2019.jpg" in page


def test_devserver_matches_production_routing(client: Client, dev: Client) -> None:
    publish(client)
    assert dev.post("/intern/x").headers["Location"] == "https://intern.gahk.dk/intern/x"
    assert dev.get("/intern/x").body == old_app_page()
    hallway = image("gang.jpg", HALLWAY)["url"]
    assert dev.get("/public/image/upload/images/gang.jpg").headers["Location"] == hallway
    assert dev.get("/public/image/a.jpg").status == 404
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
