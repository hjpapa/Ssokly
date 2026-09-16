"""Capture-desk scope regressions, synthetic pixels and temporary policies only."""
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from services.desk_transfer import DeskTransfer
from services.transfer_policy import (
    ScopeExpansionRequired, TransferPolicyStore, make_image_snapshot,
    make_text_snapshot, restore_image_snapshot,
)


class DeskTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TransferPolicyStore(self.temp.name)
        self.desk = DeskTransfer(self.store)
        self.image = Image.new('RGB', (20, 12), (30, 60, 90))
        self.masked = make_image_snapshot(self.image, rectangles=[(2, 3, 8, 9)])

    def test_new_capture_needs_explicit_choice_by_default(self):
        selected = make_image_snapshot(self.image)
        choose = Mock(return_value=selected)
        self.assertIs(self.desk.image_snapshot('one', self.image, choose=choose), selected)
        self.assertEqual(choose.call_args.kwargs['kind'], 'image')
        self.assertIsNone(choose.call_args.kwargs['previous'])
        self.assertEqual(self.store.get('capture:one'), selected.policy)

    def test_opt_in_new_capture_uses_immutable_copy_without_dialog(self):
        choose = Mock(side_effect=AssertionError('should not ask'))
        snapshot = self.desk.image_snapshot('one', self.image, auto_allowed=True, choose=choose)
        self.image.putpixel((0, 0), (255, 255, 255))
        self.assertEqual(snapshot.as_image().getpixel((0, 0)), (30, 60, 90))
        self.assertFalse(snapshot.policy.redacted)

    def test_auto_cannot_replace_existing_mask_even_on_same_image(self):
        self.store.save('capture:one', self.masked.policy)
        choose = Mock(return_value=None)
        self.assertIsNone(self.desk.image_snapshot('one', self.image, auto_allowed=True, choose=choose))
        self.assertEqual(choose.call_args.kwargs['previous'], self.masked.policy)
        self.assertEqual(self.store.get('capture:one'), self.masked.policy)

    def test_changed_same_capture_requires_choice_for_raw_or_masked_policy(self):
        changed = Image.new('RGB', (20, 12), 'white')
        for previous in (make_image_snapshot(self.image).policy, self.masked.policy):
            self.store.save('capture:one', previous)
            choose = Mock(return_value=None)
            self.assertIsNone(self.desk.image_snapshot('one', changed, auto_allowed=True, choose=choose))
            choose.assert_called_once()
            self.assertEqual(self.store.get('capture:one'), previous)

    def test_same_unredacted_capture_can_auto_read(self):
        self.store.save('capture:one', make_image_snapshot(self.image).policy)
        choose = Mock()
        self.desk.image_snapshot('one', self.image, auto_allowed=True, choose=choose)
        choose.assert_not_called()

    def test_document_only_mask_blocks_auto_capture_read(self):
        previous = make_text_snapshot('safe [가림]', excluded_strings=['SYNTHETIC_SECRET']).policy
        self.store.save('document:legacy', previous)
        choose = Mock(return_value=None)
        self.assertIsNone(self.desk.image_snapshot('one', self.image, auto_allowed=True,
                                                  document_ids=['legacy'], choose=choose))
        self.assertEqual(choose.call_args.kwargs['previous'], previous)
        self.assertIsNone(self.store.get('capture:one'))

    def test_capture_and_document_masks_do_not_assume_same_scope(self):
        self.store.save('capture:one', self.masked.policy)
        document = make_text_snapshot('safe [가림]', excluded_strings=['SYNTHETIC_SECRET']).policy
        self.store.save('document:legacy', document)
        choose = Mock(return_value=None)
        self.desk.image_snapshot('one', self.image, auto_allowed=True, document_ids=['legacy'], choose=choose)
        previous = choose.call_args.kwargs['previous']
        self.assertTrue(previous.redacted)
        self.assertFalse(previous.image_rectangles)
        self.assertFalse(previous.approved_text)
        with self.assertRaises(ScopeExpansionRequired):
            previous.guard_text('SYNTHETIC_SECRET', strict=False)

    def test_force_mask_never_silently_sends_unredacted_image(self):
        choose = Mock(return_value=make_image_snapshot(self.image))
        with self.assertRaises(ScopeExpansionRequired):
            self.desk.image_snapshot('one', self.image, auto_allowed=True, force_mask=True, choose=choose)
        self.assertIsNone(self.store.get('capture:one'))

    def test_explicit_mask_keeps_real_black_pixels(self):
        self.store.save('capture:one', self.masked.policy)
        selected = restore_image_snapshot(self.image, self.masked.policy)
        snapshot = self.desk.image_snapshot('one', self.image, auto_allowed=True, choose=Mock(return_value=selected))
        self.assertEqual(snapshot.as_image().getpixel((3, 4)), (0, 0, 0))
        self.assertEqual(snapshot.as_image().getpixel((0, 0)), (30, 60, 90))

    def test_image_dialog_policy_change_rejects_stale_selection(self):
        def choose(**kwargs):
            self.store.save('capture:one', self.masked.policy)
            return make_image_snapshot(self.image)
        with self.assertRaises(ScopeExpansionRequired):
            self.desk.image_snapshot('one', self.image, choose=choose)
        self.assertEqual(self.store.get('capture:one'), self.masked.policy)

    def test_each_text_action_asks_even_when_previously_approved(self):
        first = make_text_snapshot('safe text')
        self.store.save('document:one', first.policy)
        choose = Mock(return_value=make_text_snapshot('safe text'))
        self.desk.text_snapshot('one', 'safe text', choose=choose)
        self.desk.text_snapshot('one', 'safe text', choose=choose)
        self.assertEqual(choose.call_count, 2)

    def test_restored_clipboard_secret_is_presented_with_previous_restriction(self):
        previous = make_text_snapshot('safe [가림]', excluded_strings=['SYNTHETIC_SECRET']).policy
        self.store.save('document:one', previous)
        choose = Mock(return_value=None)
        self.assertIsNone(self.desk.text_snapshot('one', 'safe SYNTHETIC_SECRET', choose=choose))
        self.assertEqual(choose.call_args.kwargs['previous'], previous)
        self.assertEqual(self.store.get('document:one'), previous)

    def test_multiple_page_protections_do_not_union_approved_originals(self):
        one = make_text_snapshot('public A [가림]', excluded_strings=['SECRET_A']).policy
        two = make_text_snapshot('public B [가림]', excluded_strings=['SECRET_B']).policy
        self.store.save('capture:a', one)
        self.store.save('capture:b', two)
        choose = Mock(return_value=None)
        self.desk.text_snapshot('one', 'public A [가림]\npublic B [가림]', page_policies=['a', 'b'], choose=choose)
        previous = choose.call_args.kwargs['previous']
        self.assertEqual(previous.approved_text, '')
        for secret in ('SECRET_A', 'SECRET_B'):
            with self.assertRaises(ScopeExpansionRequired):
                previous.guard_text(secret, strict=False)

    def test_page_policy_change_during_selection_is_rejected(self):
        self.store.save('capture:a', self.masked.policy)
        newer = make_image_snapshot(self.image, rectangles=[(0, 0, 15, 12)])
        def choose(**kwargs):
            self.store.save('capture:a', newer.policy)
            return make_text_snapshot('safe text')
        with self.assertRaises(ScopeExpansionRequired):
            self.desk.text_snapshot('one', 'safe text', page_policies=['a'], choose=choose)
        self.assertIsNone(self.store.get('document:one'))

    def test_further_text_mask_retains_earlier_exclusions(self):
        previous = make_text_snapshot('public A B [가림1]', excluded_strings=['SECRET_A']).policy
        self.store.save('document:one', previous)
        selected = make_text_snapshot('public A [가림2] [가림1]', excluded_strings=['B'])
        result = self.desk.text_snapshot('one', previous.approved_text, choose=Mock(return_value=selected))
        with self.assertRaises(ScopeExpansionRequired):
            result.policy.guard_text('SECRET_A', strict=False)
        with self.assertRaises(ScopeExpansionRequired):
            result.policy.guard_text('B', strict=False)

    def test_ocr_output_registration_is_scope_cas_and_never_approves_raw_restore(self):
        self.store.save('capture:one', self.masked.policy)
        policy = self.desk.record_ocr('one', self.masked, 'safe OCR text')
        self.assertEqual(policy.scope_id, self.masked.policy.scope_id)
        self.assertEqual(policy.approved_text, 'safe OCR text')
        self.assertTrue(policy.redacted)
        newer = make_image_snapshot(self.image, rectangles=[(0, 0, 15, 12)])
        self.store.save('capture:one', newer.policy)
        with self.assertRaises(ScopeExpansionRequired):
            self.desk.record_ocr('one', self.masked, 'late result')
        self.assertEqual(self.store.get('capture:one'), newer.policy)

    def test_ocr_failure_or_missing_policy_never_creates_new_grant(self):
        for text in ('', None):
            with self.assertRaises(ValueError):
                self.desk.record_ocr('one', self.masked, text)
        with self.assertRaises(ScopeExpansionRequired):
            self.desk.record_ocr('one', self.masked, 'orphan result')
        self.assertIsNone(self.store.get('capture:one'))

    def test_wrong_kind_or_policy_store_failure_never_returns_fallback(self):
        with self.assertRaises(ValueError):
            self.desk.image_snapshot('one', self.image, choose=Mock(return_value=make_text_snapshot('text')))
        with patch.object(self.store, 'save', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.desk.image_snapshot('one', self.image, auto_allowed=True, choose=Mock())
        self.assertIsNone(self.store.get('capture:one'))

    def test_inherit_old_document_and_capture_restrictions_before_copy(self):
        old = make_text_snapshot('public [가림]', excluded_strings=['DOCUMENT_SECRET']).policy
        capture = make_text_snapshot('public [가림]', excluded_strings=['CAPTURE_SECRET']).policy
        self.store.save('document:old', old)
        self.store.save('capture:cap', capture)
        policy = self.desk.inherit_document_policy(['old'], ['cap'], 'new')
        self.assertEqual(self.store.get('document:new'), policy)
        self.assertTrue(policy.redacted)
        self.assertEqual(policy.approved_text, '')
        for secret in ('DOCUMENT_SECRET', 'CAPTURE_SECRET'):
            with self.assertRaises(ScopeExpansionRequired):
                policy.guard_text(secret, strict=False)

    def test_inherit_never_relaxes_existing_target_or_creates_raw_consent(self):
        self.assertIsNone(self.desk.inherit_document_policy(['missing'], [], 'empty'))
        self.assertIsNone(self.store.get('document:empty'))
        target = make_text_snapshot('safe', excluded_strings=['TARGET_SECRET']).policy
        self.store.save('document:new', target)
        self.store.save('document:raw', make_text_snapshot('new raw content').policy)
        self.assertEqual(self.desk.inherit_document_policy(['raw'], [], 'new'), target)

    def test_inherit_ancestor_change_during_commit_aborts_copy_permission(self):
        self.store.save('document:old', self.masked.policy)
        newer = make_image_snapshot(self.image, rectangles=[(0, 0, 20, 12)]).policy
        real_save = self.store.save
        def save(key, policy, **kwargs):
            real_save(key, policy, **kwargs)
            if key == 'document:new':
                real_save('document:old', newer)
        with patch.object(self.store, 'save', side_effect=save):
            with self.assertRaises(ScopeExpansionRequired):
                self.desk.inherit_document_policy(['old'], [], 'new')

    def test_inherit_failure_never_grants_success(self):
        self.store.save('document:old', self.masked.policy)
        with patch.object(self.store, 'save', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.desk.inherit_document_policy(['old'], [], 'new')
        self.assertIsNone(self.store.get('document:new'))


if __name__ == '__main__':
    unittest.main()
