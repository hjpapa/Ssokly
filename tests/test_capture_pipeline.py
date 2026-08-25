import base64
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mss.screenshot import ScreenShot
from PIL import Image

from services.capture_service import capture_region
from services.capture_store import CaptureStore, PNG_CAPTURE_COMPRESS_LEVEL
from services.ocr_service import _image_to_data_url, _prepare_image_for_model


class CapturePipelineTestCase(unittest.TestCase):
    def test_raw_bgrx_capture_conversion_preserves_rgb_pixels(self) -> None:
        screenshot = ScreenShot.from_size(
            bytearray(
                [
                    30,
                    20,
                    10,
                    255,
                    60,
                    50,
                    40,
                    255,
                ]
            ),
            2,
            1,
        )
        capture_context = mock.MagicMock()
        capture_context.__enter__.return_value.grab.return_value = screenshot

        with mock.patch(
            "services.capture_service.mss.MSS",
            return_value=capture_context,
        ):
            image = capture_region((5, 7, 7, 8))

        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (2, 1))
        self.assertEqual(list(image.getdata()), [(10, 20, 30), (40, 50, 60)])
        capture_context.__enter__.return_value.grab.assert_called_once_with(
            {"left": 5, "top": 7, "width": 2, "height": 1}
        )

    def test_capture_store_uses_balanced_lossless_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory=Path(directory))
            image = Image.new("RGB", (4, 3))
            image.putdata(
                [
                    (x * 30, y * 50, (x + y) * 20)
                    for y in range(3)
                    for x in range(4)
                ]
            )
            original_pixels = list(image.getdata())

            record = store.save(image, source="공문")

            self.assertEqual(PNG_CAPTURE_COMPRESS_LEVEL, 3)
            self.assertEqual(list(image.getdata()), original_pixels)
            with Image.open(record.path) as saved:
                self.assertEqual(saved.mode, "RGB")
                self.assertEqual(list(saved.getdata()), original_pixels)

    def test_ocr_png_data_url_round_trips_without_pixel_loss(self) -> None:
        image = Image.new("RGB", (2, 2))
        image.putdata(
            [(1, 2, 3), (10, 20, 30), (40, 50, 60), (200, 210, 220)]
        )

        data_url = _image_to_data_url(image)
        prefix, encoded = data_url.split(",", 1)

        self.assertEqual(prefix, "data:image/png;base64")
        with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
            self.assertEqual(decoded.mode, "RGB")
            self.assertEqual(list(decoded.getdata()), list(image.getdata()))

    def test_common_rgb_ocr_image_avoids_an_unneeded_full_copy(self) -> None:
        image = Image.new("RGB", (1200, 1000), "white")

        prepared = _prepare_image_for_model(image)

        self.assertIs(prepared, image)


if __name__ == "__main__":
    unittest.main()
