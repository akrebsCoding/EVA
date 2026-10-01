"""Tests der Dashboard-Nur-Lese-API (P8.D1, Layer **L0** – rein, kein Netz).

Geprüft wird dreifach:

* **Datenlogik ohne HTTP** – Clamp, Level-Parser, Redaction (Positiv- *und*
  Negativbeispiele), `LOG_FORMAT`-Zerlegung, Zustandskatalog gegen
  `PipelineState`/`PLAN.md` §5, Config-Projektion ohne Secret-Feld.
* **API über ASGI** (`httpx.ASGITransport`, `asyncio.run` – der Repo-Stil ohne
  `pytest-asyncio`) mit **injizierten Fakes** in `app.state`.
* **Robustheit** – komplett leeres `app.state` ⇒ jeder Endpunkt 200 +
  `degraded: true` (nie 500, „Dashboard darf nie weiß ausfallen").

Der HA-Ping wird **nie** echt ausgeführt: L0 hat kein Netz (`conftest`), und
der Test überschreibt dafür die Dependency `ha_health_dependency`. Der reine
Pfad wird separat mit einem Attrappe-Opener geprüft (Timeout ⇒ `ok: false`,
begrenzte Laufzeit).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from fastapi import FastAPI

from app import dashboard
from app.dashboard import DependencyProbe
from app.logger import LOGGER_NAMESPACE
from app.pipeline import PipelineState

pytestmark = pytest.mark.unit


# ── Fakes (bewusst schlank, duck-typed wie die echten Klassen) ───────────
class FakeSession:
    """Minimaler `DeviceSession`-Ersatz (device_id, dead, connected_at)."""

    def __init__(self, device_id: str, *, dead: bool = False, age: float = 12.0) -> None:
        self.device_id = device_id
        self.dead = dead
        self.connected_at = time.monotonic() - age


class FakeRegistry:
    async def snapshot(self) -> dict[str, FakeSession]:
        return {
            "dot-1": FakeSession("dot-1"),
            "dot-2": FakeSession("dot-2", dead=True),
        }

    async def size(self) -> int:
        return len(await self.snapshot())

    async def ids(self) -> tuple[str, ...]:
        return tuple(await self.snapshot())


class FakeWsServer:
    def __init__(self) -> None:
        self.registry = FakeRegistry()


class FakePipeline:
    """Pipeline-Ersatz mit `state_of` (wie `app.pipeline.Pipeline.state_of`)."""

    def __init__(self, states: Optional[dict[str, PipelineState]] = None) -> None:
        self._states = states or {}

    def state_of(self, device_id: str) -> PipelineState:
        return self._states.get(device_id, PipelineState.IDLE)


class FakeMdns:
    is_running = True


class FakeHaClient:
    base_url = "http://ha.invalid:8123"


class FakeSettings:
    """Settings-Ersatz mit deklariertem `model_fields_set` (env/default-Quelle)."""

    model_fields_set = frozenset({"router_variant", "jev_mode"})

    def __init__(self) -> None:
        self.router_variant = "entity"
        self.jev_mode = "gate"
        self.jev_model = "jev-1.13"
        self.jev_timeout = 8.0
        self.router_confidence_gate = 0.75
        self.router_needs_param_threshold = 0.5
        self.deepseek_model = "deepseek-v4.1-flash"
        self.deepseek_timeout = 12.0
        self.ha_entity_domains = "light,switch,cover"
        self.ha_allowed_entities = "light.kitchen,switch.fan"
        self.ha_cache_interval_seconds = 600
        self.ha_request_timeout = 5.0
        self.whisper_model = "base"
        self.whisper_language = "de"
        self.piper_voice = "de_DE-thorsten-high"
        self.oww_enabled = True
        self.oww_model = "hey_jarvis_v0.1"
        self.oww_threshold = 0.8
        self.oww_barge_in_enabled = True
        self.oww_barge_in_threshold = 0.15
        self.oww_cooldown_ms = 1000
        self.oww_preroll_discard_chunks = 3
        # E96: Ring-Boden und Schreibmodus (Default **aus**).
        self.wake_attempt_keep_floor = 0.2
        self.dashboard_write_enabled = False
        self.turn_hard_cap_seconds = 15.0
        self.turn_no_speech_seconds = 8.0
        self.vad_device_sided = True
        self.manager_host = "0.0.0.0"
        self.manager_port = 8767
        self.manager_mdns_enabled = True
        self.log_level = "INFO"
        self.enable_test_hooks = False
        self.run_live = 0
        # Bewusst **gesetzt**, aber nie in der Allowlist ⇒ nie ausgegeben.
        self.ha_token = "SECRET-HA-TOKEN"
        self.llm_api_key = "SECRET-LLM-KEY"


def build_app(**state: Any) -> FastAPI:
    """FastAPI-App mit eingehängtem Dashboard-Router + gesetztem `app.state`."""
    app = FastAPI()
    app.include_router(dashboard.router)
    for key, value in state.items():
        setattr(app.state, key, value)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    """Ein GET gegen die ASGI-App (kein Socket ⇒ L0 bleibt netzfrei)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.get(path)


async def _post(app: FastAPI, path: str, payload: Any) -> httpx.Response:
    """Ein POST gegen die ASGI-App – **nur** für die E96-Schreibroute."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.post(path, json=payload)


def get_json(app: FastAPI, path: str) -> Any:
    """Synchroner Wrapper (Repo-Stil: `asyncio.run` im Test)."""
    return asyncio.run(_get(app, path)).json()


def get_response(app: FastAPI, path: str) -> httpx.Response:
    return asyncio.run(_get(app, path))


def post_response(app: FastAPI, path: str, payload: Any) -> httpx.Response:
    return asyncio.run(_post(app, path, payload))


async def _post_headers(
    app: FastAPI, path: str, payload: Any, headers: dict[str, str]
) -> httpx.Response:
    """POST mit zusätzlichem Header (E110-Auth) – ASGI, kein Socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.post(path, json=payload, headers=headers)


def post_response_headers(
    app: FastAPI, path: str, payload: Any, headers: dict[str, str]
) -> httpx.Response:
    return asyncio.run(_post_headers(app, path, payload, headers))


async def _put(app: FastAPI, path: str, payload: Any) -> httpx.Response:
    """Ein PUT gegen die ASGI-App – **nur** für die E111-Schreibroute."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.put(path, json=payload)


def put_response(app: FastAPI, path: str, payload: Any) -> httpx.Response:
    return asyncio.run(_put(app, path, payload))


async def _put_headers(
    app: FastAPI, path: str, payload: Any, headers: dict[str, str]
) -> httpx.Response:
    """PUT mit zusätzlichem Header (E110-Auth) – ASGI, kein Socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.put(path, json=payload, headers=headers)


def put_response_headers(
    app: FastAPI, path: str, payload: Any, headers: dict[str, str]
) -> httpx.Response:
    return asyncio.run(_put_headers(app, path, payload, headers))


@pytest.fixture
def populated_app() -> FastAPI:
    """App mit allen Komponenten, die der Hauptagent in `main.py` setzt."""
    return build_app(
        settings=FakeSettings(),
        pipeline=FakePipeline({"dot-1": PipelineState.SPEAKING}),
        ws_server=FakeWsServer(),
        mdns=FakeMdns(),
        connect_sequences={},
    )


@pytest.fixture
def memory_log_handler() -> Any:
    """Echter `MemoryLogBuffer` am `manager`-Logger (wird weggeräumt)."""
    handler = dashboard.MemoryLogBuffer(maxlen=50)
    logging.getLogger(LOGGER_NAMESPACE).addHandler(handler)
    try:
        yield handler
    finally:
        logging.getLogger(LOGGER_NAMESPACE).removeHandler(handler)


@pytest.fixture(autouse=True)
def _reset_log_dir() -> Any:
    """Log-Verzeichnis-Override vor/nach jedem Test zurücksetzen."""
    dashboard.set_log_dir(None)
    yield
    dashboard.set_log_dir(None)


