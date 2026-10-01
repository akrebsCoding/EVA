"""Smoke-Test der Logging-Grundlage (P1.T1, `PLAN.md` §7 → P1.T1, Layer **L0**).

Geprüft wird das Schema `manager.<modul>`, die Level-Auflösung (Settings,
case-insensitiv, `INFO`-Fallback) und dass ein Log-Record tatsächlich im
Handler landet. **Kein** Netzzugang, keine externen Dienste, kein `tmp_path`
nötig – alles ist reines Stdlib-Logging auf `stdout`.

Hinweis für P1.T4: `pytest.ini` (Marker-Registrierung, `addopts`) fehlt noch,
dieser Test läuft deshalb bewusst **ohne** `--strict-markers`. Der Import von
`app.*` setzt voraus, dass die Projektwurzel in `sys.path` liegt – bei
`python -m pytest` aus der Projektwurzel ist das der Fall; `pytest.ini` sollte
das über `pythonpath = .` explizit machen.
"""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator

import pytest

from app import logger as logger_mod
from app.config import settings
from app.logger import (
    DEFAULT_LOG_LEVEL,
    LOG_FORMAT,
    LOGGER_NAMESPACE,
    configure_logging,
    get_logger,
    qualify_name,
    resolve_level,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Nach jedem Test: Manager-Level und Handler-Zustand wiederherstellen.

    Nur *eigene* Handler werden entfernt – pytest hängt seine
    `LogCaptureHandler` selbst an `manager` (weil `propagate = False`,
    siehe `_pytest/logging.py`), die dürfen nicht angefasst werden.
    """
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    level_before = manager_logger.level
    handlers_before = list(manager_logger.handlers)
    yield
    for handler in list(manager_logger.handlers):
        if handler not in handlers_before:
            manager_logger.removeHandler(handler)
    manager_logger.setLevel(level_before)
    # `_handler_installed` an den echten Zustand angleichen.
    logger_mod._handler_installed = any(
        handler.get_name() == "manager-stdout" for handler in manager_logger.handlers
    )


@pytest.fixture
def captured_manager_stream() -> Iterator[io.StringIO]:
    """Eigener Handler am `manager`-Logger ⇒ Aufzeichnung ohne pytest-Capture."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(stream=buffer)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    manager_logger.addHandler(handler)
    try:
        yield buffer
    finally:
        manager_logger.removeHandler(handler)


# ── Schema `manager.<modul>` ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("pipeline", "manager.pipeline"),
        ("manager.pipeline", "manager.pipeline"),
        ("manager", "manager"),
        ("app.ws_server", "manager.ws_server"),
        ("app.wake_word", "manager.wake_word"),
        ("manager.ha_client", "manager.ha_client"),
        ("  ws_server  ", "manager.ws_server"),
        (".manager.audio_bridge.", "manager.audio_bridge"),
    ],
)
def test_qualify_name_normalizes_to_namespace(given: str, expected: str) -> None:
    assert qualify_name(given) == expected


def test_get_logger_never_doubles_the_prefix() -> None:
    for name in ("pipeline", "manager.pipeline", "app.pipeline"):
        assert get_logger(name).name == "manager.pipeline"
    # Schema aus PLAN §7 P1.T1, konkrete Modulnamen aus P2/P5.
    for module in ("pipeline", "ws_server", "wake_word", "protocol", "router"):
        assert get_logger(module).name == f"manager.{module}"


def test_get_logger_is_idempotent_and_singleton() -> None:
    first = get_logger("pipeline")
    second = get_logger("manager.pipeline")
    assert first is second
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    assert [h.get_name() for h in manager_logger.handlers].count("manager-stdout") == 1


def test_configure_logging_does_not_duplicate_handlers() -> None:
    configure_logging()
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    after_first = len(manager_logger.handlers)
    configure_logging()
    configure_logging()
    assert after_first >= 1
    assert len(manager_logger.handlers) == after_first
    assert [h.get_name() for h in manager_logger.handlers].count("manager-stdout") == 1
    assert manager_logger.propagate is False


@pytest.mark.parametrize("bad", ["", "   ", ".", ".."])
def test_qualify_name_rejects_empty(bad: str) -> None:
    with pytest.raises(ValueError):
        qualify_name(bad)


def test_qualify_name_rejects_wrong_type() -> None:
    with pytest.raises(TypeError):
        qualify_name(object())  # type: ignore[arg-type]


