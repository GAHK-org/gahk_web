"""Sanitising bodies (the same allowlist as intern) and linking picture variants in them."""

import pytest
from conftest import Client, page, publish, snapshot

from gahk_web.bodies import img_tag, responsive_images, sanitize, sizes_for
from gahk_web.config import Config
from gahk_web.images import Picture

# Excerpts of the site's own bodies (the crawl of the old gahk.dk), the way intern's sanitiser left
# them: legacy CKEditor markup with inline styles, entities, floats, tables and links.
LEGITIMATE = [
    (
        '<h3>Madordning</h3>\n\n<p dir="ltr">Vores kollegium har en obligatorisk madordning, som omfatter'
        " morgen- middag- og aftensmad.</p>\n\n<p>&nbsp;</p>"
    ),
    (
        '<h3 style="margin-bottom: 0in; line-height: 20px; widows: 0; orphans: 0; ">Frokost</h3>\n\n'
        '<p style="margin-bottom: 0in; line-height: 20px; widows: 0; orphans: 0; "><span style="line-height:'
        ' 115%; ">Frokosten serveres i spisesalen hver hverdag fra kl. 11.45-12.45.&nbsp;Der er altid'
        ' mindst tre salater.</span><br>\n<br>\n<img alt="" src="/billeder/0123456789abcdef/spisning.jpg"'
        ' style="height: 110px; width: 420px;"></p>'
    ),
    (
        '<p><img alt="" src="/billeder/0123456789abcdef/aurora.jpg" style="caret-color: rgb(0, 0, 0); color:'
        ' rgb(0, 0, 0); width: 180px; height: 201px; float: right;">Aurora &amp; Mathias &lt;3</p>'
    ),
    (
        '<table style="width: 100%;"><tbody><tr><th scope="col">År</th><td colspan="2">Legat'
        '</td></tr><tr><td rowspan="2">2019</td><td>5.000 kr.</td></tr></tbody></table>'
    ),
    (
        '<p>Skriv til <a href="mailto:bestyrelsen@gahk.dk" rel="noopener noreferrer">bestyrelsen</a> eller se'
        ' <a href="https://intern.gahk.dk/wiki/" target="_blank" rel="noopener noreferrer">wikien</a>.</p>'
    ),
    (
        "<ul><li><strong>Fed</strong>, <em>kursiv</em>, <u>understreget</u>, <s>gennemstreget</s></li>"
        '<li><sub>2</sub><sup>3</sup> <small>lille</small> <mark>mærket</mark> <abbr title="Gl.">gl.</abbr>'
        "</li></ul><ol><li>en</li></ol><blockquote><cite>citat</cite></blockquote><pre><code>kode</code></pre>"
        '<figure><img src="/billeder/0123456789abcdef/a.png" alt="A" width="300" height="200"><figcaption>'
        'Tekst</figcaption></figure><dl><dt>t</dt><dd>d</dd></dl><hr><div class="x" id="y" lang="da">'
        "<h1>1</h1><h2>2</h2><h4>4</h4><h5>5</h5><h6>6</h6></div>"
    ),
]


@pytest.mark.parametrize("body", LEGITIMATE)
def test_sanitising_what_intern_cleaned_changes_nothing(body: str) -> None:
    assert sanitize(body) == body


def test_sanitising_is_a_fixed_point() -> None:
    for body in LEGITIMATE:
        assert sanitize(sanitize(body)) == sanitize(body)


@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        # intern's own sanitiser tests (gahk_intern app/tests/test_hjemmeside_services.py).
        (
            '<p>Hej</p><script>alert(1)</script><a href="javascript:x()">x</a>',
            '<p>Hej</p><a rel="noopener noreferrer">x</a>',
        ),
        ("<p>Kom</p><img src=x onerror=y()>", '<p>Kom</p><img src="x">'),
        ('<a href=" JaVaScRiPt:alert(1)">a</a>', '<a rel="noopener noreferrer">a</a>'),
        ('<a href="data:text/html,x">a</a>', '<a rel="noopener noreferrer">a</a>'),
        ('<p onclick="x()" onmouseover="y()">t</p>', "<p>t</p>"),
        ('<iframe src="https://evil"></iframe><form action="x"><input></form>', ""),
        ("<svg onload=alert(1)><circle/></svg><style>p{}</style><!-- note -->", ""),
        (
            '<a href="/side" target="_blank" rel="opener">l</a>',
            '<a href="/side" target="_blank" rel="noopener noreferrer">l</a>',
        ),
        # What only the rewrite may add, never the body itself.
        (
            '<img src="/billeder/0123456789abcdef/a.jpg" srcset="evil.jpg 1x" sizes="1px" loading="eager">',
            '<img src="/billeder/0123456789abcdef/a.jpg">',
        ),
    ],
)
def test_sanitising_strips_what_intern_would(dirty: str, clean: str) -> None:
    assert sanitize(dirty) == clean


