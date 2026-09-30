"""Two synthetic actor/unknown-information cases; --live explicitly calls relay."""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.official_document_cases import CASES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--direct', action='store_true', help='Use a previously authorized local key instead of the relay.')
    parser.add_argument('--output', type=Path, default=ROOT / '.local-results/task-quality-20260930.json')
    args = parser.parse_args()
    if not args.live:
        print('Plan: synthetic cases 02 and 06, task organization, two relay calls; no calls made.')
        return
    if args.output.exists():
        raise SystemExit('Choose a new output path to avoid overwriting paid results.')
    from services.ai_relay import server_url
    from services.text_actions import generate_text_action
    if not args.direct:
        assert server_url() == 'https://ssokly-ai-relay.vercel.app'
    report = {'synthetic_only': True, 'transport': 'direct' if args.direct else 'relay', 'attempts': 0, 'results': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    context = patch('services.ai_relay.server_url', return_value='') if args.direct else patch('services.text_actions.load_dotenv', side_effect=AssertionError('Local key forbidden'))
    with context:
        for case in (CASES[1], CASES[5]):
            row = {'case': case['id'], 'completed': False}
            report['results'].append(row)
            report['attempts'] += 1
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            start = time.perf_counter()
            try:
                row['text'] = generate_text_action(case['text'], '일정·할 일 정리', '교직원')
                row['completed'] = True
            except Exception as error:
                row['error_type'] = type(error).__name__
                raise
            finally:
                row['seconds'] = round(time.perf_counter() - start, 3)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