# ── Level ────────────────────────────────────────────────────────────
def test_level_comes_from_settings() -> None:
    assert DEFAULT_LOG_LEVEL == "INFO"
    assert settings.log_level == "INFO"
    assert resolve_level(settings.log_level) == logging.INFO
    configure_logging()
    assert logging.getLogger(LOGGER_NAMESPACE).level == logging.INFO


def test_level_from_settings_is_taken_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "log_level", "DEBUG")
    configure_logging()
    assert logging.getLogger(LOGGER_NAMESPACE).level == logging.DEBUG
    assert get_logger("pipeline").getEffectiveLevel() == logging.DEBUG


def test_level_is_case_insensitive() -> None:
    assert resolve_level("debug") == logging.DEBUG
    assert resolve_level("Debug") == logging.DEBUG
    assert resolve_level("  warning  ") == logging.WARNING
    assert resolve_level("error") == logging.ERROR
    configure_logging("debug")
    assert logging.getLogger(LOGGER_NAMESPACE).level == logging.DEBUG


def test_invalid_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch, captured_manager_stream: io.StringIO
) -> None:
    monkeypatch.setattr(logger_mod, "_warned_invalid_level", False)
    assert resolve_level("quatsch") == logging.INFO
    configure_logging("quatsch")
    assert logging.getLogger(LOGGER_NAMESPACE).level == logging.INFO
    warn_text = captured_manager_stream.getvalue()
    assert "Ungültiges Log-Level" in warn_text
    assert "quatsch" in warn_text
    assert "INFO" in warn_text


def test_invalid_level_warns_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logger_mod, "_warned_invalid_level", False)
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    buffer = io.StringIO()
    handler = logging.StreamHandler(stream=buffer)
    manager_logger.addHandler(handler)
    try:
        for _ in range(3):
            assert resolve_level("kaputt") == logging.INFO
    finally:
        manager_logger.removeHandler(handler)
    assert buffer.getvalue().count("Ungültiges Log-Level") == 1


def test_resolve_level_passes_numbers_through_and_defaults() -> None:
    assert resolve_level(None) == logging.INFO
    assert resolve_level(logging.CRITICAL) == logging.CRITICAL
    assert resolve_level(35) == 35
    # `bool` ist ein `int`-Subtyp, Level `True` ist aber Unsinn.
    assert resolve_level(True) == logging.INFO


# ── Ausgabe ──────────────────────────────────────────────────────────
def test_record_reaches_the_manager_handler(
    captured_manager_stream: io.StringIO,
) -> None:
    configure_logging("INFO")
    get_logger("pipeline").info("wake=%s score=%.2f", "hey_jarvis", 0.93)
    line = captured_manager_stream.getvalue().strip().splitlines()[-1]
    assert " INFO manager.pipeline: " in line
    assert "wake=hey_jarvis score=0.93" in line


def test_record_is_captured_by_caplog(caplog: pytest.LogCaptureFixture) -> None:
    # `manager` propagiert nicht ⇒ caplog-Handler explizit an den Namespace
    # hängen (pytest macht das nur für Logger, die beim Phasenstart schon
    # `propagate = False` haben ⇒ sonst reihenfolgeabhängig).
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    handler = caplog.handler
    manager_logger.addHandler(handler)
    try:
        configure_logging("DEBUG")
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAMESPACE):
            logger = get_logger("smoke")
            logger.debug("pipeline_bereit")
    finally:
        manager_logger.removeHandler(handler)
    records = [r for r in caplog.records if r.name == "manager.smoke"]
    assert records, f"kein Record für manager.smoke in {caplog.text!r}"
    assert records[-1].getMessage() == "pipeline_bereit"
    assert records[-1].levelno == logging.DEBUG


def test_respects_configured_level(captured_manager_stream: io.StringIO) -> None:
    configure_logging("WARNING")
    logger = get_logger("quiet")
    logger.debug("nicht_sichtbar")
    logger.warning("sichtbar")
    text = captured_manager_stream.getvalue()
    assert "nicht_sichtbar" not in text
    assert "sichtbar" in text


def test_stdout_handler_uses_plan_format() -> None:
    configure_logging()
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    handlers = [h for h in manager_logger.handlers if h.get_name() == "manager-stdout"]
    assert len(handlers) == 1
    handler = handlers[0]
    assert handler.formatter is not None
    assert handler.formatter._fmt == "%(asctime)s %(levelname)s %(name)s: %(message)s"
    assert LOG_FORMAT == "%(asctime)s %(levelname)s %(name)s: %(message)s"
    # „Log an stdout": der Handler schreibt auf den aktuellen `sys.stdout`.
    assert handler.stream is sys.stdout
