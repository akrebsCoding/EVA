"""Tests des Pairing-Status (P10.T3) — Layer **L0** (rein, kein Netz).

Muster wie `tests/test_dashboard.py` (P8): Fakes in `app.state`, ASGI-Transport
ohne Socket, `asyncio.run`. Geprüft wird:

* `GET /api/pairing` — Ampel (`paired`/`waiting`/`mdns_off`/`unknown`),
  mDNS-Felder (`enabled`/`running`/`announced_as`/`name_conflict`),
  Geräte-Zeilen (`mic_synced`, ehrlich `last_seen: null`).
* **Doppel-Announce** — `MdnsAnnouncer.registered_instance_name` + die
  Umbenennungs-WARN (Zeroconf-macht-`echomuse-2`) auf Datenebene.
* **Doppel-Device-ID** — zweiter `register` mit gleicher ID ⇒ WARN-Log,
  Registry-Ersetzen (Re-Connect-Semantik, kein Verhaltens-Umbau).
* `Pipeline.mic_synced` — erst nach dem ersten Mic-Frame `True`.
* Wizard-Bausteine — `pairing_summary_lines` für alle vier Ampelzustände.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app import dashboard
from app.mdns import MdnsAnnouncer, SERVICE_TYPE
from app.pipeline import Pipeline, PipelineState
from app.ws_server import WsServer

from tests.fakes.fake_echomuse import FakeEchoDot

pytestmark = pytest.mark.unit


# ── Fakes (duck-typed wie in tests/test_dashboard.py) ────────────────────
class FakeSession:
    def __init__(self, device_id: str, *, dead: bool = False, age: float = 12.0) -> None:
        self.device_id = device_id
        self.dead = dead
        self.connected_at = time.monotonic() - age


class FakeRegistry:
    def __init__(self, sessions: dict[str, FakeSession] | None = None) -> None:
        self._sessions = sessions or {}

    async def snapshot(self) -> dict[str, FakeSession]:
        return dict(self._sessions)

    async def size(self) -> int:
        return len(self._sessions)

    async def ids(self) -> tuple[str, ...]:
        return tuple(self._sessions)


class FakeWsServer:
    def __init__(self, sessions: dict[str, FakeSession] | None = None) -> None:
        self.registry = FakeRegistry(sessions)


class FakePipeline:
    def __init__(self, states: dict[str, PipelineState] | None = None,
                 synced: dict[str, bool] | None = None) -> None:
        self._states = states or {}
        self._synced = synced or {}

    def state_of(self, device_id: str) -> PipelineState:
        return self._states.get(device_id, PipelineState.IDLE)

    def mic_synced(self, device_id: str) -> bool | None:
        return self._synced.get(device_id)


class FakeMdns:
    def __init__(
        self,
        *,
        is_running: bool = True,
        enabled: bool = True,
        announced_name: str | None = "echomuse",
        name_conflict: bool = False,
    ) -> None:
        self.is_running = is_running
        self.enabled = enabled
        self.announced_name = announced_name
        self.name_conflict = name_conflict


class FakeSettings:
    model_fields_set = frozenset()

    def __init__(self, *, mdns_name: str = "echomuse") -> None:
        self.manager_mdns_name = mdns_name
        self.manager_mdns_enabled = True
        self.manager_port = 8767


def build_app(**state: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard.router)
    for key, value in state.items():
        setattr(app.state, key, value)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.get(path)


def get_json(app: FastAPI, path: str) -> Any:
    return asyncio.run(_get(app, path)).json()


# ── /api/pairing — Ampel ─────────────────────────────────────────────────
def test_pairing_leeres_state_ist_unknown_und_degradiert() -> None:
    """Ohne `app.state`-Einträge: 200 + `unknown` + ehrliche issues (nie 500)."""
    payload = get_json(build_app(), "/api/pairing")
    assert payload["status"] == "unknown"
    assert payload["degraded"] is True
    assert payload["device_count"] == 0
    assert payload["mdns_enabled"] is None
    assert any("mdns" in issue for issue in payload["issues"])


def test_pairing_wartend_mdns_an_kein_geraet() -> None:
    """`waiting`: mDNS läuft, aber kein Dot — der Wizard-Text muss lesbar sein."""
    app = build_app(
        settings=FakeSettings(),
        pipeline=FakePipeline(),
        ws_server=FakeWsServer({}),
        mdns=FakeMdns(announced_name="echomuse"),
    )
    payload = get_json(app, "/api/pairing")
    assert payload["status"] == "waiting"
    assert payload["mdns_enabled"] is True
    assert payload["mdns_running"] is True
    assert payload["mdns_announced_as"] == "echomuse"
    assert payload["mdns_name_conflict"] is False
    assert payload["device_count"] == 0
    assert "warte" in payload["hint"].lower()
    assert payload["degraded"] is False


def test_pairing_verbunden_mit_mic_sync() -> None:
    """`paired`: Gerätezeile mit state + mic_synced; `last_seen` bleibt null."""
    app = build_app(
        settings=FakeSettings(),
        pipeline=FakePipeline(
            states={"dot-1": PipelineState.LISTENING}, synced={"dot-1": True}
        ),
        ws_server=FakeWsServer({"dot-1": FakeSession("dot-1", age=5.0)}),
        mdns=FakeMdns(),
    )
    payload = get_json(app, "/api/pairing")
    assert payload["status"] == "paired"
    assert payload["device_count"] == 1
    device = payload["devices"][0]
    assert device["device_id"] == "dot-1"
    assert device["connected"] is True
    assert device["state"] == "listening"
    assert device["mic_synced"] is True
    assert device["last_seen"] is None  # bewusst nicht geführt (kein geratener Wert)
    assert device["connected_seconds"] > 0
    assert "verbunden" in payload["hint"].lower()


def test_pairing_mdns_off_ist_eigene_ampel() -> None:
    """`mdns_off`: der Dot kann den Manager nicht finden — klarer Hinweis."""
    app = build_app(
        settings=FakeSettings(),
        pipeline=FakePipeline(),
        ws_server=FakeWsServer({}),
        mdns=FakeMdns(is_running=False, enabled=False, announced_name=None),
    )
    payload = get_json(app, "/api/pairing")
    assert payload["status"] == "mdns_off"
    assert payload["mdns_enabled"] is False
    assert payload["mdns_running"] is False
    assert "MANAGER_MDNS_ENABLED" in payload["hint"]


def test_pairing_doppel_announce_wird_sichtbar() -> None:
    """Zweiter Announcer ⇒ `mdns_announced_as: echomuse-2` + Konflikt-Flag."""
    app = build_app(
        settings=FakeSettings(mdns_name="echomuse"),
        pipeline=FakePipeline(),
        ws_server=FakeWsServer({}),
        mdns=FakeMdns(announced_name="echomuse-2", name_conflict=True),
    )
    payload = get_json(app, "/api/pairing")
    assert payload["mdns_announced_as"] == "echomuse-2"
    assert payload["mdns_name_conflict"] is True
    assert payload["status"] == "waiting"  # Ampel bleibt: Dot könnte fehlen


def test_pairing_name_conflict_aus_announced_vs_configured() -> None:
    """Auch ohne explizites `name_conflict`-Flag: announced != configured ⇒ True."""
    app = build_app(
        settings=FakeSettings(mdns_name="echomuse"),
        pipeline=FakePipeline(),
        ws_server=FakeWsServer({}),
        mdns=FakeMdns(announced_name="echomuse-2", name_conflict=False),
    )
    payload = get_json(app, "/api/pairing")
    assert payload["mdns_name_conflict"] is True


def test_pairing_gestoßenes_geraet_zeigt_dead() -> None:
    """Als tot markierte Session: `connected: false`, Ampel fällt auf `waiting`."""
    app = build_app(
        settings=FakeSettings(),
        pipeline=FakePipeline(synced={"dot-1": True}),
        ws_server=FakeWsServer({"dot-1": FakeSession("dot-1", dead=True)}),
        mdns=FakeMdns(),
    )
    payload = get_json(app, "/api/pairing")
    assert payload["devices"][0]["connected"] is False
    assert payload["status"] == "waiting"


# ── Doppel-Announce: Instanznamen-Parsing + Umbenennung ──────────────────
def test_registered_instance_name_löst_instanz_aus_vollem_namen() -> None:
    assert MdnsAnnouncer.registered_instance_name(f"echomuse.{SERVICE_TYPE}") == "echomuse"
    assert MdnsAnnouncer.registered_instance_name(f"echomuse-2.{SERVICE_TYPE}") == "echomuse-2"
    assert MdnsAnnouncer.registered_instance_name("kaputt") == "kaputt"


def test_name_conflict_property_vor_dem_start_false() -> None:
    announcer = MdnsAnnouncer(name="echomuse", port=8767)
    assert announcer.name_conflict is False
    assert announcer.announced_name is None


def test_umbenennung_durch_zeroconf_wird_gewarnt(caplog: pytest.LogCaptureFixture) -> None:
    """Registriert Zeroconf unter `echomuse-2` ⇒ WARN (Doppel-Announce sichtbar).

    L0 hat kein Netz (Zeroconf-Konstruktor blockiert, conftest) — deshalb wird
    exakt die Buchhaltung nach der Registrierung (`_adopt`) mit einer
    nachträglich umgenannten ServiceInfo-Attrappe gefahren; `start()` selbst
    ruft seit P10.T3 genau diese Methode.
    """
    announcer = MdnsAnnouncer(name="echomuse", port=8767, address="127.0.0.1",
                              enabled=False)

    class FakeInfo:
        name = f"echomuse-2.{SERVICE_TYPE}"

    assert announcer.start() is False  # deaktiviert: sauberes No-op-Starten
    with caplog.at_level(logging.WARNING, logger="mdns"):
        announcer._adopt(FakeInfo())  # type: ignore[arg-type]
    assert announcer.announced_name == "echomuse-2"
    assert announcer.name_conflict is True
    assert any("Doppel-Announce" in record.message for record in caplog.records)


# ── Doppel-Device-ID am WS-Server (nur WARN, Verhalten = Ersetzen) ───────
class FakeControlSocket:
    """Minimaler WS-Ersatz für `_handshake` (receive/send/close, kein Netz)."""

    def __init__(self, register_payload: str) -> None:
        self._payload = register_payload
        self.sent: list[Any] = []
        self.closed_with: int | None = None

    async def receive_text(self) -> str:
        return self._payload

    async def send_json(self, message: Any) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


def _register_json(device_id: str) -> str:
    return json.dumps(
        {"type": "register", "device_id": device_id, "capabilities": ["mic"]}
    )


def test_zweiter_register_gleicher_id_warnt_und_ersetzt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = WsServer(pipeline=None, keepalive_interval=0)
    first = FakeControlSocket(_register_json("dot-1"))
    second = FakeControlSocket(_register_json("dot-1"))

    first_session = asyncio.run(server._handshake(first))
    with caplog.at_level(logging.WARNING, logger="ws_server"):
        second_session = asyncio.run(server._handshake(second))

    assert first_session is not None and second_session is not None
    assert second_session.control_ws is second
    current = asyncio.run(server.registry.get("dot-1"))
    assert current is second_session  # Ersetzen (dokumentierte Re-Connect-Semantik)
    assert any("Doppel-Device-ID" in record.message for record in caplog.records)
    # Kein Verhaltens-Umbau: beide wurden ordentlich ge-ack't (genau 2 Keys).
    assert first.sent[0] == {"type": "ack", "device_id": "dot-1"}
    assert second.sent[0] == {"type": "ack", "device_id": "dot-1"}


def test_reconnect_nach_toter_session_warnt_nicht(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Normales Re-Connect (alte Session tot/entfernt) ist KEIN Doppel-Fall."""
    server = WsServer(pipeline=None, keepalive_interval=0)
    first = FakeControlSocket(_register_json("dot-1"))
    session = asyncio.run(server._handshake(first))
    assert session is not None
    asyncio.run(server.registry.mark_dead("dot-1", reason="test"))
    with caplog.at_level(logging.WARNING, logger="ws_server"):
        again = FakeControlSocket(_register_json("dot-1"))
        assert asyncio.run(server._handshake(again)) is not None
    assert not [record for record in caplog.records if "Doppel-Device-ID" in record.message]


