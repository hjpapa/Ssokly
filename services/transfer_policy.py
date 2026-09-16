"""Immutable, explicitly selected outbound copies; never substitutes originals.

This module has no network calls. Policies are local metadata, not a declaration
that a document is legally safe to share or an exhaustive privacy detector.
"""
from dataclasses import dataclass, replace
from contextlib import closing
from difflib import SequenceMatcher
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import json
import sqlite3
from typing import Iterable
import uuid

from PIL import Image, ImageDraw, ImageOps


MAX_FILE_BYTES = 50 * 1024 * 1024
_ANY_SCOPE = object()


class ScopeExpansionRequired(ValueError):
    """A later request includes data outside the approved redacted copy."""


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _fingerprint(value: str) -> tuple[int, str]:
    normalized = _normalized(value)
    return len(normalized), sha256(normalized.encode("utf-8")).hexdigest()


_EDIT_TOKEN = re.compile(r"\[[^\]\r\n]+\]|\w+(?:[-.@+]\w+)*|\s+|[^\w\s]", re.UNICODE)
_MASK_TOKEN = re.compile(r"\[가림\d*\]")


def removed_text_fragments(original: str, edited: str) -> list[str]:
    """Compare whole edit tokens, never a mask number with an ID suffix.

    Names, identifiers, phone/email tokens and bracketed replacements are
    atomic. A selected redaction is recorded separately by TextRedactionDraft;
    this function handles free typing before/between/after those selections.
    """
    before, after = _EDIT_TOKEN.findall(original), _EDIT_TOKEN.findall(edited)
    matcher = SequenceMatcher(None, before, after, autojunk=False)
    return ["".join(before[start:end]).strip()
            for operation, start, end, _, _ in matcher.get_opcodes()
            if operation in {"replace", "delete"} and "".join(before[start:end]).strip()]


def is_scope_reduction(approved: str, selected: str) -> bool:
    """UI-only comparison: retained excerpts stay ordered around mask tokens.

    Not a grant to send a changed source without confirmation. Network guards
    accept only a continuous excerpt; a changed editor must be selected again.
    """
    approved = _normalized(approved)
    selected = _normalized(selected)
    if not selected:
        return True
    if not approved:
        return False
    if not _MASK_TOKEN.search(selected):
        return selected in approved
    position = 0
    for retained in _MASK_TOKEN.split(selected):
        if not retained:
            continue
        index = approved.find(retained, position)
        if index < 0:
            return False
        position = index + len(retained)
    return True


class TextRedactionDraft:
    """Local editor bookkeeping; never itself used as an outbound payload."""
    def __init__(self, text: str):
        self.baseline = text
        self.excluded: list[str] = []

    def record_selection(self, before: str, after: str, removed: str) -> None:
        self.excluded.extend(removed_text_fragments(self.baseline, before))
        self.excluded.append(removed)
        self.baseline = after

    def snapshot(self, text: str) -> "TransferSnapshot":
        return make_text_snapshot(text, original_text=self.baseline, excluded_strings=self.excluded)


