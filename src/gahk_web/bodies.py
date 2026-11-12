"""The HTML bodies of pages, news and events, sanitised again here.

intern cleans every body with nh3 before it stores it, and the snapshot is signed. gahk.dk cleans
it once more with the very same allowlist (defence in depth: a bug or a bypass on intern must not
put a script on gahk.dk). The allowlist below mirrors gahk_intern's app/cms/sanitize.py exactly, so
cleaning what intern already cleaned changes nothing; change the two together.
"""

from collections.abc import Mapping

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
