"""Preparing uploaded pictures for the readers (stem-accessibility/frontend#4: PNG uploads).

A transparent PNG used to reach the shape detector as an all-black picture. These tests build real
images in memory with Pillow; no model or network is used, and the image pipeline is replaced with
a stand-in that records what it was given.
    .venv/bin/python -m unittest tests.test_image_prep -v
"""
import base64
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image, ImageDraw

_tmp = tempfile.mkdtemp()
os.environ["AUTH_MODE"] = "dev"
os.environ["AUTO_CREATE_TABLES"] = "1"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.sqlite"
os.environ.pop("K_SERVICE", None)

from backend import image_store  # noqa: E402
from backend.app_factory import create_app  # noqa: E402
from backend.db import db  # noqa: E402
from backend.image_prep import UnreadableImage, prepare  # noqa: E402
from backend.models import Job  # noqa: E402


def diagram(mode="RGBA", size=(200, 120)):
    """Black boxes and a line on a transparent (or white) background, as diagram tools export them."""
    bg = (0, 0, 0, 0) if mode == "RGBA" else "white"
    im = Image.new(mode, size, bg)
    d = ImageDraw.Draw(im)
    ink = (0, 0, 0, 255) if mode == "RGBA" else "black"
    d.rectangle([10, 10, 80, 50], outline=ink, width=3)
    d.line([80, 30, 150, 90], fill=ink, width=3)
    return im


def encode(im, fmt, **kw):
    out = io.BytesIO()
    im.save(out, format=fmt, **kw)
    return out.getvalue()


def decoded(prepared):
    return Image.open(io.BytesIO(prepared.png))


def dark_share(im):
    px = list(im.convert("L").getdata())
    return sum(1 for v in px if v < 60) / len(px)


class PrepareTests(unittest.TestCase):
    def test_transparent_png_is_put_on_white_not_black(self):
        p = prepare(encode(diagram("RGBA"), "PNG"))
        im = decoded(p)
        self.assertEqual(im.mode, "RGB")
        self.assertEqual(im.getpixel((190, 5)), (255, 255, 255))  # background
        self.assertEqual(im.getpixel((10, 30)), (0, 0, 0))  # the box outline survives
        self.assertLess(dark_share(im), 0.2)  # before the fix the whole picture was black
        self.assertEqual(p.keepable_mime, "image/png")

    def test_palette_and_greyscale_alpha_pngs(self):
        pal = diagram("RGBA").convert("P", palette=Image.Palette.ADAPTIVE, colors=8)
        pal.info["transparency"] = pal.getpixel((190, 5))
        for data in (encode(pal, "PNG", transparency=pal.info["transparency"]), encode(diagram("RGBA").convert("LA"), "PNG")):
            im = decoded(prepare(data))
            self.assertEqual(im.getpixel((190, 5)), (255, 255, 255))
            self.assertEqual(im.getpixel((10, 30)), (0, 0, 0))

    def test_sixteen_bit_greyscale_keeps_its_contrast(self):
        im16 = Image.new("I;16", (50, 50), 65535)
        ImageDraw.Draw(im16).rectangle([10, 10, 20, 20], fill=0)
        im = decoded(prepare(encode(im16, "PNG")))
        self.assertEqual(im.getpixel((40, 40)), (255, 255, 255))
        self.assertEqual(im.getpixel((15, 15)), (0, 0, 0))

    def test_phone_photo_is_turned_upright(self):
        im = diagram("RGB", size=(200, 100))
        exif = Image.Exif()
        exif[0x0112] = 6  # orientation: rotate 90 degrees clockwise to view
        p = prepare(encode(im, "JPEG", exif=exif.tobytes()))
        self.assertEqual((p.width, p.height), (100, 200))
        self.assertEqual(p.keepable_mime, "image/jpeg")

    def test_more_formats_are_read(self):
        frames = [diagram("RGB"), Image.new("RGB", (200, 120), "red")]
        gif = io.BytesIO()
        frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:])
        cases = {
            "GIF": (gif.getvalue(), "image/gif"),
            "BMP": (encode(diagram("RGB"), "BMP"), None),
            "TIFF": (encode(diagram("RGB"), "TIFF"), None),
            "WEBP": (encode(diagram("RGBA"), "WEBP", lossless=True), "image/webp"),
            "CMYK JPEG": (encode(diagram("RGB").convert("CMYK"), "JPEG"), "image/jpeg"),
        }
        for name, (data, mime) in cases.items():
            with self.subTest(name):
                p = prepare(data)
                im = decoded(p)
                self.assertEqual(im.mode, "RGB")
                self.assertEqual((p.width, p.height), (200, 120))
                self.assertGreater(im.convert("L").getpixel((190, 110)), 200)  # first frame, not the red one
                self.assertEqual(p.keepable_mime, mime)

    def test_unusable_files_get_a_message_a_person_can_act_on(self):
        cases = {
            b"": "empty",
            b"not a picture at all": "not a picture",
            b"%PDF-1.7 something": "not a picture",
            b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>': "SVG",
            b"<svg viewBox='0 0 10 10'></svg>": "SVG",
        }
        for data, words in cases.items():
            with self.subTest(words):
                with self.assertRaises(UnreadableImage) as cm:
                    prepare(data)
                self.assertIn(words, str(cm.exception))

    def test_refuses_pictures_too_large_to_decode(self):
        with mock.patch.dict(os.environ, {"MAX_UPLOAD_PIXELS": "1000"}):
            with self.assertRaises(UnreadableImage) as cm:
                prepare(encode(diagram("RGB"), "PNG"))
        self.assertIn("200 x 120", str(cm.exception))


