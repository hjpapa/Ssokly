"""Privacy scope orchestration for the lightweight capture desk; no UI/network."""
from dataclasses import replace
from hashlib import sha256

from services.transfer_policy import (
    ScopeExpansionRequired, TransferPolicy, TransferSnapshot, is_scope_reduction,
    make_image_snapshot,
)


def _key(prefix, identity):
    if identity is None or not str(identity).strip():
        raise ValueError('전송 자료의 로컬 식별자가 필요합니다.')
    return prefix + ':' + str(identity)


def _previous(policies):
    """Intersect unknown relationships, never union redacted source contents."""
    restricted = {policy.scope_id: policy for policy in policies if policy and policy.redacted}
    if not restricted:
        return next((policy for policy in policies if policy is not None), None)
    if len(restricted) == 1:
        return next(iter(restricted.values()))
    protected = tuple(sorted({token for policy in restricted.values() for token in policy.protected}))
    scopes = '\n'.join(sorted(restricted))
    return TransferPolicy('combined-' + sha256(scopes.encode()).hexdigest(), 'text', True,
                          approved_text='', protected=protected)


def _check_snapshot(snapshot, kind, *, force_mask=False):
    if not isinstance(snapshot, TransferSnapshot) or snapshot.kind != kind or snapshot.policy.kind != kind:
        raise ValueError('선택한 전송 사본의 종류가 올바르지 않습니다. 요청을 시작하지 않았습니다.')
    if force_mask and not snapshot.policy.redacted:
        raise ScopeExpansionRequired('가린 사본을 선택해 주세요. 원본으로 대체해 보내지 않습니다.')
    if kind == 'text':
        if not snapshot.text.strip():
            raise ValueError('전송할 텍스트 사본이 비어 있습니다.')
        snapshot.policy.guard_text(snapshot.text)
    else:
        # Decode before policy persistence, so damaged copies never grant scope.
        snapshot.as_image()


class DeskTransfer:
    def __init__(self, policy_store):
        self.policy_store = policy_store

    def _read(self, keys):
        return {key: self.policy_store.get(key) for key in dict.fromkeys(keys)}

    def _unchanged(self, saved):
        if any(self.policy_store.get(key) != policy for key, policy in saved.items()):
            raise ScopeExpansionRequired('전송 범위가 다른 작업에서 변경되었습니다. 최신 선택을 확인한 뒤 다시 시도해 주세요.')

    def inherit_document_policy(self, old_doc_ids, capture_ids, new_doc_id):
        """Persist old local restrictions BEFORE the caller copies source text.

        This is not transmission consent. Unrelated masked scopes are combined
        conservatively, and existing target restrictions can never be relaxed.
        A failure must abort the caller's source copy; an empty new document is
        recoverable without exposing an unprotected copy of the manuscript.
        """
        target = _key('document', new_doc_id)
        keys = [target, *(_key('document', value) for value in old_doc_ids),
                *(_key('capture', value) for value in capture_ids)]
        saved = self._read(keys)
        policy = _previous([item for item in saved.values() if item and item.redacted])
        self._unchanged(saved)
        if policy is None:
            return None
        old_target = saved[target]
        self.policy_store.save(target, policy,
                               expected_scope_id=old_target.scope_id if old_target else None)
        # A writer can race the preceding reads. Fail closed before allowing the
        # source copy if any ancestor changed during the target commit.
        self._unchanged({key: item for key, item in saved.items() if key != target})
        return policy

    def image_snapshot(self, capture_id, image, *, auto_allowed=False, force_mask=False,
                       document_ids=(), choose):
        """Auto-read only a fresh/same unredacted capture; old masks require UI."""
        capture_key = _key('capture', capture_id)
        saved = self._read([capture_key, *(_key('document', value) for value in document_ids)])
        previous = _previous(list(saved.values()))
        candidate = make_image_snapshot(image)
        old_capture = saved[capture_key]
        unchanged_image = (old_capture is None or (old_capture.kind == 'image'
                           and old_capture.image_source_digest == candidate.policy.image_source_digest))
        if auto_allowed and not force_mask and not (previous and previous.redacted) and unchanged_image:
            selected = candidate
        else:
            selected = choose(kind='image', image=candidate.as_image(), previous=previous,
                              title='캡처 AI 전송 대상 확인')
            if selected is None:
                return None
        _check_snapshot(selected, 'image', force_mask=force_mask)
        self._unchanged(saved)
        self.policy_store.save(capture_key, selected.policy,
                               expected_scope_id=old_capture.scope_id if old_capture else None)
        return selected

    def text_snapshot(self, document_id, text, *, page_policies=(), choose):
        """Each action explicitly selects its text; old page exclusions follow it.

        page_policies may contain TransferPolicy objects or capture-ID strings.
        IDs allow rechecking current persisted policy after the dialog returns.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError('전송할 원문을 입력해 주세요.')
        document_key = _key('document', document_id)
        page_keys, detached = [], []
        for item in page_policies:
            if isinstance(item, TransferPolicy):
                detached.append(item)
            elif isinstance(item, (str, int)) and not isinstance(item, bool):
                page_keys.append(_key('capture', item))
            else:
                raise ValueError('페이지 전송 정책의 형식이 올바르지 않습니다.')
        saved = self._read([document_key, *page_keys])
        previous = _previous([*saved.values(), *detached])
        selected = choose(kind='text', text=text, previous=previous, title='텍스트 AI 전송 대상 확인')
        if selected is None:
            return None
        _check_snapshot(selected, 'text')
        if previous and previous.redacted and selected.policy.redacted:
            try:
                previous.guard_text(selected.text, strict=False)
                reduction = is_scope_reduction(previous.approved_text, selected.text)
            except ScopeExpansionRequired:
                reduction = False
            if reduction:
                selected = replace(selected, policy=replace(selected.policy, protected=tuple(sorted(
                    set(selected.policy.protected) | set(previous.protected)))))
        self._unchanged(saved)
        old_document = saved[document_key]
        self.policy_store.save(document_key, selected.policy,
                               expected_scope_id=old_document.scope_id if old_document else None)
        return selected

    def record_ocr(self, capture_id, snapshot, text):
        """Register only this approved image's OCR output, using scope CAS."""
        _check_snapshot(snapshot, 'image')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('완료된 OCR 텍스트가 없어 전송 범위를 갱신하지 않았습니다.')
        policy = snapshot.policy.with_safe_text(text)
        self.policy_store.save(_key('capture', capture_id), policy,
                               expected_scope_id=snapshot.policy.scope_id)
        return policy