@dataclass(frozen=True)
class TransferPolicy:
    scope_id: str
    kind: str
    redacted: bool = False
    approved_text: str = ""
    protected: tuple[tuple[int, str], ...] = ()
    image_source_digest: str = ""
    image_rectangles: tuple[tuple[int, int, int, int], ...] = ()

    def with_safe_text(self, text: str) -> "TransferPolicy":
        """Register OCR/model output from this copy, never a local original.

        Callers must not use this method to approve user-restored information;
        such expansion requires a fresh, explicit transfer selection instead.
        """
        self.guard_text(text, strict=False)
        return replace(self, approved_text=text)

    def guard_text(self, text: str, *, strict: bool | None = None) -> str:
        normalized = _normalized(text)
        for length, digest in self.protected:
            if any(sha256(normalized[i:i + length].encode("utf-8")).hexdigest() == digest
                   for i in range(max(0, len(normalized) - length + 1))):
                raise ScopeExpansionRequired("가렸던 내용이 다시 포함되었습니다. 전송 범위를 다시 선택해 주세요.")
        if strict is None:
            strict = self.redacted
        if strict and normalized:
            approved = _normalized(self.approved_text)
            # A continuous excerpt (ignoring whitespace) is allowed; combining
            # letters/words from unrelated positions creates unapproved data.
            if not approved or normalized not in approved:
                raise ScopeExpansionRequired("가린 사본에 없던 내용이 추가되었습니다. 전송 범위를 다시 선택해 주세요.")
        return text

    def guard_fields(self, values: Iterable[str]) -> None:
        for value in values:
            self.guard_text(value)

    def to_dict(self) -> dict:
        # No original excluded strings or file bytes are persisted here.
        return {"version": 1, "scope_id": self.scope_id, "kind": self.kind,
                "redacted": self.redacted, "approved_text": self.approved_text,
                "protected": [list(item) for item in self.protected],
                "image_source_digest": self.image_source_digest,
                "image_rectangles": [list(rect) for rect in self.image_rectangles]}

    @classmethod
    def from_dict(cls, value: dict) -> "TransferPolicy":
        if value.get("version") != 1 or value.get("kind") not in {"text", "image", "file"}:
            raise ValueError("지원하지 않는 전송 정책입니다. 전송 범위를 다시 선택해 주세요.")
        protected = tuple((int(length), str(digest)) for length, digest in value.get("protected", []))
        if any(length <= 0 or not re.fullmatch(r"[0-9a-f]{64}", digest) for length, digest in protected):
            raise ValueError("전송 정책이 손상되었습니다. 전송 범위를 다시 선택해 주세요.")
        if not isinstance(value.get("redacted"), bool) or not isinstance(value.get("approved_text", ""), str):
            raise ValueError("전송 정책이 손상되었습니다. 전송 범위를 다시 선택해 주세요.")
        digest = value.get("image_source_digest", "")
        rectangles = tuple(tuple(rect) for rect in value.get("image_rectangles", []))
        if (not isinstance(digest, str) or (digest and not re.fullmatch(r"[0-9a-f]{64}", digest))
                or any(len(rect) != 4 or any(not isinstance(v, int) or isinstance(v, bool) for v in rect)
                       or not (0 <= rect[0] < rect[2] and 0 <= rect[1] < rect[3]) for rect in rectangles)):
            raise ValueError("이미지 가림 정책이 손상되었습니다. 전송 범위를 다시 선택해 주세요.")
        return cls(str(value["scope_id"]), str(value["kind"]), value["redacted"],
                   value.get("approved_text", ""), protected, digest, rectangles)


@dataclass(frozen=True)
class TransferSnapshot:
    kind: str
    policy: TransferPolicy
    text: str = ""
    image_png: bytes = b""
    file_bytes: bytes = b""
    file_name: str = ""

    def __post_init__(self):
        if self.kind not in {"text", "image", "file"}:
            raise ValueError("지원하지 않는 전송 종류입니다.")
        if self.kind == "text" and (self.image_png or self.file_bytes):
            raise ValueError("텍스트 사본에 원본 첨부를 포함할 수 없습니다.")
        if self.kind == "image" and (not self.image_png or self.text or self.file_bytes):
            raise ValueError("이미지 사본이 없거나 잘못된 첨부가 포함되었습니다.")
        if self.kind == "file" and (not self.file_bytes or self.image_png or self.text):
            raise ValueError("파일 사본이 없거나 잘못된 첨부가 포함되었습니다.")

    @property
    def fingerprint(self) -> str:
        return sha256(self.kind.encode() + b"\0" + self.text.encode("utf-8")
                      + self.image_png + self.file_bytes).hexdigest()

    def as_image(self) -> Image.Image:
        if self.kind != "image":
            raise ValueError("전송 대상으로 선택한 이미지만 사용할 수 있습니다.")
        with Image.open(BytesIO(self.image_png)) as image:
            image.load()
            return image.copy()

    def guard_text(self, text: str, **kwargs) -> str:
        return self.policy.guard_text(text, **kwargs)

    def with_safe_text(self, text: str) -> "TransferSnapshot":
        return replace(self, policy=self.policy.with_safe_text(text))

    def to_policy_dict(self) -> dict:
        return self.policy.to_dict()


