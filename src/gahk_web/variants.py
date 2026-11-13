"""Responsive WebP variants of a published picture, made with Pillow.

A variant is the picture scaled to one of WIDTHS (never up), turned upright by its EXIF orientation,
converted to sRGB, and saved as WebP with no metadata at all: no EXIF (so no GPS position), no XMP,
no colour profile. PNG and WebP pictures keep their transparency. The widest variant is the
picture's own width when that is under the largest step, so the sharpest variant never loses detail.

Variants are a cache: a picture Pillow cannot read, an animation, a GIF or one over MAX_PIXELS gets
none, and the site shows the original instead. Nothing here touches the database or the disk; see
images.py for where variants are kept.

Pillow is imported inside the functions: most CGI requests never handle a picture.
"""

import io
import math
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

WIDTHS = (480, 960, 1600, 2400)
QUALITY = 80
# Pillow refuses more than twice this on its own; anything over it is refused here first. 30 MP
# covers a 24 MP camera picture; intern scales its uploads to 2400 px on the long side anyway.
MAX_PIXELS = 30_000_000
FORMATS = ("JPEG", "PNG", "WEBP")
ORIENTATION = 0x0112
TURNED = (5, 6, 7, 8)  # orientations that swap width and height


class NoVariants(Exception):
    """The picture gets no variants (the reason is the message)."""


@dataclass(frozen=True)
class Variants:
    width: int  # the picture's own size, upright
    height: int
    files: tuple[tuple[int, bytes], ...]  # (width, WebP bytes), narrowest first


def widths_for(width: int) -> tuple[int, ...]:
    """The variant widths for a picture this wide: each step below it, then its own width (capped)."""
    steps = [step for step in WIDTHS if step < width]
    top = min(width, WIDTHS[-1])
    if top not in steps:
        steps.append(top)
    return tuple(steps)


def _to_srgb(image: "Image.Image", icc_profile: bytes | None) -> "Image.Image":
    """Pixels in sRGB, so the variant needs no profile. A broken profile is ignored."""
    from PIL import ImageCms

    if icc_profile and image.mode in ("RGB", "RGBA", "CMYK", "L", "LA"):
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
            output_mode = "RGBA" if image.mode in ("RGBA", "LA") else "RGB"
            if image.mode in ("LA", "L") and source.profile.xcolor_space.strip() != "GRAY":
                raise ImageCms.PyCMSError("a grey picture with a colour profile")
            if image.mode == "LA":
                image = image.convert("RGBA")
                output_mode = "RGBA"
            converted = ImageCms.profileToProfile(
                image, source, ImageCms.createProfile("sRGB"), outputMode=output_mode
            )
            if converted is not None:
                return converted
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass
    return image


def _rgb_or_rgba(image: "Image.Image") -> "Image.Image":
    has_alpha = image.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        return image if image.mode == "RGBA" else image.convert("RGBA")
    if image.mode == "RGB":
        return image
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        image = image.convert("I").point(lambda value: value / 256).convert("L")  # 16-bit grey to 8 bits
    return image.convert("RGB")


def make(data: bytes) -> Variants:
    """The variants of one picture. Raises NoVariants when there are none; other exceptions (a
    corrupt file Pillow chokes on) mean the same to the caller."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            source = Image.open(io.BytesIO(data), formats=FORMATS)
        except UnidentifiedImageError as exc:
            raise NoVariants("not a JPEG, PNG or WebP picture") from exc
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise NoVariants(f"more than {MAX_PIXELS} pixels") from exc
        # Read from memory, so there is no file to close (and closing would drop the pixels).
        if getattr(source, "n_frames", 1) > 1:
            raise NoVariants("an animation")
        stored_width, stored_height = source.size
        if stored_width * stored_height > MAX_PIXELS:
            raise NoVariants(f"more than {MAX_PIXELS} pixels")
        turned = source.getexif().get(ORIENTATION) in TURNED
        width, height = (stored_height, stored_width) if turned else (stored_width, stored_height)
        widths = widths_for(width)
        if source.format == "JPEG":
            # Decode at 1/2, 1/4 or 1/8 when that still covers the widest variant: far less memory
            # and time for a big photo.
            scale = widths[-1] / width
            source.draft(None, (math.ceil(stored_width * scale), math.ceil(stored_height * scale)))
        source.load()
        icc_profile = source.info.get("icc_profile")
        ImageOps.exif_transpose(source, in_place=True)  # no second copy of a big picture
        image = _rgb_or_rgba(_to_srgb(source, icc_profile))

    files: list[tuple[int, bytes]] = []
    for variant_width in reversed(widths):  # widest first, each made from the one before
        size = (variant_width, max(1, round(height * variant_width / width)))
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        image.info = {}  # nothing of the original rides along
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=QUALITY, method=4, exif=b"", xmp=b"", icc_profile=None)
        files.append((variant_width, buffer.getvalue()))
    return Variants(width=width, height=height, files=tuple(reversed(files)))