# ── Pipeline.mic_synced (additiver Lese-Zugriff) ─────────────────────────
class _StubWake:
    def process(self, _data: bytes) -> list[Any]:
        return []


def test_mic_synced_vor_erstem_frame_none_und_false() -> None:
    pipeline = Pipeline(wake_detector=_StubWake())
    # Unbekanntes Gerät ⇒ None (nie gesehen, nie angelegt).
    assert pipeline.mic_synced("niemand") is None
    # Erstes Mic-Frame (seq=0) ⇒ Tracker synchron.
    chunk = bytes(2560)
    asyncio.run(pipeline.on_mic_frame("dot-1", 0, chunk))
    assert pipeline.mic_synced("dot-1") is True


# ── Wizard-Bausteine (Summary-Texte, rein) ───────────────────────────────
def test_wizard_pairing_summary_alle_ampeln() -> None:
    from deploy.onboarding.wizard import pairing_summary_lines

    paired = pairing_summary_lines(
        {"status": "paired", "devices": [{"device_id": "TEST-FAKE-1", "mic_synced": True}]},
        None,
    )
    assert any("TEST-FAKE-1" in line for line in paired)

    waiting = pairing_summary_lines({"status": "waiting", "devices": []}, None)
    assert any("warte auf deinen Dot" in line for line in waiting)
    assert any("INSTALL.md" in line for line in waiting)

    off = pairing_summary_lines({"status": "mdns_off", "devices": []}, None)
    assert any("MANAGER_MDNS_ENABLED" in line for line in off)

    broken = pairing_summary_lines(None, "timeout")
    assert any("nicht abfragbar" in line for line in broken)


