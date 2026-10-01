"""Generalisierter Fake-Echo-Dot für den L2-Testlayer (P9.T1, `PLAN.md:539`).

Der Fake ist ein **echter WebSocket-Client** und bildet die Geräteseite des
EchoMuse-Wire-Protokolls nach.  Autoritativ sind `docs/ECOMUSE_PROTOCOL.md`
(P0.T6) und `app/protocol.py` (P2.T0) — **alle** Frame-Längen, Typcodes,
Sequenzregeln und Control-Namen werden aus `app.protocol` importiert, es gibt
hier **keine** eigenen Magic Numbers.

Was der Fake leistet (Auftrag `PLAN.md:539`):

* **Vollständiger Handshake:** verbindet `/control`, sendet `register`, erwartet
  `ack` **ohne `features`** (K1/E27), erwartet `config` (auch den schlanken
  `listeningAnim`-Push, E67) und `mic_start`; die Reihenfolge
  `ack`→`config`→`mic_start` wird festgehalten.
* **Permanente Mic-Frames:** 3-Byte-Header `[0x01][seq_hi][seq_lo]` + 2560 B PCM
  = **2563 B** (K2/E28), Sequenz als uint16 **big-endian** mit Wrap, **injizierbare
  Lücke** (Resync-Test).
* **Audio einspeisen:** Stille (Default) oder WAV-/Roh-Chunks aus `tests/fixtures/`.
* **Kommandos senden:** `0x04`/`0x05` (4-Byte-Turn-Sentinels) und
  `{"type":"button",…}` — auf Kommando.
* **Mitschnitt ALLER empfangenen Frames** (`ack`, `config`, `mic_start`,
  `mic_stop`, `speaker_flush`, `leds`, `led_anim`, `0x02`/`0x03`) als
  transkribierbare Ereignisliste.
* **Szenario-JSON** (Replay + Assert) über `Scenario`/`run_scenario`.

**Kein echtes Gerät, kein `.123`.**  Der Fake wird im L1-Component-Test gegen
einen In-Process-Stub-Manager betrieben und in P9.T4 gegen den echten
`manager_proc`-Subprozess (L2).

Marker-Konvention: Die hieraus abgeleiteten Tests tragen `component` (L1) bzw.
`integration` (L2) — nur diese Marker heben die E36-Netzsperre auf.
"""

from __future__ import annotations

import asyncio
import json
import wave
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from websockets.asyncio.client import connect

from app.protocol import (
    CHUNK_BYTES,
    CTRL_ACK,
    CTRL_BUTTON,
    CTRL_CONFIG,
    CTRL_LED_ANIM,
    CTRL_LEDS,
    CTRL_MIC_START,
    CTRL_MIC_STOP,
    CTRL_PING,
    CTRL_PONG,
    CTRL_REGISTER,
    CTRL_SPEAKER_FLUSH,
    MIC_FRAME_BYTES,
    MusicEos,
    MusicFrame,
    SEQ_MODULUS,
    SequenceCounter,
    SpeakerEos,
    SpeakerFrame,
    build_mic_frame,
    build_no_speech_frame,
    build_vad_end_frame,
    parse_control_message,
    parse_controller_data_frame,
)

__all__ = [
    "FakeEchoDot",
    "FakeEchoDotError",
    "Plane",
    "ReceivedEvent",
    "Scenario",
    "ScenarioError",
    "CONTROL_PATH",
    "DATA_PATH",
    "DEFAULT_CLICK_TYPE",
    "DEFAULT_IDENTIFY_TYPE",
    "DEFAULT_MIC_CHUNK_BYTES",
    "DEFAULT_MIC_FRAME_BYTES",
    "silence_chunks",
    "load_pcm_chunks",
]

#: Pfade der drei Ebenen laut `docs/ECOMUSE_PROTOCOL.md` §1.
CONTROL_PATH: Final[str] = "/control"
DATA_PATH: Final[str] = "/data"

