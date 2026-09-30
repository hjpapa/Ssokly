"""Regenerate the server's fixed prompt bundle after changing desktop prompts."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.ocr_service import OCR_PROMPT
from services.text_actions import MODES, AUDIENCES, build_instructions
from services.work_image import PROMPT


def bundle():
    return {'ocr': OCR_PROMPT, 'work_image': PROMPT, 'actions': {
        mode: {audience: build_instructions(mode, audience) for audience in AUDIENCES}
        for mode in MODES}}


if __name__ == '__main__':
    (ROOT / 'backend' / 'prompts.json').write_text(
        json.dumps(bundle(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