# ── Rotationsschutz: keine neuen INFO-Logs beim Polling ────────────────
def test_router_logst_nur_bis_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Der API-Router darf beim Pollen nichts auf INFO loggen (kein Spam)."""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAMESPACE):
        entries, source, _dir, _issues = dashboard.collect_log_entries(limit=5)
    assert entries == []
    assert source == "none"
    assert [rec for rec in caplog.records if rec.levelno >= logging.INFO] == []


# ── clamp_limit ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(5000, 1000), (1000, 1000), (1, 1), (0, 1), (-7, 1)],
)
def test_clamp_limit_klemmt_auf_das_maximum(raw: int, expected: int) -> None:
    assert dashboard.clamp_limit(raw, maximum=dashboard.MAX_LOGS_LIMIT) == expected


def test_clamp_limit_ungueltige_werte_werden_minimum() -> None:
    assert dashboard.clamp_limit("keine-zahl", maximum=500) == 1  # type: ignore[arg-type]


# ── Level-Filter ────────────────────────────────────────────────────────
def test_parse_levels_komma_und_case_insensitiv() -> None:
    assert dashboard.parse_levels("warning, error") == frozenset({"WARNING", "ERROR"})


def test_parse_levels_ohne_filter_ist_none() -> None:
    assert dashboard.parse_levels(None) is None
    assert dashboard.parse_levels("  ") is None


def test_parse_levels_unbekannt_ergibt_leere_menge() -> None:
    """Unbekannter Level ⇒ leere Menge (matcht nichts) statt Exception."""
    assert dashboard.parse_levels("BOGUS") == frozenset()
    assert dashboard.parse_levels("BOGUS,warning") == frozenset({"WARNING"})


# ── Redaction ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        ("Authorization: Bearer eyJhbGciOi.JIUzI1NiJ9", "eyJhbGciOi.JIUzI1NiJ9"),
        ("HA_TOKEN=abcdef0123456789", "abcdef0123456789"),
        ("LLM_API_KEY=sk-proj-topsecret", "sk-proj-topsecret"),
        ('{"api_key": "sk-1234567890"}', "sk-1234567890"),
        ("password=hunter2", "hunter2"),
        ("ha_token: qqq-111-222", "qqq-111-222"),
        ("https://user:pw@10.0.0.1:8123/api/", "pw@"),
        ("Authorization=Bearer abc.def", "abc.def"),
        ("?api_key=sk-abc&other=1", "sk-abc"),
    ],
)
def test_redact_maskiert_secrets(raw: str, leak: str) -> None:
    masked = dashboard.redact(raw)
    assert leak not in masked
    assert dashboard.REDACTED in masked


@pytest.mark.parametrize(
    "raw",
    [
        "Turn dot-1 Endgerät: SPEAKING → IDLE",
        "HA-Client gestartet (base=http://10.0.0.10:8123, Cache=42 Entities)",
        "Jev #2 target.confidence=0.87 → ENTITY_NOT_FOUND",
        "Token-Pfad: Pipeline übernimmt",  # „Token" ohne Wert ⇒ kein Treffer
    ],
)
def test_redact_laesst_normale_logzeilen_unveraendert(raw: str) -> None:
    assert dashboard.redact(raw) == raw


def test_is_secret_field_erkennt_und_verweigert() -> None:
    assert dashboard.is_secret_field("ha_token")
    assert dashboard.is_secret_field("llm_api_key")
    assert dashboard.is_secret_field("wifi_password")
    assert not dashboard.is_secret_field("router_variant")
    assert not dashboard.is_secret_field("manager_port")


# ── Log-Zeilen ──────────────────────────────────────────────────────────
def test_parse_log_line_zerlegt_manager_format() -> None:
    entry = dashboard.parse_log_line(
        "2026-09-27 12:34:56,789 WARNING manager.ha_client: HA-Timeout beim Refresh"
    )
    assert entry.ts == "2026-09-27 12:34:56,789"
    assert entry.level == "WARNING"
    assert entry.logger == "manager.ha_client"
    assert entry.message == "HA-Timeout beim Refresh"


def test_parse_log_line_unpassend_wird_unknown_statt_verworfen() -> None:
    entry = dashboard.parse_log_line("Traceback (letzte Zeile): token=abc")
    assert entry.level == "UNKNOWN"
    assert entry.ts is None
    assert "abc" not in entry.message  # trotzdem redigiert


def test_parse_log_ts_und_vergleich() -> None:
    parsed = dashboard.parse_log_ts("2026-09-27 12:34:56,789")
    assert parsed is not None and parsed.year == 2026
    assert dashboard.parse_log_ts("kein-zeitstempel") is None
    assert dashboard.parse_log_ts(None) is None


def test_matches_filters_level_und_logger() -> None:
    entry = dashboard.parse_log_line("2026-09-27 12:00:00,000 INFO manager.pipeline: Turn ok")
    assert dashboard.matches_filters(entry, levels=frozenset({"INFO"}))
    assert not dashboard.matches_filters(entry, levels=frozenset({"ERROR"}))
    assert dashboard.matches_filters(entry, logger_filter="PIPE")
    assert not dashboard.matches_filters(entry, logger_filter="router")
    assert dashboard.matches_filters(entry, module_filter="manager")


# ── Log-Quellen ─────────────────────────────────────────────────────────
def test_collect_log_entries_ohne_quelle_ist_ehrlich_leer() -> None:
    entries, source, log_dir, issues = dashboard.collect_log_entries(limit=10)
    assert (entries, source, log_dir) == ([], "none", None)
    assert issues and "Keine In-Memory-Log-Liste" in issues[0]


def test_collect_log_entries_aus_datei_filtert_und_klemmt(tmp_path: Path) -> None:
    log_file = tmp_path / "manager.log"
    lines = [
        f"2026-09-27 12:00:0{index},000 {'INFO' if index < 2 else 'ERROR'} "
        f"manager.pipeline: Turn dot-1 Zeile {index}"
        for index in range(6)
    ]
    lines.append("2026-09-27 12:00:09,000 INFO manager.router: api_key=sk-leak")
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dashboard.set_log_dir(tmp_path)

    entries, source, log_dir, _issues = dashboard.collect_log_entries(limit=3)
    assert source == "file"
    assert log_dir == str(tmp_path)
    assert len(entries) == 3
    # Die drei **jüngsten** Zeilen (aufsteigend) – die letzte ist die api_key-Zeile.
    assert [entry.message for entry in entries] == [
        "Turn dot-1 Zeile 4",
        "Turn dot-1 Zeile 5",
        f"api_key={dashboard.REDACTED}",
    ]

    errors, _s, _d, _i = dashboard.collect_log_entries(limit=100, levels=frozenset({"ERROR"}))
    assert [entry.level for entry in errors] == ["ERROR"] * 4

    only_router, _s2, _d2, _i2 = dashboard.collect_log_entries(logger_filter="router")
    assert len(only_router) == 1
    assert "sk-leak" not in only_router[0].message  # Dateiquelle wird auch redigiert

    unknown, _s3, _d3, _i3 = dashboard.collect_log_entries(levels=frozenset())
    assert unknown == []


def test_collect_log_entries_since_verwirft_alte_und_undatierte() -> None:
    alt = time.time() - 7200
    alt_stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alt)) + ",000"
    neu_stamp = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
    entry_alt = dashboard.LogEntry(ts=alt_stamp, level="INFO", logger="manager", message="alt")
    entry_neu = dashboard.LogEntry(ts=neu_stamp, level="INFO", logger="manager", message="neu")
    records = [_as_record(entry_alt), _as_record(entry_neu), _as_record(None)]

    with _memory_source(records):
        fresh, source, _dir, _issues = dashboard.collect_log_entries(since_seconds=60.0)
    assert source == "memory"
    assert [entry.message for entry in fresh] == ["neu"]


def _as_record(entry: Optional[dashboard.LogEntry]) -> logging.LogRecord:
    """Minimaler `LogRecord` aus einem `LogEntry` (für den Memory-Pfad)."""
    record = logging.LogRecord(
        name=entry.logger if entry else "manager",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=entry.message if entry else "ohne Zeitstempel",
        args=(),
        exc_info=None,
    )
    if entry and entry.ts:
        record.created = time.mktime(time.strptime(entry.ts[:19], "%Y-%m-%d %H:%M:%S"))
    elif entry is None:
        record.created = 0.0
    return record


class _memory_source:
    """Kontextmanager: `MemoryLogBuffer` am `manager`-Logger temporär aktiv."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        self._handler = dashboard.MemoryLogBuffer(maxlen=max(1, len(records)))
        for record in records:
            self._handler.records.append(record)

    def __enter__(self) -> "_memory_source":
        logging.getLogger(LOGGER_NAMESPACE).addHandler(self._handler)
        return self

    def __exit__(self, *exc: object) -> None:
        logging.getLogger(LOGGER_NAMESPACE).removeHandler(self._handler)


def test_find_memory_records_liest_den_in_memory_puffer(memory_log_handler: Any) -> None:
    logging.getLogger(LOGGER_NAMESPACE).warning("Turn %s: 401 mit token=leak", "dot-1")
    records = dashboard.find_memory_records()
    assert records is not None and records
    entries, source, _dir, _issues = dashboard.collect_log_entries(limit=5)
    assert source == "memory"
    assert "leak" not in entries[0].message
    assert dashboard.REDACTED in entries[0].message


def test_resolve_log_dir_override_und_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard.set_log_dir(tmp_path)
    assert dashboard.resolve_log_dir() == tmp_path
    dashboard.set_log_dir(tmp_path / "gibt-es-nicht")
    assert dashboard.resolve_log_dir() is None
    dashboard.set_log_dir(None)
    monkeypatch.setenv(dashboard.LOG_DIR_ENV, str(tmp_path))
    assert dashboard.resolve_log_dir() == tmp_path
    monkeypatch.setenv(dashboard.LOG_DIR_ENV, str(tmp_path / "weg"))
    assert dashboard.resolve_log_dir() is None


# ── Zustandskatalog ─────────────────────────────────────────────────────
def test_build_state_catalog_ist_die_kanonische_liste() -> None:
    catalog = dashboard.build_state_catalog()
    assert [state.key for state in catalog] == [
        "idle",
        "listening",
        "transcribing",
        "routing",
        "executing",
        "answering",
        "speaking",
    ]
    assert [state.key for state in catalog] == [member.value for member in PipelineState]
    assert all(state.label for state in catalog)
    assert all(not state.terminal for state in catalog)  # §5 kennt keinen Endzustand
    by_key = {state.key: state for state in catalog}
    assert by_key["idle"].next == ["listening"]
    assert by_key["routing"].next == ["executing", "answering", "speaking"]
    assert "idle" in by_key["speaking"].next
    assert "listening" in by_key["speaking"].next  # Barge-in


def test_state_specs_kein_zustand_ohne_kanten_zurueck() -> None:
    keys = {member.value for member in PipelineState}
    assert set(dashboard.STATE_SPECS) == keys
    for key, spec in dashboard.STATE_SPECS.items():
        assert spec.next_states, f"{key} ohne ausgehende Kante"
        assert set(spec.next_states) <= keys


# ── Config-Projektion ───────────────────────────────────────────────────
def test_project_config_enthaelt_keine_secret_felder() -> None:
    values, _issues = dashboard.project_config(FakeSettings())
    names = [value.name for value in values]
    assert "router_variant" in names
    assert "router_confidence_gate" in names
    assert "router_needs_param_threshold" in names
    assert "jev_mode" in names
    assert "whisper_model" in names and "piper_voice" in names
    assert "log_level" in names and "enable_test_hooks" in names
    assert "jev_max_choices" in names
    # Weder Name noch Wert eines Secret-Feldes dürfen vorkommen.
    assert not [name for name in names if dashboard.is_secret_field(name)]
    rendered = json.dumps([value.model_dump() for value in values])
    assert "SECRET-HA-TOKEN" not in rendered
    assert "SECRET-LLM-KEY" not in rendered
    assert "ha_token" not in rendered and "llm_api_key" not in rendered


def test_project_config_meldet_quelle_und_typen() -> None:
    values, _issues = dashboard.project_config(FakeSettings())
    by_name = {value.name: value for value in values}
    assert by_name["router_variant"].value == "entity"
    assert by_name["router_variant"].source == "env"
    assert by_name["manager_port"].source == "default"
    assert by_name["manager_port"].value == 8767
    assert by_name["oww_barge_in_threshold"].value == 0.15
    assert isinstance(by_name["oww_enabled"].value, bool)
    assert by_name["ha_entity_domains"].value == ["light", "switch", "cover"]
    # Entity-IDs der E17-Allowlist werden nur als Anzahl ausgegeben.
    assert by_name["ha_allowed_entities"].value == 2
    assert "light.kitchen" not in json.dumps(
        [value.model_dump() for value in values], ensure_ascii=False
    )


def test_project_config_ueberspringt_fehlendes_feld() -> None:
    class Unvollstaendig:
        model_fields_set: frozenset[str] = frozenset()
        router_variant = "class"

    values, issues = dashboard.project_config(Unvollstaendig())
    assert [value.name for value in values] == ["router_variant", "jev_max_choices"]
    assert any("fehlt in den Settings" in issue for issue in issues)


# ── Historie ────────────────────────────────────────────────────────────
def test_collect_history_ohne_puffer_ist_ehrlich_leer() -> None:
    entries, available, reason = dashboard.collect_history(10, FakePipeline())
    assert (entries, available) == ([], False)
    assert reason == dashboard.HISTORY_REASON
    assert "keine Turn-Historie" in reason


def test_collect_history_nutzt_vorhandenen_puffer_mit_allowlist() -> None:
    """Puffer wird projiziert – **Allowlist**, kein Freitext.

    **Bewusste Vertragsänderung P8.D3:** P8.D1 verbot **jedes** Textfeld
    ("kein Transkript"). P8.D3 erlaubt genau **eines** – den redigierten und
    gekürzten `transcript` – damit der Nutzer im Dashboard sieht, was er gesagt
    hat. `response_text` (kompletter DeepSeek-/Template-Text) bleibt verboten,
    ebenso jeder unbekannte Key (`raw`, `service_data`, …).
    """
    pipeline = FakePipeline()
    pipeline.turn_history = [  # type: ignore[attr-defined]
        {
            "ts": 1758000000.0,
            "device_id": "dot-1",
            "intent": "command",
            "outcome": "ok",
            "duration_seconds": 3.2,
            "score": 0.91,
            "service": "turn_on",
            "domain": "light",
            "target_entity_id": "light.wohnzimmer",
            "confidence": 0.87,
            "needs_param": True,
            "extracted_param": "brightness_pct=40",
            "transcript": "mach das licht an",
            "error_code": None,
            "error": None,
            "barge_in": False,
            "response_text": "Okay.",
            "raw": {"target": {"choice": "light.wohnzimmer"}},
            "service_data": {"brightness_pct": 40},
        },
        {"device_id": "dot-2", "state": "answering"},
    ]
    entries, available, reason = dashboard.collect_history(10, pipeline)
    assert available is True
    assert reason is None
    assert len(entries) == 2
    assert entries[0].service == "turn_on"
    assert entries[0].ts is not None and entries[0].ts.startswith("2025-")
    # P8.D3: die neuen Allowlist-Felder kommen an.
    assert entries[0].target_entity_id == "light.wohnzimmer"
    assert entries[0].confidence == 0.87
    assert entries[0].needs_param is True
    assert entries[0].extracted_param == "brightness_pct=40"
    assert entries[0].transcript == "mach das licht an"
    assert entries[0].barge_in is False
    # ... aber **kein** Freitext und **kein** unbekannter Key.
    rendered = json.dumps([entry.model_dump() for entry in entries], ensure_ascii=False)
    assert "Okay." not in rendered
    assert not [key for key in entries[0].model_dump() if key not in dashboard.HISTORY_FIELDS]
    # `raw`/`service_data` sind nicht in der Allowlist ⇒ nicht im Modell.
    assert "raw" not in entries[0].model_dump()
    assert "service_data" not in entries[0].model_dump()


