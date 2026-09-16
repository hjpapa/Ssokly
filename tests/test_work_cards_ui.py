import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

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

    def test_confirmation_can_be_unchecked_saved_and_restored(self):
        self.panel.confirm_vars['deadline'].set(True)
        self.assertTrue(self.panel.has_unsaved_changes)
        self.assertTrue(self.panel.confirm_selected_fields())
        self.panel.confirm_vars['deadline'].set(False)
        self.panel.refresh()
        self.assertFalse(self.panel.confirm_vars['deadline'].get())
        self.assertTrue(self.panel.has_unsaved_changes)
        self.assertTrue(self.panel.confirm_selected_fields())
        self.assertFalse(self.store.get_card(self.card['id'])['fields']['deadline']['confirmed'])
        self.assertEqual(self.store.get_card(self.card['id'])['version'], 1)

    def test_changing_confirmed_value_requires_new_confirmation(self):
        self.panel.confirm_vars['deadline'].set(True)
        self.panel.confirm_selected_fields()
        self.panel.variables['deadline'].set('')
        self.assertFalse(self.panel.confirm_vars['deadline'].get())
        self.assertTrue(self.panel.save_selected())
        current = self.store.get_card(self.card['id'])
        self.assertEqual(current['deadline'], '')
        self.assertFalse(current['fields']['deadline']['confirmed'])

    def test_conflict_comparison_reapplies_only_explicit_local_changes(self):
        self.panel.variables['deadline'].set('')
        latest = self.store.update_card(self.card['id'], {'owner': '다른 창 담당자'}, 1)
        with patch('ui.work_cards.messagebox.showerror'):
            self.assertFalse(self.panel.save_selected())
        self.assertTrue(self.panel.resolve_conflict({'deadline': 'mine'}, latest))
        current = self.store.get_card(self.card['id'])
        self.assertEqual(current['deadline'], '')
        self.assertEqual(current['owner'], '다른 창 담당자')
        self.assertFalse(self.panel.has_unsaved_changes)

    def test_conflict_comparison_does_not_overwrite_newer_change_after_open(self):
        self.panel.variables['deadline'].set('편집 중인 날짜')
        latest = self.store.update_card(self.card['id'], {'owner': '다른 창'}, 1)
        self.store.update_card(self.card['id'], {'owner': '다시 변경'}, latest['version'])
        self.assertFalse(self.panel.resolve_conflict({'deadline': 'mine'}, latest))
        self.assertEqual(self.panel.variables['deadline'].get(), '편집 중인 날짜')
        self.assertEqual(self.store.get_card(self.card['id'])['owner'], '다시 변경')

    def test_discard_local_changes_loads_latest_and_clears_unsaved_gate(self):
        self.panel.variables['deadline'].set('저장 안 한 값')
        self.panel.refresh()
        self.store.update_card(self.card['id'], {'deadline': '현재 저장값'}, 1)
        with patch('ui.work_cards.messagebox.askyesno', return_value=True):
            self.assertTrue(self.panel.discard_selected())
        self.assertEqual(self.panel.variables['deadline'].get(), '현재 저장값')
        self.assertFalse(self.panel.has_unsaved_changes)

    def test_compact_680_width_has_visible_editors_and_persistent_action_buttons(self):
        from tkinter import ttk
        self.root.deiconify()
        self.root.geometry('680x720')
        self.root.update()
        form = self.panel.canvas.nametowidget(self.panel.canvas.itemcget(1, 'window'))
        entries = [widget for widget in form.winfo_children() if isinstance(widget, ttk.Entry)]
        self.assertEqual(len(entries), 12)
        self.assertTrue(all(widget.winfo_ismapped() for widget in entries))
        self.assertGreater(form.grid_bbox(1, 0)[2], 100)
        self.assertEqual(int(self.panel.editor_frame.grid_info()['row']), 1)
        self.root.geometry('1180x720')
        self.root.update()
        self.assertEqual(int(self.panel.editor_frame.grid_info()['row']), 0)
        self.assertTrue(all(widget.winfo_ismapped() for widget in entries))

    def test_comparison_dialog_opens_without_changing_saved_or_local_values(self):
        self.panel.variables['deadline'].set('비교할 편집값')
        before = self.store.get_card(self.card['id'])
        popup = self.panel.show_conflict_comparison()
        self.assertIn('편집 비교', popup.title())
        self.assertEqual(self.store.get_card(self.card['id']), before)
        self.assertEqual(self.panel.variables['deadline'].get(), '비교할 편집값')
        popup.destroy()


