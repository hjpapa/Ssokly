"""Windows OCR. No implicit network fallback or synthetic confidence score."""
import asyncio
import sys

def extract_local_text(image, language="ko-KR"):
    if sys.platform != "win32":
        raise RuntimeError("기본 OCR은 Windows에서 사용할 수 있습니다. 정밀 OCR을 선택해 주세요.")
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
        from winrt.windows.storage.streams import DataWriter
    except ImportError as exc:
        raise RuntimeError("Windows OCR 패키지가 없습니다. requirements.txt를 설치하거나 정밀 OCR을 선택해 주세요.") from exc
    async def recognize():
        engine = OcrEngine.try_create_from_language(Language(language))
        if engine is None:
            raise RuntimeError("Windows 한국어 OCR 언어 기능이 없습니다. 언어 기능을 설치하거나 정밀 OCR을 선택해 주세요.")
        if max(image.size) > OcrEngine.max_image_dimension:
            raise RuntimeError("Windows OCR 최대 이미지 크기를 초과했습니다. 영역을 나누거나 정밀 OCR을 선택해 주세요.")
        rgba = image.convert("RGBA")
        # Composite transparency on white, preserving dark characters.
        from PIL import Image
        pixels = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
        with DataWriter() as writer:
            writer.write_bytes(pixels.tobytes("raw", "BGRA"))
            buffer = writer.detach_buffer()
        with SoftwareBitmap.create_copy_with_alpha_from_buffer(buffer, BitmapPixelFormat.BGRA8,
                image.width, image.height, BitmapAlphaMode.IGNORE) as bitmap:
            result = await engine.recognize_async(bitmap)
        text = "\n".join(line.text for line in result.lines)
        if not text.strip():
            raise RuntimeError("기본 OCR에서 글자를 찾지 못했습니다. 영역을 확대하거나 정밀 OCR을 선택해 주세요.")
        return text
    return asyncio.run(recognize())