def test_collect_history_ignoriert_kaputten_puffer() -> None:
    pipeline = FakePipeline()
    pipeline.turn_history = "keine liste"  # type: ignore[attr-defined]
    entries, available, reason = dashboard.collect_history(10, pipeline)
    assert (entries, available) == ([], False)
    assert "keine Sequenz" in (reason or "")


def test_collect_history_begrenzt_und_ignoriert_ungueltige_eintraege() -> None:
    pipeline = FakePipeline()
    pipeline.turn_history = [  # type: ignore[attr-defined]
        {"device_id": f"dot-{index}"} for index in range(5)
    ] + ["kein-mapping"]  # type: ignore[list-item]
    entries, available, _reason = dashboard.collect_history(3, pipeline)
    assert available is True
    # Das Limit greift auf **auswertbare** Einträge (die letzten 3 Mappings).
    assert [entry.device_id for entry in entries] == ["dot-2", "dot-3", "dot-4"]


# ── HA-Probe (kein Netz: Attrappe-Opener) ──────────────────────────────
def test_probe_url_timeout_ist_begrenzt_und_degradiert() -> None:
    async def scenario() -> DependencyProbe:
        async def opener(_url: str, *, timeout: float) -> Any:
            await asyncio.sleep(5.0)  # länger als das Limit

        started = time.monotonic()
        probe = await dashboard.probe_url(
            "http://ha.invalid:8123/api/", timeout=0.2, opener=opener
        )
        return probe, time.monotonic() - started  # type: ignore[return-value]

    probe, elapsed = asyncio.run(scenario())
    assert isinstance(probe, DependencyProbe)
    assert probe.ok is False
    assert "Timeout" in (probe.reason or "")
    assert elapsed < 2.0  # kein Hänger


def test_probe_url_401_beweist_erreichbarkeit() -> None:
    class Antwort:
        status_code = 401

    async def opener(_url: str, *, timeout: float) -> Any:
        return Antwort()

    probe = asyncio.run(
        dashboard.probe_url("http://ha.invalid:8123/api/", timeout=1.0, opener=opener)
    )
    assert probe.ok is True
    assert probe.status == 401
    assert "401" in (probe.detail or "")
    assert probe.target == "ha.invalid:8123"


def test_probe_url_httpx_timeout_externer_fehler() -> None:
    async def opener(_url: str, *, timeout: float) -> Any:
        raise httpx.TimeoutException("read timeout")

    probe = asyncio.run(
        dashboard.probe_url("http://ha.invalid:8123/api/", timeout=0.5, opener=opener)
    )
    assert probe.ok is False
    assert "Timeout" in (probe.reason or "")


def test_probe_url_kein_client_ohne_base_url() -> None:
    probe = asyncio.run(dashboard.ha_health_dependency(_FakeRequest()))
    assert probe.ok is None
    assert "ha_client fehlt" in (probe.reason or "")


