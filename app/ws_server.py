"""WebSocket-Server **Teil 1+2** (P5.T1/T2) – Device-Sessions, Registry, `/control`,
`/data`, `/shell/{device_id}`.

Reine Server-Logik: Die Pipeline (Zustandsmaschine, P5.T0) wird **injiziert**, es
gibt **keinen** Import-Zyklus (`app.pipeline` wird zur Laufzeit nicht importiert).
Der einzige Netz-nahe Teil sind die WebSocket-Endpunkte selbst.

Umgesetzte Festlegungen (jede Zahl belegt):

* **K1 / E27** – Der `register`-Handshake antwortet mit **`ack` ohne `features`**:
  wörtlich `{"type": "ack", "device_id": …}`, genau **2 Keys**
  (`app.protocol.serialize_ack`). Capabilities kommen **vom Gerät** und werden nur
  gespeichert, **nie** gespiegelt.
* **P5.T1 / PLAN.md:472** – `DeviceSession` führt `device_id`, `capabilities`,
  `control_ws`, `data_ws`, `shell_ws`, `connected_at` (+ Keepalive-Task, Ping-Zähler
  und Pong-Event); `SessionRegistry` legt an/entfernt/liefert **task-safe** über
  einen `asyncio.Lock`.
* **Keepalive (E61)** – ein App-Ebene-`ping` (`serialize_ping`) alle
  `settings.ws_ping_interval` = **20 s**; fehlt die Antwort (`pong`) länger als
  `settings.ws_ping_timeout` = **30 s**, wird die Session per `mark_dead` verworfen
  und der Socket mit **Close 1011** geschlossen. Werte kommen aus `app/config.py`
  und sind für Tests konstruktoral überschreibbar, **nicht** hart verdrahtet.
* **E27-Abgleich** – die Referenz nutzt auf **WS-Ebene** 20 s/10 s (⇒ Close 1011)
  **und zusätzlich** einen App-JSON-`ping` alle **5 s** (der **nie** schließt).
  EVA hat nur **einen** Keepalive-Pfad mit den §4-Config-Werten 20/30 (kein
  5-s-/10-s-Key existiert ⇒ keine erfundene Konstante); Details/Begründung in
  STATE §4/E61.
* **E60** – die Pipeline-Handler sind Koroutinen; `mark_dead` awaited
  `pipeline.on_device_gone(device_id)`. Die Transporte sind der injizierte
  `control_handler` und die öffentlichen Attribute der Session.

**Teil 2 (P5.T2, `PLAN.md:473`) – baut auf Teil 1 auf, dessen Interface bleibt
unverändert (gleiche Signaturen von `WsServer.__init__`, `DeviceSession`,
`SessionRegistry`):**

* **`/data`** (Spec §1/§7) – permanenter Binär-Stream: erste Nachricht ist JSON
  `{"type":"identify","device_id":…}` (wörtlich die Referenz
  `em_controller.py:3991-4017`); danach wird **jedes** Binärframe per
  `parse_data_frame(raw, Direction.DEVICE_TO_CONTROLLER)` geparst. Jedes `0x01`-
  **Mic-Frame** geht **genau einmal** an `pipeline.on_mic_frame(device_id, seq,
  pcm)` (Dauer-Stream, **kein** Frame-Verlust, E5); `0x04`/`0x05` (4-Byte-Sentinel,
  E28) an `on_vad_end`/`on_no_speech`. Kein eigenes VAD.
* **`/shell/{device_id}`** – **roher Binär-Proxy**: Bytes werden **unverändert**
  an den injizierbaren `shell_handler(session, data)` weitergereicht, **keine**
  Interpretation; `session.shell_ws` wird gesetzt.
* **Control-Handler** – `button` (K6/E29): `clickType == 138` **und** `down:false`
  ⇒ `pipeline.on_button(device_id, 138, held_ms=…, muted=…)`; `down:true` wird
  **ignoriert**, `heldMs`/`muted` geparst. Die übrigen Typen (`mic_stop`/`leds`/
  `led_anim`/`speaker_flush`) sind **ausgehende** C→D-Nachrichten und werden
  eingehend protokollgerecht **ignoriert** (Spec §2) – sendbar über
  :meth:`WsServer.send_control`.
* **Ausgehende `mic_start`** – :meth:`WsServer.send_mic_start` (Sender/Fähigkeit;
  die Connect-Sequenz selbst ist P5.T5). Transport generisch:
  :meth:`WsServer.send_control` (JSON `/control`) und :meth:`WsServer.send_binary`
  (`/data`).
* **E27** – `0x06` (BLE) und `0x07` (Session-Audio) existieren nicht und werden
  **nie** gesendet.

Nicht Teil von P5.T2 (folgt in P5.T3/T4/T5): mDNS, Entrypoint, Connect-Sequenz.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Mapping, Optional, TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.logger import get_logger
from app.protocol import (
    CTRL_BUTTON,
    CTRL_PONG,
    CTRL_REGISTER,
    Direction,
    MicFrame,
    MicSentinel,
    ProtocolError,
    SentinelKind,
    SequenceCounter,
    parse_control_message,
    parse_data_frame,
    serialize_ack,
    serialize_mic_start,
    serialize_ping,
)

if TYPE_CHECKING:  # pragma: no cover – nur für die Typprüfung, kein Laufzeit-Import
    from app.pipeline import Pipeline

__all__ = [
    "CONTROL_PATH",
    "DATA_PATH",
    "SHELL_PATH",
    "KEEPALIVE_CLOSE_CODE",
    "WsServerError",
    "DeviceSession",
    "SessionRegistry",
    "WsServer",
    "ControlHandler",
    "ShellHandler",
    "ConnectHandler",
]

_LOG: Final = get_logger("ws_server")

#: Pfad des Control-Planes (`PLAN.md` §3: `/control` JSON).
CONTROL_PATH: Final[str] = "/control"
#: Pfad des Daten-Planes (`PLAN.md` §3: `/data`, binär; Spec §1).
DATA_PATH: Final[str] = "/data"
#: Pfad des Shell-Planes (`PLAN.md` §3: `/shell/{device_id}`; Spec §1).
SHELL_PATH: Final[str] = "/shell/{device_id}"
#: E27: WS-Level-Keepalive scheitert ⇒ Close 1011 (`try again later`).
KEEPALIVE_CLOSE_CODE: Final[int] = 1011
#: Close-Code für einen Protokollverstoß im Handshake.
POLICY_CLOSE_CODE: Final[int] = 1008

# ── `/data`-Handshake (Spec §1, Referenz `em_controller.py:3991-4017`) ─────
#: Erste `/data`-Nachricht ist JSON (kein erfundener Wert, Spec §1 Tabelle).
DATA_IDENTIFY_TYPE: Final[str] = "identify"
#: Erste Nachricht muss binnen 10 s eintreffen (`em_controller.py:3991`).
IDENTIFY_TIMEOUT_SECONDS: Final[float] = 10.0
#: `/data` wartet bis zu 20×0,1 s auf das registrierende `/control`
#: (`em_controller.py:4007-4011`, Spec §1) – **keine** erfundene Zahl.
DATA_IDENTIFY_RETRIES: Final[int] = 20
DATA_IDENTIFY_INTERVAL: Final[float] = 0.1
#: E29/K6: nur dieser `clickType` erreicht den Controller (deckenungsgleich mit
#: `app.pipeline.BUTTON_CLICK_TYPE`; hier bewusst als Literal, um keinen
#: Pipeline-Import in den Server zu ziehen).
BUTTON_CLICK_TYPE: Final[int] = 138
#: E29: geräteseitige Hold-Schwelle – nur Diagnose, K6 bleibt unverändert.
HOLD_MS_THRESHOLD: Final[int] = 750

#: `async (session, message) -> None` – von P5.T2 gelieferter Control-Handler.
ControlHandler = Callable[["DeviceSession", Mapping[str, Any]], Awaitable[None]]
#: `async (session, data) -> None` – roher `/shell`-Binär-Senke (P5.T2).
ShellHandler = Callable[["DeviceSession", bytes], Awaitable[None]]
#: `async (session) -> None` – nach `register`/`ack` die Connect-Sequenz
#: (`app/device_session.ConnectSequence`, P5.T5).  Additiv injiziert; fehlt der
#: Handler, bleibt es beim reinen `ack` (P5.T1-Verhalten, E77-Fix).
ConnectHandler = Callable[["DeviceSession"], Awaitable[None]]


class WsServerError(ValueError):
    """Fehler im WebSocket-Server (definiert statt still zu degradieren)."""


def _normalize_capabilities(raw: Any) -> tuple[str, ...]:
    """Capabilities tolerant in ein `tuple[str, ...]` überführen.

    Der Dot sendet im `register` eine Liste; Strings werden kommagetrennt
    akzeptiert. Alles andere (auch `None`) ⇒ leeres Tuple. Es wird **nichts**
    erfunden und **nichts** an das Gerät zurückgespiegelt (K1).
    """
    if raw is None:
        return ()
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode("utf-8", "replace")
    if isinstance(raw, str):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(part) for part in raw if str(part))
    return ()


# ── Session ───────────────────────────────────────────────────────────────
@dataclass
class DeviceSession:
    """Zustand **eines** verbundenen Geräts (P5.T1).

    Die ersten sechs Felder sind wörtlich `PLAN.md:472`; der Rest ist das
    Betriebs-Inventar (Keepalive/Pong, Dead-Flag). P5.T2 setzt `data_ws` und
    `shell_ws`, sobald es die zugehörigen Endpunkte implementiert.
    """

    device_id: str
    capabilities: tuple[str, ...] = ()
    control_ws: Any = None
    data_ws: Any = None
    shell_ws: Any = None
    connected_at: float = field(default_factory=time.monotonic)

    #: Fortlaufende App-Ping-ID (`serialize_ping`), uint16-Wrap aus protocol.py.
    ping_sequence: SequenceCounter = field(default_factory=SequenceCounter)
    #: Wird vom `/control`-Lesepfad gesetzt, sobald ein `pong` eintrifft.
    pong_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    #: Der Keepalive-Task dieser Session (oder `None`).
    keepalive_task: Optional[asyncio.Task[None]] = field(default=None, repr=False)
    #: True, sobald `mark_dead`/Teardown die Session verworfen hat.
    dead: bool = False

    def note_pong(self, message: Mapping[str, Any]) -> None:
        """`pong` aus dem `/control`-Lesepfad entgegennehmen.

        Die ID wird **nicht** erzwungen (nur ein Ping gleichzeitig offen); ein
        `pong` ohne/mit falscher `id` gilt trotzdem als Lebenszeichen.
        """
        _LOG.debug("pong von %s (id=%s)", self.device_id, message.get("id"))
        self.pong_event.set()

    async def stop_keepalive(self) -> None:
        """Keepalive-Task beenden – idempotent und **self-safe**.

        Wird aus dem Keepalive-Task selbst aufgerufen (Timeout), wird er nicht
        gegen sich selbst gecancelt, sondern kehrt nur zurück.
        """
        task = self.keepalive_task
        self.keepalive_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("Keepalive-Stopp %s: %s", self.device_id, exc)


# ── Registry ──────────────────────────────────────────────────────────────
class SessionRegistry:
    """`device_id` → `DeviceSession`, **task-safe** über einen `asyncio.Lock`.

    Alle Zugriffe (anlegen, entnehmen, abrufen, auflisten, `mark_dead`) laufen
    unter dem Lock, weil Mic-Lesen, Keepalive und Disconnect in verschiedenen
    Tasks gleichzeitig auf die Registry zugreifen können.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """Der schützende Lock (Diagnose/Tests)."""
        return self._lock

    async def register(self, session: DeviceSession) -> DeviceSession:
        """Session eintragen; ein erneutes `register` desselben Geräts ersetzt sie.

        Liefert die nun **aktive** Session zurück. Die alte Session (falls
        vorhanden) wird nicht stillschweigend weiterbetrieben, sondern vom
        Aufrufer über den Rückgabewert gepflegt; P5.T5 (Connect-Sequenz) regelt
        den sauberen Re-Connect.
        """
        if not isinstance(session, DeviceSession):
            raise WsServerError("register erwartet eine DeviceSession")
        if not session.device_id:
            raise WsServerError("Session ohne device_id")
        async with self._lock:
            self._sessions[session.device_id] = session
        return session

    async def get(self, device_id: str) -> Optional[DeviceSession]:
        """Session zu `device_id` oder `None` (unter dem Lock)."""
        async with self._lock:
            return self._sessions.get(device_id)

    async def contains(self, device_id: str) -> bool:
        """True, wenn eine Session mit dieser ID existiert."""
        async with self._lock:
            return device_id in self._sessions

    async def remove(self, device_id: str) -> Optional[DeviceSession]:
        """Session entnehmen (idempotent – unbekannte ID ⇒ `None`)."""
        async with self._lock:
            return self._sessions.pop(device_id, None)

    async def mark_dead(
        self, device_id: str, *, reason: Optional[str] = None
    ) -> Optional[DeviceSession]:
        """Session als tot entnehmen; das Dead-Flag setzt der Aufrufer.

        `reason` ist rein diagnostisch und wird nur geloggt.
        """
        async with self._lock:
            session = self._sessions.pop(device_id, None)
        if session is not None:
            session.dead = True
            _LOG.info("Session %s als tot markiert (%s)", device_id, reason or "?")
        return session

    async def ids(self) -> tuple[str, ...]:
        """Alle aktuell registrierten `device_id`s (Reihenfolge = Einfügung)."""
        async with self._lock:
            return tuple(self._sessions)

    async def snapshot(self) -> dict[str, DeviceSession]:
        """Flache Kopie der Registry (Diagnose/Tests)."""
        async with self._lock:
            return dict(self._sessions)

    async def size(self) -> int:
        """Anzahl registrierter Sessions."""
        async with self._lock:
            return len(self._sessions)

    async def clear(self) -> None:
        """Alle Sessions entnehmen (Shutdown-Hilfe, P5.T4)."""
        async with self._lock:
            self._sessions.clear()


