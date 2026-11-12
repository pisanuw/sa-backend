"""Uploaded pictures, made ready for the readers that look at them.

Pictures arrive as PNG, JPEG, WebP, GIF, BMP or TIFF: screenshots, phone photos and exports from
diagram tools. The readers (Google Vision OCR, the YOLO shape detector, the chart reader and the AI
model) want a plain, upright RGB picture. Without this step two things go wrong:

* A PNG exported with a transparent background (draw.io, PowerPoint, the Mermaid editor) stores
  its transparent pixels as black. OpenCV, which the detector loads images with, and Pillow's
  `convert("RGB")` both drop the alpha channel, so a black-on-transparent diagram becomes an
  all-black picture and nothing is found. Flattening onto white keeps the drawing.
* A phone photo records its rotation in EXIF. Readers that ignore it see the diagram sideways;
  browsers apply it, so the coordinates would not match the picture people see.

`prepare` returns the picture as an RGB PNG, white where it was transparent, the right way up,
and the same size as the original (coordinates found in it apply to the original).
"""
import io
import os
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

# Pillow format names we accept. MPO is what some phones call a JPEG with a depth map.
READABLE_FORMATS = {"PNG", "JPEG", "MPO", "WEBP", "GIF", "BMP", "TIFF"}
FORMAT_NAMES = "PNG, JPEG, WebP, GIF, BMP or TIFF"
# The MIME type a format is stored under when the original is kept (see image_store.ALLOWED_MIME).
KEEPABLE_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "MPO": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def max_pixels():
    """Largest picture we decode, in pixels (default 40 million, e.g. 8000 x 5000)."""
    try:
        return int(os.environ.get("MAX_UPLOAD_PIXELS", "40000000"))
    except ValueError:
        return 40_000_000


class UnreadableImage(ValueError):
    """The upload cannot be used. The message is safe to show to the person."""


@dataclass
class PreparedImage:
    png: bytes          # RGB PNG: upright, white where the original was transparent
    width: int
    height: int
    source_format: str  # Pillow's name for the original's format, e.g. "PNG"

    @property
    def keepable_mime(self):
        """MIME type to keep the original under, or None when only the prepared PNG can be kept."""
        return KEEPABLE_MIME.get(self.source_format)


def _looks_like_svg(data):
    head = data[:2048].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _flatten(im):
    """Any mode to RGB, putting transparent areas on white."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16- and 32-bit greyscale: scale to 8 bits rather than clip everything to white.
        im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if im.mode == "P" or "transparency" in im.info:
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        im = im.convert("RGBA")
        white = Image.new("RGB", im.size, (255, 255, 255))
        white.paste(im, mask=im.getchannel("A"))
        return white
    return im.convert("RGB")


def prepare(data):
    """Bytes of an uploaded picture -> PreparedImage. Raises UnreadableImage."""
    if not data:
        raise UnreadableImage("The file is empty.")
    if _looks_like_svg(data):
        raise UnreadableImage("SVG drawings can't be read yet. Export the diagram as a PNG and upload that.")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            if fmt not in READABLE_FORMATS:
                raise UnreadableImage(f"{fmt or 'This'} files can't be read. Upload a {FORMAT_NAMES} image.")
            if im.width * im.height > max_pixels():
                raise UnreadableImage(
                    f"The picture is {im.width} x {im.height} pixels, which is too large to read. "
                    "Resize it to under 8000 pixels on the long side and upload it again."
                )
            im.seek(0)  # first frame of an animated GIF or a multi-page TIFF
            im.load()
            upright = ImageOps.exif_transpose(im)
            rgb = _flatten(upright)
    except UnreadableImage:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise UnreadableImage(f"This file is not a picture that can be read. Upload a {FORMAT_NAMES} image.")
    out = io.BytesIO()
    rgb.save(out, format="PNG")
    return PreparedImage(png=out.getvalue(), width=rgb.width, height=rgb.height, source_format=fmt)