#: Erstes Message auf `/data` (Spec §1): JSON `identify`.
DEFAULT_IDENTIFY_TYPE: Final[str] = "identify"

#: Dot-Button-`clickType` (Spec §6, E29); nur 138 erreicht den Controller.
DEFAULT_CLICK_TYPE: Final[int] = 138

#: Vom Fake erzeugte Mic-Payload: exakt ein OWW-Chunk, aus `app.protocol`.
DEFAULT_MIC_CHUNK_BYTES: Final[int] = CHUNK_BYTES
#: Gesamtlänge eines Mic-Frames (K2/E28) — Invariante, aus `app.protocol`.
DEFAULT_MIC_FRAME_BYTES: Final[int] = MIC_FRAME_BYTES


class FakeEchoDotError(RuntimeError):
    """Verletzung des Fake-Vertrags (Handshake, Leser, Szenario)."""


class ScenarioError(FakeEchoDotError):
    """Ein Szenario wurde nicht wie beschrieben erfüllt (Assert)."""


class Plane(str, Enum):
    """Die zwei im Fake genutzten Ebenen (Spec §1)."""

    CONTROL = "control"
    DATA = "data"


@dataclass(frozen=True)
class ReceivedEvent:
    """Ein vom Fake **empfangener** Frame, transkribierbar.

    `control`-Frames sind JSON-Objekte (`message`), `data`-Frames rohe Bytes
    (`raw`) mit geparstem Typ (`kind`, `pcm`, `seq`).  Für `ack` hält
    `features_present` fest, ob ein verbotenes `features`-Feld anhing (K1/E27).
    """

    plane: Plane
    kind: str
    message: Mapping[str, Any] | None = None
    raw: bytes | None = None
    pcm: bytes | None = None
    seq: int | None = None
    features_present: bool = False

    def transcript_line(self) -> str:
        """Eine Zeile für die Ereignisliste (keine Secrets, keine Rohdumps)."""
        if self.plane is Plane.CONTROL:
            marker = " [features!]" if self.features_present else ""
            return f"[control] {self.kind}{marker}"
        size = len(self.raw) if self.raw is not None else 0
        return f"[data] {self.kind} ({size} B)"


# ── PCM-Helfer (deterministisch, keine Zufallszahlen) ─────────────────────
def silence_chunks(count: int = 1, *, chunk_bytes: int = DEFAULT_MIC_CHUNK_BYTES) -> tuple[bytes, ...]:
    """`count` Chunks Stille (Nullbytes) — der Default-Audio-Eingang."""
    return tuple(bytes(chunk_bytes) for _ in range(count))