class TransferPolicyStore:
    """Local scope metadata only; corrupt/unreadable policies fail closed."""
    def __init__(self, app_data_dir: Path | str):
        directory = Path(app_data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "transfer_policies.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS transfer_policies (scope_key TEXT PRIMARY KEY, policy_json TEXT NOT NULL)")

    def get(self, key: str) -> TransferPolicy | None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute("SELECT policy_json FROM transfer_policies WHERE scope_key = ?", (key,)).fetchone()
        return TransferPolicy.from_dict(json.loads(row[0])) if row else None

    def save(self, key: str, policy: TransferPolicy, *, expected_scope_id=_ANY_SCOPE) -> None:
        """Atomically save; optional compare-and-swap prevents stale grants.

        Omit expected_scope_id to explicitly replace the policy. Passing None
        requires that no policy exists; a string requires that exact scope.
        """
        if not key or not isinstance(policy, TransferPolicy):
            raise ValueError("저장할 전송 정책이 올바르지 않습니다.")
        serialized = json.dumps(policy.to_dict(), ensure_ascii=False)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_scope_id is not _ANY_SCOPE:
                row = connection.execute("SELECT policy_json FROM transfer_policies WHERE scope_key = ?", (key,)).fetchone()
                current_scope = TransferPolicy.from_dict(json.loads(row[0])).scope_id if row else None
                if current_scope != expected_scope_id:
                    raise ScopeExpansionRequired("전송 범위가 다른 작업에서 변경되었습니다. 최신 선택을 확인한 뒤 다시 시도해 주세요.")
            connection.execute("INSERT INTO transfer_policies(scope_key, policy_json) VALUES (?, ?) ON CONFLICT(scope_key) DO UPDATE SET policy_json = excluded.policy_json", (key, serialized))


def make_text_snapshot(text: str, *, original_text: str | None = None,
                       excluded_strings: Iterable[str] = (),
                       previous: TransferPolicy | None = None) -> TransferSnapshot:
    """Create an outbound string, protecting removed/replaced original spans."""
    excluded = list(excluded_strings)
    if original_text is not None and original_text != text:
        excluded.extend(removed_text_fragments(original_text, text))
    protected = set(previous.protected if previous else ())
    for item in excluded:
        if _normalized(item):
            protected.add(_fingerprint(item))
    policy = TransferPolicy(uuid.uuid4().hex, "text",
                            bool(protected) or bool(previous and previous.redacted), text,
                            tuple(sorted(protected)))
    # Replacement fragments can also legitimately occur in unmodified text.
    # In that case the redaction is incomplete; stop, do not send an original.
    policy.guard_text(text, strict=False)
    return TransferSnapshot("text", policy, text=text)


def redact_text(text: str, spans: Iterable[tuple[int, int]]) -> TransferSnapshot:
    selected = sorted(spans)
    last_end = 0
    chunks, excluded = [], []
    for number, (start, end) in enumerate(selected, 1):
        if not isinstance(start, int) or not isinstance(end, int) or start < last_end or end <= start or end > len(text):
            raise ValueError("가림 범위가 올바르지 않거나 겹칩니다.")
        chunks.extend([text[last_end:start], f"[가림{number}]"])
        excluded.append(text[start:end])
        last_end = end
    if not selected:
        raise ValueError("가릴 텍스트를 선택해 주세요.")
    chunks.append(text[last_end:])
    return make_text_snapshot("".join(chunks), excluded_strings=excluded)


