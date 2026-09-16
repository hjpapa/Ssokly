"""Prior mask drawing is separate from combined transmission permission."""
from hashlib import sha256
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from services.transfer_policy import TransferPolicy, make_image_snapshot
from ui.transfer_dialog import TransferDialog, choose_transfer


class DeskMaskRestoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.image = Image.new('RGB', (30, 20), (40, 80, 120))
        self.image_policy = make_image_snapshot(self.image, rectangles=[(2, 3, 15, 12)]).policy
        self.combined = TransferPolicy('combined-synthetic', 'text', True, approved_text='',
            protected=((6, sha256(b'SECRET').hexdigest()),))

    def dialog(self, *, image=None, previous=True):
        dialog = TransferDialog(self.root, kind='image', image=image or self.image,
            previous=self.combined if previous else None, image_previous=self.image_policy)
        dialog.window.withdraw()
        return dialog

    def test_restore_preview_does_not_replace_combined_scope(self):
        dialog = self.dialog()
        self.assertEqual(dialog.rectangles, list(self.image_policy.image_rectangles))
        self.assertEqual(dialog.safe_image.getpixel((3, 4)), (0, 0, 0))
        self.assertEqual(dialog.safe_image.getpixel((0, 0)), (40, 80, 120))
        self.assertIs(dialog.previous, self.combined)
        dialog.start_masking()
        selected = make_image_snapshot(self.image, rectangles=dialog.rectangles)
        self.assertTrue(dialog._expands_previous(selected))

    def test_restored_mask_still_requires_explicit_combined_scope_confirmation(self):
        dialog = self.dialog()
        dialog.start_masking()
        with patch('ui.transfer_dialog.messagebox.askyesno', return_value=False) as confirm:
            dialog.accept_redacted()
        confirm.assert_called_once()
        self.assertIsNone(dialog.result)
        self.assertTrue(dialog.window.winfo_exists())
        with patch('ui.transfer_dialog.messagebox.askyesno', return_value=True) as confirm:
            dialog.accept_redacted()
        confirm.assert_called_once()
        self.assertTrue(dialog.result.policy.redacted)
        self.assertEqual(dialog.result.as_image().getpixel((3, 4)), (0, 0, 0))

    def test_changed_original_rejects_visual_hint_even_if_same_dimensions(self):
        dialog = self.dialog(image=Image.new('RGB', self.image.size, 'white'))
        self.assertEqual(dialog.rectangles, [])
        self.assertEqual(dialog.safe_image.getpixel((3, 4)), (255, 255, 255))
        self.assertIs(dialog.previous, self.combined)

    def test_direct_unmasked_send_is_not_allowed_without_confirmation(self):
        dialog = self.dialog()
        with patch('ui.transfer_dialog.messagebox.askyesno', return_value=False) as confirm:
            dialog.accept_direct()
        confirm.assert_called_once()
        self.assertIsNone(dialog.result)

    def test_hint_alone_cannot_show_a_mask_then_silently_send_unmasked_original(self):
        dialog = self.dialog(previous=False)
        self.assertIs(dialog.previous, self.image_policy)
        with patch('ui.transfer_dialog.messagebox.askyesno', return_value=False) as confirm:
            dialog.accept_direct()
        confirm.assert_called_once()
        self.assertIsNone(dialog.result)

    def test_choose_transfer_forwards_optional_hint_without_changing_previous(self):
        parent = Mock()
        with patch('ui.transfer_dialog.TransferDialog') as constructor:
            constructor.return_value.result = None
            choose_transfer(parent, kind='image', image=self.image, previous=self.combined,
                            image_previous=self.image_policy)
        self.assertIs(constructor.call_args.kwargs['previous'], self.combined)
        self.assertIs(constructor.call_args.kwargs['image_previous'], self.image_policy)
        parent.wait_window.assert_called_once_with(constructor.return_value.window)


if __name__ == '__main__':
    unittest.main()