def test_published_and_previewed_bodies_are_sanitised(client: Client, cfg: Config) -> None:
    data = snapshot()
    data["pages"][3]["body"] = '<p onclick="x()">Skriv til os</p><script>alert(1)</script>'
    data["news"][1]["body"] = '<p>Nu<img src="x" onerror="y()"></p>'
    data["events"][0]["description"] = '<p><a href="javascript:x()">Mad</a></p>'
    publish(client, data)
    assert "<p>Skriv til os</p>\n" in (cfg.public_html / "kontakt/index.html").read_text()
    begivenheder = (cfg.public_html / "begivenheder/index.html").read_text()
    assert '<p>Nu<img src="x"></p>' in begivenheder
    assert '<a rel="noopener noreferrer">Mad</a>' in begivenheder
    for html in ((cfg.public_html / "kontakt/index.html").read_text(), begivenheder):
        assert "alert(1)" not in html and "onerror" not in html and "javascript:" not in html

    item = page(9, "ny", "Ny", '<p><img src="/media/cms/ny.jpg" onload="x()"></p><script>alert(1)</script>')
    result = client.api(
        "POST", "/api/preview", {"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"}
    )
    assert '<p><img src="https://intern.gahk.dk/media/cms/ny.jpg"></p>' in result.text
    assert "alert(1)" not in result.text and "onload" not in result.text


def picture(url: str = "/billeder/0123456789abcdef/a.jpg", width: int = 2000, height: int = 1000) -> Picture:
    widths = [step for step in (480, 960, 1600) if step < width] + [min(width, 2400)]
    return Picture(url, width, height, tuple((w, f"/billeder/0123456789abcdef/_{w}w.webp") for w in widths))


def test_an_image_with_variants_links_them() -> None:
    pic = picture()
    body = '<p>Før &amp; <b>efter</b></p><p><img alt="Gangen" src="/billeder/0123456789abcdef/a.jpg"></p><p>Slut</p>'
    out = responsive_images(body, {pic.url: pic})
    assert out == (
        '<p>Før &amp; <b>efter</b></p><p><img src="/billeder/0123456789abcdef/_2000w.webp" alt="Gangen"'
        ' width="2000" height="1000" srcset="/billeder/0123456789abcdef/_480w.webp 480w,'
        " /billeder/0123456789abcdef/_960w.webp 960w, /billeder/0123456789abcdef/_1600w.webp 1600w,"
        ' /billeder/0123456789abcdef/_2000w.webp 2000w" sizes="(min-width: 768px) 720px,'
        ' calc(100vw - 3rem)" loading="lazy" decoding="async"></p><p>Slut</p>'
    )
    assert "a.jpg" not in out  # the original is no longer linked


def test_sizes_follow_the_width_the_body_gives() -> None:
    pic = picture()
    styled = responsive_images(
        f'<img src="{pic.url}" style="height: 110px; width: 420px; float: right;">', {pic.url: pic}
    )
    assert 'sizes="(min-width: 468px) 420px, calc(100vw - 3rem)"' in styled
    assert 'style="height: 110px; width: 420px; float: right;"' in styled
    assert sizes_for(5000) == "(min-width: 768px) 720px, calc(100vw - 3rem)"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("", 'width="2000" height="1000"'),  # the picture's own size
        ('width="300"', 'width="300" height="150"'),  # the editor's width, the height to match
        ('height="100"', 'width="200" height="100"'),
        ('width="300" height="300"', 'width="300" height="300"'),  # the editor's choice stays
        ('width="50%"', 'width="50%"'),  # not a number of pixels: left as it is
    ],
)
def test_width_and_height(given: str, expected: str) -> None:
    pic = picture()
    out = responsive_images(f'<img src="{pic.url}" {given}>', {pic.url: pic})
    assert f'<img src="{pic.src}" {expected} srcset=' in out


def test_only_the_front_pages_first_picture_loads_eagerly() -> None:
    pic = picture()
    body = f'<img src="{pic.url}"><img src="{pic.url}">'
    eager = responsive_images(body, {pic.url: pic}, eager_first=True)
    first, second = eager.split("><")
    assert "loading" not in first and "decoding" not in first
    assert 'loading="lazy" decoding="async"' in second
    assert responsive_images(body, {pic.url: pic}).count('loading="lazy"') == 2


def test_other_images_and_the_rest_of_the_body_are_left_alone() -> None:
    pic = picture()
    body = (
        '<p title="a &quot;b&quot; c">x<br>\n<img src="/billeder/fedcba9876543210/gif.gif" alt="">\n'
        '<img src="https://example.com/x.jpg"><img alt="ingen src"></p><a href="/billeder/0123456789abcdef/a.jpg">'
        "Fuld størrelse</a>"
    )
    assert responsive_images(body, {pic.url: pic}) == body
    assert responsive_images(body, {}) == body


def test_attributes_are_escaped() -> None:
    pic = picture()
    tag = img_tag([("src", pic.url), ("alt", 'Et "citat" & <tegn>'), ("class", None)], pic, lazy=False)
    assert 'alt="Et &quot;citat&quot; &amp; &lt;tegn&gt;"' in tag
    assert " class " in tag