class WorkCardsAppLayoutTests(unittest.TestCase):
    """Tk geometry checks in the actual app; not a native manual visual test."""

    def test_real_app_compact_and_normal_controls_and_unsaved_exit_recovery(self):
        from tkinter import ttk
        from services.capture_store import CaptureStore
        from services.task_store import TaskStore
        from ui.app import SsoklyApp

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        def inside(widget, outer):
            x, y = widget.winfo_rootx(), widget.winfo_rooty()
            ox, oy = outer.winfo_rootx(), outer.winfo_rooty()
            return (widget.winfo_ismapped() and x >= ox and y >= oy
                    and x + widget.winfo_width() <= ox + outer.winfo_width()
                    and y + widget.winfo_height() <= oy + outer.winfo_height())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app = SsoklyApp(task_store=TaskStore(app_data_dir=base),
                           capture_store=CaptureStore(directory=base / 'capture-inbox'))
            try:
                source = '모든 학교는 신청서를 제출한다.\n제출일: 2026. 10. 15.'
                app._replace_ocr_text(source, track_change=True)
                app._sync_work_source()
                card = app.work_cards.merge_analysis(app.document_id, [{
                    'action': '신청서 제출', 'deadline': '2026. 10. 15.',
                    'evidence': source.splitlines()[0], 'field_evidence': {'deadline': source.splitlines()[1]},
                    'issues': ['상세 검수 사유 ' + str(number) for number in range(20)],
                }])[0]
                panel = app.card_panel
                panel.refresh()
                app.notebook.select(app.cards_tab)
                for compact, width, height in ((True, 680, 720), (False, 1180, 720), (True, 680, 720)):
                    with self.subTest(compact=compact):
                        app.set_compact_mode(compact, persist=False)
                        app.geometry(f'{width}x{height}')
                        app.update()
                        # Normal mode enforces the existing 760px minimum.
                        self.assertEqual(app.winfo_width(), width)
                        self.assertEqual(app.winfo_height(), height if compact else 760)
                        form = panel.canvas.nametowidget(panel.canvas.itemcget(1, 'window'))
                        entries = [widget for widget in form.winfo_children() if isinstance(widget, ttk.Entry)]
                        evidence = [widget for widget in form.winfo_children() if isinstance(widget, ttk.Button)]
                        buttons = [widget for widget in descendants(panel) if isinstance(widget, ttk.Button) and widget not in evidence]
                        self.assertEqual(len(entries), 12)
                        self.assertTrue(all(widget.winfo_ismapped() for widget in entries))
                        self.assertEqual(len(buttons), 7)
                        self.assertTrue(all(inside(widget, app) for widget in buttons),
                                        [(widget.cget('text'), widget.winfo_ismapped()) for widget in buttons])
                        panel.canvas.yview_moveto(0)
                        app.update()
                        self.assertTrue(inside(evidence[0], panel.canvas))
                        panel.canvas.yview_moveto(1)
                        app.update()
                        self.assertTrue(inside(evidence[-1], panel.canvas))
                popup = panel.show_evidence('deadline')
                self.assertTrue(popup.winfo_exists())
                popup.destroy()
                panel.variables['deadline'].set('2026. 10. 16.')
                self.assertTrue(panel.save_selected())
                self.assertEqual(app.work_cards.get_card(card['id'])['deadline'], '2026. 10. 16.')
                panel.variables['deadline'].set('저장하지 않은 편집')
                with patch('ui.app.messagebox.showinfo'):
                    app._on_close()
                self.assertFalse(app._closing)
                self.assertTrue(app.winfo_exists())
                self.assertTrue(panel.discard_selected(ask=False))
                with patch('ui.app.messagebox.askyesnocancel', return_value=False), patch('ui.app.messagebox.showwarning'):
                    app._on_close()
                self.assertTrue(app._closing)
            finally:
                if not app._closing:
                    app._closing = True
                    app.destroy()


if __name__ == '__main__':
    unittest.main()