def make_image_snapshot(image: Image.Image, *, rectangles: Iterable[tuple[int, int, int, int]] = ()) -> TransferSnapshot:
    """Bake opaque rectangles into RGB pixels and strip original metadata."""
    rects = tuple(rectangles)
    oriented = ImageOps.exif_transpose(image)
    rgba = oriented.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    flattened = Image.alpha_composite(background, rgba).convert("RGB")
    # A fresh image prevents EXIF/comments/hidden metadata from accompanying it.
    clean = Image.new("RGB", flattened.size)
    clean.paste(flattened)
    width, height = clean.size
    if width <= 0 or height <= 0:
        raise ValueError("이미지 크기가 올바르지 않습니다.")
    source_digest = sha256(f"{width}x{height}:".encode() + clean.tobytes()).hexdigest()
    draw = ImageDraw.Draw(clean)
    for rect in rects:
        if (len(rect) != 4 or any(not isinstance(value, int) or isinstance(value, bool) for value in rect)):
            raise ValueError("이미지 가림 좌표는 정수여야 합니다.")
        left, top, right, bottom = rect
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("이미지 가림 영역이 비어 있거나 이미지 밖에 있습니다.")
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
    buffer = BytesIO()
    clean.save(buffer, format="PNG")
    return TransferSnapshot("image", TransferPolicy(uuid.uuid4().hex, "image", bool(rects),
                                                    image_source_digest=source_digest,
                                                    image_rectangles=rects),
                            image_png=buffer.getvalue())


def restore_image_snapshot(image: Image.Image, policy: TransferPolicy) -> TransferSnapshot:
    """Reapply saved opaque masks only to the exact original pixel content."""
    if not policy.redacted or not policy.image_rectangles or not policy.image_source_digest:
        raise ValueError("저장된 이미지 가림 범위가 없습니다.")
    normalized = make_image_snapshot(image)
    if normalized.policy.image_source_digest != policy.image_source_digest:
        raise ScopeExpansionRequired("이미지가 바뀌어 이전 가림 영역을 적용하지 않았습니다. 전송 범위를 다시 선택해 주세요.")
    restored = make_image_snapshot(normalized.as_image(), rectangles=policy.image_rectangles)
    return replace(restored, policy=policy)


def make_file_snapshot(path: Path | str) -> TransferSnapshot:
    source = Path(path)
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("첨부 파일이 50MB를 초과합니다.")
    payload = source.read_bytes()
    if not payload or len(payload) > MAX_FILE_BYTES:
        raise ValueError("첨부 파일이 비어 있거나 50MB를 초과합니다.")
    return TransferSnapshot("file", TransferPolicy(uuid.uuid4().hex, "file"),
                            file_bytes=payload, file_name=source.name)


@dataclass(frozen=True)
class RiskCandidate:
    category: str
    start: int
    end: int


def risk_candidates(text: str) -> list[RiskCandidate]:
    """Small local warning hints, deliberately not all names/contact numbers."""
    found: set[tuple[str, int, int]] = set()
    patterns = [
        ("식별번호", r"(?<!\d)\d{6}\s*[- ]\s*[1-8]\d{6}(?!\d)"),
        ("인증정보", r"(?i)(?:비밀번호|인증번호|api[_ -]?key|password|access[_ -]?token)\s*[:=：]\s*[^\s,;]{3,}"),
        ("금융정보", r"(?:계좌(?:번호)?|카드번호)\s*[:：]?\s*[\d -]{8,}"),
        ("학생·학부모 식별", r"(?:학생|학부모|보호자)\s*(?:성명|이름|연락처|전화|휴대전화)\s*[:：]\s*[^\n\t,;]{2,40}"),
        ("건강·상담 문맥", r"(?:학생|학부모|보호자)[^\n]{0,45}(?:진단|질환|투약|병력|상담내용|상담 내용)[^\n]{0,45}"),
        ("개인 연락처", r"(?:학생|학부모|보호자|개인)[^\n]{0,35}01[016789][- ]?\d{3,4}[- ]?\d{4}"),
    ]
    for category, pattern in patterns:
        for match in re.finditer(pattern, text):
            found.add((category, match.start(), match.end()))
    return [RiskCandidate(*item) for item in sorted(found, key=lambda item: (item[1], item[2], item[0]))]