class _EmptyState:
    """`app.state`-Ersatz ohne jede Komponente (leeres Dashboard-Deployment)."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover – nie benutzt
        raise AttributeError(name)


class _FakeApp:
    def __init__(self) -> None:
        self.state = _EmptyState()


class _FakeRequest:
    """Minimaler `Request`-Ersatz: `ha_health_dependency` liest nur `app.state`."""

    def __init__(self) -> None:
        self.app = _FakeApp()


# ── API: /api/status ────────────────────────────────────────────────────
def test_status_liefert_form_und_geraete(populated_app: FastAPI) -> None:
    body = get_json(populated_app, "/api/status")
    assert body["service"] == "wyoming-manager"
    assert body["version"] == "0.1.0"
    assert body["uptime_seconds"] >= 0.0
    assert body["mdns_running"] is True
    assert body["device_count"] == 2
    assert body["health"] == {"router_ok": True, "exception": None}
    assert body["degraded"] is False
    devices = {item["device_id"]: item for item in body["devices"]}
    assert devices["dot-1"]["state"] == "speaking"
    assert devices["dot-1"]["connected"] is True
    assert devices["dot-1"]["connected_seconds"] >= 10
    assert devices["dot-2"]["connected"] is False
    assert devices["dot-2"]["state"] == "idle"


def test_status_mdns_fehlt_ist_degraded_aber_200(populated_app: FastAPI) -> None:
    delattr(populated_app.state, "mdns")
    response = get_response(populated_app, "/api/status")
    body = response.json()
    assert response.status_code == 200
    assert body["mdns_running"] is None
    assert body["degraded"] is True
    assert any("mdns" in issue for issue in body["issues"])


# ── API: /api/state ─────────────────────────────────────────────────────
def test_state_kanonische_zustaende_und_transitions_flag(populated_app: FastAPI) -> None:
    body = get_json(populated_app, "/api/state")
    assert [state["key"] for state in body["states"]] == [
        "idle",
        "listening",
        "transcribing",
        "routing",
        "executing",
        "answering",
        "speaking",
    ]
    assert body["transitions_tracked"] is False
    assert body["graph_source"] == "PLAN.md §5"
    assert body["degraded"] is False
    workflows = {item["device_id"]: item for item in body["devices"]}
    assert workflows["dot-1"]["current_state"] == "speaking"
    # Ohne Transition-Tracking ehrlich null statt eines geratenen Werts.
    assert workflows["dot-1"]["previous_state"] is None
    assert workflows["dot-1"]["since"] is None


def test_state_ohne_pipeline_ist_degraded(populated_app: FastAPI) -> None:
    delattr(populated_app.state, "pipeline")
    response = get_response(populated_app, "/api/state")
    body = response.json()
    assert response.status_code == 200
    assert body["degraded"] is True
    assert body["devices"][0]["current_state"] is None


# ── API: /api/logs ──────────────────────────────────────────────────────
def test_logs_filtert_level_und_limit(populated_app: FastAPI, tmp_path: Path) -> None:
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "\n".join(
            [
                "2026-09-27 12:00:00,000 INFO manager.pipeline: Turn dot-1 ok",
                "2026-09-27 12:00:01,000 ERROR manager.ha_client: Timeout beim Refresh",
                "2026-09-27 12:00:02,000 WARNING manager.router: Gate abgelehnt",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    dashboard.set_log_dir(tmp_path)

    body = get_json(populated_app, "/api/logs")
    assert body["source"] == "file"
    assert body["redacted"] is True
    assert body["count"] == 3
    assert body["order"] == "ascending"

    warnings = get_json(populated_app, "/api/logs?level=warning")
    assert [entry["level"] for entry in warnings["entries"]] == ["WARNING"]

    limited = get_json(populated_app, "/api/logs?limit=2")
    assert limited["count"] == 2
    assert [entry["message"] for entry in limited["entries"]] == [
        "Timeout beim Refresh",
        "Gate abgelehnt",
    ]

    by_logger = get_json(populated_app, "/api/logs?logger=router")
    assert by_logger["count"] == 1
    by_module = get_json(populated_app, "/api/logs?module=pipeline")
    assert by_module["count"] == 1


def test_logs_unbekannter_level_ist_leer_ohne_500(populated_app: FastAPI) -> None:
    response = get_response(populated_app, "/api/logs?level=QUATSCH")
    body = response.json()
    assert response.status_code == 200
    assert body["entries"] == []
    assert body["redacted"] is True


def test_logs_limit_wird_geklemmt(populated_app: FastAPI) -> None:
    body = get_json(populated_app, "/api/logs?limit=99999")
    assert body["count"] <= dashboard.MAX_LOGS_LIMIT
    assert any("geklemmt" in issue for issue in body["issues"])


def test_logs_redigiert_secrets_aus_der_antwort(
    populated_app: FastAPI, tmp_path: Path
) -> None:
    (tmp_path / "manager.log").write_text(
        "\n".join(
            [
                "2026-09-27 12:00:00,000 INFO manager.ha_client: Authorization: Bearer eyJraWQx",
                '2026-09-27 12:00:01,000 INFO manager.llm_client: {"api_key": "sk-proj-abc"}',
                "2026-09-27 12:00:02,000 INFO manager.config: HA_TOKEN=abcdef123456",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    dashboard.set_log_dir(tmp_path)
    response = get_response(populated_app, "/api/logs?limit=50")
    raw = response.text
    for leak in ("eyJraWQx", "sk-proj-abc", "abcdef123456"):
        assert leak not in raw
    assert body_messages(response) and all(
        dashboard.REDACTED in message for message in body_messages(response)
    )


def body_messages(response: httpx.Response) -> list[str]:
    return [entry["message"] for entry in response.json()["entries"]]


def test_logs_ohne_quelle_meldet_source_none(populated_app: FastAPI) -> None:
    body = get_json(populated_app, "/api/logs")
    assert body["source"] == "none"
    assert body["entries"] == []
    assert body["degraded"] is True
    assert body["issues"]


def test_logs_since_seconds_filtert(populated_app: FastAPI, tmp_path: Path) -> None:
    alt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7200)) + ",000"
    neu = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
    (tmp_path / "manager.log").write_text(
        f"{alt} INFO manager.pipeline: alt\n{neu} INFO manager.pipeline: neu\n",
        encoding="utf-8",
    )
    dashboard.set_log_dir(tmp_path)
    body = get_json(populated_app, "/api/logs?since_seconds=60")
    assert [entry["message"] for entry in body["entries"]] == ["neu"]


def test_logs_memory_quelle(populated_app: FastAPI, memory_log_handler: Any) -> None:
    logging.getLogger(LOGGER_NAMESPACE).info("Turn dot-1: IDLE → SPEAKING")
    body = get_json(populated_app, "/api/logs?limit=10")
    assert body["source"] == "memory"
    assert body["log_dir"] is None
    assert body["entries"][-1]["logger"] == "manager"


# ── API: /api/history ───────────────────────────────────────────────────
def test_history_verfuegbarkeits_pfad(populated_app: FastAPI) -> None:
    body = get_json(populated_app, "/api/history")
    assert body["available"] is False
    assert body["entries"] == []
    assert body["reason"] == dashboard.HISTORY_REASON
    assert body["degraded"] is False


def test_history_mit_fake_puffer(populated_app: FastAPI) -> None:
    pipeline = FakePipeline()
    pipeline.turn_history = [  # type: ignore[attr-defined]
        {"device_id": "dot-1", "intent": "command", "duration_seconds": 3.4}
    ]
    populated_app.state.pipeline = pipeline
    body = get_json(populated_app, "/api/history")
    assert body["available"] is True
    assert body["count"] == 1
    assert body["entries"][0]["intent"] == "command"


def test_history_limit_wird_geklemmt(populated_app: FastAPI) -> None:
    body = get_json(populated_app, "/api/history?limit=5000")
    assert any("geklemmt" in issue for issue in body["issues"])


# ── API: /api/config ────────────────────────────────────────────────────
def test_config_enthaelt_variant_und_kein_secret(populated_app: FastAPI) -> None:
    response = get_response(populated_app, "/api/config")
    body = response.json()
    assert response.status_code == 200
    values = {item["name"]: item for item in body["values"]}
    assert values["router_variant"]["value"] == "entity"
    assert values["router_variant"]["source"] == "env"
    assert values["jev_mode"]["value"] == "gate"
    assert values["router_needs_param_threshold"]["value"] == 0.5
    assert values["log_level"]["value"] == "INFO"
    assert values["enable_test_hooks"]["value"] is False
    assert values["whisper_model"]["value"] == "base"
    assert values["piper_voice"]["value"] == "de_DE-thorsten-high"
    assert values["oww_model"]["value"] == "hey_jarvis_v0.1"
    assert values["ha_entity_domains"]["value"] == ["light", "switch", "cover"]
    assert body["degraded"] is False
    # Kein Secret-Feld im **gesamten** Antworttext.
    assert "SECRET-HA-TOKEN" not in response.text
    assert "SECRET-LLM-KEY" not in response.text
    assert "ha_token" not in response.text
    assert "llm_api_key" not in response.text


def test_config_ohne_state_settings_nutzt_default(populated_app: FastAPI) -> None:
    delattr(populated_app.state, "settings")
    response = get_response(populated_app, "/api/config")
    body = response.json()
    assert response.status_code == 200
    assert body["degraded"] is True
    assert any("app.state.settings fehlt" in issue for issue in body["issues"])
    names = [item["name"] for item in body["values"]]
    assert "router_variant" in names


def test_config_liefert_schreibstatus_und_live_map(populated_app: FastAPI) -> None:
    """P11.T3: `write_token_required` (E110) + `effective_without_restart` (E111).

    Das Flag ist `False`, solange kein Token gesetzt ist; die Live-Map ist
    exakt das statische Mapping (4 live, 7 Neustart) – **kein** Read-Back.
    """
    body = get_json(populated_app, "/api/config")
    assert body["write_token_required"] is False
    assert body["effective_without_restart"] == dashboard.CONFIG_WRITE_LIVE_MAP
    for live in ("oww_threshold", "log_level", "audio_dump_enabled", "whisper_language"):
        assert body["effective_without_restart"][live] is True
    for restart in (
        "oww_barge_in_threshold",
        "oww_cooldown_ms",
        "wake_attempt_keep_floor",
        "router_confidence_gate",
        "router_needs_param_threshold",
        "jev_mode",
        "ha_entity_domains",
    ):
        assert body["effective_without_restart"][restart] is False


def test_config_token_flag_nur_wenn_token_gesetzt(populated_app: FastAPI) -> None:
    """Gesetzter `dashboard_api_token` ⇒ `write_token_required: true`, und der
    Tokenwert selbst bleibt **nie** in der Antwort (E110)."""
    populated_app.state.settings.dashboard_api_token = "sekret-test"
    response = get_response(populated_app, "/api/config")
    body = response.json()
    assert body["write_token_required"] is True
    assert "sekret-test" not in response.text


def test_config_degradiert_liefert_trotzdem_schreibstatus(
    populated_app: FastAPI, monkeypatch
) -> None:
    """Auch der Degradate-Pfad meldet Schreibstatus + Live-Map (kein 500)."""
    monkeypatch.setattr(dashboard._module_settings, "dashboard_api_token", "")
    delattr(populated_app.state, "settings")
    body = get_json(populated_app, "/api/config")
    assert body["write_token_required"] is False
    assert body["effective_without_restart"] == dashboard.CONFIG_WRITE_LIVE_MAP


# ── API: /api/dependencies ──────────────────────────────────────────────
def test_dependencies_mit_gemocktem_timeout(populated_app: FastAPI) -> None:
    async def timeout_override() -> DependencyProbe:
        return DependencyProbe(
            name="home_assistant", ok=False, reason="Timeout nach 2.0s"
        )

    populated_app.dependency_overrides[dashboard.ha_health_dependency] = timeout_override
    started = time.monotonic()
    response = get_response(populated_app, "/api/dependencies")
    elapsed = time.monotonic() - started
    body = response.json()
    assert response.status_code == 200
    assert elapsed < 2.0  # kein Hänger
    assert body["degraded"] is True
    ha = {item["name"]: item for item in body["dependencies"]}["home_assistant"]
    assert ha["ok"] is False
    assert "Timeout" in ha["reason"]
    # Whisper/Piper bleiben ehrlich `null` mit Begründung.
    for name in ("whisper_stt", "piper_tts"):
        entry = {item["name"]: item for item in body["dependencies"]}[name]
        assert entry["ok"] is None
        assert "keinen Health-Endpunkt" in entry["reason"]


def test_dependencies_ok_fall(populated_app: FastAPI) -> None:
    async def healthy() -> DependencyProbe:
        return DependencyProbe(
            name="home_assistant", ok=True, status=200, latency_ms=4.2, target="ha:8123"
        )

    populated_app.dependency_overrides[dashboard.ha_health_dependency] = healthy
    body = get_json(populated_app, "/api/dependencies")
    # Whisper/Piper bleiben null ⇒ degraded bleibt true (ehrlich, kein Fake-Grün).
    assert body["degraded"] is True
    assert body["timeout_seconds"] == dashboard.DEFAULT_DEPENDENCY_TIMEOUT


def test_dependencies_ohne_ha_client_ohne_netz() -> None:
    app = build_app(ha_client=FakeHaClient())
    delattr(app.state, "ha_client")
    body = get_json(app, "/api/dependencies")
    ha = {item["name"]: item for item in body["dependencies"]}["home_assistant"]
    assert ha["ok"] is None
    assert ha["target"] is None
    assert body["degraded"] is True


def test_dependencies_echter_pfad_haengt_nicht_und_sendet_kein_credential() -> None:
    """Echter Dependency-Pfad gegen eine unerreichbare Basis-URL: kein Hänger.

    `ha.invalid` ist nicht auflösbar; L0 blockiert DNS ohnehin. Geprüft wird:
    200 statt 500, `ok is False` und ein `target` **ohne** Credential.
    """
    started = time.monotonic()
    response = get_response(build_app(ha_client=FakeHaClient()), "/api/dependencies")
    elapsed = time.monotonic() - started
    body = response.json()
    assert response.status_code == 200
    assert elapsed < 5.0
    ha = {item["name"]: item for item in body["dependencies"]}["home_assistant"]
    assert ha["ok"] is False
    assert ha["reason"]
    assert ha["target"] == "ha.invalid:8123"
    assert "SECRET" not in response.text


# ── Robustheit: komplett leeres app.state ──────────────────────────────
@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/state",
        "/api/logs",
        "/api/logs?limit=10&level=WARNING",
        "/api/history",
        "/api/config",
        "/api/dependencies",
    ],
)
def test_leeres_app_state_antwortet_200_und_degraded(path: str) -> None:
    response = get_response(build_app(), path)
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["issues"]


def test_leeres_app_state_fuehlt_keine_ha_und_keine_logs() -> None:
    status = get_json(build_app(), "/api/status")
    assert status["device_count"] == 0
    assert status["devices"] == []
    assert status["health"]["router_ok"] is True
    logs = get_json(build_app(), "/api/logs")
    assert logs["source"] == "none" and logs["entries"] == []


def test_defekte_komponenten_erzeugen_keinen_500() -> None:
    class KaputteRegistry:
        async def snapshot(self) -> dict[str, Any]:
            raise RuntimeError("registry explodiert")

    class KaputterServer:
        registry = KaputteRegistry()

    class KaputtePipeline:
        def state_of(self, device_id: str) -> PipelineState:
            raise RuntimeError("pipeline explodiert")

    app = build_app(ws_server=KaputterServer(), pipeline=KaputtePipeline())
    for path in ("/api/status", "/api/state", "/api/config"):
        response = get_response(app, path)
        assert response.status_code == 200, path
        assert response.json()["degraded"] is True, path
    # /api/history bleibt ehrlich „nicht verfügbar" – das ist kein Ausfall.
    history = get_json(app, "/api/history")
    assert history["available"] is False
    assert history["entries"] == []


# ═══════════════════════════════════════════════════════════════════════════
# P8.D3 – die drei behobenen Live-Befunde
# ═══════════════════════════════════════════════════════════════════════════
# Jeder Test zitiert den **live gemessenen** Vorher-Zustand auf `.123`:
#   Bug 1  `/api/logs`  ⇒ `source:"none"`, 0 Einträge, `degraded:true`
#   Bug 2  `/api/config` ⇒ `degraded:true` + „app.state.settings fehlt"
#   Bug 3  `/api/history` ⇒ `available:false`, 0 Einträge
# Literale (E56/E59) statt Import der Konstanten, damit Mutationen auffallen.


# ── Bug 1: In-Memory-Logpuffer ───────────────────────────────────────────
def test_log_buffer_maxlen_ist_2000_und_default() -> None:
    """Harter Standard-Cap 2000 (`LOG_BUFFER_MAXLEN`, Literal im Test)."""
    assert dashboard.LOG_BUFFER_MAXLEN == 2000
    assert dashboard.MemoryLogBuffer().maxlen == 2000
    assert dashboard.MemoryLogBuffer(maxlen=5).maxlen == 5


def test_log_buffer_verwirft_die_aelteren_und_behaelt_die_neusten() -> None:
    """Ringpuffer: nach 2005 Records bleiben **genau 2000** – die neuesten."""
    buffer = dashboard.MemoryLogBuffer()
    for index in range(2005):
        record = logging.LogRecord(
            name="manager.pipeline",
            level=logging.INFO,
            pathname=__file__,
            lineno=index,
            msg="Zeile %d",
            args=(index,),
            exc_info=None,
        )
        buffer.emit(record)
    records = list(buffer.records)
    assert len(records) == 2000
    assert records[0].getMessage() == "Zeile 5"  # FIFO: älteste raus
    assert records[-1].getMessage() == "Zeile 2004"


def test_attach_memory_log_buffer_ist_idempotent() -> None:
    """Zweimal attachen ⇒ **eine** Instanz, **ein** Handler (Lifespan-Reload)."""
    logger = logging.getLogger(LOGGER_NAMESPACE)
    before = list(logger.handlers)
    try:
        first = dashboard.attach_memory_log_buffer()
        second = dashboard.attach_memory_log_buffer()
        assert first is second
        assert logger.handlers.count(first) == 1
        assert len(logger.handlers) == len(before) + 1
    finally:
        dashboard.detach_memory_log_buffer(first)
    assert logger.handlers == before


def test_detach_memory_log_buffer_entfernt_und_schliesst() -> None:
    """`removeHandler` **und** `close()` – Pflicht gegen den Handler-Leck."""
    logger = logging.getLogger(LOGGER_NAMESPACE)
    buffer = dashboard.attach_memory_log_buffer()
    closed: list[bool] = []
    real_close = buffer.close

    def spy_close() -> None:  # type: ignore[misc]
        closed.append(True)
        real_close()

    buffer.close = spy_close  # type: ignore[method-assign]
    assert dashboard.detach_memory_log_buffer(buffer) is True
    assert closed == [True]
    assert buffer not in logger.handlers


def test_detach_memory_log_buffer_ist_tolerant_gegen_fehlen() -> None:
    """Shutdown ohne (oder mit schon entferntem) Puffer ⇒ kein 500, kein Raise."""
    assert dashboard.detach_memory_log_buffer(None) is False
    buffer = dashboard.MemoryLogBuffer()
    assert dashboard.detach_memory_log_buffer(buffer) is False


def test_logs_mit_gehängtem_puffer_sind_nicht_mehr_degraded(
    populated_app: FastAPI,
) -> None:
    """Der Live-Bug: mit Puffer ⇒ `source:"memory"`, Einträge da, kein `degraded`."""
    logger = logging.getLogger(LOGGER_NAMESPACE)
    buffer = dashboard.attach_memory_log_buffer()
    try:
        logging.getLogger("manager.pipeline").info("Turn dot-1: IDLE → SPEAKING")
        body = get_json(populated_app, "/api/logs?limit=10")
        assert body["source"] == "memory"
        assert body["degraded"] is False
        assert body["issues"] == []
        assert body["entries"][-1]["message"] == "Turn dot-1: IDLE → SPEAKING"
        # Der Handler sitzt am **Manager**-Logger, nicht am Root ⇒ Kind-Logger
        # (`manager.pipeline`) landen mit.
        assert buffer.records
    finally:
        dashboard.detach_memory_log_buffer(buffer)
    assert buffer not in logger.handlers


def test_logs_nach_detach_melden_wieder_source_none(populated_app: FastAPI) -> None:
    """Ohne Handler (z. B. vor dem Start) bleibt es ehrlich `none`/`degraded`."""
    get_json(populated_app, "/api/logs")  # kein Buffer angehängt
    body = get_json(populated_app, "/api/logs")
    assert body["source"] == "none"
    assert body["degraded"] is True


# ── Bug 2: Config-`degraded` / `settings_source` ─────────────────────────
def test_config_meldet_settings_source_app_state_ohne_degraded(
    populated_app: FastAPI,
) -> None:
    """Live-Fall: `app.state.settings` **ist** gesetzt ⇒ kein `degraded`."""
    response = get_response(populated_app, "/api/config")
    body = response.json()
    assert response.status_code == 200
    assert body["settings_source"] == dashboard.SETTINGS_SOURCE_APP_STATE
    assert body["settings_source"] == "app.state"
    assert body["degraded"] is False
    assert not [issue for issue in body["issues"] if "app.state.settings" in issue]


def test_config_werte_kommen_aus_app_state_nicht_aus_dem_modul_default() -> None:
    """Der eigentliche Identitäts-Bug: die Werte müssen die **App** liefern.

    `app.state.settings` trägt hier einen Markierwert, der im Modul-Default
    *nicht* vorkommt. Der alte Code (``settings_obj is _module_settings`` als
    Verfügbarkeitsprüfung) hat in diesem Fall ein **zweites** `Settings()`
    erzeugt und dessen Werte gemeldet – der Markierwert wäre verschwunden.
    """
    settings = FakeSettings()
    settings.router_variant = "class"  # bewusst ≠ Modul-Default "entity"
    app = build_app(settings=settings, pipeline=FakePipeline())
    body = get_json(app, "/api/config")
    values = {item["name"]: item for item in body["values"]}
    assert values["router_variant"]["value"] == "class"
    assert body["degraded"] is False


def test_config_ohne_state_settings_meldet_module_default(populated_app: FastAPI) -> None:
    """Fehlendes `app.state.settings` ⇒ ehrlich `module-default` + `degraded`."""
    delattr(populated_app.state, "settings")
    response = get_response(populated_app, "/api/config")
    body = response.json()
    assert response.status_code == 200
    assert body["settings_source"] == dashboard.SETTINGS_SOURCE_MODULE_DEFAULT
    assert body["settings_source"] == "module-default"
    assert body["degraded"] is True
    assert dashboard.SETTINGS_MISSING_ISSUE in body["issues"]
    # Werte werden trotzdem geliefert (kein 500 aus dem Poller heraus).
    names = [item["name"] for item in body["values"]]
    assert "router_variant" in names


def test_settings_source_for_ist_kein_identitaetsvergleich() -> None:
    """Ein *anderes* Settings-Objekt gilt trotzdem als „vorhanden"."""
    class Anfrage:
        class app:  # noqa: N801 – Attrappen-Form wie in den anderen Tests
            class state:  # noqa: N801
                settings = FakeSettings()

    assert (
        dashboard.settings_source_for(Anfrage()) == dashboard.SETTINGS_SOURCE_APP_STATE
    )


