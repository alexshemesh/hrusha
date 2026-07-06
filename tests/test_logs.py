import logging

from hrusha.logs import setup_logging


def test_httpx_request_logging_is_silenced():
    """httpx logs full URLs at INFO; Alchemy URLs embed the API key."""
    setup_logging(logging.DEBUG)  # even in verbose mode
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
