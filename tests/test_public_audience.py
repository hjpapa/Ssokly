import unittest
from services.card_outputs import is_public_person, render_current_cards
from services.work_card_store import CARD_FIELDS


def public_card(owner, target='희망 학생', event_date=''):
    values = dict(action='학교 신청서 제출', owner=owner, target=target, condition='희망 학생만',
        obligation='조건부', deadline='2026. 10. 15.', event_date=event_date,
        deliverable='참가 신청서', destination='교육지원청')
    return {'id': 'synthetic-card', 'comparison_candidate': False, 'ai_proposal': {'task_type': '학교 업무'},
        'fields': {name: {'value': values.get(name, ''), 'ai_value': values.get(name, ''),
            'edited': False, 'confirmed': False, 'issues': [],
            'evidence': {'verified': True, 'stale': False}} for name in CARD_FIELDS}}


class PublicAudienceTests(unittest.TestCase):
    def test_staff_and_organizations_are_not_public_actors(self):
        for owner in ('학생부', '학생과학부', '학생가정교육부', '학생지원부장', '학부모지원센터', '학생안전 담당교사', '학생 및 담임', '학부모회', '학생회 담당자'):
            with self.subTest(owner=owner):
                self.assertFalse(is_public_person(owner))
                text = render_current_cards([public_card(owner)], '가정통신문 초안')
                self.assertNotIn('2026. 10. 15.', text)
                self.assertNotIn('교육지원청', text)

    def test_staff_owned_public_event_shows_only_event_and_condition(self):
        text = render_current_cards([public_card('학생부', event_date='2026. 10. 20.')], '학부모 메신저')
        self.assertIn('2026. 10. 20.', text)
        self.assertIn('희망 학생만', text)
        self.assertNotIn('2026. 10. 15.', text)
        self.assertNotIn('교육지원청', text)

    def test_actual_public_roles_keep_reply_details(self):
        for owner in ('학부모', '보호자', '참가 희망 학생', '초등학생', '학생(보호자)'):
            with self.subTest(owner=owner):
                self.assertTrue(is_public_person(owner))
                card = public_card(owner)
                card['fields']['destination']['value'] = '담임에게 회신'
                text = render_current_cards([card], '학부모 메신저')
                self.assertIn('참가 신청서', text)
                self.assertIn('담임에게 회신', text)

    def test_staff_target_is_not_assumed_public(self):
        text = render_current_cards([public_card('학교', target='학생지원부', event_date='2026. 10. 20.')], '가정통신문 초안')
        self.assertNotIn('2026. 10. 20.', text)
        self.assertIn('발송 전 확인', text)


if __name__ == '__main__':
    unittest.main()
