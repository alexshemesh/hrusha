"""The API key must never reach a log handler.

Alchemy puts the key in the URL path, so any library that logs a request
URL leaks it. `-v` turned web3/urllib3 to DEBUG and wrote the key to the
log file hundreds of times per sync; quieting those two names is not
enough, because the next dependency logs URLs too.
"""

import json
import logging

from hrusha.logs import JsonFormatter, redact, setup_logging

KEY = "FAKEkey0123456789abc"  # synthetic: shape of an Alchemy key, never a real one
RPC_URL = f"https://base-mainnet.g.alchemy.com/v2/{KEY}"
PRICES_URL = f"https://api.g.alchemy.com/prices/v1/{KEY}/tokens/historical"
PORTFOLIO_URL = f"https://api.g.alchemy.com/data/v1/{KEY}/assets/tokens/by-address"


def format_record(**kwargs) -> dict:
    record = logging.LogRecord("x", logging.INFO, "f", 1, kwargs.pop("msg", "m"), (), None)
    for field, value in kwargs.items():
        setattr(record, field, value)
    return json.loads(JsonFormatter().format(record))


def test_key_is_masked_in_the_message():
    line = format_record(msg=f"Making request HTTP. URI: {RPC_URL}, Method: eth_call")
    assert KEY not in json.dumps(line)
    assert "alchemy.com/v2/***" in line["message"]


def test_key_is_masked_in_every_alchemy_url_shape():
    for url in (RPC_URL, PRICES_URL, PORTFOLIO_URL):
        assert KEY not in redact(url), url
        assert "***" in redact(url), url


def test_key_is_masked_when_host_and_path_are_logged_apart():
    """urllib3's line shape: the host is not adjacent to the path, so a
    pattern anchored on 'alchemy.com/v2/' never matches it."""
    line = format_record(
        msg=f'https://base-mainnet.g.alchemy.com:443 "POST /v2/{KEY} HTTP/1.1" 200 42'
    )
    assert KEY not in json.dumps(line)


def test_real_path_words_are_not_mangled():
    assert redact("/v2/tokens/by-address") == "/v2/tokens/by-address"


def test_key_is_masked_in_extra_fields():
    """`extra={...}` values land in the JSON line verbatim."""
    line = format_record(msg="provider call failed", url=RPC_URL)
    assert KEY not in json.dumps(line)


def test_non_secret_content_survives():
    line = format_record(msg="syncing address", label="main", since_block=51403662)
    assert line["message"] == "syncing address"
    assert line["label"] == "main"
    assert line["since_block"] == 51403662


def test_url_logging_libraries_are_quieted(caplog):
    """A blocklist alone is not the defence, but it should still hold for
    the libraries we know log URLs."""
    setup_logging()
    for name in ("httpx", "httpcore", "web3", "urllib3", "requests"):
        assert logging.getLogger(name).level == logging.WARNING, name


def test_debug_level_does_not_lower_those_libraries():
    """`hrusha -v` is what caused the leak: it sets the ROOT logger to
    DEBUG, and web3/urllib3 inherited it."""
    setup_logging(logging.DEBUG)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("web3.providers.HTTPProvider").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("urllib3.connectionpool").getEffectiveLevel() == logging.WARNING