def test_wizard_fetch_pairing_nie_wirft(tmp_path: Any) -> None:
    """Netzfehler/Timeout ⇒ (None, fehler) statt Exception (Summary bleibt stehen)."""
    from deploy.onboarding import wizard

    payload, error = wizard.fetch_pairing(port="1")  # Port 1: sofort leer
    assert payload is None
    assert error is not None


def test_wizard_run_summary_zeigt_pairing_wartezustand(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Full-Up-Pfad (injiziert): die Abschluss-Summary zeigt die Pairing-Ampel."""
    import subprocess as sp
    from deploy.onboarding import wizard

    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    monkeypatch.setattr(wizard, "lan_ip_suggestion", lambda: "10.0.0.7")
    monkeypatch.setattr(wizard, "run_compose",
                        lambda dest, *a, capture=False: sp.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(wizard, "wait_healthy", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "manager_health", lambda *a, **k: (True, "/health 200"))
    monkeypatch.setattr(
        wizard, "fetch_pairing",
        lambda *a, **k: ({"status": "waiting", "devices": []}, None),
    )
    answers = tmp_path / "answers.env"
    answers.write_text(
        "TOPOLOGY=single\n"
        "HA_BASE_URL=http://10.0.0.5:8123\n"
        "HA_TOKEN=FAKE-HA-TOKEN\n"
        "LLM_API_KEY=FAKE-LLM-KEY-0123456789abcdef\n"
        "MANAGER_MDNS_ENABLED=true\n"
    )
    rc = wizard.main(["--dest", str(tmp_path / "dest"),
                      "--non-interactive", "--answers-file", str(answers)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "warte auf deinen Dot" in out
    assert "INSTALL.md" in out


def test_fake_dot_module_für_lxc_beleg_importierbar() -> None:
    """Der LXC-Beleg nutzt exakt diesen Fake — Import-Vertrag bleibt intakt."""
    dot = FakeEchoDot(base_uri="ws://127.0.0.1:1", device_id="TEST-FAKE-1")
    assert dot.device_id == "TEST-FAKE-1"
