import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from services.work_card_store import WorkCardStore
from ui.work_cards import WorkCardsPanel


class WorkCardsPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = WorkCardStore(self.directory.name)
        self.store.ensure_document('synthetic', '신청서를 제출한다.\n2026. 10. 15.')
        self.card = self.store.merge_analysis('synthetic', [{
            'action': '신청서 제출', 'deadline': '2026. 10. 15.',
            'evidence': '신청서를 제출한다.', 'field_evidence': {'deadline': '2026. 10. 15.'},
        }])[0]
        self.root = tk.Tk()
        self.root.withdraw()
        self.on_changed = Mock()
        self.on_todo = Mock()
        self.on_generate = Mock()
        self.panel = WorkCardsPanel(self.root, self.store, lambda: 'synthetic',
                                    self.on_changed, self.on_todo, self.on_generate)
        self.panel.pack(fill='both', expand=True)
        self.panel.refresh()
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.directory.cleanup()

    def test_a01_card_edit_save_and_callbacks_use_current_values(self):
        self.panel.variables['deadline'].set('2026. 10. 16.')
        self.assertTrue(self.panel.has_unsaved_changes)
        self.assertTrue(self.panel.save_selected())
        self.assertEqual(self.on_changed.call_args.args[0]['deadline'], '2026. 10. 16.')
        self.panel.add_selected_todo()
        self.assertEqual(self.on_todo.call_args.args[0]['deadline'], '2026. 10. 16.')
        self.panel.generate_current()
        self.on_generate.assert_called_once_with('업무 일정·체크리스트')

    def test_r02_failed_save_and_refresh_keep_explicit_empty_editor(self):
        self.panel.variables['deadline'].set('')
        with patch.object(self.store, 'update_card', side_effect=OSError('synthetic save failure')), patch('ui.work_cards.messagebox.showerror'):
            self.assertFalse(self.panel.save_selected())
        self.panel.refresh()
        self.assertEqual(self.panel.variables['deadline'].get(), '')
        self.assertTrue(self.panel.has_unsaved_changes)
        self.assertTrue(self.panel.save_selected())
        self.assertTrue(self.store.get_card(self.card['id'])['fields']['deadline']['edited'])
        self.assertEqual(self.store.get_card(self.card['id'])['deadline'], '')

    def test_a08_late_refresh_keeps_editor_and_version_conflict_is_reported(self):
        self.panel.variables['deadline'].set('교사 편집 중')
        self.store.update_card(self.card['id'], {'owner': '다른 창'}, 1)
        self.panel.refresh()
        self.assertEqual(self.panel.variables['deadline'].get(), '교사 편집 중')
        with patch('ui.work_cards.messagebox.showerror') as notice:
            self.assertFalse(self.panel.save_selected())
            notice.assert_called_once()
        self.assertEqual(self.panel.variables['deadline'].get(), '교사 편집 중')
        self.assertEqual(self.store.get_card(self.card['id'])['deadline'], '2026. 10. 15.')

    def test_a06_evidence_popup_contains_actual_quote_and_source_version(self):
        popup = self.panel.show_evidence('deadline')
        texts = [child for child in popup.winfo_children() if isinstance(child, tk.Frame)]
        text = next(child for frame in texts for child in frame.winfo_children() if isinstance(child, tk.Text))
        content = text.get('1.0', 'end-1c')
        self.assertIn('2026. 10. 15.', content)
        self.assertIn('버전 1', content)
        self.assertIn('추출 텍스트 2행', content)
        self.assertIn('원본 페이지 번호 아님', content)
        popup.destroy()

    def test_confirmation_does_not_confirm_other_fields(self):
        self.panel.confirm_vars['deadline'].set(True)
        self.assertTrue(self.panel.confirm_selected_fields())
        fields = self.store.get_card(self.card['id'])['fields']
        self.assertTrue(fields['deadline']['confirmed'])
        self.assertFalse(fields['owner']['confirmed'])


if __name__ == '__main__':
    unittest.main()