# ── Server ────────────────────────────────────────────────────────────────
class WsServer:
    """FastAPI-WebSocket-Server **Teil 1+2** (P5.T1/T2).

    Der Server ist die Brücke zwischen den WS-Planes und der injizierten
    `Pipeline`. Teil 2 ergänzt `/data` und `/shell/{device_id}` über
    :attr:`router`, reicht die JSON-Control-Nachrichten an die Pipeline und
    stellt die ausgehenden Transporte (`send_control`/`send_binary`/
    `send_mic_start`) bereit. **Das P5.T1-Interface wird dabei nicht geändert**
    (nur additive Methoden/Attribute; `__init__`-Signatur unverändert).
    """

    def __init__(
        self,
        pipeline: Optional["Pipeline"] = None,
        *,
        registry: Optional[SessionRegistry] = None,
        control_handler: Optional[ControlHandler] = None,
        settings_obj: Any = None,
        keepalive_interval: Optional[float] = None,
        keepalive_timeout: Optional[float] = None,
    ) -> None:
        self._pipeline = pipeline
        self._settings = settings_obj if settings_obj is not None else settings
        self.registry = registry if registry is not None else SessionRegistry()
        self.control_handler = control_handler
        #: Keepalive-Werte aus der Config; konstruktoral überschreibbar (Tests).
        self.keepalive_interval = float(
            self._settings.ws_ping_interval
            if keepalive_interval is None
            else keepalive_interval
        )
        self.keepalive_timeout = float(
            self._settings.ws_ping_timeout
            if keepalive_timeout is None
            else keepalive_timeout
        )
        #: Rohe `/shell`-Binär-Senke (P5.T2) – additiv, kein neuer
        #: `__init__`-Parameter (P5.T1-Interface unverändert).
        self.shell_handler: Optional[ShellHandler] = None
        #: Connect-Sequenz-Senke (P5.T5) – additiv wie `shell_handler`, **kein**
        #: neuer `__init__`-Parameter (P5.T1-Interface unverändert).  Wird nach
        #: `register`/`ack` mit der frischen Session aufgerufen.
        self.connect_handler: Optional[ConnectHandler] = None
        self.router: APIRouter = APIRouter()
        self._build_routes()

    # ── Eigenschaften (für P5.T2/T4) ──────────────────────────────────
    @property
    def pipeline(self) -> Optional["Pipeline"]:
        """Die injizierte Pipeline (oder `None`)."""
        return self._pipeline

    @property
    def settings(self) -> Any:
        """Die wirksame Settings-Instanz."""
        return self._settings

    # ── Routen ────────────────────────────────────────────────────────
    def _build_routes(self) -> None:
        @self.router.websocket(CONTROL_PATH)
        async def _control_endpoint(websocket: WebSocket) -> None:  # pragma: no cover
            await self.handle_control(websocket)

        @self.router.websocket(DATA_PATH)
        async def _data_endpoint(websocket: WebSocket) -> None:  # pragma: no cover
            await self.handle_data(websocket)

        @self.router.websocket("/shell/{device_id}")
        async def _shell_endpoint(  # pragma: no cover
            websocket: WebSocket, device_id: str
        ) -> None:
            await self.handle_shell(websocket, device_id)

    # ── `/control` ────────────────────────────────────────────────────
    async def handle_control(self, websocket: WebSocket) -> None:
        """`/control`-Endpunkt: `register` → `ack` (ohne `features`) → Keepalive.

        Danach werden `pong` und Duplikate von `register` intern behandelt; jede
        andere Nachricht geht an den injizierten :attr:`control_handler`
        (P5.T2). Beim Verlassen wird die Session **genau einmal** aufgeräumt.
        """
        await websocket.accept()
        session: Optional[DeviceSession] = None
        try:
            session = await self._handshake(websocket)
            if session is None:
                return
            await self._start_connect_sequence(session)
            self._start_keepalive(session)
            while not session.dead:
                raw = await websocket.receive_text()
                await self._dispatch(session, parse_control_message(raw))
        except WebSocketDisconnect:
            _LOG.info(
                "/control getrennt: %s",
                session.device_id if session else "(ohne register)",
            )
        except ProtocolError as exc:
            _LOG.warning("/control-Protokollfehler: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("/control-Fehler: %s", exc)
        finally:
            if session is not None:
                await self._teardown(session)

    async def _handshake(self, websocket: WebSocket) -> Optional[DeviceSession]:
        """Erste Nachricht muss `register` sein; antwortet mit dem nackten `ack`."""
        raw = await websocket.receive_text()
        message = parse_control_message(raw)
        if message.get("type") != CTRL_REGISTER:
            _LOG.warning(
                "Erste /control-Nachricht ist kein register: %r", message.get("type")
            )
            await self._close(websocket, POLICY_CLOSE_CODE)
            return None
        device_id = message.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            _LOG.warning("register ohne nicht-leere device_id")
            await self._close(websocket, POLICY_CLOSE_CODE)
            return None
        session = DeviceSession(
            device_id=device_id,
            capabilities=_normalize_capabilities(message.get("capabilities")),
            control_ws=websocket,
        )
        # T3/Doppel-Pairing (nur Diagnose, KEIN Verhaltens-Umbau): eine zweite
        # Verbindung mit identischer `device_id` ersetzt die Registry-Session
        # (dokumentierte Re-Connect-Semantik von `SessionRegistry.register`).
        # Ein solcher Ersatz, während die alte Verbindung noch lebt, ist aber
        # fast immer ein zweites Gerät mit geklonter ID oder ein Client-Bug ⇒
        # sichtbar warnen; der Live-Betrieb bleibt unverändert.
        existing = await self.registry.get(device_id)
        if existing is not None and not existing.dead and existing.control_ws is not websocket:
            _LOG.warning(
                "Doppel-Device-ID: %s registriert sich erneut, während die "
                "vorige Session noch verbunden ist — alte Session wird ersetzt "
                "(zweites Gerät mit gleicher ID oder Client-Reconnect).",
                device_id,
            )
        await self.registry.register(session)
        # K1/E27: genau 2 Keys – kein features, kein time_ms.
        await websocket.send_json(serialize_ack(device_id))
        _LOG.info(
            "Gerät registriert: %s (caps=%d)", device_id, len(session.capabilities)
        )
        return session

    async def _start_connect_sequence(self, session: DeviceSession) -> None:
        """Nach `register`/`ack` die Connect-Sequenz ausspielen (P5.T5, E77-Fix).

        Der Handler ist **additiv** injiziert (wie :attr:`shell_handler`); ohne
        Handler bleibt es beim reinen `ack` (P5.T1-Verhalten).  Reihenfolge
        `ack` → `config` (K4) → `listeningAnim` (E11) → `mic_start` (K3) ergibt
        sich aus :meth:`app.device_session.ConnectSequence.on_ack`; der `ack`
        selbst ist zu diesem Zeitpunkt bereits gesendet (K1).  Ein Fehler wird
        protokolliert, schließt die Session aber **nicht**.
        """
        handler = self.connect_handler
        if handler is None:
            return
        try:
            await handler(session)
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning(
                "Connect-Sequenz für %s scheiterte: %s", session.device_id, exc
            )

    async def _dispatch(
        self, session: DeviceSession, message: Mapping[str, Any]
    ) -> None:
        """`/control`-Nachricht routen: `pong`/`register` intern, Rest P5.T2."""
        message_type = message.get("type")
        if message_type == CTRL_PONG:
            session.note_pong(message)
            return
        if message_type == CTRL_REGISTER:
            return  # Duplikat nach dem Handshake: kein zweites ack (K1)
        handler = self.control_handler
        if handler is not None:
            await handler(session, message)
            return
        # Kein externer Handler gesetzt ⇒ P5.T2-Standardverhalten (button).
        await self.handle_device_message(session, message)

    async def handle_device_message(
        self, session: DeviceSession, message: Mapping[str, Any]
    ) -> None:
        """P5.T2-Standardhandler für Geräte-`/control`-Nachrichten.

        ``button`` wird (K6/E29) an :meth:`_handle_button` gereicht.  Die
        übrigen Typen – insbesondere die **ausgehenden** C→D-Nachrichten
        ``mic_stop``/``leds``/``led_anim``/``speaker_flush`` – sind eingehend
        protokollgerecht **unbekannt** und werden ignoriert (Spec §2: „unbekannte
        ``type`` werden ignoriert"), ebenso ``ble_adverts``/``stats`` usw.
        """
        if message.get("type") == CTRL_BUTTON:
            await self._handle_button(session, message)
            return
        _LOG.debug(
            "Control-Nachricht %r von %s ignoriert (Spec §2)",
            message.get("type"),
            session.device_id,
        )

    async def _handle_button(
        self, session: DeviceSession, message: Mapping[str, Any]
    ) -> None:
        """`button` (K6/E29): nur `clickType == 138` **und** `down:false` zählt.

        `down:true` wird **ignoriert** (E29); `heldMs`/`muted` werden geparst und
        an :meth:`Pipeline.on_button` weitergereicht (E60: **awaited**).
        """
        if bool(message.get("down", False)):
            _LOG.debug("Button down:true von %s ignoriert (K6/E29)", session.device_id)
            return
        click_type = message.get("clickType")
        if isinstance(click_type, bool) or not isinstance(click_type, int):
            _LOG.debug(
                "Button ohne int-clickType (%r) von %s ignoriert",
                click_type,
                session.device_id,
            )
            return
        if click_type != BUTTON_CLICK_TYPE:
            _LOG.debug(
                "Button clickType=%s von %s nicht 138 – ignoriert (E29)",
                click_type,
                session.device_id,
            )
            return
        held_ms_raw = message.get("heldMs")
        held_ms: Optional[int] = None
        if held_ms_raw is not None:
            try:
                held_ms = int(held_ms_raw)
            except (TypeError, ValueError):
                _LOG.debug("Button heldMs=%r nicht als int lesbar", held_ms_raw)
                held_ms = None
        muted = bool(message.get("muted", False))
        if held_ms is not None and held_ms >= HOLD_MS_THRESHOLD:
            _LOG.debug("Button-Hold %s ms – K6 bleibt Turn/Cancel", held_ms)
        pipeline = self._pipeline
        if pipeline is None:
            _LOG.debug("Button ohne Pipeline – %s verworfen", session.device_id)
            return
        try:
            await pipeline.on_button(
                session.device_id,
                click_type,
                held_ms=held_ms,
                muted=muted,
                down=False,
            )
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("on_button(%s) scheiterte: %s", session.device_id, exc)

    # ── `/data` (P5.T2) ───────────────────────────────────────────────
    async def handle_data(self, websocket: WebSocket) -> None:
        """`/data`-Endpunkt: `identify` → permanenter Binär-Stream (Spec §7, E5).

        Erste Nachricht **muss** JSON ``{"type":"identify","device_id":…}`` sein
        (sonst Close 1008, wörtlich die Referenz). Danach wird jedes Binärframe
        mit :func:`parse_data_frame` (Richtung D→C) geparst und **verlustfrei**
        an die Pipeline gereicht. Ein fehlerhaftes Frame schließt die Verbindung
        **nicht** (Referenz-Verhalten `em_controller.py:4034-4088`).
        """
        await websocket.accept()
        session: Optional[DeviceSession] = None
        try:
            device_id = await self._identify_device(websocket)
            if device_id is None:
                return
            session = await self._bind_data_session(device_id)
            if session is None:
                _LOG.warning("/data ohne registriertes /control für %s", device_id)
                await self._close(websocket, POLICY_CLOSE_CODE)
                return
            session.data_ws = websocket
            _LOG.info("Datenverbindung etabliert: %s", device_id)
            while not session.dead:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is None:
                    # Text auf /data nach identify → keine Interpretation.
                    continue
                await self._dispatch_data_frame(device_id, raw)
        except WebSocketDisconnect:
            _LOG.info(
                "/data getrennt: %s",
                session.device_id if session else "(ohne identify)",
            )
        except asyncio.TimeoutError:
            _LOG.warning("/data ohne identify binnen %.0fs", IDENTIFY_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("/data-Fehler: %s", exc)
        finally:
            if session is not None and session.data_ws is websocket:
                session.data_ws = None

    async def _identify_device(self, websocket: WebSocket) -> Optional[str]:
        """Erste `/data`-Nachricht prüfen: JSON `identify` mit `device_id`."""
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=IDENTIFY_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            _LOG.warning("/data identify-Timeout")
            await self._close(websocket, POLICY_CLOSE_CODE)
            return None
        try:
            message = parse_control_message(raw)
        except ProtocolError as exc:
            _LOG.warning("/data identify ungültig: %s", exc)
            await self._close(websocket, POLICY_CLOSE_CODE)
            return None
        device_id = message.get("device_id")
        if message.get("type") != DATA_IDENTIFY_TYPE or not isinstance(
            device_id, str
        ) or not device_id:
            _LOG.warning("/data erste Nachricht war kein identify: %r", message.get("type"))
            await self._close(websocket, POLICY_CLOSE_CODE)
            return None
        return device_id

    async def _bind_data_session(self, device_id: str) -> Optional[DeviceSession]:
        """Bis zu 20×0,1 s auf das registrierende `/control` warten (Spec §1)."""
        for _ in range(DATA_IDENTIFY_RETRIES):
            session = await self.registry.get(device_id)
            if session is not None:
                return session
            await asyncio.sleep(DATA_IDENTIFY_INTERVAL)
        return await self.registry.get(device_id)

    async def _dispatch_data_frame(self, device_id: str, raw: bytes) -> None:
        """Ein D→C-Frame parsen und **genau einmal** an die Pipeline reichen."""
        try:
            frame = parse_data_frame(raw, Direction.DEVICE_TO_CONTROLLER)
        except ProtocolError as exc:
            _LOG.debug("Ungültiges /data-Frame von %s: %s", device_id, exc)
            return
        if isinstance(frame, MicFrame):
            await self._pipeline_on_mic_frame(device_id, frame.seq, frame.pcm)
            return
        if isinstance(frame, MicSentinel):
            if frame.kind is SentinelKind.VAD_END:
                await self._pipeline_on_vad_end(device_id)
            else:  # SentinelKind.NO_SPEECH_TIMEOUT
                await self._pipeline_on_no_speech(device_id)
            return
        # Unbekanntes/zu kurzes/0x02/0x03-Frame: verworfen, Stream bleibt.
        _LOG.debug("Nicht-Mic-Frame auf /data von %s verworfen", device_id)

    async def _pipeline_on_mic_frame(
        self, device_id: str, seq: Optional[int], pcm: bytes
    ) -> None:
        if self._pipeline is None:
            return
        try:
            await self._pipeline.on_mic_frame(device_id, seq, pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv, Stream bleibt
            _LOG.warning("on_mic_frame(%s) scheiterte: %s", device_id, exc)

    async def _pipeline_on_vad_end(self, device_id: str) -> None:
        if self._pipeline is None:
            return
        try:
            await self._pipeline.on_vad_end(device_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("on_vad_end(%s) scheiterte: %s", device_id, exc)

    async def _pipeline_on_no_speech(self, device_id: str) -> None:
        if self._pipeline is None:
            return
        try:
            await self._pipeline.on_no_speech(device_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("on_no_speech(%s) scheiterte: %s", device_id, exc)

    # ── `/shell/{device_id}` (P5.T2) ──────────────────────────────────
    async def handle_shell(self, websocket: WebSocket, device_id: str) -> None:
        """`/shell/{device_id}` – **roher** Binär-Proxy (keine Interpretation).

        Die Bytes werden **unverändert** an :attr:`shell_handler` weitergereicht
        (falls gesetzt); `session.shell_ws` wird angehängt.  Es gibt **kein**
        Handshake auf dieser Ebene (Spec §1).
        """
        await websocket.accept()
        session: Optional[DeviceSession] = None
        try:
            session = await self.registry.get(device_id)
            if session is None:
                _LOG.warning("/shell ohne registrierte Session: %s", device_id)
                await self._close(websocket, POLICY_CLOSE_CODE)
                return
            session.shell_ws = websocket
            _LOG.info("Shell-Verbindung: %s", device_id)
            while not session.dead:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is None:
                    # Text wird roh als UTF-8-Bytes weitergereicht (kein Parse).
                    text = message.get("text")
                    if text is None:
                        continue
                    raw = text.encode("utf-8")
                await self._forward_shell(session, raw)
        except WebSocketDisconnect:
            _LOG.info("/shell getrennt: %s", device_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("/shell-Fehler (%s): %s", device_id, exc)
        finally:
            if session is not None and session.shell_ws is websocket:
                session.shell_ws = None

    async def _forward_shell(self, session: DeviceSession, data: bytes) -> None:
        handler = self.shell_handler
        if handler is None:
            _LOG.debug("kein shell_handler – %d B von %s verworfen", len(data), session.device_id)
            return
        try:
            await handler(session, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("shell_handler(%s) scheiterte: %s", session.device_id, exc)

    # ── Ausgehende Transporte (P5.T2) ─────────────────────────────────
    async def send_control(self, device_id: str, message: Mapping[str, Any]) -> bool:
        """JSON auf `/control` senden (C→D). True, wenn ein Socket existiert.

        Generischer Sender für `mic_start`/`mic_stop`/`leds`/`led_anim`/
        `speaker_flush`; gedacht als Bindung für `pipeline.send_control` (P5.T4/T5).
        """
        session = await self.registry.get(device_id)
        if session is None or session.control_ws is None:
            return False
        try:
            await session.control_ws.send_json(dict(message))
            return True
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("send_control(%s) scheiterte: %s", device_id, exc)
            return False

    async def send_binary(self, device_id: str, data: bytes) -> bool:
        """Binär auf `/data` senden (C→D, Speaker). True, wenn ein Socket existiert."""
        session = await self.registry.get(device_id)
        if session is None or session.data_ws is None:
            return False
        try:
            await session.data_ws.send_bytes(bytes(data))
            return True
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("send_binary(%s) scheiterte: %s", device_id, exc)
            return False

    async def send_mic_start(self, device_id: str, *, lock_mic: bool = False) -> bool:
        """**Ausgehendes** `mic_start` (`{"type":"mic_start"[,"lock_mic":true]}`).

        Die Connect-Sequenz selbst ist P5.T5; hier wird nur die Fähigkeit bereit-
        gestellt.
        """
        return await self.send_control(device_id, serialize_mic_start(lock_mic))

    # ── Keepalive ─────────────────────────────────────────────────────
    def _start_keepalive(self, session: DeviceSession) -> None:
        """Keepalive-Task starten; `interval <= 0` deaktiviert ihn (Tests)."""
        if self.keepalive_interval <= 0:
            return
        session.keepalive_task = asyncio.create_task(
            self._keepalive_loop(session), name=f"keepalive:{session.device_id}"
        )

    async def _keepalive_loop(self, session: DeviceSession) -> None:
        """App-`ping` im Intervall; ohne `pong` binnen Timeout ⇒ `mark_dead`."""
        try:
            while not session.dead:
                await asyncio.sleep(self.keepalive_interval)
                if session.dead:
                    return
                session.pong_event.clear()
                ping_id = session.ping_sequence.next()
                await session.control_ws.send_json(serialize_ping(ping_id))
                try:
                    await asyncio.wait_for(
                        session.pong_event.wait(), self.keepalive_timeout
                    )
                except asyncio.TimeoutError:
                    _LOG.warning(
                        "Keepalive-Timeout für %s (%.1fs ohne pong, id=%d)",
                        session.device_id,
                        self.keepalive_timeout,
                        ping_id,
                    )
                    await self.mark_dead(
                        session.device_id, reason="keepalive_timeout"
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning(
                "Keepalive-Fehler für %s: %s – Session wird verworfen",
                session.device_id,
                exc,
            )
            await self.mark_dead(session.device_id, reason=f"keepalive_error:{exc}")

    # ── Lebenszyklus ──────────────────────────────────────────────────
    async def mark_dead(
        self, device_id: str, *, reason: str = "unknown"
    ) -> Optional[DeviceSession]:
        """Session verwerfen: Keepalive stoppen, Close 1011, Pipeline informieren.

        Idempotent – ein zweiter Aufruf (z. B. aus dem Endpoint-`finally`) findet
        keine Session mehr und ist ein No-op.
        """
        session = await self.registry.mark_dead(device_id, reason=reason)
        if session is None:
            return None
        await session.stop_keepalive()
        await self._close(session.control_ws, KEEPALIVE_CLOSE_CODE)
        await self._pipeline_on_device_gone(device_id)
        return session

    async def _teardown(self, session: DeviceSession) -> None:
        """Reguläres Aufräumen im Endpoint-`finally` – genau einmal pro Session."""
        await session.stop_keepalive()
        await self.registry.remove(session.device_id)
        if not session.dead:
            session.dead = True
            await self._pipeline_on_device_gone(session.device_id)

    async def _pipeline_on_device_gone(self, device_id: str) -> None:
        if self._pipeline is None:
            return
        try:
            await self._pipeline.on_device_gone(device_id)
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("on_device_gone(%s) scheiterte: %s", device_id, exc)

    @staticmethod
    async def _close(websocket: Any, code: int) -> None:
        try:
            await websocket.close(code=code)
        except Exception:  # pragma: no cover – bereits geschlossen
            pass
