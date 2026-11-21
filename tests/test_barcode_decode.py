"""pdf_extraction.decode_barcodes on QR codes shared as phone screenshots.

zbar alone can't read a QR code with a logo in the middle and no white
margin ("quiet zone") around it -- a WeChat "join group" image cropped
tight, a colored code, a dark page behind it, or a dark page plus a tilt.
decode_barcodes falls back to OpenCV + cleaned-up/straightened copies for
those. Images are generated in memory; no config or storage is touched.
"""
import io
import unittest

import qrcode
from PIL import Image, ImageDraw

import pdf_extraction

URL = "https://weixin.qq.com/g/AQYAAKxyz123ABCdefGHIjkl_testgroup0001"


def _qr(data=URL, box=10, fg=(0, 0, 0), bg=(255, 255, 255), logo=0.26):
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H,
                      box_size=box, border=0)  # no quiet zone
    q.add_data(data)
    q.make()
    im = q.make_image(fill_color=fg, back_color=bg).convert("RGB")
    if logo:
        w = im.size[0]
        s = int(w * logo)
        d = ImageDraw.Draw(im)
        o = (w - s) // 2
        d.rounded_rectangle([o, o, o + s, o + s], radius=s // 5, fill=(7, 193, 96))
    return im


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _decoded(im):
    return [r["data"] for r in pdf_extraction.decode_barcodes(_png(im))]


@unittest.skipUnless(pdf_extraction.barcode_scan_available(), "no barcode decoder installed")
class HardQrScreenshots(unittest.TestCase):
    def test_logo_cropped_to_edge(self):
        self.assertEqual(_decoded(_qr()), [URL])

    def test_colored_code(self):
        self.assertEqual(_decoded(_qr(fg=(20, 90, 60), bg=(220, 240, 225))), [URL])

    def test_dark_background_touching_code(self):
        code = _qr()
        bg = Image.new("RGB", (code.width + 120, code.height + 120), (30, 30, 30))
        bg.paste(code, (60, 60))
        self.assertEqual(_decoded(bg.resize((bg.width * 2 // 3, bg.height * 2 // 3))), [URL])

    def test_dark_background_and_tilted(self):
        code = _qr()
        bg = Image.new("RGB", (code.width + 120, code.height + 120), (30, 30, 30))
        bg.paste(code, (60, 60))
        bg = bg.resize((bg.width * 2 // 3, bg.height * 2 // 3))
        for deg in (5, 15, 25):
            tilted = bg.rotate(deg, expand=True, fillcolor=(30, 30, 30), resample=Image.BICUBIC)
            self.assertEqual(_decoded(tilted), [URL], f"tilt {deg}")

    def test_wechat_style_card_in_full_screenshot(self):
        code = _qr()
        card = Image.new("RGB", (code.width + 20, code.height + 260), (255, 255, 255))
        card.paste(code, (10, 130))
        shot = Image.new("RGB", (1170, 2532), (237, 237, 237))
        shot.paste(card, ((1170 - card.width) // 2, 700))
        self.assertEqual(_decoded(shot), [URL])

    def test_chinese_payload(self):
        text = "加入群聊：DMS 用户交流群 https://example.cn/g/abc"
        self.assertEqual(_decoded(_qr(data=text, box=8)), [text])

    def test_plain_code_still_works(self):
        q = qrcode.QRCode(border=4)
        q.add_data(URL)
        q.make()
        self.assertEqual(_decoded(q.make_image().convert("RGB")), [URL])

    def test_no_code_returns_empty(self):
        self.assertEqual(_decoded(Image.new("RGB", (1200, 1600), (120, 160, 200))), [])


def _linear(fmt_name, value):
    import zxingcpp
    fmt = getattr(zxingcpp.BarcodeFormat, fmt_name)
    arr = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(value, fmt), scale=4)
    return Image.fromarray(__import__("numpy").array(arr)).convert("RGB")


try:
    import zxingcpp  # noqa: F401
    HAVE_ZXING = True
except ImportError:
    HAVE_ZXING = False


@unittest.skipUnless(HAVE_ZXING, "zxing-cpp not installed")
class WithoutZbar(unittest.TestCase):
    """Most users' machines have no zbar system library (the built app only
    finds it if `brew install zbar` was run). zxing-cpp + OpenCV must cover
    both product barcodes and the hard screenshot QR codes on their own."""

    def setUp(self):
        self._orig = pdf_extraction._zbar_decode
        pdf_extraction._zbar_decode = lambda: None

    def tearDown(self):
        pdf_extraction._zbar_decode = self._orig

    def test_still_available(self):
        self.assertTrue(pdf_extraction.barcode_scan_available())

    def test_ean13(self):
        r = pdf_extraction.decode_barcodes(_png(_linear("EAN13", "4006381333931")))
        self.assertEqual([(x["type"], x["data"]) for x in r], [("EAN13", "4006381333931")])

    def test_code128_rotated(self):
        im = _linear("Code128", "WO-2026-00451").rotate(90, expand=True)
        self.assertEqual(_decoded(im), ["WO-2026-00451"])

    def test_hard_qr_cases(self):
        HardQrScreenshots("test_logo_cropped_to_edge").test_logo_cropped_to_edge()
        t = HardQrScreenshots("test_dark_background_and_tilted")
        t.test_dark_background_and_tilted()
        t.test_wechat_style_card_in_full_screenshot()
        t.test_chinese_payload()


if __name__ == "__main__":
    unittest.main()