def test_settings_source_for_meldet_fehlendes_settings() -> None:
    class Anfrage:
        class app:  # noqa: N801
            class state:  # noqa: N801
                settings = None

    assert (
        dashboard.settings_source_for(Anfrage())
        == dashboard.SETTINGS_SOURCE_MODULE_DEFAULT
    )


# ── Bug 3: Turn-Historie verfügbar ──────────────────────────────────────
def test_collect_history_meldet_leeren_puffer_als_verfuegbar() -> None:
    """Live-Bug: leere, aber **vorhandene** Historie ist `available`, nicht `false`."""
    pipeline = FakePipeline()
    pipeline.turn_history = []  # type: ignore[attr-defined]
    entries, available, reason = dashboard.collect_history(10, pipeline)
    assert (entries, available, reason) == ([], True, None)


def test_history_api_meldet_leeren_puffer_als_verfuegbar(populated_app: FastAPI) -> None:
    pipeline = FakePipeline()
    pipeline.turn_history = []  # type: ignore[attr-defined]
    populated_app.state.pipeline = pipeline
    body = get_json(populated_app, "/api/history")
    assert body["available"] is True
    assert body["entries"] == []
    assert body["count"] == 0
    assert body["reason"] is None
    assert body["degraded"] is False


def test_history_api_zeigt_alle_p8d3_felder(populated_app: FastAPI) -> None:
    """Ein echter Turn kommt vollständig und **ohne** Fremdfelder an."""
    pipeline = FakePipeline()
    pipeline.turn_history = [  # type: ignore[attr-defined]
        {
            "ts": "2026-09-27T10:00:03+00:00",
            "device_id": "dot-1",
            "state": "speaking",
            "outcome": "ok",
            "duration_seconds": 3.412,
            "score": 0.91,
            "confidence": 0.87,
            "service": "turn_on",
            "domain": "light",
            "target_entity_id": "light.wohnzimmer",
            "needs_param": True,
            "extracted_param": "brightness_pct=40",
            "transcript": "mach das licht an",
            "error_code": None,
            "error": None,
            "barge_in": False,
        }
    ]
    populated_app.state.pipeline = pipeline
    body = get_json(populated_app, "/api/history")
    assert body["available"] is True
    assert body["count"] == 1
    entry = body["entries"][0]
    # Die vom unveränderten UI gelesenen Kernspalten …
    assert entry["state"] == "speaking"
    assert entry["duration_seconds"] == 3.412  # Sekunden, nicht Millisekunden
    assert entry["score"] == 0.91
    assert entry["confidence"] == 0.87
    # … plus die P8.D3-Ergänzungen.
    assert entry["target_entity_id"] == "light.wohnzimmer"
    assert entry["needs_param"] is True
    assert entry["extracted_param"] == "brightness_pct=40"
    assert entry["transcript"] == "mach das licht an"
    assert entry["barge_in"] is False
    # Kein Feld außerhalb der Allowlist.
    assert set(entry) == set(dashboard.HISTORY_FIELDS)


def test_history_allowlist_ist_dokumentiert_und_ohne_service_data() -> None:
    """Die Allowlist nennt genau die P8.D3-Felder – kein `raw`/`service_data`."""
    assert "transcript" in dashboard.HISTORY_FIELDS
    assert "error" in dashboard.HISTORY_FIELDS
    assert "needs_param" in dashboard.HISTORY_FIELDS
    assert "extracted_param" in dashboard.HISTORY_FIELDS
    assert "target_entity_id" in dashboard.HISTORY_FIELDS
    assert "barge_in" in dashboard.HISTORY_FIELDS
    for verboten in ("raw", "service_data", "response_text", "audio", "pcm"):
        assert verboten not in dashboard.HISTORY_FIELDS


# ═══════════════════════════════════════════════════════════════════════════
# P9.T0 – GET /api/wake (reine Lese-Ansicht) + Latenzfelder
# ═══════════════════════════════════════════════════════════════════════════
class FakeWakeRing:
    """Attrappe für `WakeWordDetector.wake_attempts` (Property mit Listenwert)."""

    def __init__(self, entries: list[dict]) -> None:
        self.wake_attempts = entries
        self.threshold = 0.9


class FakeWakePipeline:
    """Pipeline-Attrappe **mit** Wake-Zugriff (Ring, IDs, Schwelle)."""

    def __init__(self, rings: Optional[dict[str, list[dict]]] = None) -> None:
        self._rings = rings or {}
        self._wake = {dev: FakeWakeRing(entries) for dev, entries in self._rings.items()}

    def wake_device_ids(self) -> list[str]:
        return sorted(self._rings)

    def wake_attempts(self, device_id: str) -> list[dict]:
        wake = self._wake.get(device_id)
        return [] if wake is None else list(wake.wake_attempts)

    def wake_threshold(self) -> Optional[float]:
        for device_id in sorted(self._wake):
            return self._wake[device_id].threshold
        return None


def _wake_app(rings: Optional[dict[str, list[dict]]] = None) -> FastAPI:
    return build_app(
        settings=FakeSettings(),
        pipeline=FakeWakePipeline(rings if rings is not None else {}),
    )


def _wake_attempt(score: float, accepted: bool, reason: str, index: int) -> dict:
    return {
        "ts": "2026-09-27T13:09:23+00:00",
        "score": score,
        "accepted": accepted,
        "reason": reason,
        "threshold": 0.9,
        "chunk_index": index,
    }


# ── Leerzustand: ehrlich, kein Fehler, keine erfundenen Zahlen ──────────
def test_wake_ohne_versuch_ist_leer_nicht_fehler() -> None:
    """Kein bewerteter Chunk ⇒ HTTP 200, `count: 0`, Kennzahlen `null`."""
    body = get_json(_wake_app(), "/api/wake")
    assert body["count"] == 0
    assert body["attempts"] == []
    assert body["stats"]["count"] == 0
    assert body["stats"]["accepted"] == 0
    assert body["stats"]["rejected"] == 0
    # **null**, nicht 0.0 – „nichts gemessen" ist nicht „0 Sekunden".
    for field in ("score_min", "score_median", "score_max", "best_rejected_score"):
        assert body["stats"][field] is None, field
    assert body["degraded"] is False


def test_wake_ohne_pipeline_antwortet_200_mit_begruendung() -> None:
    """Fehlende Pipeline ⇒ trotzdem 200, `count: 0`, offene Begründung."""
    app = build_app(settings=FakeSettings())
    response = get_response(app, "/api/wake")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["attempts"] == []
    assert body["degraded"] is True
    assert body["issues"], "fehlende Pipeline muss benannt werden"
    assert "Pipeline" in body["issues"][0]


def test_wake_ohne_detektor_nennt_die_settings_schwelle_offen() -> None:
    """Kein Gerät ⇒ die Config-Schwelle, aber als `settings` gekennzeichnet."""
    body = get_json(_wake_app(), "/api/wake")
    assert body["threshold"] == 0.8
    assert body["threshold_source"] == "settings"
    assert body["barge_in_threshold"] == 0.15
    # E96: der Schreibmodus kommt **aus den Settings** und ist standardmäßig
    # aus – das Frontend blendet die Schreib-UI genau danach.
    assert body["write_enabled"] is False
    assert body["writable_setting"] == "oww_threshold"
    assert body["keep_floor"] == 0.2
    assert body["default_threshold"] == 0.8


def test_wake_mit_detektor_nennt_die_wirksame_schwelle_als_device() -> None:
    """Mit Gerät ⇒ die Schwelle des laufenden Detektors, Quelle `device`."""
    app = _wake_app({"dot-1": [_wake_attempt(0.95, True, "accepted", 1)]})
    body = get_json(app, "/api/wake")
    assert body["threshold"] == 0.9
    assert body["threshold_source"] == "device"
    assert body["window"] == dashboard.WAKE_ATTEMPTS_MAXLEN == 50


