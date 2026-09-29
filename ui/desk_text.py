"""Text helpers shared by the capture desk modules."""
import hashlib


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
