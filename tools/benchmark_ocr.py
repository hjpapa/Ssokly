"""Synthetic-only smoke benchmark. --cloud explicitly enables paid API calls."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from services.local_ocr import extract_local_text
from services.ocr_service import extract_text_from_image

TEXT = "자료 제출 안내\n담임은 9월 30일까지 계획서를 교무실에 제출한다.\n제출 대상: 3학년 / 제출 부수: 2부"

def character_error_rate(expected, actual):
    expected, actual = "".join(expected.split()), "".join(actual.split())
    row = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        next_row = [i]
        for j, right in enumerate(actual, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j-1] + (left != right)))
        row = next_row
    return round(row[-1] / max(1, len(expected)), 4)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--models", nargs="+", default=["gpt-5-nano", "gpt-4.1-mini"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    image = Image.new("RGB", (1600, 360), "white")
    ImageDraw.Draw(image).multiline_text((30, 35), TEXT,
        font=ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 36), fill="black", spacing=20)
    engines = [("Windows", lambda: extract_local_text(image))]
    if args.cloud:
        engines.extend((model, lambda m=model: extract_text_from_image(image, model_override=m, raise_errors=True)) for model in args.models)
    results = []
    for name, call in engines:
        start = time.perf_counter()
        try:
            text = call()
            result = {"engine": name, "seconds": round(time.perf_counter()-start, 3),
                      "cer_without_whitespace": character_error_rate(TEXT, text), "text": text}
        except Exception as exc:
            result = {"engine": name, "seconds": round(time.perf_counter()-start, 3), "error_type": type(exc).__name__}
        results.append(result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"synthetic": True, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
