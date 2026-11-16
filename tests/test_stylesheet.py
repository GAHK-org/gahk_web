"""The committed stylesheet (built by Tailwind from styles/site.css; CI checks that it is current)."""

from gahk_web.render import STATIC_DIR

CSS = (STATIC_DIR / "css" / "site.css").read_text(encoding="utf-8")


def test_the_classes_an_editor_may_use_in_a_body_are_styled() -> None:
    """Bodies are not scanned by Tailwind; these are written out whether a template uses them or not
    (spec/features/website.md, "Page text")."""
    for selector in (
        ".prose-gahk .lead",
        ".prose-gahk .note",
        ".prose-gahk a.button",
        ".prose-gahk .columns",
        ".prose-gahk .center",
    ):
        assert selector + "{" in CSS or selector + "," in CSS, selector


def test_the_classes_the_code_writes_are_styled() -> None:
    assert ".hero__img{" in CSS  # Site.hero_img
    assert ".editor-link{" in CSS  # base.html's "Rediger siden"


def test_the_fonts_are_the_sites_own() -> None:
    for font in ("julius-sans-one-400", "ubuntu-400", "ubuntu-500"):
        assert f"url(../fonts/{font}.woff2)" in CSS.replace('"', "")
        assert (STATIC_DIR / "fonts" / f"{font}.woff2").is_file()
    assert "fonts.googleapis" not in CSS
