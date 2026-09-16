from datetime import timezone
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from PIL import Image

from services.capture_store import (
    CaptureConflictError,
    CaptureLinkedError,
    CaptureRecord,
    CaptureStore,
    CaptureStoreError,
)


class CaptureInboxStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.inbox = self.root / "capture-inbox"
        self.store = CaptureStore(directory=self.inbox)

    @staticmethod
    def _image(seed: int = 0) -> Image.Image:
        image = Image.new("RGBA", (4, 3))
        image.putdata(
            [
                (
                    (seed + x * 31) % 256,
                    (seed + y * 47) % 256,
                    (seed + x * 13 + y * 17) % 256,
                    (100 + x * 20 + y * 10) % 256,
                )
                for y in range(3)
                for x in range(4)
            ]
        )
        return image

    def _save(self, seed: int = 0, source: str = "capture") -> CaptureRecord:
        return self.store.save(self._image(seed), source=source)

    def test_save_is_lossless_persistent_and_records_full_metadata(self) -> None:
        image = self._image(7)
        original_mode = image.mode
        original_pixels = list(image.getdata())

        record = self.store.save(image, source="공문 캡처")

        self.assertEqual(str(UUID(record.id)), record.id)
        self.assertEqual(record.source, "공문 캡처")
        self.assertEqual((record.width, record.height), image.size)
        self.assertEqual(record.byte_size, record.path.stat().st_size)
        self.assertEqual(
            record.content_sha256,
            hashlib.sha256(record.path.read_bytes()).hexdigest(),
        )
        self.assertEqual(record.ocr_status, "unread")
        self.assertEqual(record.created_at.tzinfo, timezone.utc)
        self.assertEqual(image.mode, original_mode)
        self.assertEqual(list(image.getdata()), original_pixels)
        with Image.open(record.path) as stored:
            self.assertEqual(stored.mode, original_mode)
            self.assertEqual(list(stored.getdata()), original_pixels)

        reopened = CaptureStore(directory=self.inbox)
        self.assertEqual(reopened.get(record.id), record)
        loaded = reopened.load(record)
        self.assertEqual(loaded.mode, original_mode)
        self.assertEqual(list(loaded.getdata()), original_pixels)

        with closing(sqlite3.connect(reopened.db_path)) as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)

    def test_max_items_is_compatibility_only_and_no_longer_prunes(self) -> None:
        store = CaptureStore(directory=self.root / "unpruned", max_items=5)
        records = [store.save(self._image(seed)) for seed in range(9)]

        self.assertEqual(len(store.list_recent()), 9)
        self.assertTrue(all(record.path.exists() for record in records))

    def test_ocr_text_is_exact_searchable_and_failure_keeps_last_text(self) -> None:
        record = self._save(source="가정통신문")
        exact_text = "\n  제출일: 9월 2일  \n학생 이름은 [?]\n"

        ready = self.store.update_ocr(
            record.id,
            exact_text,
            profile="gpt-5-nano/high/ocr-v2",
        )

        self.assertEqual(ready.ocr_status, "ready")
        self.assertEqual(ready.ocr_text, exact_text)
        self.assertEqual(ready.ocr_preview, "제출일: 9월 2일")
        self.assertEqual(ready.ocr_error, "")
        self.assertEqual(ready.ocr_profile, "gpt-5-nano/high/ocr-v2")
        self.assertEqual([item.id for item in self.store.search("학생 이름")], [record.id])
        self.assertEqual([item.id for item in self.store.search("가정통신문")], [record.id])

        failed = self.store.set_ocr_failure(
            record.id,
            "네트워크 연결 실패",
            profile="retry/high",
        )
        self.assertEqual(failed.ocr_status, "failed")
        self.assertEqual(failed.ocr_text, exact_text)
        self.assertEqual(failed.ocr_error, "네트워크 연결 실패")
        self.assertEqual(failed.ocr_profile, "retry/high")

    def test_teacher_verified_text_is_exact_searchable_and_preferred(self) -> None:
        record = self._save(source="학생 상담 기록")
        raw_ocr = "김민수  010-1234-567B\n상담일 9월 2일"
        verified_exact = "\n김민수  010-1234-5678\n상담일\t9월 2일  \n"
        self.store.update_ocr(record.id, raw_ocr, profile="raw/high")

        verified = self.store.save_verified_text(record.id, verified_exact)

        self.assertTrue(verified.is_verified)
        self.assertEqual(verified.review_status, "verified")
        self.assertEqual(verified.ocr_text, raw_ocr)
        self.assertEqual(verified.verified_text, verified_exact)
        self.assertEqual(verified.effective_text, verified_exact)
        self.assertEqual(verified.ocr_preview, "김민수  010-1234-5678")
        self.assertEqual(
            [item.id for item in self.store.search("010-1234-5678")],
            [record.id],
        )

        reopened = CaptureStore(directory=self.inbox).get(record.id)
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.verified_text, verified_exact)
        self.assertEqual(reopened.effective_text, verified_exact)

    def test_new_ocr_and_failure_never_overwrite_teacher_verified_text(self) -> None:
        record = self._save()
        self.store.update_ocr(record.id, "첫 AI OCR", profile="first")
        self.store.save_verified_text(record.id, "교사가 확정한 정확한 원문")

        updated = self.store.update_ocr(record.id, "새 AI OCR 후보", profile="second")
        self.assertEqual(updated.review_status, "ocr_updated")
        self.assertEqual(updated.ocr_text, "새 AI OCR 후보")
        self.assertEqual(updated.effective_text, "교사가 확정한 정확한 원문")

        failed = self.store.set_ocr_failure(record.id, "다시 읽기 실패")
        self.assertEqual(failed.ocr_status, "failed")
        self.assertEqual(failed.ocr_text, "새 AI OCR 후보")
        self.assertEqual(failed.effective_text, "교사가 확정한 정확한 원문")

        cleared = self.store.clear_verified_text(record.id)
        self.assertFalse(cleared.is_verified)
        self.assertEqual(cleared.review_status, "unverified")
        self.assertEqual(cleared.effective_text, "새 AI OCR 후보")

    def test_stale_review_cannot_claim_or_overwrite_a_newer_capture(self) -> None:
        record = self._save()
        first_view = self.store.update_ocr(record.id, "첫 AI OCR", profile="first")
        self.assertIsNotNone(first_view.updated_at)

        newer = self.store.update_ocr(record.id, "다른 창의 새 OCR", profile="second")
        with self.assertRaises(CaptureConflictError):
            self.store.save_verified_text(
                record.id,
                "첫 OCR만 보고 만든 오래된 검수본",
                expected_updated_at=first_view.updated_at,
            )
        self.assertFalse(self.store.get(record.id).is_verified)

        verified = self.store.save_verified_text(
            record.id,
            "먼저 저장된 정확한 검수본",
            expected_updated_at=newer.updated_at,
        )
        with self.assertRaises(CaptureConflictError):
            self.store.save_verified_text(
                record.id,
                "두 번째 창의 덮어쓰기",
                expected_updated_at=newer.updated_at,
            )
        self.assertEqual(
            self.store.get(record.id).verified_text,
            verified.verified_text,
        )

    def test_version_one_database_migrates_without_losing_capture_or_links(self) -> None:
        migration_inbox = self.root / "migration-inbox"
        migration_inbox.mkdir()
        image_path = migration_inbox / "existing.png"
        self._image(23).save(image_path, format="PNG")
        image_bytes = image_path.read_bytes()
        capture_id = "existing-capture"
        created_at = "2026-08-25T01:02:03.000000+00:00"
        db_path = migration_inbox / "capture_inbox.sqlite3"
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE capture_items (
                    id TEXT PRIMARY KEY,
                    storage_name TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    width INTEGER NOT NULL CHECK (width > 0),
                    height INTEGER NOT NULL CHECK (height > 0),
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    content_sha256 TEXT NOT NULL,
                    ocr_status TEXT NOT NULL DEFAULT 'unread'
                        CHECK (ocr_status IN ('unread', 'ready', 'failed')),
                    ocr_text TEXT NOT NULL DEFAULT '',
                    ocr_error TEXT NOT NULL DEFAULT '',
                    ocr_profile TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trashed_at TEXT
                );
                CREATE TABLE capture_links (
                    capture_id TEXT NOT NULL
                        REFERENCES capture_items(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (capture_id, task_id)
                );
                CREATE TABLE legacy_imports (
                    legacy_key TEXT PRIMARY KEY,
                    capture_id TEXT NOT NULL
                        REFERENCES capture_items(id) ON DELETE CASCADE
                );
                CREATE INDEX idx_capture_items_active_created
                ON capture_items (trashed_at, created_at DESC);
                CREATE INDEX idx_capture_links_task
                ON capture_links (task_id, capture_id);
                PRAGMA user_version = 1;
                """
            )
            connection.execute(
                """
                INSERT INTO capture_items (
                    id, storage_name, source, width, height, byte_size,
                    content_sha256, ocr_status, ocr_text, ocr_error,
                    ocr_profile, created_at, updated_at, trashed_at
                ) VALUES (?, ?, ?, 4, 3, ?, ?, 'ready', ?, '', ?, ?, ?, NULL)
                """,
                (
                    capture_id,
                    image_path.name,
                    "기존 공문",
                    len(image_bytes),
                    hashlib.sha256(image_bytes).hexdigest(),
                    "기존 AI OCR",
                    "legacy/high",
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO capture_links (capture_id, task_id, linked_at) "
                "VALUES (?, 'task-existing', ?)",
                (capture_id, created_at),
            )

        migrated = CaptureStore(directory=migration_inbox)
        record = migrated.get(capture_id)

        self.assertIsNotNone(record)
        self.assertEqual(record.ocr_text, "기존 AI OCR")
        self.assertFalse(record.is_verified)
        self.assertEqual(record.linked_task_ids, ("task-existing",))
        self.assertEqual(image_path.read_bytes(), image_bytes)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)

    def test_link_filters_support_multiple_captures_and_tasks(self) -> None:
        first = self._save(1, "첫 캡처")
        second = self._save(2, "둘째 캡처")
        third = self._save(3, "셋째 캡처")

        linked = self.store.link_to_task([first.id, second.id], "task-a")
        self.store.link_to_task(second.id, "task-b")

        self.assertEqual(linked[0].linked_task_ids, ("task-a",))
        self.assertEqual(
            self.store.get(second.id).linked_task_ids,
            ("task-a", "task-b"),
        )
        self.assertEqual(
            {record.id for record in self.store.search(link_filter="linked")},
            {first.id, second.id},
        )
        self.assertEqual(
            [record.id for record in self.store.search(link_filter="unclassified")],
            [third.id],
        )

        self.store.unlink_task([first.id, second.id], "task-a")
        self.assertEqual(self.store.get(first.id).linked_task_ids, ())
        self.assertEqual(self.store.get(second.id).linked_task_ids, ("task-b",))

    def test_task_capture_order_preserves_initial_bundle_and_later_append(self) -> None:
        first = self._save(1, "첫 장")
        second = self._save(2, "둘째 장")
        later_append = self._save(3, "나중에 추가")

        self.store.link_to_task([first.id, second.id], "task-ordered")
        self.store.link_to_task(later_append.id, "task-ordered")
        self.store.link_to_task(first.id, "task-ordered")

        self.assertEqual(
            [record.id for record in self.store.captures_for_task("task-ordered")],
            [first.id, second.id, later_append.id],
        )

    def test_batch_link_is_atomic_when_one_capture_is_missing_or_trashed(self) -> None:
        first = self._save(1)
        second = self._save(2)
        self.store.trash(second.id)

        with self.assertRaises(CaptureStoreError):
            self.store.link_to_task([first.id, second.id], "task-a")
        self.assertEqual(self.store.get(first.id).linked_task_ids, ())

        with self.assertRaises(CaptureStoreError):
            self.store.link_to_task([first.id, "missing"], "task-a")
        self.assertEqual(self.store.get(first.id).linked_task_ids, ())

    def test_trash_restore_and_legacy_delete_are_recoverable(self) -> None:
        first = self._save(1)
        second = self._save(2)
        total_bytes = first.byte_size + second.byte_size

        trashed = self.store.trash(first.id)[0]
        self.assertIsNotNone(trashed.trashed_at)
        self.assertTrue(first.path.exists())
        self.assertEqual([record.id for record in self.store.list_recent()], [second.id])
        self.assertEqual([record.id for record in self.store.search(trashed=True)], [first.id])
        self.assertEqual(self.store.storage_bytes(), total_bytes)
        self.assertEqual(self.store.storage_bytes(trashed=True), first.byte_size)
        self.assertEqual(self.store.storage_bytes(trashed=False), second.byte_size)

        restored = self.store.restore(first.id)[0]
        self.assertIsNone(restored.trashed_at)
        self.store.delete(restored)
        self.assertTrue(first.path.exists())
        self.assertEqual([record.id for record in self.store.search(trashed=True)], [first.id])

    def test_linked_capture_must_be_unlinked_and_trashed_before_purge(self) -> None:
        record = self._save()
        self.store.link_to_task(record.id, "task-1")

        with self.assertRaises(CaptureLinkedError):
            self.store.trash(record.id)
        with self.assertRaises(CaptureLinkedError):
            self.store.purge(record.id)

        self.store.unlink_task(record.id, "task-1")
        with self.assertRaises(CaptureStoreError):
            self.store.purge(record.id)

        self.store.trash(record.id)
        self.assertEqual(self.store.purge(record.id), 1)
        self.assertIsNone(self.store.get(record.id))
        self.assertFalse(record.path.exists())

    def test_failed_purge_file_stage_rolls_back_metadata_and_image(self) -> None:
        record = self._save()
        self.store.trash(record.id)

        with mock.patch.object(
            type(record.path),
            "replace",
            side_effect=PermissionError("capture is locked"),
        ):
            with self.assertRaises(PermissionError):
                self.store.purge(record.id)

        self.assertTrue(record.path.exists())
        self.assertIsNotNone(self.store.get(record.id))
        self.assertIsNotNone(self.store.get(record.id).trashed_at)

    def test_only_metadata_owned_path_is_purged_and_external_file_is_untouched(self) -> None:
        record = self._save()
        external = self.root / "teacher-original.png"
        external.write_bytes(b"external original bytes")
        forged = CaptureRecord(
            id=record.id,
            path=external,
            created_at=record.created_at,
        )

        # The compatibility delete uses the ID and is only a soft delete.
        self.store.delete(forged)
        self.store.purge(record.id)

        self.assertEqual(external.read_bytes(), b"external original bytes")
        self.assertFalse(record.path.exists())

    def test_load_rejects_a_record_outside_the_app_owned_directory(self) -> None:
        outside = self.root / "outside.png"
        self._image().save(outside, format="PNG")
        forged = CaptureRecord(path=outside, created_at=self._save().created_at)

        with self.assertRaisesRegex(ValueError, "outside"):
            self.store.load(forged)

    def test_default_store_imports_legacy_png_once_without_modifying_it(self) -> None:
        legacy_directory = self.root / "legacy-temp-captures"
        legacy_directory.mkdir()
        legacy = legacy_directory / "20260825_093011_123456_교직원메시지.png"
        self._image(19).save(legacy, format="PNG", compress_level=9)
        legacy_bytes = legacy.read_bytes()
        app_data = self.root / "local-app-data"

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(app_data)}):
            with mock.patch(
                "services.capture_store._legacy_store_dir",
                return_value=legacy_directory.resolve(),
            ):
                first_store = CaptureStore()
                first_records = first_store.list_recent()
                second_store = CaptureStore()
                second_records = second_store.list_recent()

        self.assertEqual(first_store.directory, app_data / "Ssokly" / "capture_inbox")
        self.assertEqual(len(first_records), 1)
        self.assertEqual(len(second_records), 1)
        self.assertEqual(first_records[0].id, second_records[0].id)
        self.assertEqual(first_records[0].source, "교직원메시지")
        self.assertEqual(first_records[0].path.read_bytes(), legacy_bytes)
        self.assertEqual(legacy.read_bytes(), legacy_bytes)

    def test_custom_store_never_imports_the_global_legacy_directory(self) -> None:
        legacy_directory = self.root / "legacy"
        legacy_directory.mkdir()
        self._image().save(legacy_directory / "old.png", format="PNG")

        with mock.patch(
            "services.capture_store._legacy_store_dir",
            return_value=legacy_directory,
        ):
            custom = CaptureStore(directory=self.root / "custom")

        self.assertEqual(custom.list_recent(), [])

    def test_input_validation_and_missing_ids_do_not_partially_mutate(self) -> None:
        record = self._save()

        with self.assertRaises(ValueError):
            self.store.search(link_filter="unknown")
        with self.assertRaises(ValueError):
            self.store.search(limit=0)
        with self.assertRaises(TypeError):
            self.store.search(query=123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.link_to_task([], "task")
        with self.assertRaises(CaptureStoreError):
            self.store.trash([record.id, "missing"])

        self.assertIsNone(self.store.get(record.id).trashed_at)

    def test_count_and_limit_support_large_inbox_ui_without_loading_every_row(self) -> None:
        records = [self._save(seed) for seed in range(6)]
        self.store.link_to_task(records[0].id, "task-count")
        self.store.trash(records[1].id)

        self.assertEqual(self.store.count("all"), 5)
        self.assertEqual(self.store.count("unclassified"), 4)
        self.assertEqual(self.store.count("linked"), 1)
        self.assertEqual(self.store.count("all", trashed=True), 1)
        self.assertEqual(len(self.store.search(limit=3)), 3)


if __name__ == "__main__":
    unittest.main()
