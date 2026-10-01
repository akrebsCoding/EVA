"""Entrypoint des wyoming-managers (P5.T4, `PLAN.md:475`).

Nur **App-Verdrahtung** – es wird keine Protokoll-, Pipeline- oder Client-Logik
dupliziert.  Die Bausteine aus P1–P5 werden hier zusammengesteckt:

* `app.config.settings` – Host/Port/mDNS/Test-Hooks (PLAN §4).
* `app.logger.configure_logging` – idempotenter Logging-Einstieg (P1.T1).
* `app.ha_client.HomeAssistantClient` – HA-REST-Client (P4.T0).
* `app.mdns.MdnsAnnouncer` – `_emcontroller`-Announcement (P5.T3).
* `app.pipeline.Pipeline` – Zustandsmaschine (P5.T0), bekommt die ausgehenden
  Transporte des Servers injiziert.
* `app.ws_server.WsServer` – `/control`, `/data`, `/shell/{device_id}` (P5.T1/T2).

`lifespan`-Reihenfolge (Auftrag): **`configure_logging()` → In-Memory-Logpuffer
fürs Dashboard (P8.D3) → HA-Client starten → LLM-Clients starten (R1-Fix) →
mDNS announcen → (Server läuft)**.  Beim Shutdown wird in umgekehrter
Reihenfolge freigegeben: **In-Memory-Logpuffer entfernen → mDNS abmelden →
offene WS-Sessions schließen → LLM-Clients stoppen → HA-Client stoppen**.
Alle Schritte sind idempotent; es bleiben keine Tasks hängen (der HA-Refresh-Loop
wird gecancelt+awaited, WS-Keepalive-Tasks enden über `WsServer.mark_dead`, der
mDNS-Refresh-Thread ist ein Daemon und wird gestoppt).

Der Logpuffer (`dashboard.MemoryLogBuffer`) hängt am ``manager``-Logger
(`app.logger.LOGGER_NAMESPACE`) und ist auf `dashboard.LOG_BUFFER_MAXLEN`
Records begrenzt – ohne ihn liefert `/api/logs` live `source: "none"`, weil der
Container nur `stdout` sammelt. Er wird **zuerst** wieder entfernt, damit er
auch dann nicht am Logger hängen bleibt, wenn ein späterer Shutdown-Schritt
einmal wirft (Handler-Leck bei Reload/Neustart).

**`/health`** liefert immer **HTTP 200** mit einem kleinen Status-Body.  Der
Compose-Healthcheck (P1.T3) prüft genau diesen Pfad; mit P5.T4 wird er
**verschärft** (E33): ein fehlender Listener und ein `404` auf `/health` sind
jetzt **echte Fehler** statt „pending", weil Route und WS-Server existieren.

**Conditionaler Test-Hook-Router `/internal/*`** – nur registriert, wenn
`settings.enable_test_hooks` (`ENABLE_TEST_HOOKS=true`) gilt; sonst existiert
der Pfad nicht (**404**).  Grundlage für L5a (PLAN §7/P9.T7).  Die Hooks rufen
ausschließlich die ohnehin vorhandenen Pipeline-Handler auf und kodieren selbst
**kein** Protokoll.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Final, Optional

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, dashboard
from app.config import settings
from app.device_session import ConnectSequence
from app.ha_client import HomeAssistantClient
from app.logger import configure_logging, get_logger
from app.mdns import MdnsAnnouncer
from app.pipeline import BUTTON_CLICK_TYPE, Pipeline
from app.protocol import serialize_ack
from app.router import Router
from app.ws_server import WsServer

_LOG: Final = get_logger("main")

#: Pfad des Healthchecks (P1.T3-Compose prüft genau diesen Pfad).
HEALTH_PATH: Final[str] = "/health"
#: Test-Hook-Präfix (nur bei `ENABLE_TEST_HOOKS=true` geroutet; P5.T4/P9.T7).
INTERNAL_PREFIX: Final[str] = "/internal"
#: Anzeigename in Antworten/Logs.
SERVICE_NAME: Final[str] = "wyoming-manager"

# ── Dashboard (P8.D2) ───────────────────────────────────────────────────
#: Wurzel der mitgelieferten Frontend-Dateien.  Bewusst relativ zu **dieser**
#: Datei und `resolve()`d – nicht relativ zum Arbeitsverzeichnis, nicht aus
#: `PROJECT_ROOT` geraten: das Dockerfile kopiert `app/` nach `/app/app/`
#: (`COPY --chown=appuser:app app/ /app/app/`), deshalb liegt das Verzeichnis
#: neben `main.py` im Image.
STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"
#: LAN-Pfad der Dashboard-Seite (der Nutzer ruft
#: `http://10.0.0.10:8767/dashboard` auf).  Die API darunter hängt an
#: `/api` (P8.D1), die Assets unter `/static`.
DASHBOARD_PATH: Final[str] = "/dashboard"
#: Fester Dateiname der Seite – **kein** Nutzer-`path`-Parameter, damit die
#: Route gar keinen Traversal-Pfad haben kann (`STATIC_DIR` ist zudem fix).
DASHBOARD_HTML: Final[Path] = STATIC_DIR / "dashboard" / "index.html"


def _build_internal_router(pipeline: Pipeline, ws_server: WsServer) -> APIRouter:
    """Minimaler `/internal/*`-Router für L5a – **nur** Testbetrieb.

    Die Endpunkte sind reine Weiterleitungen auf die bestehenden Pipeline-Handler
    bzw. den Session-Registry-Zustand; sie kodieren nichts selbst.  Registriert
    wird der Router ausschließlich bei `settings.enable_test_hooks == True`.
    """
    router = APIRouter(prefix=INTERNAL_PREFIX, tags=["internal"])

    @router.get("/status")
    async def internal_status() -> dict[str, Any]:
        ids = list(await ws_server.registry.ids())
        return {
            "status": "ok",
            "sessions": ids,
            "states": {device_id: pipeline.state_of(device_id).value for device_id in ids},
        }

    @router.post("/wake")
    async def internal_wake(device_id: str, score: float) -> dict[str, Any]:
        accepted = await pipeline.on_wake_detected(device_id, score)
        return {"ok": True, "accepted": bool(accepted)}

    @router.post("/vad_end")
    async def internal_vad_end(device_id: str) -> dict[str, Any]:
        ended = await pipeline.on_vad_end(device_id)
        return {"ok": True, "ended": bool(ended)}

    @router.post("/no_speech")
    async def internal_no_speech(device_id: str) -> dict[str, Any]:
        silenced = await pipeline.on_no_speech(device_id)
        return {"ok": True, "silenced": bool(silenced)}

    @router.post("/button")
    async def internal_button(
        device_id: str,
        click_type: int = BUTTON_CLICK_TYPE,
        held_ms: Optional[int] = None,
        muted: bool = False,
    ) -> dict[str, Any]:
        handled = await pipeline.on_button(
            device_id, click_type, held_ms=held_ms, muted=muted
        )
        return {"ok": True, "handled": bool(handled)}

    return router


def create_app() -> FastAPI:
    """Die FastAPI-App mit Lifespan, Healthcheck, WS-Routen und Test-Hooks bauen."""
    # Ausgehende Transporte der Pipeline an den Server binden.  Der Server
    # braucht die Pipeline und die Pipeline den Server – die Auflösung läuft
    # lazy über einen Halter, damit kein Import-Zyklus entsteht.
    server_holder: dict[str, WsServer] = {}

    async def _send_control(device_id: str, message: Any) -> bool:
        server = server_holder.get("server")
        if server is None:
            return False
        return await server.send_control(device_id, message)

    async def _send_binary(device_id: str, data: bytes) -> bool:
        server = server_holder.get("server")
        if server is None:
            return False
        return await server.send_binary(device_id, data)

    ha_client = HomeAssistantClient()
    # R1-Fix (P9.T4-Nachtrag): Die LLM-Clients (`JevClient`/`DeepSeekClient`)
    # werden hier in der Kompositionswurzel aufgebaut und dem Pipeline explizit
    # injiziert, damit der Lifespan sie – genau wie den HA-Client – starten und
    # beim Shutdown stoppen kann.  Der `Router` baut die Clients lazy (keine
    # I/O), `start()` initialisiert erst den httpx-AsyncClient + Bearer
    # (`JEV_MODE=off` ist kein Fehler, nur Client-Aufbau).
    router = Router(ha_client=ha_client)
    pipeline = Pipeline(
        ha_client=ha_client,
        router=router,
        send_control=_send_control,
        send_binary=_send_binary,
        settings_obj=settings,
    )
    ws_server = WsServer(pipeline=pipeline, settings_obj=settings)
    server_holder["server"] = ws_server

    # Connect-Sequenz (P5.T5) an den `register`-Pfad binden (E77-Fix):
    # nach `register` → `ack` spielt pro Gerät **dieselbe** `ConnectSequence`
    # `config` (K4) → `listeningAnim` (E11) → `mic_start` (K3) aus.  Die
    # ausgehenden Transporte sind die bereits gebundenen Server-Methoden; der
    # Button-Pfad (K6) bleibt unverändert über `Pipeline.on_button` (P5.T2).
    connect_sequences: dict[str, ConnectSequence] = {}

    #: P8.D3: der aktuell gehängte In-Memory-Logpuffer.  Halter statt
    #: `nonlocal`, damit Start (`lifespan`) und Shutdown (`_shutdown`) dieselbe
    #: Instanz sehen – `attach_memory_log_buffer` ist idempotent, es kann also
    #: nie mehr als **eine** Instanz am `manager`-Logger geben.
    log_buffer: dict[str, dashboard.MemoryLogBuffer] = {}

    async def _connect_device(session: Any) -> None:
        sequence = ConnectSequence(
            session.device_id,
            send_control=_send_control,
            send_binary=_send_binary,
            settings_obj=settings,
        )
        connect_sequences[session.device_id] = sequence
        await sequence.on_ack(serialize_ack(session.device_id))

    ws_server.connect_handler = _connect_device

    mdns = MdnsAnnouncer(enabled=settings.manager_mdns_enabled)

    async def _close_sessions() -> int:
        """Alle offenen Device-Sessions schließen (Cancelt laufende Turns).

        Idempotent: `mark_dead` ist ein No-op, wenn die Session schon weg ist.
        """
        ids = await ws_server.registry.ids()
        for device_id in ids:
            try:
                await ws_server.mark_dead(device_id, reason="shutdown")
            except Exception as exc:  # noqa: BLE001 – Shutdown ist best-effort.
                _LOG.warning("Session %s beim Shutdown nicht geschlossen: %s", device_id, exc)
        await ws_server.registry.clear()
        return len(ids)

    async def _shutdown() -> None:
        """Logpufer weg → mDNS abmelden → WS-Sessions → LLM-Clients → HA-Client."""
        # P8.D3: **zuerst** der In-Memory-Logpuffer. `removeHandler` + `close()`
        # sind Pflicht (sonst Handler-Leck bei Reload/Neustart) und passieren
        # vor jedem Schritt, der noch loggen oder werfen könnte.
        dashboard.detach_memory_log_buffer(log_buffer.get("handler"))
        log_buffer.clear()
        # E76: Zeroconf ist synchron und würde den Event-Loop blockieren ⇒ Thread.
        await asyncio.to_thread(mdns.stop)
        closed = await _close_sessions()
        # R1-Fix: die in der Lifespan gestarteten LLM-Clients idempotent schließen.
        await router.deepseek_client.stop()
        await router.jev_client.stop()
        await ha_client.stop()
        _LOG.info("wyoming-manager gestoppt (WS-Sessions geschlossen: %d)", closed)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # (1) Logging zuerst (idempotent; P1.T1).
        configure_logging()
        # (1b) P8.D3: In-Memory-Logpuffer für `/api/logs` an den `manager`-Logger
        # hängen (dieselbe Musterform wie die Clients unten: Start hier, Weg
        # im Shutdown).  Ohne ihn wäre `source: "none"` – der Container sammelt
        # nur `stdout`.  `attach_memory_log_buffer` ist **idempotent** (doppelter
        # Lifespan ⇒ kein doppelter Eintrag) und `maxlen` ist ein harter Cap.
        # **Vor** der ersten eigenen Logzeile: sonst fehlte gerade die
        # Startzeile (und ein Fehler beim Start) im Dashboard.
        log_buffer["handler"] = dashboard.attach_memory_log_buffer()
        _LOG.info(
            "wyoming-manager startet (host=%s, port=%d, mDNS=%s, Test-Hooks=%s)",
            settings.manager_host,
            settings.manager_port,
            settings.manager_mdns_enabled,
            settings.enable_test_hooks,
        )
        # (2) HA-Client (fehlende HA-Instanz blockiert den Start nicht, P4.T0).
        await ha_client.start()
        # (2b) LLM-Clients starten (R1-Fix, E84/P9.T4-Nachtrag): erst `start()`
        # baut den httpx-AsyncClient + Bearer, sonst wirft `_post_json`
        # „Client nicht gestartet".  `JEV_MODE=off` schlägt hier nicht fehl.
        await router.jev_client.start()
        await router.deepseek_client.start()
        _LOG.info("LLM-Clients gestartet (Jev-Modus=%s)", router.jev_mode)
        # (3) mDNS (`start()` wirft nie – kein mDNS ⇒ WARN, Manager läuft weiter).
        # E76: die synchrone Zeroconf-API im Worker-Thread starten, sonst
        # blockiert `register_service` den Event-Loop (EventLoopBlocked).
        announced = await asyncio.to_thread(mdns.start)
        _LOG.info("mDNS-Announcement: %s", "aktiv" if announced else "inaktiv")
        try:
            yield
        finally:
            await _shutdown()

    application = FastAPI(
        title=SERVICE_NAME,
        version=__version__,
        lifespan=lifespan,
    )

    # WS-Planes aus P5.T1/T2: /control, /data, /shell/{device_id}.
    application.include_router(ws_server.router)

    # ── Dashboard (P8.D2) ────────────────────────────────────────────────
    # Die **rein lesende** API aus P8.D1 hängt unter `/api`; das Frontend ist
    # statisch (kein Build-Step, kein CDN) und pollt ausschließlich diese
    # GET-Routen.  Es existiert hier bewusst **keine** Schreib-Route: ein
    # POST auf `/api/...` ist 405 (Assertion in `tests/test_dashboard_ui.py`).
    application.include_router(dashboard.router)
    # `StaticFiles` ist traversalsicher (jeder Pfad wird gegen das Wurzel-
    # verzeichnis normalisiert) und liefert die drei Assets des Dashboards.
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get(DASHBOARD_PATH, include_in_schema=False)
    @application.get(f"{DASHBOARD_PATH}/", include_in_schema=False)
    async def dashboard_page() -> FileResponse:
        """Die Dashboard-Seite – identisch für `/dashboard` und `/dashboard/`."""
        return FileResponse(DASHBOARD_HTML)

    @application.get(HEALTH_PATH)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": __version__,
            "mdns": mdns.is_running,
            "devices": await ws_server.registry.size(),
        }

    # E33/Test-Hooks: NUR registrieren, wenn ausdrücklich freigeschaltet.
    if settings.enable_test_hooks:
        application.include_router(_build_internal_router(pipeline, ws_server))
        _LOG.warning(
            "ENABLE_TEST_HOOKS=true – /internal/* ist geroutet (NUR Testbetrieb!)"
        )

    # Komponenten für Tests/Diagnose bereitstellen (reine Verdrahtung).
    application.state.settings = settings
    application.state.ha_client = ha_client
    application.state.pipeline = pipeline
    application.state.ws_server = ws_server
    application.state.connect_sequences = connect_sequences
    application.state.mdns = mdns
    return application


#: Modulweiter App-Singleton für `uvicorn app.main:app` und `python -m app.main`.
app: Final[FastAPI] = create_app()


def main() -> None:
    """Startpfad: Secrets erzwingen, dann uvicorn mit Config-Host/-Port starten.

    Host und Port kommen **ausschließlich** aus `app.config` (keine hart
    verdrahteten uvicorn-Argumente); die WS-Level-Ping-Werte folgen E61 aus der
    Config (der App-Ebene-Ping bleibt in `app.ws_server`).

    SIGTERM ⇒ rc=0: uvicorn 0.54 führt nach dem sauberen Herunterfahren den
    empfangenen Signal-`raise` erneut aus (`Server.capture_signals`); ohne
    Gegenmaßnahme endet der Prozess mit `-15`/143.  Wir setzen den
    SIGTERM-Handler so, dass dieses erneute Auslösen ein No-op ist – der
    Lifespan-Shutdown (mDNS/HA/WS-Sessions) bleibt unverändert und der Prozess
    beendet sich regulär mit **0** (Docker-STOPSIGNAL).
    """
    settings.require_secrets()
    configure_logging()

    import signal

    import uvicorn

    def _noop_on_terminate(signum: int, frame: object) -> None:
        # Nur für den von uvicorn nach dem Shutdown erneut ausgelösten
        # SIGTERM gedacht; der eigentliche Graceful-Stop läuft über uvicorn.
        return

    previous = signal.signal(signal.SIGTERM, _noop_on_terminate)
    try:
        uvicorn.run(
            app,
            host=settings.manager_host,
            port=settings.manager_port,
            ws_ping_interval=settings.ws_ping_interval,
            ws_ping_timeout=settings.ws_ping_timeout,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":  # pragma: no cover – echter Startpfad.
    main()
