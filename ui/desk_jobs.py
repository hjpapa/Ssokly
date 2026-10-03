"""Capture, file import, OCR/AI job and transfer actions for the capture desk."""
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from uuid import uuid4

from PIL import Image

from services.capture_service import capture_selected_region
from services.image_clipboard import copy_image
from services.document_service import LEGACY_DOCUMENT_EXTENSIONS, LOCAL_DOCUMENT_EXTENSIONS, SUPPORTED_FILE_EXTENSIONS, attachment_kind, mime_type_for, read_hwpx_file, read_text_file
from services.file_import import import_local_document
from ui.desk_text import fingerprint
from services.diagnostics import log_failure


class JobsMixin:
    # OCR/AI operations are connected below; storage and editing never require AI.
    def _capture_mode_changed(self, _event=None):
        mode = self.capture_mode.get()
        try:
            if mode == '자동 인식' and self.library.get_setting('automatic_ocr_consent', False) is not True:
                approved = messagebox.askyesno('자동 OCR 설정',
                    '앞으로 새로 캡처한 이미지가 자동으로 OpenAI API에 전송되고 비용이 발생합니다. 서버 모드에서는 중계 서버를 거칩니다.\n'
                    '기관의 외부 AI 이용 기준을 확인해 주세요. 민감한 자료는 캡처 전에 보관만 또는 가리고 읽기를 선택하세요.\n\n자동 인식을 켤까요?', parent=self)
                if not approved:
                    self.capture_mode.set('보관만')
                    return
                self.library.set_setting('automatic_ocr_consent', True)
            self.library.set_setting('capture_mode', self.capture_mode.get())
        except Exception as error:
            log_failure('desk_jobs._capture_mode_changed', error)
            self.capture_mode.set('보관만')
            self.status.set('설정을 저장하지 못해 보관만 모드로 유지합니다.')

    def capture_new(self):
        self._capture(add=False)

    def capture_page(self):
        self._capture(add=True)

    def capture_and_copy(self):
        self._capture(copy_to_clipboard=True)

    def _capture(self, add=False, *, copy_to_clipboard=False):
        if not self.flush_edits():
            return
        if add and (not self.document or self.document.get('readonly') or self.document.get('trashed')):
            self.status.set('페이지를 추가할 새 문서를 먼저 열어 주세요.')
            return
        doc_id = self.document['id'] if add else None
        self.withdraw()
        def select():
            try:
                image = capture_selected_region(self)
            except Exception as error:
                log_failure('desk_jobs.select', error)
                self.status.set('캡처하지 못했습니다. 다시 시도하세요.')
                image = None
            finally:
                self.deiconify()
            if image is not None:
                try:
                    if copy_to_clipboard:
                        page = self.accept_capture(image, document_id=doc_id, auto_read=False)
                        storage_status = self.status.get()
                        self._copy_capture_image(image)
                        if page is None:
                            self.status.set(self.status.get() + ' ' + storage_status)
                    else:
                        self.accept_capture(image, document_id=doc_id)
                finally:
                    image.close()
        self.after_idle(select)

    def _copy_capture_image(self, image):
        try:
            copy_image(image, owner=self.winfo_id())
            self.status.set('이미지를 복사했습니다. 다른 문서에서 Ctrl+V로 붙여넣으세요.')
            return True
        except Exception as error:
            log_failure('desk_jobs.copy_image', error)
            self.status.set('이미지를 복사하지 못했습니다. 다른 프로그램의 클립보드 작업이 끝난 뒤 이미지 복사를 다시 누르세요.')
            return False

    def copy_current_image(self):
        path = self.page.get('path') if self.page else None
        if not path:
            self.status.set('복사할 원본 이미지가 있는 페이지를 선택하세요.')
            return False
        try:
            with Image.open(path) as image:
                return self._copy_capture_image(image)
        except Exception as error:
            log_failure('desk_jobs.copy_current_image', error)
            self.image_copy_button.configure(state='disabled')
            self.status.set('원본 이미지를 읽지 못해 복사하지 않았습니다. 저장된 텍스트는 계속 사용할 수 있습니다.')
            return False

    def accept_capture(self, image, *, document_id=None, source='capture', auto_read=True, title=None):
        """Durable original first; recoverable orphan if document linking fails."""
        record = None
        try:
            record = self.library.capture_store.save(image, source=source)
            doc = self.library.get_document(document_id) if document_id else self.library.create_document(title or '새 캡처')
            page = self.library.add_capture(doc['id'], record.id)
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
            self._load_page(next(i for i, value in enumerate(self.page_records) if value['id'] == page['id']))
            if self._is_compact():
                self._compact_library = False
                self._apply_layout()
            self.status.set('캡처를 보관했습니다.')
            if auto_read and self.capture_mode.get() != '보관만':
                self._read_image(page, automatic=True)
            return page
        except Exception as error:
            log_failure('desk_jobs.accept_capture', error)
            self.refresh_library()
            self.status.set('원본 이미지는 보관했습니다. 문서 연결은 다시 시도하세요.' if record else
                            '캡처 보관을 완료하지 못했습니다. 저장 공간과 보관함을 확인하세요. 저장된 PNG는 조회·재시작 때 복구합니다.')
            return None

    def _transfer(self):
        if not hasattr(self, '_desk_transfer'):
            from services.desk_transfer import DeskTransfer
            from services.transfer_policy import TransferPolicyStore
            self._desk_transfer = DeskTransfer(TransferPolicyStore(self.library.app_data_dir))
        return self._desk_transfer

    def _choose_transfer(self, **kwargs):
        from ui.transfer_dialog import choose_transfer
        return choose_transfer(self, **kwargs)

    def _scope_tokens(self, document_id, capture_ids=(), document_ids=()):
        keys = ['document:' + value for value in dict.fromkeys([document_id, *document_ids])]
        keys += ['capture:' + value for value in capture_ids]
        store = self._transfer().policy_store
        return {key: (policy.to_dict() if (policy := store.get(key)) else None) for key in keys}

    def _scopes_match(self, tokens):
        store = self._transfer().policy_store
        return all((policy.to_dict() if (policy := store.get(key)) else None) == expected for key, expected in tokens.items())

    def _copy_legacy_policy(self, old_id, new_id):
        captures = self.library.capture_store.captures_for_task(old_id)
        self._transfer().inherit_document_policy([old_id], [capture.id for capture in captures], new_id)

    def read_current_page(self):
        if self.page and (self.page.get('readonly') or self.page.get('recovery_required')):
            self.status.set('이 페이지는 읽기 전용입니다. 복구가 필요한 원본은 다시 읽거나 전송하지 않습니다.')
            return
        if not self.page or not self.document or self.document.get('readonly') or self.document.get('trashed') or not self.flush_edits():
            self.status.set('편집할 문서를 선택하세요. 기존 기록은 먼저 새 문서로 가져오세요.')
            return
        if self.page.get('capture_id'):
            self._read_image(self.page, automatic=False)
        elif self.page.get('source_path'):
            self._read_file(self.page)
        else:
            self.status.set('이 페이지는 직접 입력한 텍스트입니다.')

    def _read_image(self, page, *, automatic=False):
        from services.ocr_service import extract_text_from_image
        if page.get('readonly') or page.get('recovery_required'):
            self.status.set('복구가 필요한 캡처는 읽거나 전송하지 않습니다. 저장된 텍스트는 유지합니다.')
            return
        if any(meta['kind'] == 'ocr' and meta.get('capture_id') == page['capture_id'] for meta in self._jobs.values()):
            self.status.set('이 페이지를 읽고 있습니다.')
            return
        try:
            capture = self.library.capture_store.get(page['capture_id'])
            image = self.library.capture_store.load(capture)
            mode = self.capture_mode.get()
            auto_allowed = automatic and mode == '자동 인식' and self.library.get_setting('automatic_ocr_consent', False) is True
            document_ids = tuple(dict.fromkeys([page['document_id'], *capture.linked_task_ids]))
            snapshot = self._transfer().image_snapshot(capture.id, image, auto_allowed=auto_allowed,
                force_mask=mode == '가리고 읽기', document_ids=document_ids, choose=self._choose_transfer)
            if snapshot is None:
                self.status.set('읽기를 취소했습니다. 캡처 원본은 보관되어 있습니다.')
                return
            metadata = {'document_id': page['document_id'], 'page_id': page['id'], 'capture_id': capture.id,
                'snapshot': snapshot, 'scopes': self._scope_tokens(page['document_id'], [capture.id], document_ids)}
            self._start_job('ocr', lambda cancel: extract_text_from_image(snapshot.as_image(), detail='high', raise_errors=True), metadata)
        except Exception as error:
            log_failure('desk_jobs._read_image', error)
            self.status.set('읽기를 시작하지 못했습니다. 전송 범위와 원본을 확인하세요. 원본으로 대체 전송하지 않았습니다.')

    def open_file(self):
        if not self.flush_edits():
            return
        selected = filedialog.askopenfilename(parent=self, title='이미지 또는 문서 열기', filetypes=[
            ('지원 파일', ';'.join('*' + suffix for suffix in sorted(SUPPORTED_FILE_EXTENSIONS))),
            ('모든 파일', '*.*')])
        if selected:
            self.import_file(Path(selected))

    def import_file(self, path):
        if not self.flush_edits():
            return
        path = Path(path)
        kind = attachment_kind(path)
        try:
            if path.suffix.lower() in LEGACY_DOCUMENT_EXTENSIONS:
                self.status.set('구형 HWP/DOC/PPT/XLS는 직접 읽지 않습니다. HWPX/DOCX/PPTX/XLSX 또는 PDF로 저장해 열어 주세요.')
                return
            if path.is_file() and path.stat().st_size > 50 * 1024 * 1024:
                raise ValueError('파일은 50MB까지 열 수 있습니다. 파일을 나눠 주세요.')
            if path.suffix.lower() in LOCAL_DOCUMENT_EXTENSIONS:
                self.status.set('문서의 텍스트와 이미지를 PC에서 읽고 있습니다…')
                self.update_idletasks()
                doc, unread = import_local_document(self.library, path)
                self.refresh_library(doc['id'])
                self.open_document(doc['id'])
                self.status.set(f"로컬에서 {doc['page_count']}개 항목을 보관했습니다. 외부 전송 없음. "
                                + (f'이미지 {unread}개는 해당 쪽에서 다시 읽기를 누르세요.' if unread else '원본과 추출 텍스트를 대조하세요.')
                                + (' HWPX는 본문·삽입 이미지 구분이며 실제 지면 쪽 구분은 아닙니다.' if kind == 'hwpx' else ''))
                if path.suffix.lower() not in ('.pdf', '.hwpx'):
                    self.status.set(self.status.get() + ' 문단·표 텍스트 기준입니다. 그림·차트의 내용은 PDF로 저장해 확인하세요.')
                return
            if kind == 'image':
                with Image.open(path) as image:
                    page = self.accept_capture(image.copy(), source='file_' + path.stem, auto_read=False, title=path.stem)
                # File attachments require explicit choice, regardless of auto OCR.
                if page and self.capture_mode.get() != '보관만':
                    self._read_image(page, automatic=False)
                return
            if kind in ('legacy_hwp', 'unsupported'):
                self.status.set('지원하지 않는 형식입니다. HWPX/PDF로 변환하거나 화면을 캡처하세요.')
                return
            text = (read_hwpx_file(path) if kind == 'hwpx' else read_text_file(path)) if kind in ('hwpx', 'text') else ''
            doc = self.library.create_document(path.stem)
            page = self.library.add_text_page(doc['id'], text, source_name=path.name, source_path=str(path))
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
            if kind == 'openai_document':
                self._read_file(page)
            else:
                self.status.set('로컬에서 읽었습니다. 외부 전송 없음.')
        except ValueError as error:
            self.status.set(str(error))
            messagebox.showerror('문서 열기 실패', str(error), parent=self)
        except Exception as error:
            log_failure('desk_jobs.import_file', error)
            self.status.set('파일을 읽지 못했습니다. 원본 형식과 표 구조를 확인하세요. 기존 자료는 유지합니다.')

    def open_source_file(self):
        path = Path(self.page['source_path']) if self.page and self.page.get('source_path') else None
        if path is None or not path.is_file():
            self.status.set('원본 파일이 없습니다. 보관한 이미지와 텍스트는 계속 사용할 수 있습니다.')
            return
        if path.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
            self.status.set('이 형식은 외부 문서 프로그램으로 열지 않습니다.')
            return
        try:
            os.startfile(str(path))
        except (OSError, AttributeError):
            self.status.set('원본 파일을 여는 프로그램을 확인하세요.')

    def _read_file(self, page):
        from services.ocr_service import extract_text_from_file, extract_text_from_image
        if page.get('readonly') or page.get('recovery_required'):
            self.status.set('복구가 필요한 페이지는 읽거나 전송하지 않습니다. 저장된 텍스트는 유지합니다.')
            return
        path = Path(page['source_path'])
        kind = attachment_kind(path)
        try:
            if path.suffix.lower() in LOCAL_DOCUMENT_EXTENSIONS - {'.pdf', '.hwpx'}:
                from services.office_reader import read_office
                candidates = read_office(path)
                selected = next((entry for entry in candidates if entry['name'] == page['source_name']), None)
                if selected is None:
                    raise ValueError('원본 구조가 바뀌었습니다. 파일 열기로 새 문서를 가져오세요.')
                self.library.remember_initial_ocr(page['id'], page.get('ocr_text') or selected['text'])
                self.library.update_page_ocr(page['id'], selected['text'], expected_updated_at=page['updated_at'])
                self._refresh_current_page(page['document_id'], page['id'])
                self.status.set('로컬 원문을 다시 읽었습니다. 수정본은 유지합니다.')
                return
            if kind in ('hwpx', 'text'):
                text = read_hwpx_file(path) if kind == 'hwpx' else read_text_file(path)
                self.library.remember_initial_ocr(page['id'], page.get('ocr_text') or text)
                self.library.update_page_ocr(page['id'], text, expected_updated_at=page['updated_at'])
                self._refresh_current_page(page['document_id'], page['id'])
                self.status.set('로컬 원문을 다시 읽었습니다. 수정본은 유지합니다.')
                return
            policy_store = self._transfer().policy_store
            key = 'document:' + page['document_id']
            previous = policy_store.get(key)
            snapshot = self._choose_transfer(kind='file', path=path, previous=previous, title='첨부 파일 전송 확인')
            if snapshot is None:
                self.status.set('전송을 취소했습니다. 파일 경로는 문서에 남아 있습니다.')
                return
            # Only the selected immutable copy reaches a worker.
            policy_store.save(key, snapshot.policy, expected_scope_id=previous.scope_id if previous else None)
            metadata = {'document_id': page['document_id'], 'page_id': page['id'],
                'expected_updated_at': page['updated_at'], 'snapshot': snapshot,
                'scopes': self._scope_tokens(page['document_id'])}
            def read(cancel):
                if snapshot.kind == 'text':
                    return snapshot.text
                if snapshot.kind == 'image':
                    return extract_text_from_image(snapshot.as_image(), detail='high', raise_errors=True)
                return extract_text_from_file(path, mime_type_for(path), raise_errors=True,
                    file_bytes=snapshot.file_bytes, filename=snapshot.file_name)
            self._start_job('file', read, metadata)
        except Exception as error:
            log_failure('desk_jobs._read_file', error)
            self.status.set('파일 읽기를 시작하지 못했습니다. 원본과 전송 범위를 확인하세요.')

    def generate(self):
        from services.text_actions import generate_text_action
        if not self.document or self.document.get('readonly') or self.document.get('trashed') or not self.flush_edits():
            self.status.set('편집할 문서를 먼저 열어 주세요.')
            return
        if any(meta['kind'] == 'ai' and meta['document_id'] == self.document['id'] for meta in self._jobs.values()):
            self.status.set('이 문서의 AI 정리가 진행 중입니다.')
            return
        full_text = self.library.document_text(self.document['id'])
        text = full_text
        if self.selection_only.get():
            try:
                text = self.source_editor.get('sel.first', 'sel.last')
            except tk.TclError:
                self.status.set('텍스트 탭에서 정리할 부분을 선택하세요.')
                return
        if not text.strip():
            self.status.set('먼저 캡처를 읽거나 텍스트를 입력하세요.')
            return
        try:
            capture_ids = [page['capture_id'] for page in self.library.pages(self.document['id']) if page.get('capture_id')]
            snapshot = self._transfer().text_snapshot(self.document['id'], text, page_policies=capture_ids, choose=self._choose_transfer)
            if snapshot is None:
                self.status.set('AI 정리를 취소했습니다. 원문과 기존 결과는 유지합니다.')
                return
            mode = self.ai_mode.get()
            audience = self.audience.get() if mode == '안내문' else '교직원'
            label = mode + (' · ' + audience if mode == '안내문' else '') + (' · 선택 부분' if self.selection_only.get() else '')
            metadata = {'document_id': self.document['id'], 'mode': label,
                'source_fingerprint': fingerprint(full_text), 'output_revision': self._output_revision,
                'snapshot': snapshot, 'scopes': self._scope_tokens(self.document['id'], capture_ids)}
            self._start_job('ai', lambda cancel: generate_text_action(snapshot.text, mode, audience, cancel_event=cancel), metadata)
            self.editor_tabs.select(self.ai_panel)
        except Exception as error:
            log_failure('desk_jobs.generate', error)
            self.status.set('AI 정리를 시작하지 못했습니다. 전송 범위를 다시 확인하세요.')

    def _start_job(self, kind, operation, metadata):
        snapshot = metadata['snapshot']
        scope_key = ('capture:' + metadata['capture_id']) if kind == 'ocr' else ('document:' + metadata['document_id'])
        if metadata['scopes'].get(scope_key) != snapshot.policy.to_dict() or not self._scopes_match(metadata['scopes']):
            raise ValueError('전송 범위가 변경되어 요청을 시작하지 않았습니다.')
        identifier = uuid4().hex
        cancel = threading.Event()
        self._jobs[identifier] = {**metadata, 'kind': kind, 'cancel': cancel, 'started': time.monotonic()}
        def work():
            try:
                if cancel.is_set():
                    self._results.put((identifier, False, '취소됨'))
                    return
                if not self._scopes_match(metadata['scopes']):
                    raise ValueError('전송 범위 변경')
                value = operation(cancel)
                self._results.put((identifier, True, value))
            except Exception as error:
                log_failure('desk_jobs.work', error)
                # SDK exception strings may contain request details; never persist them.
                self._results.put((identifier, False, '요청이 완료되지 않았습니다. 연결과 입력을 확인한 뒤 다시 시도하세요.'))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as error:
            log_failure('desk_jobs._start_job', error)
            self._jobs.pop(identifier, None)
            raise
        self.status.set('텍스트를 읽고 있습니다… 원본은 보관됨' if kind != 'ai' else 'AI 정리 중… 기존 결과는 유지됩니다.')
        self.job_progress.refresh(self._jobs)
        return identifier

    def cancel_jobs(self):
        for metadata in self._jobs.values():
            metadata['cancel'].set()
        self.job_progress.refresh(self._jobs)
        self.status.set('처리를 취소했습니다. 이미 전송된 요청의 비용은 발생할 수 있습니다. 원본은 유지합니다.')

    def _poll_results(self):
        while not self._results.empty():
            identifier, success, value = self._results.get_nowait()
            metadata = self._jobs.pop(identifier, None)
            if not metadata or metadata['cancel'].is_set() or self._closing:
                continue
            try:
                doc = self.library.get_document(metadata['document_id'])
                if doc.get('trashed') or not self._scopes_match(metadata.get('scopes', {})):
                    self.status.set('문서 또는 전송 범위가 변경되어 이전 응답을 적용하지 않았습니다.')
                    continue
                if not success:
                    if metadata['kind'] == 'ocr':
                        before = next(p for p in self.library.pages(metadata['document_id']) if p['id'] == metadata['page_id'])
                        self.library.capture_store.set_ocr_failure(metadata['capture_id'], value, profile='gpt-6-luna')
                        self._refresh_current_page(metadata['document_id'], metadata['page_id'], previous_token=before['updated_at'])
                    self.status.set(value + ' 원본과 마지막 성공 결과는 유지했습니다.')
                    continue
                if not isinstance(value, str) or not value.strip():
                    self.status.set('완료된 텍스트가 없어 기존 내용을 유지했습니다.')
                    continue
                if metadata['kind'] == 'ocr':
                    before = next(p for p in self.library.pages(metadata['document_id']) if p['id'] == metadata['page_id'])
                    self.library.remember_initial_ocr(metadata['page_id'], before.get('ocr_text') or value)
                    self._transfer().record_ocr(metadata['capture_id'], metadata['snapshot'], value)
                    self.library.capture_store.update_ocr(metadata['capture_id'], value, profile='gpt-6-luna')
                    self._refresh_current_page(metadata['document_id'], metadata['page_id'], previous_token=before['updated_at'])
                    self.status.set('텍스트 인식 완료 · 직접 수정한 텍스트는 유지합니다.')
                elif metadata['kind'] == 'file':
                    before = next(p for p in self.library.pages(metadata['document_id']) if p['id'] == metadata['page_id'])
                    self.library.remember_initial_ocr(metadata['page_id'], before.get('ocr_text') or value)
                    self.library.update_page_ocr(metadata['page_id'], value, expected_updated_at=metadata['expected_updated_at'])
                    self._refresh_current_page(metadata['document_id'], metadata['page_id'], previous_token=metadata['expected_updated_at'])
                    self.status.set('파일 읽기 완료 · 원문과 대조해 활용하세요.')
                else:
                    output = self.library.save_output(metadata['document_id'], metadata['mode'], value, metadata['source_fingerprint'])
                    current = self.document and self.document['id'] == metadata['document_id']
                    unchanged = current and not self._source_dirty and not self._output_dirty and self._output_revision == metadata['output_revision']
                    unchanged = unchanged and fingerprint(self.library.document_text(metadata['document_id'])) == metadata['source_fingerprint']
                    if unchanged:
                        self._load_output(output)
                        self.editor_tabs.select(self.ai_panel)
                        self.status.set('AI 정리를 보관했습니다. 사용 전 원문과 대조하세요.')
                    else:
                        self.status.set('AI 정리는 이전 결과 목록에 보관했습니다. 작업 중인 입력은 유지합니다.')
                self.refresh_library()
            except Exception as error:
                log_failure('desk_jobs._poll_results', error)
                self.status.set('응답을 저장하지 못했습니다. 현재 입력과 원본을 유지합니다. 저장 상태를 확인하세요.')
        if not self._closing:
            self.job_progress.refresh(self._jobs)
            self._poll_id = self.after(80, self._poll_results)

    def _refresh_current_page(self, document_id, page_id, previous_token=None):
        if not self.document or self.document['id'] != document_id or not self.page or self.page['id'] != page_id:
            return
        fresh = self.library.pages(document_id)
        index = next(i for i, page in enumerate(fresh) if page['id'] == page_id)
        if self._source_dirty:
            # Rebase only an OCR-only update against the same saved manuscript.
            if previous_token is not None and self.page['updated_at'] == previous_token:
                self.page_records, self.page = fresh, fresh[index]
            return
        self.page_records = fresh
        self._load_page(index)
