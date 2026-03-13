"""Centralised logging configuration for AML Rule Tester.

All modules call get_logger(__name__) to obtain a child logger.
Logs are written to logs/aml_tester_YYYY-MM-DD.log (daily rotation, 7-day retention).
Console output is WARNING+ only to keep the Streamlit terminal clean.
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler

_LOGGER_NAME = "aml_tester"
_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _configure():
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(logging.DEBUG)

    # File handler — DEBUG+, rotates daily, keeps 7 days
    log_path = os.path.join(_LOG_DIR, "aml_tester.log")
    fh = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    fh.suffix = "%Y-%m-%d"
    root.addHandler(fh)

    # Console handler — WARNING+ only (don't spam Streamlit terminal)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the aml_tester namespace.

    Usage in any module:
        from logging_config import get_logger
        log = get_logger(__name__)
    """
    _configure()
    # Strip the package prefix so log names are short (e.g. "llm.llm_wrapper")
    short = name.replace("__main__", "app")
    return logging.getLogger(f"{_LOGGER_NAME}.{short}")