def load_pcm_chunks(
    path: str | Path,
    *,
    chunk_bytes: int = DEFAULT_MIC_CHUNK_BYTES,
    max_chunks: int | None = None,
) -> tuple[bytes, ...]:
    """WAV-Datei in `chunk_bytes`-PCM-Chunks zerlegen (16 kHz S16_LE mono).

    Bewusst minimal: keine Resampling-/Kanal-Logik — die Fixtures der Suite
    (`tests/fixtures/sample_16k.wav`) liegen bereits 16 kHz/S16_LE/mono vor.
    Der letzte unvollständige Chunk wird genullt aufgefüllt, damit **jeder**
    Chunk exakt `chunk_bytes` hat (`build_mic_frame` erzwingt das).
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != 16000 or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise FakeEchoDotError(
                f"{path}: erwartet 16 kHz/S16_LE/mono, es ist "
                f"{handle.getframerate()} Hz/{handle.getsampwidth() * 8} bit/"
                f"{handle.getnchannels()} ch"
            )
        pcm = handle.readframes(handle.getnframes())
    chunks: list[bytes] = []
    for offset in range(0, len(pcm), chunk_bytes):
        piece = pcm[offset : offset + chunk_bytes]
        if len(piece) < chunk_bytes:
            piece = piece + bytes(chunk_bytes - len(piece))
        chunks.append(piece)
        if max_chunks is not None and len(chunks) >= max_chunks:
            break
    return tuple(chunks) or silence_chunks(1, chunk_bytes=chunk_bytes)


# ── Szenario-JSON (Replay + Assert) ───────────────────────────────────────
@dataclass(frozen=True)
class Scenario:
    """Beschreibt, was der Fake sendet und was danach erwartet wird.

    JSON-Form (Beispiel)::

        {
          "name": "handshake-button-vad",
          "send": [{"kind": "button", "clickType": 138, "down": false},
                   {"kind": "vad_end"}],
          "expect_handshake": ["ack", "config", "mic_start"],
          "expect_received": ["ack", "config", "mic_start"],
          "expect_ack_features": false,
          "mic_frames": 2
        }

    `send`-Elemente: `button`, `vad_end`, `no_speech` oder
    `{"kind":"data","hex":"0100…"}`.  `expect_received` prüft die vom Fake
    **empfangenen** Control-/Data-Typen (Mitschnitt).
    """

    name: str
    send: tuple[Mapping[str, Any], ...] = ()
    expect_handshake: tuple[str, ...] = (CTRL_ACK, CTRL_CONFIG, CTRL_MIC_START)
    expect_received: tuple[str, ...] = ()
    expect_ack_features: bool = False
    mic_frames: int = 0

    @classmethod
    def from_json(cls, data: str | bytes | Mapping[str, Any]) -> "Scenario":
        """Szenario aus JSON-Text/-Bytes oder bereits geparstem Mapping bauen."""
        if isinstance(data, (str, bytes, bytearray)):
            obj = json.loads(bytes(data).decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
        else:
            obj = dict(data)
        if not isinstance(obj, Mapping) or "name" not in obj:
            raise ScenarioError("Szenario braucht ein `name`-Feld")
        return cls(
            name=str(obj["name"]),
            send=tuple(dict(item) for item in obj.get("send", ())),
            expect_handshake=tuple(str(x) for x in obj.get("expect_handshake", (CTRL_ACK, CTRL_CONFIG, CTRL_MIC_START))),
            expect_received=tuple(str(x) for x in obj.get("expect_received", ())),
            expect_ack_features=bool(obj.get("expect_ack_features", False)),
            mic_frames=int(obj.get("mic_frames", 0)),
        )

    def to_json(self) -> str:
        """Szenario als JSON-Text (Replay-fähig, stabile Schlüsselreihenfolge)."""
        return json.dumps(
            {
                "name": self.name,
                "send": [dict(item) for item in self.send],
                "expect_handshake": list(self.expect_handshake),
                "expect_received": list(self.expect_received),
                "expect_ack_features": self.expect_ack_features,
                "mic_frames": self.mic_frames,
            },
            ensure_ascii=False,
            indent=2,
        )


# ── Der Fake selbst ───────────────────────────────────────────────────────
@dataclass
class _SentMic:
    seq: int
    frame: bytes


class FakeEchoDot:
    """Fake-Echo-Dot als echter `/control`+`/data`-WebSocket-Client.

    Konstruktor::
        FakeEchoDot(base_uri="ws://127.0.0.1:18767", device_id="fake-dot",
                    capabilities=("mic","speaker","leds"), ...)

    Lebenszyklus: ``await dot.start()`` (Register + Handshake + `/data`
    `identify`) … ``await dot.stop()``.  Dazwischen:
    ``send_mic_frames(n)``, ``start_mic_streaming()``, ``inject_gap(n)``,
    ``send_button()``, ``send_vad_end()``, ``send_no_speech()``,
    ``run_scenario(scenario)``.
    """

    def __init__(
        self,
        base_uri: str = "ws://127.0.0.1:18767",
        *,
        device_id: str = "fake-echomuse-dot",
        capabilities: Sequence[str] = ("mic", "speaker", "leds"),
        version: str = "v2.15.0-fake",
        ip: str | None = None,
        mic_chunks: Iterable[bytes] | None = None,
        start_sequence: int = 0,
        require_no_features: bool = True,
    ) -> None:
        self.base_uri = base_uri.rstrip("/")
        self.control_uri = f"{self.base_uri}{CONTROL_PATH}"
        self.data_uri = f"{self.base_uri}{DATA_PATH}"
        self.device_id = device_id
        self.capabilities = tuple(capabilities)
        self.version = version
        self.ip = ip
        self.require_no_features = bool(require_no_features)

        self.control: Any | None = None
        self.data: Any | None = None
        self._control_task: asyncio.Task[None] | None = None
        self._data_task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._running = False

        #: Mitschnitt ALLER empfangenen Frames (siehe Modul-Docstring).
        self.events: list[ReceivedEvent] = []
        #: Empfangene Control-Typen in Reihenfolge (nur Handshake-relevante).
        self.handshake_events: list[str] = []
        self.ack: Mapping[str, Any] | None = None
        self.ack_features_present = False
        self.received_configs: list[Mapping[str, Any]] = []
        self.received_mic_start: Mapping[str, Any] | None = None
        self.received_mic_stops: list[Mapping[str, Any]] = []
        #: Vom Fake gesendete Mic-Frames (seq + Rohbytes, für L2-Asserts).
        self.sent_mic: list[_SentMic] = []

        self._handshake_ready = asyncio.Event()
        self._reader_error: Exception | None = None

        self.sequence = SequenceCounter(start=start_sequence)
        self._gap_pending = 0
        self._mic_chunks: tuple[bytes, ...] = tuple(mic_chunks) if mic_chunks else silence_chunks(1)
        for chunk in self._mic_chunks:
            if len(chunk) != DEFAULT_MIC_CHUNK_BYTES:
                raise FakeEchoDotError(
                    f"Mic-Chunk hat {len(chunk)} B, erwartet {DEFAULT_MIC_CHUNK_BYTES}"
                )
        self._mic_index = 0
        self._pcm_override: bytes | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def start(self, *, timeout: float = 5.0) -> tuple[str, ...]:
        """Verbinden, `register`, Handshake abwarten, `/data`-`identify`.

        Liefert die **normalisierte** Handshake-Reihenfolge
        (`("ack","config","mic_start")`).  Fehler (Timeout, fehlender Teil,
        `features` im `ack`) ⇒ `FakeEchoDotError`, Verbindung wird geschlossen.
        """
        if self._running:
            return self.normalized_handshake
        try:
            self.control = await connect(self.control_uri, proxy=None, open_timeout=timeout)
            self._control_task = asyncio.create_task(self._read_control(self.control))
            await self.control.send(json.dumps(self._register_message()))
            handshake = await self.wait_for_handshake(timeout=timeout)

            self.data = await connect(self.data_uri, proxy=None, open_timeout=timeout)
            self._data_task = asyncio.create_task(self._read_data(self.data))
            await self.data.send(
                json.dumps({"type": DEFAULT_IDENTIFY_TYPE, "device_id": self.device_id})
            )
            self._running = True
            return handshake
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Streaming beenden, Leser canceln, beide Verbindungen schließen (idempotent)."""
        await self.stop_mic_streaming()
        for task in (self._control_task, self._data_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._control_task, self._data_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - Teardown best-effort
                    pass
        self._control_task = None
        self._data_task = None
        for ws in (self.control, self.data):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001 - Teardown best-effort
                    pass
        self.control = None
        self.data = None
        self._running = False

    @property
    def running(self) -> bool:
        """True zwischen `start()` und `stop()`, solange keine Leser-Fehler anliegen."""
        return self._running and self._reader_error is None

    # ── Handshake ─────────────────────────────────────────────────────────
    def _register_message(self) -> dict[str, Any]:
        """`register`-Payload (Spec §2.1); `ip` nur wenn gesetzt."""
        message: dict[str, Any] = {
            "type": CTRL_REGISTER,
            "device_id": self.device_id,
            "version": self.version,
            "capabilities": list(self.capabilities),
        }
        if self.ip is not None:
            message["ip"] = self.ip
        return message

    @property
    def normalized_handshake(self) -> tuple[str, ...]:
        """Handshake-Typen mit aufeinanderfolgenden Duplikaten zusammengefasst.

        `config` kann zweimal kommen (`config` + schlanker `listeningAnim`-Push,
        E67); die Reihenfolge `ack`→`config`→`mic_start` bleibt erkennbar.
        """
        collapsed: list[str] = []
        for name in self.handshake_events:
            if not collapsed or collapsed[-1] != name:
                collapsed.append(name)
        return tuple(collapsed)

    async def wait_for_handshake(self, *, timeout: float = 5.0) -> tuple[str, ...]:
        """Auf `ack` + ≥1 `config` + `mic_start` warten; Reihenfolge zurückgeben."""
        try:
            await asyncio.wait_for(self._handshake_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise FakeEchoDotError(
                f"Handshake nicht binnen {timeout:g}s vollständig "
                f"(ack={self.ack is not None}, configs={len(self.received_configs)}, "
                f"mic_start={self.received_mic_start is not None})"
            ) from exc
        if self._reader_error is not None:
            raise self._reader_error
        if self.ack is None or not self.received_configs or self.received_mic_start is None:
            raise FakeEchoDotError("Handshake unvollständig nach Event")
        return self.normalized_handshake

    def _maybe_ready(self) -> None:
        if self.ack is not None and self.received_configs and self.received_mic_start is not None:
            self._handshake_ready.set()

    # ── Leser + Mitschnitt ────────────────────────────────────────────────
    async def _read_control(self, ws: Any) -> None:
        try:
            async for raw in ws:
                message = parse_control_message(raw)
                kind = str(message.get("type"))
                features = kind == CTRL_ACK and "features" in message
                self.events.append(
                    ReceivedEvent(
                        plane=Plane.CONTROL,
                        kind=kind,
                        message=message,
                        features_present=features,
                    )
                )
                if kind == CTRL_ACK:
                    self.ack = message
                    self.ack_features_present = features
                    if self.require_no_features and features:
                        self._reader_error = FakeEchoDotError(
                            "`ack` enthält `features` — K1/E27 verletzt (wörtlich 2 Keys erwartet)"
                        )
                        self._handshake_ready.set()
                        return
                    self.handshake_events.append(CTRL_ACK)
                elif kind == CTRL_CONFIG:
                    self.received_configs.append(message)
                    self.handshake_events.append(CTRL_CONFIG)
                elif kind == CTRL_MIC_START:
                    self.received_mic_start = message
                    self.handshake_events.append(CTRL_MIC_START)
                elif kind == CTRL_MIC_STOP:
                    self.received_mic_stops.append(message)
                elif kind == CTRL_PING:
                    await ws.send(
                        json.dumps({"type": CTRL_PONG, "id": message.get("id"), "mono": 0})
                    )
                self._maybe_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Lesefehler sichtbar machen
            self._reader_error = FakeEchoDotError(f"/control-Leser beendet: {exc!r}")
            self._handshake_ready.set()

    async def _read_data(self, ws: Any) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    self.events.append(ReceivedEvent(plane=Plane.DATA, kind="text", raw=raw.encode("utf-8")))
                    continue
                blob = bytes(raw)
                parsed = parse_controller_data_frame(blob)
                if isinstance(parsed, SpeakerFrame):
                    self.events.append(ReceivedEvent(plane=Plane.DATA, kind="speaker_frame", raw=blob, pcm=parsed.pcm))
                elif isinstance(parsed, SpeakerEos):
                    self.events.append(ReceivedEvent(plane=Plane.DATA, kind="speaker_eos", raw=blob))
                elif isinstance(parsed, MusicFrame):
                    self.events.append(ReceivedEvent(plane=Plane.DATA, kind="music_frame", raw=blob, pcm=parsed.pcm))
                elif isinstance(parsed, MusicEos):
                    self.events.append(ReceivedEvent(plane=Plane.DATA, kind="music_eos", raw=blob))
                else:
                    self.events.append(ReceivedEvent(plane=Plane.DATA, kind="unknown", raw=blob))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Lesefehler sichtbar machen
            self._reader_error = FakeEchoDotError(f"/data-Leser beendet: {exc!r}")

    # ── Mic-Frames ────────────────────────────────────────────────────────
    def _next_sequence(self) -> int:
        """Nächste Sequenznummer; eine injizierte Lücke wird hier verrechnet."""
        if self._gap_pending:
            gap = self._gap_pending
            self._gap_pending = 0
            self.sequence.reset((self.sequence.value + gap) % SEQ_MODULUS)
        return self.sequence.next()

    def inject_gap(self, count: int = 1) -> None:
        """Nächsten Mic-Frame `count` Sequenznummern **überspringen** lassen.

        Der Manager sieht damit eine Lücke und muss per `SequenceTracker`
        re-synchronisieren (E28).  Der Fake selbst bleibt korrekt (uint16-BE,
        Wrap `65535→0`).
        """
        if count < 1:
            raise FakeEchoDotError(f"gap muss ≥ 1 sein, ist {count}")
        self._gap_pending = int(count)

    def set_mic_pcm(self, pcm: bytes | None) -> None:
        """PCM `pcm` (genau `CHUNK_BYTES`) für **alle** weiteren Frames nutzen.

        `None` schaltet zurück auf die rotierende Audio-Quelle.
        """
        if pcm is not None and len(pcm) != DEFAULT_MIC_CHUNK_BYTES:
            raise FakeEchoDotError(f"PCM hat {len(pcm)} B, erwartet {DEFAULT_MIC_CHUNK_BYTES}")
        self._pcm_override = pcm

    def _next_chunk(self) -> bytes:
        if self._pcm_override is not None:
            return self._pcm_override
        chunk = self._mic_chunks[self._mic_index % len(self._mic_chunks)]
        self._mic_index += 1
        return chunk

    def _require_data(self) -> Any:
        if self.data is None:
            raise FakeEchoDotError("`/data` ist nicht verbunden — zuerst `await start()`")
        return self.data

    async def send_mic_frames(self, count: int, *, pcm: bytes | None = None) -> list[int]:
        """`count` Mic-Frames senden; liefert die verwendeten Sequenznummern."""
        ws = self._require_data()
        seqs: list[int] = []
        for _ in range(count):
            seq = self._next_sequence()
            frame = build_mic_frame(seq, pcm if pcm is not None else self._next_chunk())
            assert len(frame) == DEFAULT_MIC_FRAME_BYTES, len(frame)
            await ws.send(frame)
            self.sent_mic.append(_SentMic(seq=seq, frame=frame))
            seqs.append(seq)
        return seqs

    async def start_mic_streaming(self, *, interval: float = 0.05) -> None:
        """Permanenten Mic-Stream als Background-Task starten (idempotent)."""
        if self._stream_task is not None and not self._stream_task.done():
            return

        async def _loop() -> None:
            while True:
                await self.send_mic_frames(1)
                await asyncio.sleep(interval)

        self._stream_task = asyncio.create_task(_loop())

    async def stop_mic_streaming(self) -> None:
        """Background-Mic-Stream stoppen (idempotent)."""
        task = self._stream_task
        self._stream_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ── Kommandos ─────────────────────────────────────────────────────────
    def _require_control(self) -> Any:
        if self.control is None:
            raise FakeEchoDotError("`/control` ist nicht verbunden — zuerst `await start()`")
        return self.control

    async def send_button(
        self,
        *,
        click_type: int = DEFAULT_CLICK_TYPE,
        down: bool = False,
        held_ms: int = 0,
        muted: bool = False,
    ) -> None:
        """`{"type":"button",…}` senden (Spec §6)."""
        await self._require_control().send(
            json.dumps(
                {
                    "type": CTRL_BUTTON,
                    "clickType": int(click_type),
                    "down": bool(down),
                    "heldMs": int(held_ms),
                    "muted": bool(muted),
                    "button": {"type": "Dot"},
                }
            )
        )

    async def send_vad_end(self) -> None:
        """4-Byte-Sentinel `[0x01][0x00][0x00][0x04]` senden (K5, `0x04`)."""
        await self._require_data().send(build_vad_end_frame())

    async def send_no_speech(self) -> None:
        """4-Byte-Sentinel `[0x01][0x00][0x00][0x05]` senden (E6, `0x05`)."""
        await self._require_data().send(build_no_speech_frame())

    async def send_data(self, raw: bytes) -> None:
        """Rohen `/data`-Binärframe senden (Debug/Szenario-`hex`)."""
        await self._require_data().send(bytes(raw))

    async def send_control(self, message: Mapping[str, Any]) -> None:
        """Rohes `/control`-JSON senden (Debug/Weiterleitung)."""
        await self._require_control().send(json.dumps(dict(message)))

    # ── Szenarien ─────────────────────────────────────────────────────────
    async def run_scenario(self, scenario: Scenario) -> None:
        """Szenario abspielen (Replay) und danach verifizieren (Assert)."""
        if not self._running:
            await self.start()
        handshake = self.normalized_handshake
        if scenario.mic_frames:
            await self.send_mic_frames(scenario.mic_frames)
        for item in scenario.send:
            await self._dispatch_scenario_item(item)
        # Lesern Gelegenheit geben, die ausgelösten Antworten aufzunehmen.
        for _ in range(5):
            await asyncio.sleep(0)
        self.check_scenario(scenario, handshake=handshake)

    async def _dispatch_scenario_item(self, item: Mapping[str, Any]) -> None:
        kind = item.get("kind")
        if kind == "button":
            await self.send_button(
                click_type=int(item.get("clickType", DEFAULT_CLICK_TYPE)),
                down=bool(item.get("down", False)),
                held_ms=int(item.get("heldMs", 0)),
                muted=bool(item.get("muted", False)),
            )
        elif kind == "vad_end":
            await self.send_vad_end()
        elif kind == "no_speech":
            await self.send_no_speech()
        elif kind == "data":
            await self.send_data(bytes.fromhex(str(item["hex"])))
        else:
            raise ScenarioError(f"Unbekanntes Sende-Element: {kind!r}")

    def check_scenario(
        self, scenario: Scenario, *, handshake: Sequence[str] | None = None
    ) -> None:
        """Szenario-Erwartungen prüfen; `ScenarioError` mit allen Abweichungen."""
        problems: list[str] = []
        actual = tuple(handshake if handshake is not None else self.normalized_handshake)
        if actual != tuple(scenario.expect_handshake):
            problems.append(f"handshake {actual!r} != erwartet {tuple(scenario.expect_handshake)!r}")
        if self.ack_features_present != scenario.expect_ack_features:
            problems.append(
                f"ack.features_present={self.ack_features_present} "
                f"!= erwartet {scenario.expect_ack_features}"
            )
        recorded = set(self.received_kinds())
        for expected in scenario.expect_received:
            if expected not in recorded:
                problems.append(f"empfangener Typ fehlt: {expected!r}")
        if problems:
            raise ScenarioError(f"Szenario {scenario.name!r}: " + "; ".join(problems))

    # ── Mitschnitt-Abfragen ───────────────────────────────────────────────
    def received_kinds(self, plane: Plane | None = None) -> list[str]:
        """Alle empfangenen Typen in Reihenfolge (optional nach Ebene gefiltert)."""
        return [ev.kind for ev in self.events if plane is None or ev.plane is plane]

    def received_count(self, kind: str) -> int:
        """Wie oft ein Typ empfangen wurde."""
        return sum(1 for ev in self.events if ev.kind == kind)

    def transcript(self) -> list[str]:
        """Die Ereignisliste als menschlich lesbare Zeilen (Mitschnitt)."""
        return [ev.transcript_line() for ev in self.events]

    def mic_frames_sent(self) -> list[bytes]:
        """Kopien der gesendeten Mic-Rohframes (für L2-Asserts)."""
        return [sent.frame for sent in self.sent_mic]
