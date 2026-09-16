import base64
from contextlib import closing
from dataclasses import FrozenInstanceError
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from services.transfer_policy import (
    ScopeExpansionRequired, TransferPolicy, TransferPolicyStore, TransferSnapshot,
    make_file_snapshot, make_image_snapshot, make_text_snapshot, redact_text,
    restore_image_snapshot, risk_candidates,
)


class TransferPolicyTests(unittest.TestCase):
    def test_text_mask_changes_actual_copy_and_keeps_original(self):
        original = "학생 이름: 합성학생 / 기한 2026-10-15"
        start = original.index("합성학생")
        snapshot = redact_text(original, [(start, start + len("합성학생"))])
        self.assertIn("합성학생", original)
        self.assertNotIn("합성학생", snapshot.text)
        self.assertIn("[가림1]", snapshot.text)
        self.assertIn("2026-10-15", snapshot.text)
        self.assertFalse(snapshot.file_bytes)
        self.assertFalse(snapshot.image_png)
        self.assertTrue(snapshot.policy.redacted)

    def test_unique_mask_tokens_preserve_dates_and_roles(self):
        original = "담당 가상가 / 대상 가상나 / 2026-10-15"
        spans = [(original.index(value), original.index(value) + len(value)) for value in ("가상가", "가상나")]
        snapshot = redact_text(original, spans)
        self.assertEqual(snapshot.text, "담당 [가림1] / 대상 [가림2] / 2026-10-15")

    def test_protected_information_stays_blocked_after_serialization(self):
        snapshot = make_text_snapshot("담당 [가림1]", excluded_strings=["합성비밀가"])
        serialized = json.dumps(snapshot.to_policy_dict(), ensure_ascii=False)
        self.assertNotIn("합성비밀가", serialized)
        policy = TransferPolicy.from_dict(json.loads(serialized))
        for text in ("합성비밀가", "합성 비밀가", "[가림1] 안내: 합성비밀가"):
            with self.subTest(text=text), self.assertRaises(ScopeExpansionRequired):
                policy.guard_text(text, strict=False)

    def test_known_secret_cannot_be_registered_as_safe_ocr(self):
        policy = make_text_snapshot("[가림1]", excluded_strings=["합성비밀가"]).policy
        with self.assertRaises(ScopeExpansionRequired):
            policy.with_safe_text("합성비밀가")

    def test_restored_manual_value_requires_new_scope(self):
        snapshot = make_image_snapshot(Image.new("RGB", (10, 10), "white"), rectangles=[(1, 1, 5, 5)])
        policy = snapshot.with_safe_text("대상 희망 학급 / 기한 2026-10-15").policy
        policy.guard_fields(["희망 학급", "2026-10-15"])
        with self.assertRaises(ScopeExpansionRequired):
            policy.guard_fields(["새로운학생"])
        with self.assertRaises(ScopeExpansionRequired):
            policy.guard_fields(["2026-10-16"])
        # A fresh explicit selection can intentionally expand the scope.
        expanded = make_text_snapshot("대상 새로운학생 / 기한 2026-10-16")
        expanded.guard_text(expanded.text)
        self.assertNotEqual(expanded.policy.scope_id, policy.scope_id)

    def test_image_without_ocr_cannot_implicitly_approve_followup_text(self):
        snapshot = make_image_snapshot(Image.new("RGB", (4, 4)), rectangles=[(0, 0, 1, 1)])
        with self.assertRaises(ScopeExpansionRequired):
            snapshot.guard_text("나중에 복원한 입력")

    def test_ordinary_unmasked_edits_are_not_blanket_blocked(self):
        snapshot = make_text_snapshot("담당자 연락처: 010-1234-5678")
        snapshot.guard_text("담당자 연락처: 010-1111-2222")
        self.assertFalse(snapshot.policy.redacted)

    def test_invalid_text_ranges_fail_closed(self):
        for spans in ([], [(-1, 2)], [(2, 2)], [(0, 8)], [(0, 3), (2, 4)]):
            with self.subTest(spans=spans), self.assertRaises(ValueError):
                redact_text("abcde", spans)

    def test_incomplete_duplicate_redaction_does_not_send_secret(self):
        with self.assertRaises(ScopeExpansionRequired):
            redact_text("합성비밀가 / 합성비밀가", [(0, 5)])

    def test_editing_transmission_copy_tracks_removed_value(self):
        snapshot = make_text_snapshot("참가자 [가림1]", original_text="참가자 TEST_SECRET_729")
        with self.assertRaises(ScopeExpansionRequired):
            snapshot.guard_text("TEST_SECRET_729", strict=False)

    def test_image_mask_is_opaque_pixels_not_overlay(self):
        original = Image.new("RGBA", (12, 10), (210, 100, 50, 128))
        snapshot = make_image_snapshot(original, rectangles=[(2, 3, 7, 8)])
        masked = snapshot.as_image()
        self.assertEqual(masked.mode, "RGB")
        self.assertEqual(original.getpixel((3, 4)), (210, 100, 50, 128))
        for y in range(3, 8):
            for x in range(2, 7):
                self.assertEqual(masked.getpixel((x, y)), (0, 0, 0))
        control = make_image_snapshot(original).as_image()
        self.assertEqual(masked.getpixel((1, 3)), control.getpixel((1, 3)))
        self.assertEqual(masked.getpixel((7, 8)), control.getpixel((7, 8)))

    def test_image_metadata_and_mutable_original_do_not_leak(self):
        original = Image.new("RGB", (20, 20), "white")
        original.info["comment"] = "TEST_HIDDEN_SECRET"
        snapshot = make_image_snapshot(original, rectangles=[(0, 0, 5, 5)])
        original.paste("red", (10, 10, 20, 20))
        self.assertNotIn(b"TEST_HIDDEN_SECRET", snapshot.image_png)
        self.assertEqual(snapshot.as_image().getpixel((15, 15)), (255, 255, 255))
        first = snapshot.as_image()
        first.paste("red", (0, 0, 20, 20))
        self.assertEqual(snapshot.as_image().getpixel((1, 1)), (0, 0, 0))

    def test_invalid_image_ranges_fail_closed(self):
        image = Image.new("RGB", (10, 10))
        for rect in ((1, 1, 1, 3), (-1, 0, 3, 3), (0, 0, 11, 3), (0.1, 0, 3, 3), (0, 1, 2), (True, 0, 3, 3)):
            with self.subTest(rect=rect), self.assertRaises(ValueError):
                make_image_snapshot(image, rectangles=[rect])

    def test_image_mask_reopens_on_same_pixels_only(self):
        original = Image.new("RGB", (10, 10), "white")
        first = make_image_snapshot(original, rectangles=[(1, 1, 4, 4)])
        policy = TransferPolicy.from_dict(first.to_policy_dict())
        restored = restore_image_snapshot(original, policy)
        self.assertEqual(restored.image_png, first.image_png)
        self.assertEqual(restored.policy.scope_id, first.policy.scope_id)
        changed = original.copy()
        changed.putpixel((9, 9), (255, 0, 0))
        with self.assertRaises(ScopeExpansionRequired):
            restore_image_snapshot(changed, policy)

    def test_p03_network_boundary_uses_actual_masked_pixels(self):
        from services.ocr_service import extract_text_from_image
        snapshot = make_image_snapshot(Image.new("RGB", (1000, 1000), (220, 230, 240)),
                                       rectangles=[(100, 100, 200, 200)])
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(status="completed", output_text="가린 이미지 결과")
        with patch("services.ocr_service._load_openai_settings", return_value=("synthetic-key", "gpt-5-nano")), patch("openai.OpenAI", return_value=client):
            extract_text_from_image(snapshot.as_image(), raise_errors=True)
        request = client.responses.create.call_args.kwargs
        content = request["input"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_image"])
        transmitted = Image.open(BytesIO(base64.b64decode(content[1]["image_url"].split(",", 1)[1])))
        self.assertEqual(transmitted.getpixel((150, 150)), (0, 0, 0))
        self.assertEqual(transmitted.getpixel((250, 250)), (220, 230, 240))

    def test_file_snapshot_is_immutable_and_masked_text_has_no_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pdf"
            path.write_bytes(b"%PDF synthetic original")
            snapshot = make_file_snapshot(path)
            path.write_bytes(b"%PDF later change")
            self.assertEqual(snapshot.file_bytes, b"%PDF synthetic original")
            self.assertEqual(snapshot.file_name, "synthetic.pdf")
            masked = make_text_snapshot("safe selected page text", original_text="safe selected page text SECRET")
            self.assertFalse(masked.file_bytes)
            self.assertFalse(masked.image_png)
            self.assertEqual(masked.kind, "text")

    def test_snapshot_rejects_mixed_original_attachment(self):
        policy = make_text_snapshot("safe").policy
        with self.assertRaises(ValueError):
            TransferSnapshot("text", policy, text="safe", file_bytes=b"hidden original")
        with self.assertRaises(ValueError):
            TransferSnapshot("image", policy)
        with self.assertRaises(FrozenInstanceError):
            make_text_snapshot("safe").text = "original"

    def test_p01_ordinary_business_contacts_have_no_candidate_popup(self):
        self.assertEqual(risk_candidates("담당자: 홍길동 장학사\n업무 문의: 010-1234-5678 / office@example.test"), [])

    def test_p02_sensitive_candidates_are_local_contextual_hints(self):
        text = "학생 이름: 합성학생\n학부모 개인 연락처 010-1234-5678\n학생 합성학생 상담 내용: 합성 주의\n비밀번호: SYNTHETIC_ONLY\n계좌번호: 123-456-789012"
        categories = {item.category for item in risk_candidates(text)}
        self.assertTrue({"학생·학부모 식별", "개인 연락처", "건강·상담 문맥", "인증정보", "금융정보"} <= categories)

    def test_policy_store_reopen_preserves_restrictions_without_plain_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TransferPolicyStore(directory)
            snapshot = make_text_snapshot("담당 [가림1]", excluded_strings=["SECRET_TEST_PERSON"])
            store.save("document:synthetic", snapshot.policy)
            restored = TransferPolicyStore(directory).get("document:synthetic")
            self.assertEqual(restored, snapshot.policy)
            self.assertIsNone(store.get("capture:missing"))
            with self.assertRaises(ScopeExpansionRequired):
                restored.guard_text("SECRET_TEST_PERSON", strict=False)
            self.assertNotIn(b"SECRET_TEST_PERSON", store.path.read_bytes())

    def test_corrupt_policy_fails_closed_without_erasing_saved_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TransferPolicyStore(directory)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("INSERT INTO transfer_policies VALUES (?, ?)", ("document:broken", "bad json"))
            with self.assertRaises(ValueError):
                store.get("document:broken")
            with closing(sqlite3.connect(store.path)) as connection, connection:
                value = connection.execute("SELECT policy_json FROM transfer_policies").fetchone()[0]
            self.assertEqual(value, "bad json")

    def test_policy_compare_and_swap_preserves_newer_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TransferPolicyStore(directory)
            original = make_text_snapshot("original").policy
            newer = make_text_snapshot("[가림1]", excluded_strings=["original"]).policy
            store.save("document:cas", original, expected_scope_id=None)
            store.save("document:cas", newer, expected_scope_id=original.scope_id)
            with self.assertRaises(ScopeExpansionRequired):
                store.save("document:cas", original.with_safe_text("late OCR"), expected_scope_id=original.scope_id)
            self.assertEqual(store.get("document:cas"), newer)
            with self.assertRaises(ScopeExpansionRequired):
                store.save("document:cas", original, expected_scope_id=None)
            updated = newer.with_safe_text("safe OCR")
            store.save("document:cas", updated, expected_scope_id=newer.scope_id)
            self.assertEqual(store.get("document:cas"), updated)

    def test_new_mask_tokens_do_not_count_as_scope_expansion(self):
        snapshot = make_text_snapshot("담당 [가림1] / 날짜 2026-10-15", excluded_strings=["합성학생"])
        snapshot.guard_text("담당 [가림1] / 날짜 [가림2]")

    def test_redacted_dialog_detects_restored_text_and_reduced_pixel_masks(self):
        from ui.transfer_dialog import TransferDialog
        dialog = TransferDialog.__new__(TransferDialog)
        dialog.previous = make_text_snapshot("담당 [가림1] / 기한 2026-10-15", excluded_strings=["SECRET"]).policy
        self.assertTrue(dialog._expands_previous(make_text_snapshot("담당 SECRET / 기한 [가림1]")))
        self.assertFalse(dialog._expands_previous(make_text_snapshot("담당 [가림1] / 기한 [가림2]")))
        original = Image.new("RGB", (10, 10), "white")
        dialog.previous = make_image_snapshot(original, rectangles=[(1, 1, 5, 5)]).policy
        self.assertFalse(dialog._expands_previous(make_image_snapshot(original, rectangles=[(1, 1, 3, 5), (3, 1, 5, 5)])))
        self.assertTrue(dialog._expands_previous(make_image_snapshot(original, rectangles=[(1, 1, 3, 5)])))

    def test_rejecting_expansion_keeps_dialog_open_without_selected_payload(self):
        from ui.transfer_dialog import TransferDialog
        dialog = TransferDialog.__new__(TransferDialog)
        dialog.window = Mock()
        dialog.kind, dialog.masking = "image", True
        dialog.original_image = Image.new("RGB", (10, 10), "white")
        dialog.previous = make_image_snapshot(dialog.original_image, rectangles=[(1, 1, 5, 5)]).policy
        dialog.rectangles = [(7, 7, 9, 9)]
        dialog.canvas = Mock()
        dialog.canvas.winfo_exists.return_value = True
        dialog.result = None
        with patch("ui.transfer_dialog.messagebox.askyesno", return_value=False) as confirm:
            dialog.accept_redacted()
        confirm.assert_called_once()
        self.assertIsNone(dialog.result)
        dialog.window.destroy.assert_not_called()

    def test_p05_cancel_and_mask_failure_never_returns_original(self):
        from ui.transfer_dialog import TransferDialog
        dialog = TransferDialog.__new__(TransferDialog)
        dialog.window = Mock()
        dialog.result = make_text_snapshot("original")
        dialog.cancel()
        self.assertIsNone(dialog.result)
        dialog.masking, dialog.kind = True, "image"
        dialog.original_image = Image.new("RGB", (10, 10))
        dialog.canvas = Mock()
        dialog.canvas.winfo_exists.return_value = True
        dialog.rectangles = [(1, 1, 1, 2)]
        dialog.result = None
        with patch("ui.transfer_dialog.messagebox.showerror") as show_error:
            dialog.accept_redacted()
        self.assertIsNone(dialog.result)
        self.assertTrue(show_error.called)

    def test_previous_redaction_requires_explicit_expansion_confirmation(self):
        from ui.transfer_dialog import TransferDialog
        dialog = TransferDialog.__new__(TransferDialog)
        dialog.window = Mock()
        dialog.result = None
        dialog.previous = make_text_snapshot("[가림1]", excluded_strings=["SECRET"]).policy
        dialog.kind, dialog.original_text = "text", "SECRET"
        with patch("ui.transfer_dialog.messagebox.askyesno", return_value=False):
            dialog.accept_direct()
        self.assertIsNone(dialog.result)
        dialog.window.destroy.assert_not_called()
        with patch("ui.transfer_dialog.messagebox.askyesno", return_value=True):
            dialog.accept_direct()
        self.assertEqual(dialog.result.text, "SECRET")
        self.assertFalse(dialog.result.policy.redacted)


if __name__ == "__main__":
    unittest.main()
