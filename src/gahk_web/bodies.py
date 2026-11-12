"""The HTML bodies of pages, news and events: sanitised again here, then given responsive images.

intern cleans every body with nh3 before it stores it, and the snapshot is signed. gahk.dk cleans
it once more with the very same allowlist (defence in depth: a bug or a bypass on intern must not
put a script on gahk.dk). The allowlist below mirrors gahk_intern's app/cms/sanitize.py exactly, so
cleaning what intern already cleaned changes nothing; change the two together.

Then each <img> whose src is a published image with variants (images.Picture) links the variants:
src is the widest one, srcset lists them all, sizes says how wide the content column shows it, and
width/height give the browser the picture's shape before it loads. Pictures load lazily, except the
first one of the front page. The src of any other /images/ image is percent-encoded the way the
page's other image URLs are (tovtr%C3%A6kning.jpg); intern may write it either way. The rewrite uses
the standard library's HTML tokenizer and replaces only the <img> tags it changes, so the rest of the
body stays byte for byte as it was.
"""

import html
import re
import unicodedata
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import unquote

from .snapshot import image_href, image_url_match

if TYPE_CHECKING:
    from .images import Picture

# --- sanitising: the same allowlist as gahk_intern's cms/sanitize.py ---

ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "hr",
        "div",
        "span",
        "a",
        "img",
        "figure",
        "figcaption",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "caption",
        "colgroup",
        "col",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "small",
        "sub",
        "sup",
        "blockquote",
        "pre",
        "code",
        "abbr",
        "cite",
        "mark",
    }
)
ALLOWED_ATTRIBUTES: Mapping[str, frozenset[str]] = {
    "*": frozenset({"style", "class", "id", "title", "dir", "lang"}),
    "a": frozenset({"href", "target"}),  # rel is set by LINK_REL
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
}
URL_SCHEMES = frozenset({"http", "https", "mailto"})  # relative URLs are allowed as well
LINK_REL = "noopener noreferrer"


def sanitize(body: str) -> str:
    """The body with anything intern's sanitiser would not allow removed."""
    if not body:
        return body
    import nh3  # only rendering needs it

    return nh3.clean(
        body,
        tags=set(ALLOWED_TAGS),
        attributes={tag: set(names) for tag, names in ALLOWED_ATTRIBUTES.items()},
        url_schemes=set(URL_SCHEMES),
        link_rel=LINK_REL,
    )


# --- responsive images ---

# The widest the content column gets (.main and .page-card in site.css: 48rem less padding), and
# what the column leaves of a narrower window.
COLUMN_PX = 720
COLUMN_GUTTER_PX = 48
SMALL_COLUMN = "calc(100vw - 3rem)"
# Attributes the rewrite sets itself; a body's own copies are dropped.
_REPLACED = frozenset({"src", "srcset", "sizes", "loading", "decoding", "width", "height"})
_PIXELS_RE = re.compile(r"^\s*([0-9]{1,5})(?:\.[0-9]*)?(?:px)?\s*$", re.IGNORECASE)
_STYLE_WIDTH_RE = re.compile(r"(?:^|;)\s*width\s*:\s*([0-9]{1,5})(?:\.[0-9]*)?px\b", re.IGNORECASE)


class _ImgTags(HTMLParser):
    """Finds every <img> start tag: (offset in the source, its text, its attributes)."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.line_starts = [0] + [match.end() for match in re.finditer("\n", source)]
        self.source = source
        self.found: list[tuple[int, str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        text = self.get_starttag_text() or ""
        line, column = self.getpos()
        offset = self.line_starts[line - 1] + column
        if text and self.source.startswith(text, offset):
            self.found.append((offset, text, attrs))


def _pixels(value: str | None) -> int | None:
    match = _PIXELS_RE.match(value or "")
    number = int(match.group(1)) if match else 0
    return number or None


def sizes_for(display_width: int) -> str:
    """The sizes attribute for a picture shown at most display_width CSS pixels wide in the column."""
    width = min(display_width, COLUMN_PX)
    return f"(min-width: {width + COLUMN_GUTTER_PX}px) {width}px, {SMALL_COLUMN}"


def img_tag(attrs: list[tuple[str, str | None]], picture: "Picture", *, lazy: bool) -> str:
    """The <img> tag for a picture with variants, keeping the body's other attributes."""
    values = dict(attrs)
    width, height = _pixels(values.get("width")), _pixels(values.get("height"))
    given_width, given_height = values.get("width"), values.get("height")
    if width and not height and given_height is None:
        height = max(1, round(width * picture.height / picture.width))
    elif height and not width and given_width is None:
        width = max(1, round(height * picture.width / picture.height))
    elif given_width is None and given_height is None:
        width, height = picture.width, picture.height
    style_width = _STYLE_WIDTH_RE.search(values.get("style") or "")
    display_width = int(style_width.group(1)) if style_width else width or picture.width

    out: list[tuple[str, str | None]] = [("src", picture.src)]
    out += [(name, value) for name, value in attrs if name not in _REPLACED]
    out += [
        ("width", str(width) if width else given_width),
        ("height", str(height) if height else given_height),
        ("srcset", picture.srcset),
        ("sizes", sizes_for(max(1, display_width))),
    ]
    if lazy:
        out += [("loading", "lazy"), ("decoding", "async")]
    return _tag([(name, value) for name, value in out if not (value is None and name in ("width", "height"))])


def _tag(attrs: list[tuple[str, str | None]]) -> str:
    parts = [name if value is None else f'{name}="{html.escape(value, quote=True)}"' for name, value in attrs]
    return "<img " + " ".join(parts) + ">"


def image_url(src: str) -> str:
    """The snapshot's URL of an <img> src: a /images/ address percent-decoded (and NFC, as the
    snapshot's names are), whether the body spells its letters out or encodes them."""
    if not src.startswith("/images/"):
        return src
    return unicodedata.normalize("NFC", unquote(src, errors="replace"))


def responsive_images(body: str, pictures: Mapping[str, "Picture"], *, eager_first: bool = False) -> str:
    """The body with each <img> of a picture with variants rewritten (see img_tag), and the src of
    every other /images/ image percent-encoded. With eager_first, the first picture in the body
    loads at once (it is likely in view when the page opens)."""
    if "<img" not in body.lower():
        return body
    finder = _ImgTags(body)
    finder.feed(body)
    finder.close()
    pieces: list[str] = []
    done = 0
    first = True
    for offset, text, attrs in finder.found:
        if offset < done:
            continue
        src = dict(attrs).get("src") or ""
        url = image_url(src)
        picture = pictures.get(src) or pictures.get(url)
        if picture is not None:
            tag = img_tag(attrs, picture, lazy=not (eager_first and first))
            first = False
        elif image_url_match(url) and image_href(url) != src:
            tag = _tag([(name, image_href(url) if name == "src" else value) for name, value in attrs])
        else:
            continue
        pieces += [body[done:offset], tag]
        done = offset + len(text)
    if not pieces:
        return body
    pieces.append(body[done:])
    return "".join(pieces)