class UploadRouteTests(unittest.TestCase):
    """POST /jobs/process-upload with the image pipeline replaced by a recorder."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def setUp(self):
        self.seen = {}

        def process_image(path, base_name=None):
            im = Image.open(path)
            self.seen.update(mode=im.mode, size=im.size, corner=im.convert("RGB").getpixel((im.width - 1, 0)))
            return {"nodes": [{"id": "node_1", "label": "Start", "shape": "rectangle"}], "edges": []}

        fake = types.ModuleType("backend.pipeline.pipeline2")
        fake.process_image = process_image
        patcher = mock.patch.dict(sys.modules, {"backend.pipeline.pipeline2": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, body, name="diagram.png", content_type="image/png"):
        data = {"file": (io.BytesIO(body), name, content_type)}
        with mock.patch.dict(os.environ, {"RUN_JOBS_INLINE": "1"}):
            return self.client.post("/jobs/process-upload", data=data, content_type="multipart/form-data")

    def job_count(self):
        with self.app.app_context():
            return Job.query.filter_by(type="process_upload").count()

    def test_transparent_png_reaches_the_pipeline_on_white_and_the_original_is_kept(self):
        original = encode(diagram("RGBA"), "PNG")
        r = self.upload(original)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.seen["mode"], "RGB")
        self.assertEqual(self.seen["corner"], (255, 255, 255))
        with self.app.app_context():
            job = db.session.get(Job, r.get_json()["job_id"])
            self.assertEqual(job.status, "done", job.error)
            result = json.loads(job.result)
        self.assertEqual(result["image_mime"], "image/png")
        self.assertEqual(base64.b64decode(result["image_data_url"].split(",", 1)[1]), original)

    def test_bmp_is_read_and_kept_as_png(self):
        r = self.upload(encode(diagram("RGB"), "BMP"), name="diagram.bmp", content_type="image/bmp")
        self.assertEqual(r.status_code, 201, r.get_json())
        with self.app.app_context():
            result = json.loads(db.session.get(Job, r.get_json()["job_id"]).result)
        self.assertEqual(result["image_mime"], "image/png")
        image_store.parse_data_url(result["image_data_url"])  # the store accepts what the page will save

    def test_unreadable_files_are_refused_before_a_job_exists(self):
        before = self.job_count()
        r = self.upload(b"<svg></svg>", name="diagram.svg", content_type="image/svg+xml")
        self.assertEqual(r.status_code, 400)
        self.assertIn("SVG", r.get_json()["error"])
        self.assertEqual(self.job_count(), before)
        self.assertEqual(self.seen, {})

    def test_pictures_larger_than_the_store_takes_are_refused(self):
        before = self.job_count()
        with mock.patch.object(image_store, "MAX_IMAGE_BYTES", 100):
            r = self.upload(encode(diagram("RGB"), "PNG"))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.job_count(), before)


if __name__ == "__main__":
    unittest.main()
