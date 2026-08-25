from datetime import timezone
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from services.task_store import TaskConflictError, TaskRecord, TaskStore


class TaskStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.app_data_dir = self.root / "app-data"
        self.db_path = self.root / "database" / "tasks.sqlite3"
        self.store = TaskStore(
            db_path=self.db_path,
            app_data_dir=self.app_data_dir,
        )

    def test_initializes_schema_version_one_and_creates_full_record(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

        attachment = self.root / "outside" / "notice.pdf"
        attachment.parent.mkdir()
        attachment.write_bytes(b"external attachment")

        record = self.store.create(
            title="가정통신문 제출",
            source_kind="file",
            source_name="notice.pdf",
            source_path=attachment,
            source_text="제출 기한은 금요일입니다.",
            analysis_text="금요일까지 제출",
        )

        self.assertIsInstance(record, TaskRecord)
        self.assertEqual(str(UUID(record.id)), record.id)
        self.assertEqual(record.status, "open")
        self.assertEqual(record.source_kind, "file")
        self.assertEqual(record.source_name, "notice.pdf")
        self.assertEqual(record.source_path, str(attachment.resolve()))
        self.assertEqual(record.output_mode, "통합 실행안")
        self.assertIsNone(record.capture_path)
        self.assertIsNone(record.completed_at)
        self.assertEqual(record.created_at.tzinfo, timezone.utc)
        self.assertEqual(record.updated_at.tzinfo, timezone.utc)
        self.assertEqual(self.store.get(record.id), record)

    def test_searches_all_planned_text_fields_filters_status_and_sorts_updated(self) -> None:
        title_match = self.store.create(
            title="예산 신청",
            source_kind="manual",
            source_name="직접 입력",
        )
        source_name_match = self.store.create(
            title="두 번째 업무",
            source_kind="file",
            source_name="예산안.pdf",
        )
        source_text_match = self.store.create(
            title="세 번째 업무",
            source_kind="manual",
            source_text="예산 담당자 확인",
        )
        analysis_match = self.store.create(
            title="네 번째 업무",
            source_kind="manual",
            analysis_text="예산 제출 체크리스트",
        )

        completed = self.store.set_status(source_text_match.id, "completed")
        self.assertIsNotNone(completed)
        updated = self.store.update(title_match.id, analysis_text="가장 최근 수정")
        self.assertIsNotNone(updated)

        matches = self.store.search("예산")
        self.assertEqual(
            {record.id for record in matches},
            {
                title_match.id,
                source_name_match.id,
                source_text_match.id,
                analysis_match.id,
            },
        )
        self.assertEqual(matches[0].id, title_match.id)
        self.assertEqual(
            [record.id for record in self.store.search("예산", status="completed")],
            [source_text_match.id],
        )
        self.assertTrue(all(record.status == "open" for record in self.store.search(status="open")))

    def test_updates_allowed_fields_and_manages_completion_timestamp(self) -> None:
        record = self.store.create(title="초안", source_kind="manual")

        updated = self.store.update(
            record.id,
            title="수정된 업무",
            source_name="붙여넣기",
            source_path=self.root / "source.txt",
            source_text="원문",
            analysis_text="분석",
            output_mode="업무 프로세스",
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.title, "수정된 업무")
        self.assertEqual(updated.source_name, "붙여넣기")
        self.assertEqual(updated.source_text, "원문")
        self.assertEqual(updated.analysis_text, "분석")
        self.assertEqual(updated.output_mode, "업무 프로세스")
        self.assertGreaterEqual(updated.updated_at, record.updated_at)

        completed = self.store.set_status(record.id, "completed")
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(completed.completed_at)
        self.assertEqual(completed.completed_at.tzinfo, timezone.utc)

        unchanged = self.store.set_status(record.id, "completed")
        self.assertEqual(unchanged.completed_at, completed.completed_at)

        reopened = self.store.set_status(record.id, "open")
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.status, "open")
        self.assertIsNone(reopened.completed_at)

        with self.assertRaises(ValueError):
            self.store.update(record.id, status="completed")
        with self.assertRaises(ValueError):
            self.store.set_status(record.id, "archived")
        self.assertIsNone(self.store.update("missing", title="없음"))
        self.assertIsNone(self.store.set_status("missing", "open"))

    def test_copies_capture_into_owned_storage_and_deletes_only_owned_copy(self) -> None:
        temporary_capture = self.root / "temporary-captures" / "screen.png"
        temporary_capture.parent.mkdir()
        temporary_capture.write_bytes(b"png capture bytes")

        record = self.store.create(
            title="캡처 업무",
            source_kind="capture",
            source_name="screen.png",
            source_path=temporary_capture,
            capture_path=temporary_capture,
        )

        owned_capture = Path(record.capture_path)
        self.assertTrue(owned_capture.is_file())
        self.assertEqual(owned_capture.read_bytes(), temporary_capture.read_bytes())
        self.assertEqual(owned_capture.parent, self.store.captures_dir)
        self.assertNotEqual(owned_capture, temporary_capture)
        with sqlite3.connect(self.db_path) as connection:
            stored_capture_path = connection.execute(
                "SELECT capture_path FROM tasks WHERE id = ?",
                (record.id,),
            ).fetchone()[0]
        self.assertEqual(stored_capture_path, owned_capture.name)

        temporary_capture.unlink()
        self.assertTrue(owned_capture.exists())
        self.assertTrue(self.store.delete(record.id))
        self.assertFalse(owned_capture.exists())
        self.assertIsNone(self.store.get(record.id))
        self.assertFalse(self.store.delete(record.id))

    def test_capture_paths_follow_a_relocated_app_data_directory(self) -> None:
        old_app_data = self.root / "portable-old"
        portable_store = TaskStore(
            db_path=old_app_data / "ssokly.db",
            app_data_dir=old_app_data,
        )
        temporary_capture = self.root / "portable-capture.png"
        temporary_capture.write_bytes(b"portable capture")
        record = portable_store.create(
            title="이동할 캡처",
            source_kind="capture",
            capture_path=temporary_capture,
        )

        new_app_data = self.root / "portable-new"
        shutil.move(str(old_app_data), str(new_app_data))
        relocated_store = TaskStore(
            db_path=new_app_data / "ssokly.db",
            app_data_dir=new_app_data,
        )
        relocated = relocated_store.get(record.id)

        self.assertIsNotNone(relocated)
        relocated_capture = Path(relocated.capture_path)
        self.assertEqual(relocated_capture.parent, relocated_store.captures_dir)
        self.assertTrue(relocated_capture.exists())
        self.assertTrue(relocated_store.delete(record.id))
        self.assertFalse(relocated_capture.exists())

    def test_rejects_incompatible_unversioned_schema_without_promoting_it(self) -> None:
        legacy_db = self.root / "legacy" / "ssokly.db"
        legacy_db.parent.mkdir()
        with sqlite3.connect(legacy_db) as connection:
            connection.execute(
                "CREATE TABLE tasks (id TEXT, status TEXT, updated_at TEXT)"
            )

        with self.assertRaisesRegex(RuntimeError, "incompatible"):
            TaskStore(db_path=legacy_db, app_data_dir=legacy_db.parent)

        with sqlite3.connect(legacy_db) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 0)

    def test_optimistic_update_rejects_a_stale_app_instance(self) -> None:
        record = self.store.create(
            title="동시 수정 업무",
            source_kind="manual",
            analysis_text="초기 실행안",
        )
        stale_copy = self.store.get(record.id)
        updated = self.store.update(
            record.id,
            expected_updated_at=record.updated_at,
            analysis_text="다른 창의 최신 실행안",
        )

        self.assertIsNotNone(updated)
        with self.assertRaises(TaskConflictError):
            self.store.update(
                record.id,
                expected_updated_at=stale_copy.updated_at,
                analysis_text="오래된 창의 실행안",
            )
        self.assertEqual(
            self.store.get(record.id).analysis_text,
            "다른 창의 최신 실행안",
        )

    def test_deleting_file_task_never_deletes_external_attachment(self) -> None:
        external_attachment = self.root / "external" / "original.hwpx"
        external_attachment.parent.mkdir()
        external_attachment.write_bytes(b"original document")
        record = self.store.create(
            title="외부 첨부 업무",
            source_kind="file",
            source_name=external_attachment.name,
            source_path=external_attachment,
        )

        self.assertTrue(self.store.delete(record.id))
        self.assertTrue(external_attachment.exists())
        self.assertEqual(external_attachment.read_bytes(), b"original document")

    def test_locked_owned_capture_keeps_task_record_intact(self) -> None:
        temporary_capture = self.root / "locked-capture.png"
        temporary_capture.write_bytes(b"locked capture")
        record = self.store.create(
            title="잠긴 캡처",
            source_kind="capture",
            capture_path=temporary_capture,
        )
        owned_capture = Path(record.capture_path)

        with mock.patch.object(
            type(owned_capture),
            "replace",
            side_effect=PermissionError("capture is locked"),
        ):
            with self.assertRaises(PermissionError):
                self.store.delete(record.id)

        self.assertIsNotNone(self.store.get(record.id))
        self.assertTrue(owned_capture.exists())

    def test_post_commit_capture_cleanup_failure_does_not_report_false_delete(self) -> None:
        temporary_capture = self.root / "cleanup-capture.png"
        temporary_capture.write_bytes(b"cleanup capture")
        record = self.store.create(
            title="정리 실패 캡처",
            source_kind="capture",
            capture_path=temporary_capture,
        )
        owned_capture = Path(record.capture_path)

        with mock.patch.object(
            type(owned_capture),
            "unlink",
            side_effect=PermissionError("cleanup denied"),
        ):
            self.assertTrue(self.store.delete(record.id))

        self.assertIsNone(self.store.get(record.id))

    def test_default_paths_use_localappdata_and_fallback_to_home(self) -> None:
        local_app_data = self.root / "local-app-data"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
            local_store = TaskStore()
        self.assertEqual(local_store.app_data_dir, (local_app_data / "Ssokly").resolve())
        self.assertEqual(local_store.db_path, local_store.app_data_dir / "ssokly.db")

        fallback_home = self.root / "home"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            with mock.patch("services.task_store.Path.home", return_value=fallback_home):
                fallback_store = TaskStore()
        self.assertEqual(fallback_store.app_data_dir, (fallback_home / ".ssokly").resolve())

    def test_rejects_invalid_source_and_capture_ownership_combinations(self) -> None:
        capture = self.root / "capture.png"
        capture.write_bytes(b"capture")
        with self.assertRaises(ValueError):
            self.store.create(title="잘못된 상태", status="pending")
        with self.assertRaises(ValueError):
            self.store.create(title="잘못된 출처", source_kind="clipboard")
        with self.assertRaises(ValueError):
            self.store.create(
                title="외부 파일",
                source_kind="file",
                capture_path=capture,
            )


if __name__ == "__main__":
    unittest.main()