# ── Genau ein Versuch ────────────────────────────────────────────────────
def test_wake_mit_einem_versuch() -> None:
    """Ein Versuch: alle Felder, `count: 1`, `accepted: 1`, `rejected: 0`."""
    app = _wake_app({"dot-1": [_wake_attempt(0.95, True, "accepted", 7)]})
    body = get_json(app, "/api/wake")
    assert body["count"] == 1
    attempt = body["attempts"][0]
    assert attempt["score"] == 0.95
    assert attempt["accepted"] is True
    assert attempt["reason"] == "accepted"
    assert attempt["threshold"] == 0.9
    assert attempt["chunk_index"] == 7
    assert attempt["device_id"] == "dot-1"  # von der Pipeline ergänzt
    stats = body["stats"]
    assert stats["count"] == 1
    assert stats["accepted"] == 1
    assert stats["rejected"] == 0
    # Kein abgelehnter Versuch ⇒ kein „best_rejected".
    assert stats["best_rejected_score"] is None
    assert stats["score_min"] == stats["score_median"] == stats["score_max"] == 0.95
    assert stats["reasons"] == {"accepted": 1}
    assert body["devices"] == [
        {
            "device_id": "dot-1",
            "threshold": 0.9,
            "barge_in_threshold": 0.15,
            "count": 1,
            "accepted": 1,
            "rejected": 0,
        }
    ]


# ── Ablehnungen: die eigentliche Diagnose ───────────────────────────────
def test_wake_zeigt_ablehnungsgrund_und_besten_abgelehnten_score() -> None:
    """Der höchste abgelehnte Score ist die Zahl „so knapp war es"."""
    app = _wake_app(
        {
            "dot-1": [
                _wake_attempt(0.10, False, "below_threshold", 1),
                _wake_attempt(0.50, False, "below_threshold", 2),
                _wake_attempt(0.88, False, "below_threshold", 3),
                _wake_attempt(0.95, True, "accepted", 4),
            ]
        }
    )
    body = get_json(app, "/api/wake")
    stats = body["stats"]
    assert stats["count"] == 4
    assert stats["accepted"] == 1
    assert stats["rejected"] == 3
    assert stats["best_rejected_score"] == 0.88
    assert stats["score_min"] == 0.1
    assert stats["score_max"] == 0.95
    assert stats["score_median"] == 0.69  # (0.50 + 0.88) / 2
    assert stats["reasons"] == {"below_threshold": 3, "accepted": 1}
    assert body["devices"][0]["rejected"] == 3


def test_wake_zaehlt_alle_vier_gruende() -> None:
    """Jeder der vier echten Gründe taucht in `reasons` auf."""
    app = _wake_app(
        {
            "dot-1": [
                _wake_attempt(0.99, False, "warmup_gate", 1),
                _wake_attempt(0.99, False, "cooldown", 2),
                _wake_attempt(0.30, False, "below_threshold", 3),
                _wake_attempt(0.95, True, "accepted", 4),
            ]
        }
    )
    stats = get_json(app, "/api/wake")["stats"]
    assert stats["reasons"] == {
        "warmup_gate": 1,
        "cooldown": 1,
        "below_threshold": 1,
        "accepted": 1,
    }
    assert stats["accepted"] + stats["rejected"] == stats["count"]


def test_wake_liefert_die_juengsten_versuche_zuerst() -> None:
    """Antwort ist absteigend nach Zeit – wie die Turn-Historie."""
    entries = [_wake_attempt(0.1 * i, False, "below_threshold", i) for i in range(1, 6)]
    app = _wake_app({"dot-1": entries})
    body = get_json(app, "/api/wake")
    assert [a["chunk_index"] for a in body["attempts"]] == [5, 4, 3, 2, 1]


def test_wake_begrenzt_die_antwort_auf_das_limit() -> None:
    """`?limit=` beschneidet **hinten** weg ⇒ die jüngsten bleiben."""
    entries = [_wake_attempt(0.1, False, "below_threshold", i) for i in range(1, 21)]
    app = _wake_app({"dot-1": entries})
    body = get_json(app, "/api/wake?limit=3")
    assert body["count"] == 3
    assert [a["chunk_index"] for a in body["attempts"]] == [20, 19, 18]
    assert body["stats"]["count"] == 3  # Kennzahlen nur über die Antwort


def test_wake_bei_mehreren_geraeten_bleibt_zuordenbar() -> None:
    """Jeder Versuch trägt seine `device_id`; Kennzahlen je Gerät."""
    app = _wake_app(
        {
            "dot-1": [_wake_attempt(0.95, True, "accepted", 1)],
            "dot-2": [
                _wake_attempt(0.20, False, "below_threshold", 1),
                _wake_attempt(0.30, False, "below_threshold", 2),
            ],
        }
    )
    body = get_json(app, "/api/wake")
    assert body["count"] == 3
    assert {a["device_id"] for a in body["attempts"]} == {"dot-1", "dot-2"}
    devices = {d["device_id"]: d for d in body["devices"]}
    assert devices["dot-1"]["accepted"] == 1
    assert devices["dot-1"]["rejected"] == 0
    assert devices["dot-2"]["accepted"] == 0
    assert devices["dot-2"]["rejected"] == 2


def test_wake_verweigert_schreibmethoden_wie_alle_api_routen() -> None:
    """`/api/*` bleibt rein lesend (E17/E94) – auch die neue Route."""

    async def _write() -> httpx.Response:
        transport = httpx.ASGITransport(app=_wake_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://manager"
        ) as client:
            return await client.post("/api/wake", json={})

    assert asyncio.run(_write()).status_code == 405


# ── Robustheit: kaputte/fehlende Quelle wird benannt, nicht erfunden ─────
def test_wake_mit_zeitstempel_als_zahl_wird_iso() -> None:
    """Ein numerischer `ts` wird wie bei der Historie in ISO-UTC gewandelt."""
    app = _wake_app(
        {"dot-1": [{"ts": 1758000000.0, "score": 0.95, "accepted": True,
                    "reason": "accepted", "threshold": 0.9, "chunk_index": 1}]}
    )
    body = get_json(app, "/api/wake")
    assert body["attempts"][0]["ts"].endswith("+00:00")


def test_wake_verwirft_unbrauchbare_werte_statt_sie_zu_raten() -> None:
    """`bool`/`NaN`/Text im Score ⇒ `null`, kein kaputter JSON, kein 0,0."""
    app = _wake_app(
        {
            "dot-1": [
                {"ts": "t", "score": True, "accepted": True, "reason": "accepted",
                 "threshold": 0.9, "chunk_index": 1},
                {"ts": "t", "score": "keine zahl", "accepted": False,
                 "reason": "below_threshold", "threshold": 0.9, "chunk_index": 2},
            ]
        }
    )
    body = get_json(app, "/api/wake")
    assert [a["score"] for a in body["attempts"]] == [None, None]
    # Ohne **eine** verwertbare Zahl bleiben die Kennzahlen `null`.
    assert body["stats"]["score_min"] is None
    assert body["stats"]["score_median"] is None
    assert body["stats"]["score_max"] is None
    # Die Zählung funktioniert trotzdem – `accepted` ist ja lesbar.
    assert body["stats"]["accepted"] == 1
    assert body["stats"]["rejected"] == 1


def test_wake_mit_stub_ohne_wake_attribut_meldet_422_nein_500() -> None:
    """Eine Attrappe ohne die Zugriffe ⇒ 200 + Begründung, **kein** 500."""
    app = build_app(settings=FakeSettings(), pipeline=FakePipeline({}))
    body = get_json(app, "/api/wake")
    assert body["count"] == 0
    assert body["degraded"] is True
    assert any("wake_attempts" in issue for issue in body["issues"])


def test_median_berechnet_gerade_und_ungerade_richtig() -> None:
    """Median der Statistik: ungerade = Mitte, gerade = Mittel der zwei Mitte."""
    assert dashboard._median([]) is None
    assert dashboard._median([0.5]) == 0.5
    assert dashboard._median([0.1, 0.9]) == 0.5
    assert dashboard._median([0.9, 0.1]) == 0.5  # Reihenfolge egal
    assert dashboard._median([3.0, 1.0, 2.0]) == 2.0


# ── Latenzfelder in der History-Allowlist ────────────────────────────────
def test_latency_felder_sind_teil_der_history_allowlist() -> None:
    """Alle neun P9.T0-Felder stehen in `HISTORY_FIELDS` – und nur zusätzlich."""
    for field in (
        "latency_after_speech_seconds",
        "latency_after_wake_seconds",
        "phase_stt_seconds",
        "phase_route_seconds",
        "phase_tts_first_audio_seconds",
        "phase_tts_total_seconds",
        "phase_listen_seconds",
        "phase_execute_seconds",
        "phase_tts_first_frame_seconds",
    ):
        assert field in dashboard.HISTORY_FIELDS, field
        assert field in dashboard.HistoryEntry.model_fields, field
    # Nichts ist doppelt, die Reihenfolge ist eindeutig.
    assert len(set(dashboard.HISTORY_FIELDS)) == len(dashboard.HISTORY_FIELDS)
    # Die P8.D3-Felder existieren unverändert weiter.
    for field in ("ts", "duration_seconds", "score", "confidence", "transcript",
                  "error", "barge_in", "needs_param", "extracted_param"):
        assert field in dashboard.HISTORY_FIELDS, field


def test_history_liefert_latenzfelder_null_statt_fehlend() -> None:
    """Ein Eintrag ohne Phasen-Felder ⇒ die Felder sind `null`, nicht weg.

    Genau der Fall bei einem Alt-Puffer nach dem Neustart: das Frontend soll
    „–" sehen und nicht einen stillen Sprung in der Anzeige.
    """
    pipeline = FakePipeline({"dot-1": PipelineState.SPEAKING})
    pipeline.turn_history = [  # type: ignore[attr-defined]
        {"ts": "t", "device_id": "dot-1", "duration_seconds": 1.5}
    ]
    app = build_app(settings=FakeSettings(), pipeline=pipeline)
    body = get_json(app, "/api/history")
    entry = body["entries"][0]
    for field in ("latency_after_speech_seconds", "phase_stt_seconds",
                  "phase_tts_total_seconds"):
        assert entry[field] is None, field
    assert entry["duration_seconds"] == 1.5  # Bestand unverändert


def test_history_zeile_mit_latenz_kommt_vollstaendig_an() -> None:
    """Ein gemessener Eintrag wird inklusive aller neun Felder ausgeliefert."""
    phases = {
        "latency_after_speech_seconds": 3.5,
        "latency_after_wake_seconds": 4.5,
        "phase_stt_seconds": 2.0,
        "phase_route_seconds": 1.0,
        "phase_tts_first_audio_seconds": 2.0,
        "phase_tts_total_seconds": 3.4,
        "phase_listen_seconds": 1.0,
        "phase_execute_seconds": 0.5,
        "phase_tts_first_frame_seconds": 2.2,
    }
    pipeline = FakePipeline({"dot-1": PipelineState.SPEAKING})
    pipeline.turn_history = [  # type: ignore[attr-defined]
        {"ts": "t", "device_id": "dot-1", "duration_seconds": 11.326, **phases}
    ]
    app = build_app(settings=FakeSettings(), pipeline=pipeline)
    entry = get_json(app, "/api/history")["entries"][0]
    for field, value in phases.items():
        assert entry[field] == value, field
    # `duration_seconds` bleibt die Gesamtzeit und wird **nicht** ersetzt.
    assert entry["duration_seconds"] == 11.326
    assert set(entry) == set(dashboard.HISTORY_FIELDS)


# ═══════════════════════════════════════════════════════════════════════════
# E96 (P9.T1) – die **eine** Schreibroute: /api/config/oww-threshold
# ═══════════════════════════════════════════════════════════════════════════
# Zusicherungen, jede einzeln rot bei Verstoß:
#   * Standard ist **aus** ⇒ 403, und es ändert sich **nichts**,
#   * es ist **genau eine** Route, und sie bewegt **nur** `oww_threshold`,
#   * Fremdfeld ⇒ 400, unbrauchbarer/zu großer Wert ⇒ 422,
#   * wirkt ohne Neustart (Settings werden zurückgelesen),
#   * persistiert in die `.env`, ohne eine andere Zeile anzufassen,
#   * protokolliert jede Änderung (Audit) – ohne Secret,
#   * **kein** HA-Call, **kein** `/api/services`, **kein** Gerät.

