"""Legacy UI scenarios start after an explicit synthetic transfer approval.

Cancellation/redaction/payload guarantees have separate V2 tests; this helper
must not be used to bypass the actual selector in those acceptance cases.
"""
from services.transfer_policy import make_file_snapshot, make_image_snapshot, make_text_snapshot


def approve_synthetic_transfer(_parent, *, kind, text='', image=None, path=None, **_kwargs):
    if kind == 'text':
        return make_text_snapshot(text)
    if kind == 'image':
        return make_image_snapshot(image)
    if kind == 'file':
        return make_file_snapshot(path)
    raise AssertionError('Unsupported synthetic transfer kind: ' + kind)
