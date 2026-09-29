"""Local diagnostics containing only static operation names and exception types."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER = logging.getLogger('ssokly.diagnostics')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
LOGGER.setLevel(logging.WARNING)


class _LocalHandler(RotatingFileHandler):
    def emit(self, record):
        try:
            super().emit(record)
        finally:
            # Do not lock the user's data folder between diagnostic writes.
            self.close()

    def handleError(self, record):
        # A full/unwritable log disk must not break saving or expose a traceback.
        pass


def configure_local_logging(app_data_dir):
    for handler in list(LOGGER.handlers):
        if isinstance(handler, _LocalHandler):
            LOGGER.removeHandler(handler)
            handler.close()
    try:
        directory = Path(app_data_dir) / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        handler = _LocalHandler(directory / 'diagnostics.log', maxBytes=256 * 1024,
                                backupCount=2, encoding='utf-8', delay=True)
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        LOGGER.addHandler(handler)
    except OSError:
        return False
    return True


def log_failure(operation, error):
    """Call with a code-defined operation, never document data or error text."""
    LOGGER.warning('%s: %s', operation, type(error).__name__)