WRITE_PATH = "/api/config/oww-threshold"
#: **E111 (P11.T2)** – die Multi-Field-Schreibroute.
CONFIG_PATH = "/api/config"


def _write_app(**state: Any) -> FastAPI:
    settings = FakeSettings()
    for key, value in state.items():
        setattr(settings, key, value)
    return build_app(settings=settings)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Echte `.env` in einem Temp-Verzeichnis (Secrets nur als Platzhalter)."""
    path = tmp_path / ".env"
    path.write_text(
        "MANAGER_PORT=8767\n"
        "HA_TOKEN=SECRET-HA\n"
        "OWW_THRESHOLD=0.90  # von Hand getuned\n"
        "LLM_API_KEY=SECRET-LLM\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "ENV_FILE", path)
    # Der Schreibpfad setzt das **Modul**-Singleton, das `app.wake_word` liest.
    # `monkeypatch` stellt es nach dem Test automatisch zurück – sonst leckt der
    # Wert in die nächsten Tests (und in die Testreihenfolge selbst).
    monkeypatch.setattr(dashboard._module_settings, "oww_threshold", 0.80)
    monkeypatch.setattr(dashboard._module_settings, "dashboard_write_enabled", False)
    # E111: die Live-Felder auf Known-Defaults pinnen (lecken sonst zwischen
    # den Tests, weil `write_config` das Modul-Singleton mitsetzt).
    monkeypatch.setattr(dashboard._module_settings, "log_level", "INFO")
    monkeypatch.setattr(dashboard._module_settings, "audio_dump_enabled", False)
    monkeypatch.setattr(dashboard._module_settings, "whisper_language", "de")
    return path


def test_schreiben_ist_standardmaessig_aus_403_und_aendert_nichts(
    env_file,
) -> None:
    """Ohne `dashboard_write_enabled` ⇒ 403, `.env` und Settings unangetastet."""
    before = env_file.read_text(encoding="utf-8")
    app = _write_app()  # FakeSettings: dashboard_write_enabled = False
    response = post_response(app, WRITE_PATH, {"threshold": 0.75})
    assert response.status_code == 403
    assert "nichts" in response.json()["detail"]
    assert env_file.read_text(encoding="utf-8") == before
    # Auch das Modul-Singleton (das `app.wake_word` liest) blieb unberührt.
    assert dashboard._module_settings.oww_threshold == 0.80


def test_403_gilt_auch_fuer_ein_korrektes_sonst_unzulaessiges_feld(
    env_file,
) -> None:
    """Die Sperre kommt **vor** der Validierung: nichts wird ausgewertet."""
    app = _write_app()
    for payload in ({"threshold": 0.75}, {"oww_threshold": 1.5}, {"nonsense": "x"}):
        assert post_response(app, WRITE_PATH, payload).status_code == 403
    assert "0.90" in env_file.read_text(encoding="utf-8")


def test_schreiben_setzt_genau_einen_wert_und_wirkt_ohne_neustart(
    env_file,
) -> None:
    """200: alter/neuer Wert, Rücklese-Bestätigung, `.env` – ohne Fremdzeile."""
    app = _write_app(dashboard_write_enabled=True)
    response = post_response(app, WRITE_PATH, {"threshold": 0.75})
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["previous"] == 0.80
    assert body["threshold"] == 0.75
    assert body["setting"] == "oww_threshold"
    assert body["source"] == "ui"
    assert body["settings_applied"] is True
    assert body["effective_without_restart"] is True
    assert body["persisted_env"] is True
    # Die Datei: **nur** die eine Zeile ist neu, der Rest bytegleich.
    text = env_file.read_text(encoding="utf-8")
    assert "OWW_THRESHOLD=0.75  # von Hand getuned\n" in text
    assert "HA_TOKEN=SECRET-HA" in text
    assert "LLM_API_KEY=SECRET-LLM" in text
    assert "MANAGER_PORT=8767" in text
    # Und der laufende Prozess sieht es sofort (er ist die Quelle des Vergleichs).
    assert dashboard._module_settings.oww_threshold == 0.75
    # Kein Geheimnis in der Antwort.
    assert "SECRET" not in response.text


def test_schreiben_haengt_eine_fehlende_zeile_an_und_behaelt_den_modus(
    env_file,
) -> None:
    """Ohne `OWW_THRESHOLD` in der `.env` wird sie angehängt, sonst bleibt es."""
    env_file.write_text("MANAGER_PORT=8767\n", encoding="utf-8")
    app = _write_app(dashboard_write_enabled=True)
    assert post_response(app, WRITE_PATH, {"threshold": 0.70}).status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert text == "MANAGER_PORT=8767\nOWW_THRESHOLD=0.70\n"
    assert env_file.stat().st_mode & 0o777 != 0o000  # kein 0-Modus


def test_fremdfeld_wird_abgewiesen_400(env_file) -> None:
    """Nur `threshold` – alles andere ist ein **Fehler**, kein stilles Ignorieren."""
    app = _write_app(dashboard_write_enabled=True)
    for payload in (
        {"threshold": 0.75, "oww_threshold": 0.9},
        {"threshold": 0.75, "device_id": "dot-1"},
        {"oww_threshold": 0.9},
        {},
    ):
        response = post_response(app, WRITE_PATH, payload)
        assert response.status_code == 400, payload
    assert env_file.read_text(encoding="utf-8").count("OWW_THRESHOLD") == 1


def test_unbrauchbarer_wert_wird_abgewiesen_422(env_file) -> None:
    """Text, `bool`, `NaN`, `0.0` und `> 1.0` ⇒ 422, ohne Änderung."""
    app = _write_app(dashboard_write_enabled=True)
    for payload in (
        {"threshold": "0.75"},
        {"threshold": True},
        {"threshold": 0.0},
        {"threshold": -0.1},
        {"threshold": 1.01},
    ):
        response = post_response(app, WRITE_PATH, payload)
        assert response.status_code == 422, payload
    assert dashboard._module_settings.oww_threshold == 0.80


def test_jeder_schreibversuch_wird_im_log_protokolliert(
    env_file, caplog,
) -> None:
    """Audit auf `INFO`: alter Wert, neuer Wert, Quelle `ui` – ohne Secret."""
    app = _write_app(dashboard_write_enabled=True)
    with caplog.at_level(logging.INFO, logger=f"{LOGGER_NAMESPACE}.dashboard"):
        assert post_response(app, WRITE_PATH, {"threshold": 0.75}).status_code == 200
    messages = [r.getMessage() for r in caplog.records]
    audit = [m for m in messages if "oww_threshold" in m and "Quelle=ui" in m]
    assert audit, messages
    assert "0.800" in audit[0] and "0.750" in audit[0]
    assert "SECRET" not in audit[0]


def test_ablehnungen_landen_nicht_im_audit_log(env_file, caplog) -> None:
    """Ein abgewiesener Versuch verändert nichts und wird **nicht** geloggt."""
    app = _write_app(dashboard_write_enabled=True)
    before = env_file.read_text(encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=f"{LOGGER_NAMESPACE}.dashboard"):
        assert post_response(app, WRITE_PATH, {"threshold": 0.0}).status_code == 422
    assert env_file.read_text(encoding="utf-8") == before
    assert not [r for r in caplog.records if "oww_threshold" in r.getMessage()]


def test_die_schreibroute_bewegt_kein_haus_assistant_obj(env_file) -> None:
    """Härteste E96-Zusage: **kein** HA-Client wird angefasst.

    `app.state.ha_client` bekommt in diesem Test einen Attrappen, der jede
    Berührung als Fehler meldet – ein Aufruf hätte ihn sichtbar gemacht.
    (Die Fixture `env_file` kapselt zusätzlich das Modul-Singleton und die
    `.env`, damit der Schreibpfad nicht in andere Tests durchschlägt.)
    """
    class ExplodingHA:
        def __getattr__(self, name: str) -> Any:  # pragma: no cover
            raise AssertionError(f"HA wurde angefasst: {name}")

    app = _write_app(dashboard_write_enabled=True, ha_client=ExplodingHA())
    assert post_response(app, WRITE_PATH, {"threshold": 0.75}).status_code == 200
    # Und es gibt weiterhin **keine** Services-Route, die man missbrauchen könnte.
    paths = {route.path for route in dashboard.router.routes}
    assert "/api/services" not in paths
    assert {p for p in paths if p.startswith("/api/config")} == {
        "/api/config",
        "/api/config/oww-threshold",
    }


def test_get_wake_meldet_den_schreibmodus_als_false_ohne_freigabe(env_file) -> None:
    """Das Frontend entscheidet die Schreib-UI allein nach `write_enabled`."""
    body = get_json(_write_app(), "/api/wake")
    assert body["write_enabled"] is False
    body = get_json(_write_app(dashboard_write_enabled=True), "/api/wake")
    assert body["write_enabled"] is True


# ── E110 (P11.T1) – optionale Auth-Schicht: DASHBOARD_API_TOKEN ──────────
#
# Schicht 1 (neu) liegt **vor** Schicht 2 (bestehend): Token-Check (401) vor
# `dashboard_write_enabled` (403).  Token **leer** ⇒ offen (Default,
# backward-kompatibel).  Gesetzt ⇒ `Authorization: Bearer <token>` muss exakt
# stimmen (konstantenzeitig), sonst 401.  Geschützt ist nur `POST /api/config/*`
# – `GET`-Routen und `/health` bleiben ohne Token erreichbar.

AUTH_TOKEN: Final[str] = "test-token"


def test_auth_token_leer_offen_schreiben_funktioniert(env_file) -> None:
    """`DASHBOARD_API_TOKEN` leer (Default) ⇒ Auth aus ⇒ 200 ohne Header."""
    app = _write_app(dashboard_write_enabled=True)
    assert post_response(app, WRITE_PATH, {"threshold": 0.75}).status_code == 200


def test_auth_token_gesetzt_korrekter_bearer_200(env_file) -> None:
    """Token gesetzt + korrekter Bearer + Write=true ⇒ 200."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN, dashboard_write_enabled=True)
    response = post_response_headers(
        app, WRITE_PATH, {"threshold": 0.75}, {"Authorization": f"Bearer {AUTH_TOKEN}"}
    )
    assert response.status_code == 200


