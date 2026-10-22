"""JSON log lines: mandatory fields, redaction, principal hashing, correlation id propagation."""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

import tidal
from tidal.common.config import Settings
from tidal.common.logging import (
    REDACTED,
    bind_correlation_id,
    configure_logging,
    correlation_scope,
    get_correlation_id,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    scrub_text,
)

ULID_RE = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
CID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
MANDATORY = {"ts", "level", "service", "version", "correlation_id", "event"}


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def _configure(**environ: str) -> None:
    configure_logging(Settings.from_env({"TIDAL_SERVICE_NAME": "worker", **environ}))


def test_log_line_has_mandatory_fields(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with correlation_scope(CID):
        get_logger("t").info("job_started", job_id="bt_1")
    (line,) = _lines(capsys)
    assert line.keys() >= MANDATORY
    assert line["service"] == "worker"
    assert line["version"] == tidal.__version__
    assert line["level"] == "info"
    assert line["event"] == "job_started"
    assert line["correlation_id"] == CID
    assert line["ts"].endswith("Z")
    assert line["job_id"] == "bt_1"


def test_correlation_id_null_when_unbound(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    get_logger().info("boot")
    (line,) = _lines(capsys)
    assert line["correlation_id"] is None


def test_stdlib_loggers_share_format_and_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with correlation_scope(CID):
        logging.getLogger("azure.core").warning("retrying")
    (line,) = _lines(capsys)
    assert line.keys() >= MANDATORY
    assert line["event"] == "retrying"
    assert line["level"] == "warning"
    assert line["correlation_id"] == CID


def test_secret_keys_and_values_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    secret_keys = (
        "password",
        "connection_string",
        "Authorization",
        "eia_api_key",
        "refresh_token",
        "client_secret",
    )
    get_logger().info(
        "connect",
        **dict.fromkeys(secret_keys, "hunter2"),
        some_value=SecretStr("hunter2"),
        nested={"sql": {"password": "hunter2", "server": "db"}},
        listed=[SecretStr("hunter2"), "ok"],
        rows=3,
    )
    raw = capsys.readouterr().out
    assert "hunter2" not in raw
    line = json.loads(raw)
    for key in (*secret_keys, "some_value"):
        assert line[key] == REDACTED
    assert line["nested"] == {"sql": {"password": REDACTED, "server": "db"}}
    assert line["listed"] == [REDACTED, "ok"]
    assert line["rows"] == 3


def test_settings_secret_never_logged(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings.from_env({"TIDAL_SQL__PASSWORD": "hunter2"})
    configure_logging(settings)
    get_logger().info("settings_loaded", settings=settings, sql=settings.sql)
    assert "hunter2" not in capsys.readouterr().out


def test_principal_oid_hashed(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    oid = "11111111-2222-3333-4444-555555555555"
    get_logger().info("request", principal_oid=oid)
    (line,) = _lines(capsys)
    assert line["principal_oid"] == hashlib.sha256(oid.encode()).hexdigest()
    assert oid not in json.dumps(line)


def test_raw_oid_hashed(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    oid = "11111111-2222-3333-4444-555555555555"
    get_logger().info("request", oid=oid)
    (line,) = _lines(capsys)
    assert line["oid"] == hashlib.sha256(oid.encode()).hexdigest()


JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("DefaultEndpointsProtocol=https;AccountKey=LEAKED==;EndpointSuffix=x", "LEAKED"),
        ("Endpoint=sb://x/;SharedAccessKeyName=k;SharedAccessKey=LEAKED=", "LEAKED"),
        ("SharedAccessSignature=LEAKED;Endpoint=x", "LEAKED"),
        ("https://acct.blob.core.windows.net/c?sv=2024&sig=LEAKED%2Fx&se=1", "LEAKED"),
        ("header Authorization: Bearer LEAKED.tok-en_9", "LEAKED"),
        ("Server=db;Password=LEAKED;", "LEAKED"),
        ("Server=db;pwd=LEAKED", "LEAKED"),
        (f"token was {JWT} ok", "c2lnbmF0dXJl"),
    ],
)
def test_scrub_text_masks_secret_patterns(text: str, leaked: str) -> None:
    scrubbed = scrub_text(text)
    assert leaked not in scrubbed
    assert REDACTED in scrubbed


def test_scrub_text_keeps_ordinary_text() -> None:
    text = "fetched 3 bars for EIA.BRENT.SPOT signal=long key_count=2"
    assert scrub_text(text) == text


def test_structlog_exception_text_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    try:
        raise RuntimeError("AccountKey=LEAKED")
    except RuntimeError:
        get_logger().exception("upload_failed")
    raw = capsys.readouterr().out
    assert "LEAKED" not in raw
    assert "RuntimeError: AccountKey=" in json.loads(raw)["exception"]


def test_stdlib_exc_info_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    try:
        raise RuntimeError(f"Bearer {JWT}")
    except RuntimeError:
        logging.getLogger("azure.core").exception("request failed")
    raw = capsys.readouterr().out
    assert JWT not in raw
    assert "RuntimeError" in json.loads(raw)["exception"]


def test_stdlib_args_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    logging.getLogger("azure").warning("conn %s", "AccountKey=x")
    (line,) = _lines(capsys)
    assert line["event"] == f"conn AccountKey={REDACTED}"


def test_structlog_positional_args_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    get_logger().info("conn %s", "Password=LEAKED")
    raw = capsys.readouterr().out
    assert "LEAKED" not in raw
    assert json.loads(raw)["event"] == f"conn Password={REDACTED}"


def test_sets_are_walked(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    get_logger().info(
        "x", tags={SecretStr("LEAKED")}, frozen=frozenset({"sig=LEAKED"}), plain={"a"}
    )
    raw = capsys.readouterr().out
    assert "LEAKED" not in raw
    line = json.loads(raw)
    assert line["tags"] == [REDACTED]
    assert line["frozen"] == [f"sig={REDACTED}"]
    assert line["plain"] == ["a"]


def test_log_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    _configure(TIDAL_LOG_LEVEL="WARNING")
    log = get_logger()
    log.info("hidden")
    log.warning("shown")
    assert [line["event"] for line in _lines(capsys)] == ["shown"]


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    _configure()
    _configure(TIDAL_SERVICE_NAME="api")
    get_logger().info("once")
    lines = _lines(capsys)
    assert len(lines) == 1
    assert lines[0]["service"] == "api"


def test_exception_logged_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger().exception("failed")
    (line,) = _lines(capsys)
    assert "ValueError: boom" in line["exception"]
    assert line["level"] == "error"


def test_new_correlation_id_is_ulid() -> None:
    first, second = new_correlation_id(), new_correlation_id()
    assert first != second
    assert re.fullmatch(ULID_RE, first)


def test_bind_get_reset() -> None:
    assert get_correlation_id() is None
    token = bind_correlation_id("abc")
    assert get_correlation_id() == "abc"
    reset_correlation_id(token)
    assert get_correlation_id() is None
    with correlation_scope() as cid:
        assert get_correlation_id() == cid
        assert re.fullmatch(ULID_RE, cid)
    assert get_correlation_id() is None


def test_correlation_id_propagates_per_task(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()

    async def handle(cid: str) -> str | None:
        with correlation_scope(cid):
            await asyncio.sleep(0)
            get_logger().info("step", expected=cid)
            await asyncio.sleep(0)
            return get_correlation_id()

    async def main() -> list[str | None]:
        return await asyncio.gather(*(handle(f"cid-{i}") for i in range(5)))

    assert asyncio.run(main()) == [f"cid-{i}" for i in range(5)]
    lines = _lines(capsys)
    assert len(lines) == 5
    assert all(line["correlation_id"] == line["expected"] for line in lines)
    assert get_correlation_id() is None


def test_http_client_loggers_never_log_urls_at_info() -> None:
    configure_logging(Settings.from_env({"TIDAL_LOG_LEVEL": "DEBUG"}))
    for name in ("httpx", "httpcore", "urllib3"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
