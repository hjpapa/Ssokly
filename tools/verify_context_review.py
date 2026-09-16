"""Opt-in paid API check with synthetic school notices only (3-5 requests)."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.ai_service import analyze_document_task
from services.analysis_document import Action, AnalysisDocument, inspect_action, verify_evidence
from services.analysis_review import repair_actions
from services.personal_todos import checklist_items


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--case', choices=('all', '1', '2', 'repair'), default='all')
    args = parser.parse_args()
    if not args.live:
        parser.error('Actual API calls require --live; only synthetic text is used.')
    sources = [
        '과학행사 운영 안내\n학교는 과학행사 참가 신청서를 제출한다.\n'
        '과학행사 신청 기한: 2026. 10. 2.(금) 16:00까지\n'
        '과학행사 신청서 제출처: 교육지원청\n교육지원청은 참가 명단을 안내한다.\n'
        '별도 미술행사 참고: 미술행사 신청 기한은 2026. 10. 9.(금) 18:00까지이다.',
        '도서관 이용 안내\n담임은 학생에게 도서관 이용 수칙을 안내한다.\n'
        '학교에서 제출해야 하는 서류는 없으며 제출처나 별도 마감일은 정하지 않는다.'
    ]
    with tempfile.TemporaryDirectory() as directory:
        for i, source in enumerate(sources):
            if args.case not in ('all', str(i+1)):
                continue
            metrics = []
            result = analyze_document_task(source, cache_dir=Path(directory), raise_errors=True, on_metrics=metrics.append)
            items = checklist_items(result)
            assert items, result
            if i == 0:
                science = [item for item in items if '과학' in item]
                assert science and any('2026. 10. 2.(금) 16:00까지' in item and '교육지원청' in item for item in science), result
                assert all('10. 9.' not in item and '명단 안내' not in item for item in science), result
            else:
                assert all('제출처:' not in item and '기한:' not in item for item in items), result
            assert '근거에서 해당 값을 확인하지 못했습니다' not in result
            assert '## 확인할 사항' not in result, result
            print(json.dumps({'case': i+1, 'passed': True, 'checklist_count': len(items), 'unnecessary_warnings': 0, 'metrics': metrics}, ensure_ascii=True), flush=True)
    if args.case not in ('all', 'repair'):
        return
    # Deliberately wrong deadline to exercise the actual selective repair API.
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    item = Action(action='과학행사 참가 신청서 제출', owner='학교', deadline='2026-10-09',
                  deliverable='신청서', destination='교육지원청', evidence=sources[0].splitlines()[1], kind='명시된 의무')
    doc = AnalysisDocument(title='과학행사', summary='', actions=[item], questions=[], message='')
    with OpenAI() as client:
        repaired, status = repair_actions(client, doc, sources[0], 'gpt-5-nano', {'reasoning': {'effort': 'minimal'}}, lambda: None)
    assert status == 'completed' and not inspect_action(repaired.actions[0], sources[0]), status
    assert '10. 2.' in verify_evidence(repaired, sources[0]).actions[0].deadline, repaired.actions[0].deadline
    print(json.dumps({'selective_repair_passed': True}), flush=True)


if __name__ == '__main__':
    main()