def test_auth_token_gesetzt_kein_header_401(env_file) -> None:
    """Token gesetzt + kein `Authorization`-Header ⇒ 401 (leaks nichts)."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN, dashboard_write_enabled=True)
    response = post_response(app, WRITE_PATH, {"threshold": 0.75})
    assert response.status_code == 401
    assert AUTH_TOKEN not in response.text  # Tokenwert nie im Antwortbody


def test_auth_token_gesetzt_falscher_token_401(env_file) -> None:
    """Token gesetzt + falsches Token ⇒ 401."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN, dashboard_write_enabled=True)
    response = post_response_headers(
        app, WRITE_PATH, {"threshold": 0.75}, {"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_auth_token_falsches_praefix_401(env_file) -> None:
    """Token gesetzt + falsches Präfix (nicht `Bearer `) ⇒ 401."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN, dashboard_write_enabled=True)
    response = post_response_headers(
        app, WRITE_PATH, {"threshold": 0.75}, {"Authorization": f"Basic {AUTH_TOKEN}"}
    )
    assert response.status_code == 401


def test_auth_token_vor_schreibmodus_401_schlaegt_403(env_file) -> None:
    """Reihenfolge: fehlender Token + Write **aus** ⇒ 401, nicht 403."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN)  # Write default False
    assert post_response(app, WRITE_PATH, {"threshold": 0.75}).status_code == 401


def test_auth_token_korrekt_dann_schreibmodus_403(env_file) -> None:
    """Reihenfolge: korrektes Token + Write **aus** ⇒ 403 (Schicht 2)."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN)  # Write default False
    response = post_response_headers(
        app, WRITE_PATH, {"threshold": 0.75}, {"Authorization": f"Bearer {AUTH_TOKEN}"}
    )
    assert response.status_code == 403


def test_get_config_braucht_kein_token(env_file) -> None:
    """`GET /api/config` bleibt ohne Token 200 (nur `POST /api/config/*` geschützt)."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN)
    assert get_json(app, "/api/config") is not None


# ══════════════════════════════════════════════════════════════════════
#  E111 (P11.T2) – `PUT /api/config` (Multi-Field-Schreibroute)
# ══════════════════════════════════════════════════════════════════════

#: Alle elf E111-Felder in einem Request (für den Multi-Field-Test).
_ALL_E111_FIELDS: dict[str, Any] = {
    "oww_threshold": 0.75,
    "log_level": "DEBUG",
    "audio_dump_enabled": True,
    "whisper_language": "en",
    "oww_barge_in_threshold": 0.2,
    "oww_cooldown_ms": 500,
    "wake_attempt_keep_floor": 0.1,
    "router_confidence_gate": 0.8,
    "router_needs_param_threshold": 0.6,
    "jev_mode": "gate",
    "ha_entity_domains": "light,switch",
}


def test_config_write_ist_standardmaessig_aus_403_und_aendert_nichts(
    env_file,
) -> None:
    """Ohne `dashboard_write_enabled` ⇒ 403, `.env` und Settings unangetastet."""
    before = env_file.read_text(encoding="utf-8")
    app = _write_app()  # FakeSettings: dashboard_write_enabled = False
    response = put_response(app, CONFIG_PATH, {"log_level": "DEBUG"})
    assert response.status_code == 403
    assert "nichts" in response.json()["detail"]
    assert env_file.read_text(encoding="utf-8") == before
    assert dashboard._module_settings.log_level == "INFO"


def test_config_write_multi_feld_alle_elf(env_file) -> None:
    """200: alle elf Felder, `written` sortiert, Live-/Neustart-Mapping korrekt."""
    app = _write_app(dashboard_write_enabled=True)
    response = put_response(app, CONFIG_PATH, dict(_ALL_E111_FIELDS))
    assert response.status_code == 200
    body = response.json()
    assert body["written"] == sorted(_ALL_E111_FIELDS)
    assert body["effective_without_restart"] == {
        "oww_threshold": True,
        "log_level": True,
        "audio_dump_enabled": True,
        "whisper_language": True,
        "oww_barge_in_threshold": False,
        "oww_cooldown_ms": False,
        "wake_attempt_keep_floor": False,
        "router_confidence_gate": False,
        "router_needs_param_threshold": False,
        "jev_mode": False,
        "ha_entity_domains": False,
    }
    assert body["restarted_required"] == [
        "ha_entity_domains",
        "jev_mode",
        "oww_barge_in_threshold",
        "oww_cooldown_ms",
        "router_confidence_gate",
        "router_needs_param_threshold",
        "wake_attempt_keep_floor",
    ]
    # Die `.env`: jede Key-Zeile ist da, die `OWW_THRESHOLD`-Zeile ersetzt
    # (Kommentar bleibt), die Secrets sind **bytegleich** erhalten.
    text = env_file.read_text(encoding="utf-8")
    assert "OWW_THRESHOLD=0.75  # von Hand getuned" in text
    assert "LOG_LEVEL=DEBUG" in text
    assert "AUDIO_DUMP_ENABLED=true" in text
    assert "WHISPER_LANGUAGE=en" in text
    assert "OWW_BARGE_IN_THRESHOLD=0.2" in text
    assert "OWW_COOLDOWN_MS=500" in text
    assert "WAKE_ATTEMPT_KEEP_FLOOR=0.1" in text
    assert "ROUTER_CONFIDENCE_GATE=0.8" in text
    assert "ROUTER_NEEDS_PARAM_THRESHOLD=0.6" in text
    assert "JEV_MODE=gate" in text
    assert "HA_ENTITY_DOMAINS=light,switch" in text
    assert "HA_TOKEN=SECRET-HA" in text
    assert "LLM_API_KEY=SECRET-LLM" in text
    assert "MANAGER_PORT=8767" in text
    # Live-Felder wirken **ohne** Neustart (Modul-Singleton, die `app/*` liest).
    assert dashboard._module_settings.oww_threshold == 0.75
    assert dashboard._module_settings.log_level == "DEBUG"
    assert dashboard._module_settings.audio_dump_enabled is True
    assert dashboard._module_settings.whisper_language == "en"
    # Kein Geheimnis in der Antwort.
    assert "SECRET" not in response.text


def test_config_write_partial_update(env_file) -> None:
    """Teil-Update: nur die angebotenen Felder werden geschrieben."""
    app = _write_app(dashboard_write_enabled=True)
    response = put_response(
        app, CONFIG_PATH, {"log_level": "WARNING", "oww_cooldown_ms": 250}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["written"] == ["log_level", "oww_cooldown_ms"]
    assert body["effective_without_restart"] == {
        "log_level": True,
        "oww_cooldown_ms": False,
    }
    assert body["restarted_required"] == ["oww_cooldown_ms"]
    text = env_file.read_text(encoding="utf-8")
    assert "LOG_LEVEL=WARNING" in text
    assert "OWW_COOLDOWN_MS=250" in text
    # Nicht angebotene Felder bleiben unangetastet.
    assert "OWW_THRESHOLD=0.90" in text
    assert dashboard._module_settings.log_level == "WARNING"
    assert dashboard._module_settings.oww_threshold == 0.80


def test_config_write_leerer_body_400(env_file) -> None:
    """Leeres `{}` ⇒ 400 (mindestens ein Feld angeben)."""
    app = _write_app(dashboard_write_enabled=True)
    response = put_response(app, CONFIG_PATH, {})
    assert response.status_code == 400
    assert "leer" in response.json()["detail"]


def test_config_write_fremdfeld_wird_abgewiesen_400(env_file) -> None:
    """Fremd-/Secret-Felder ⇒ **400** (kein stilles Ignorieren), `.env` ruhig."""
    app = _write_app(dashboard_write_enabled=True)
    before = env_file.read_text(encoding="utf-8")
    for payload in (
        {"ha_token": "x"},
        {"llm_api_key": "x"},
        {"manager_port": 8767},
        {"whisper_model": "small"},
        {"piper_voice": "x"},
        {"oww_model": "x"},
        {"enable_test_hooks": True},
        {"audio_width": 2},
        {"log_level": "DEBUG", "dashboard_write_enabled": True},
    ):
        response = put_response(app, CONFIG_PATH, payload)
        assert response.status_code == 400, payload
        assert "Unbekannt" in response.json()["detail"], payload
    assert env_file.read_text(encoding="utf-8") == before


def test_config_write_typ_und_bereichsfehler_422(env_file) -> None:
    """Falscher Typ/Bereich/Wert ⇒ **422**, Settings unangetastet."""
    app = _write_app(dashboard_write_enabled=True)
    for payload in (
        {"oww_threshold": "0.75"},
        {"oww_threshold": True},
        {"oww_threshold": 0.0},
        {"oww_threshold": 1.5},
        {"oww_cooldown_ms": -1},
        {"oww_cooldown_ms": 1.5},
        {"wake_attempt_keep_floor": 1.0},
        {"log_level": "LOUD"},
        {"jev_mode": "bogus"},
        {"audio_dump_enabled": "yes"},
        {"whisper_language": ""},
        {"ha_entity_domains": "light,Bad_Domain!"},
        {"oww_threshold": None},
    ):
        response = put_response(app, CONFIG_PATH, payload)
        assert response.status_code == 422, payload
    assert dashboard._module_settings.oww_threshold == 0.80
    assert dashboard._module_settings.log_level == "INFO"


def test_config_write_secrets_bleiben_bytegleich(env_file) -> None:
    """Ein Multi-Write verändert **nur** die eigenen Zeilen; Secrets intakt."""
    app = _write_app(dashboard_write_enabled=True)
    before = env_file.read_text(encoding="utf-8")
    assert put_response(app, CONFIG_PATH, dict(_ALL_E111_FIELDS)).status_code == 200
    after = env_file.read_text(encoding="utf-8")
    for secret_line in ("HA_TOKEN=SECRET-HA", "LLM_API_KEY=SECRET-LLM"):
        assert secret_line in before and secret_line in after
    # Und die Datei hat keine doppelten Key-Zeilen bekommen.
    for key in ("HA_TOKEN", "LLM_API_KEY", "MANAGER_PORT"):
        assert after.count(f"{key}=") == before.count(f"{key}=")


def test_config_write_idempotenz(env_file) -> None:
    """Zweifacher identischer Write ⇒ **keine** Duplikat-Zeile in der `.env`."""
    app = _write_app(dashboard_write_enabled=True)
    assert put_response(app, CONFIG_PATH, {"log_level": "DEBUG"}).status_code == 200
    assert put_response(app, CONFIG_PATH, {"log_level": "DEBUG"}).status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert text.count("LOG_LEVEL=") == 1
    assert "LOG_LEVEL=DEBUG" in text


def test_config_write_auth_401_vor_403(env_file) -> None:
    """Reihenfolge: fehlender Token + Write aus ⇒ **401** (nicht 403)."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN)  # Write default False
    response = put_response(app, CONFIG_PATH, {"log_level": "DEBUG"})
    assert response.status_code == 401
    assert AUTH_TOKEN not in response.text


def test_config_write_token_korrekt_dann_schreibmodus_403(env_file) -> None:
    """Korrektes Token + Write **aus** ⇒ **403** (Schicht 2)."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN)  # Write default False
    response = put_response_headers(
        app, CONFIG_PATH, {"log_level": "DEBUG"}, {"Authorization": f"Bearer {AUTH_TOKEN}"}
    )
    assert response.status_code == 403


def test_config_write_token_gesetzt_korrekt_dann_200(env_file) -> None:
    """Korrektes Token + Write **an** ⇒ **200** (beide Schichten grün)."""
    app = _write_app(dashboard_api_token=AUTH_TOKEN, dashboard_write_enabled=True)
    response = put_response_headers(
        app, CONFIG_PATH, {"log_level": "DEBUG"}, {"Authorization": f"Bearer {AUTH_TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json()["written"] == ["log_level"]


def test_config_write_wird_im_log_protokolliert(env_file, caplog) -> None:
    """Jeder Config-Write landet auf `INFO` (E111-Marker, kein Secret)."""
    app = _write_app(dashboard_write_enabled=True)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAMESPACE):
        assert put_response(app, CONFIG_PATH, {"log_level": "DEBUG"}).status_code == 200
    assert any("E111" in record.getMessage() for record in caplog.records)
    assert not any("SECRET" in record.getMessage() for record in caplog.records)


def test_config_write_log_level_wirkt_sofort_auf_den_laufenden_logger(env_file) -> None:
    """`log_level` live: der laufende `manager`-Logger übernimmt das neue Level.

    Live-Befund P11.T4 (`.106`): ohne `configure_logging()`-Re-Apply blieb der
    Logger bis zum Neustart auf dem alten Level, obwohl `effective_without_
    restart` `True` meldete.  `configure_logging()` ist idempotent — er löst
    den Level neu aus `settings.log_level` auf (die der Write soeben gesetzt
    hat); der conftest-Fix `_restore_manager_logging` rollt das danach zurück.
    """
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    assert put_response(
        _write_app(dashboard_write_enabled=True),
        CONFIG_PATH,
        {"log_level": "DEBUG"},
    ).status_code == 200
    assert manager_logger.level == logging.DEBUG
