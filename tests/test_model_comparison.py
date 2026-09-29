"""Offline safeguards for the explicitly opt-in model comparison."""
import unittest

from tools.compare_ai_models import CONFIGS, aggregate, completed_text, edit_distance, normalized, request_body, safe_usage


class ModelComparisonTests(unittest.TestCase):
    def test_incomplete_comparison_cannot_produce_success_metrics(self):
        for rows in ([], [{'completed': False}], [{'completed': True, 'configuration': 'nano', 'case': '01', 'operation': 'ocr'}]):
            with self.assertRaises(ValueError):
                aggregate({'results': rows})

    def test_comparison_changes_only_model_and_effort(self):
        for operation in ('ocr', 'summary'):
            bodies = [request_body(config, operation, 'synthetic-input') for config in CONFIGS]
            reference = {k: v for k, v in bodies[0].items() if k not in ('model', 'reasoning')}
            for body in bodies:
                self.assertFalse(body['store'])
                self.assertEqual({k: v for k, v in body.items() if k not in ('model', 'reasoning')}, reference)
            self.assertEqual([b['reasoning']['effort'] for b in bodies],
                             ['minimal' if operation == 'ocr' else 'low', 'none', 'low'])

    def test_incomplete_refused_and_empty_are_not_success(self):
        for data in ({'status': 'incomplete'}, {'status': 'completed', 'output': []},
                     {'status': 'completed', 'output': [{'type': 'message', 'status': 'completed',
                                                       'content': [{'type': 'refusal'}]}]}):
            with self.assertRaises(ValueError):
                completed_text(data)

    def test_usage_preserves_reasoning_as_subset_of_output(self):
        usage = safe_usage({'usage': {'input_tokens': 100, 'output_tokens': 30, 'total_tokens': 130,
                                     'input_tokens_details': {'cached_tokens': 20},
                                     'output_tokens_details': {'reasoning_tokens': 10}}, 'secret': 'excluded'})
        self.assertEqual(usage['total_tokens'], 130)
        self.assertEqual(usage['output_tokens'], 30)
        self.assertEqual(usage['reasoning_tokens'], 10)
        self.assertNotIn('secret', usage)
        self.assertIsNone(safe_usage({})['total_tokens'])

    def test_character_metric_ignores_layout_but_catches_known_errors(self):
        self.assertEqual(normalized('낮 | 12시'), normalized('낮\t12시'))
        self.assertEqual(edit_distance(normalized('낮 12시'), normalized('낳 12시')), 1)
        self.assertGreater(edit_distance(normalized('행사 일시 | 결과 보고'), normalized('행사 시니 결과 보고')), 0)
        self.assertEqual(edit_distance('', '123'), 3)


if __name__ == '__main__':
    unittest.main()
