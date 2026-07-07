import logging

from hrusha.logs import setup_logging


def test_httpx_request_logging_is_silenced():
    """httpx logs full URLs at INFO; Alchemy URLs embed the API key."""
    setup_logging(logging.DEBUG)  # even in verbose mode
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_log_dir_env_mirrors_json_lines_to_rotating_file(tmp_path, monkeypatch):
    import json
    import logging

    from hrusha.logs import LOG_FILE_NAME, setup_logging

    monkeypatch.setenv("HRUSHA_LOG_DIR", str(tmp_path / "logs"))
    setup_logging()
    logging.getLogger("hrusha.test").info("mirrored", extra={"sync_run_id": "abc"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    line = json.loads((tmp_path / "logs" / LOG_FILE_NAME).read_text().strip().splitlines()[-1])
    assert line["message"] == "mirrored"
    assert line["sync_run_id"] == "abc"
    # and without the env var, no file handler comes back
    monkeypatch.delenv("HRUSHA_LOG_DIR")
    setup_logging()
    assert len(logging.getLogger().handlers) == 1
