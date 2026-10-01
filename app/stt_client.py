"""Wyoming-STT-Client (P3.T0, `PLAN.md:441`) – reine Client-Logik, kein `wyoming`-Paket.

Auftrag: ``AsyncTcpClient`` → ``Transcribe(language=de)`` →
``AudioStart(rate=16000,width=2,channels=1)`` → ``AudioChunk``* →
``AudioStop`` → ``Transcript``.

**Framing – selbst implementiert (JSON-Zeilen, kein Binär).**  Der
Referenz-Dienst spricht ``wyoming`` 2.4.3 (STATE §3/P0.T3), dessen
`event.py` folgendes Format schreibt/liest:

* Event-Header = **eine JSON-Zeile** (``\\n``-terminiert) mit
  ``type``, **``version``** und – nur wenn vorhanden – ``data_length``
  und ``payload_length``.
* direkt nach der JSON-Zeile: ``data_length`` Bytes **JSON-Daten**
  (UTF-8, ``ensure_ascii=False``), danach ``payload_length`` Bytes
  **Roh-Payload** (z. B. PCM).
* Beim Schreiben wird ``data`` **weggelassen**, wenn es leer ist
  (``data_length`` fehlt dann); ebenso entfällt ``payload_length`` ohne
  Payload.
* Beim Lesen werden die Datenblöcke **in** ein ggf. schon inline
  vorhandenes ``data``-Objekt gemerged; eine unbekannte ``version`` wird
  ignoriert.

Das alte Binär-Framing (``[1-Byte-Type][uint32 BE Length][Payload]``) wird
**nicht** gesprochen (P0.T3-Probe: stille Verbindung/Timeout).

**Konstanten – belegt, nicht erfunden** (alle aus `app.config`):

* Rate/Breite/Kanäle: ``audio_mic_rate`` 16000 · ``audio_width`` 2 ·
  ``audio_channels`` 1 (Mic-Pfad, 16 kHz S16_LE mono, P0.T3/K2/E28).
* Chunk-Größe: ``oww_chunk_bytes`` 2560 B = 80 ms @ 16 kHz (Mic-Chunk,
  P0.T6/E28) – derselbe Wert, den der Wake-Pfad nutzt.
* Host/Port: ``whisper_host`` 127.0.0.1 · ``whisper_port`` 10300.
* Sprache: ``whisper_language`` ``de``.
* **Timeout:** In `app/config.py` existiert **kein** STT-/Request-Timeout-
  Key (geprüft über alle Felder).  Deshalb der **benannte** Default
  ``STT_TIMEOUT_SECONDS = 2 · turn_hard_cap_seconds`` (15,0 s) = **30,0 s**
  – dieselbe Größenordnung wie die expliziten ``jev_timeout`` 8 s /
  ``deepseek_timeout`` 12 s, aber großzügig genug für Laden+Inferenz eines
  ``base``-int8-Whisper auf dem knappen `.123` (E21/E32).  Pro Client
  überschreibbar.
* Wyoming-``version``: ``WYOMING_VERSION`` = **2.4.3** (STATE §3/P0.T3,
  live im Piper-Container verifiziert).  Der Feldwert ist reine
  Metadaten – die Gegenseite ignoriert ihn beim Lesen – wird aber
  geschrieben, um exakt dem beobachteten Header zu entsprechen.

**Nicht** hier: TTS (`app/tts_client.py`, P3.T1), Fixtures (P3.T2) und
Tests (P3.T3).  Dieses Modul importiert bewusst **nichts** aus einer
Pipeline o. Ä.; nur stdlib, `app.config` und `app.logger`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping, Optional, Union

from app.config import settings
from app.logger import get_logger

__all__ = [
    "SttClientError",
    "SttTimeoutError",
    "WyomingProtocolError",
    "WyomingEvent",
    "AsyncTcpClient",
    "SttClient",
    "transcribe",
    "WYOMING_VERSION",
    "STT_HOST",
    "STT_PORT",
    "STT_LANGUAGE",
    "STT_RATE",
    "STT_WIDTH",
    "STT_CHANNELS",
    "STT_CHUNK_BYTES",
    "STT_TIMEOUT_SECONDS",
    "EVENT_TRANSCRIBE",
    "EVENT_AUDIO_START",
    "EVENT_AUDIO_CHUNK",
    "EVENT_AUDIO_STOP",
    "EVENT_TRANSCRIPT",
    "EVENT_ERROR",
]

_LOG: Final = get_logger("stt_client")

# ── Wyoming-Event-Typen (asr/audio, `wyoming/asr.py` + `wyoming/audio.py`) ──
#: STT-Anfrage: `{"data":{"language":"de"}}`, danach Audio folgt.
EVENT_TRANSCRIBE: Final[str] = "transcribe"
#: Beginn eines Audio-Stroms; `data` trägt `rate`/`width`/`channels`.
EVENT_AUDIO_START: Final[str] = "audio-start"
#: PCM-Stück; `data` trägt `rate`/`width`/`channels`, `payload` = Rohbytes.
EVENT_AUDIO_CHUNK: Final[str] = "audio-chunk"
#: Ende des Audio-Stroms.
EVENT_AUDIO_STOP: Final[str] = "audio-stop"
#: Antwort des ASR: `data.text`.
EVENT_TRANSCRIPT: Final[str] = "transcript"
#: Fehler-Event des Wyoming-Servers (`wyoming/error.py`), `data.text`/`data.code`.
EVENT_ERROR: Final[str] = "error"

#: Wyoming-Protokoll-/Distribution-Version (STATE §3/P0.T3: live verifiziert).
WYOMING_VERSION: Final[str] = "2.4.3"

# ── Verbindungs- und Audio-Parameter (alle aus `app.config`, P0.T3/§4) ─────
STT_HOST: Final[str] = settings.whisper_host
STT_PORT: Final[int] = settings.whisper_port
STT_LANGUAGE: Final[str] = settings.whisper_language
STT_RATE: Final[int] = settings.audio_mic_rate
STT_WIDTH: Final[int] = settings.audio_width
STT_CHANNELS: Final[int] = settings.audio_channels
STT_CHUNK_BYTES: Final[int] = settings.oww_chunk_bytes
#: Benannter Default (kein Config-Key vorhanden): 2 × Turn-Hard-Cap (15 s).
STT_TIMEOUT_SECONDS: Final[float] = max(30.0, 2.0 * settings.turn_hard_cap_seconds)

#: Bytes-artiger PCM-Eingang.
_BytesLike = Union[bytes, bytearray, memoryview]


# ── Fehler ────────────────────────────────────────────────────────────────
class SttClientError(Exception):
    """Basisklasse: Verbindungs-/Clientfehler des STT-Clients."""


class SttTimeoutError(SttClientError):
    """Timeout – die Gegenstelle hat nicht (rechtzeitig) geantwortet."""


class WyomingProtocolError(SttClientError):
    """Verletzung des Wyoming-Framings/Event-Vertrags (definiert statt raten)."""


# ── Event-Modell + Framing ────────────────────────────────────────────────
@dataclass(frozen=True)
class WyomingEvent:
    """Ein Wyoming-Event: ``type``, ``data`` (JSON) und optionaler ``payload``."""

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    payload: Optional[bytes] = None


def encode_event(event: WyomingEvent, *, version: str = WYOMING_VERSION) -> bytes:
    """Event exakt wie ``wyoming.event.async_write_event`` kodieren.

    Reihenfolge der Header-Keys (JSON-Insertion-Order, byte-faithful):
    ``type``, ``version``, danach optional ``data_length`` und
    ``payload_length``.  Es folgen unmittelbar die JSON-Daten und die
    Roh-Payload.
    """
    if not isinstance(event.type, str) or not event.type:
        raise WyomingProtocolError("Event ohne nicht-leeren type")
    if not isinstance(version, str) or not version:
        raise WyomingProtocolError("Event ohne nicht-leere version")

    event_dict: dict[str, Any] = {"type": event.type, "data": dict(event.data or {})}
    event_dict["version"] = version

    data = event_dict.pop("data", None)
    data_bytes: Optional[bytes] = None
    if data:
        data_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        event_dict["data_length"] = len(data_bytes)

    payload = event.payload
    if payload:
        event_dict["payload_length"] = len(payload)

    out = json.dumps(event_dict, ensure_ascii=False).encode("utf-8") + b"\n"
    if data_bytes:
        out += data_bytes
    if payload:
        out += bytes(payload)
    return out


def _iter_pcm_chunks(
    source: Union[_BytesLike, Iterable[_BytesLike]],
    chunk_bytes: int,
) -> Iterable[bytes]:
    """PCM in exakt ``chunk_bytes`` große Stücke schneiden (letztes darf kürzer sein)."""
    if not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
        raise SttClientError(f"chunk_bytes muss positiv sein, ist {chunk_bytes!r}")

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        for offset in range(0, len(data), chunk_bytes):
            yield data[offset : offset + chunk_bytes]
        return

    buffer = bytearray()
    for piece in source:
        if not isinstance(piece, (bytes, bytearray, memoryview)):
            raise SttClientError(
                f"PCM-Stück muss bytes-artig sein, ist {type(piece).__name__}"
            )
        buffer += bytes(piece)
        while len(buffer) >= chunk_bytes:
            yield bytes(buffer[:chunk_bytes])
            del buffer[:chunk_bytes]
    if buffer:
        yield bytes(buffer)


# ── AsyncTcpClient (asyncio-Streams, JSON-Zeilen-Framing) ──────────────────
class AsyncTcpClient:
    """Asynchroner TCP-Client mit selbst implementiertem Wyoming-Framing.

    Basisklasse für `SttClient` (diese Datei) und – später – den TTS-Pfad
    (P3.T1).  Benutzt ausschließlich ``asyncio.open_connection``; **kein**
    ``websockets`` und **kein** ``wyoming``-Paket.
    """

    def __init__(
        self,
        host: str = STT_HOST,
        port: int = STT_PORT,
        *,
        timeout: float = STT_TIMEOUT_SECONDS,
        connect_timeout: Optional[float] = None,
        version: str = WYOMING_VERSION,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.connect_timeout = (
            float(connect_timeout) if connect_timeout is not None else self.timeout
        )
        self.version = version
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    # ── Lebenszyklus ───────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        """True, wenn ein Writer offen ist."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Verbindung aufbauen (idempotent – bereits verbunden ⇒ no-op)."""
        if self.connected:
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                self.connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise SttTimeoutError(
                f"Verbindung zu {self.host}:{self.port} nach "
                f"{self.connect_timeout:g}s nicht möglich"
            ) from exc
        except OSError as exc:
            raise SttClientError(
                f"Verbindung zu {self.host}:{self.port} fehlgeschlagen: {exc}"
            ) from exc
        self._reader, self._writer = reader, writer
        _LOG.debug("STT verbunden mit %s:%s", self.host, self.port)

    async def aclose(self) -> None:
        """Verbindung schließen – **idempotent** (mehrfach aufrufbar, nie Fehler).

        Schließt den Writer und bricht damit laufende Reads ab (der Stream
        erhält EOF).  Der zweite Aufruf findet keinen Writer mehr und kehrt
        sofort zurück.
        """
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is None:
            return
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - Best-effort-Close.
            pass
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        _LOG.debug("STT-Verbindung geschlossen")

    async def __aenter__(self) -> AsyncTcpClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ── Schreiben ──────────────────────────────────────────────────────
    async def send_event(self, event: WyomingEvent) -> None:
        """Ein Event (JSON-Zeile + optionale Daten + Payload) senden."""
        writer = self._writer
        if writer is None or writer.is_closing():
            raise SttClientError("Nicht verbunden – send_event ohne offene Verbindung")
        try:
            writer.write(encode_event(event, version=self.version))
            await writer.drain()
        except (OSError, ConnectionError) as exc:
            raise SttClientError(f"Verbindung beim Senden verloren: {exc}") from exc

    async def send(
        self,
        event_type: str,
        data: Optional[Mapping[str, Any]] = None,
        payload: Optional[bytes] = None,
    ) -> None:
        """Kurzform: Event aus Typ/Daten/Payload erzeugen und senden."""
        await self.send_event(WyomingEvent(event_type, dict(data or {}), payload))

    # ── Lesen ──────────────────────────────────────────────────────────
    async def read_event(self) -> WyomingEvent:
        """Genau ein Event lesen (JSON-Zeile, dann Daten, dann Payload).

        Fehler werden **definiert** geworfen: EOF/Verbindungsabbruch ⇒
        `SttClientError`, ungültige JSON-Zeile/Typ ⇒ `WyomingProtocolError`.
        """
        reader = self._reader
        if reader is None:
            raise SttClientError("Nicht verbunden – read_event ohne offene Verbindung")

        try:
            line = await reader.readline()
        except (OSError, ConnectionError) as exc:
            raise SttClientError(f"Verbindung beim Lesen verloren: {exc}") from exc
        if not line:
            raise SttClientError("Verbindung geschlossen (EOF) vor einem Event")
        if not line.endswith(b"\n"):
            raise WyomingProtocolError("JSON-Zeile ohne \\n-Terminator")

        try:
            header = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WyomingProtocolError(f"Ungültige JSON-Zeile: {exc}") from exc
        if not isinstance(header, dict):
            raise WyomingProtocolError("Event-Header ist kein JSON-Objekt")

        event_type = header.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise WyomingProtocolError(f"Event ohne gültigen type: {header!r}")

        data = header.get("data")
        if not isinstance(data, dict):
            data = {}

        data_length = header.get("data_length")
        if data_length:
            raw_data = await self._read_exactly(
                data_length, "Datenblock"
            )
            try:
                parsed = json.loads(raw_data)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise WyomingProtocolError(
                    f"Ungültiger Datenblock im Event {event_type!r}: {exc}"
                ) from exc
            if isinstance(parsed, dict):
                data.update(parsed)

        payload: Optional[bytes] = None
        payload_length = header.get("payload_length")
        if payload_length:
            payload = await self._read_exactly(payload_length, "Payload")

        return WyomingEvent(type=event_type, data=data, payload=payload)

    async def _read_exactly(self, count: int, what: str) -> bytes:
        """``readexactly`` mit definiertem Fehler bei abgeschnittener Verbindung."""
        assert self._reader is not None
        try:
            return await self._reader.readexactly(int(count))
        except asyncio.IncompleteReadError as exc:
            raise SttClientError(
                f"{what} unvollständig – {exc.partial!r} statt {count} Byte"
            ) from exc
        except (OSError, ConnectionError) as exc:
            raise SttClientError(f"Verbindung beim Lesen des {what}s verloren: {exc}") from exc


# ── Protokoll-spezifischer STT-Client ─────────────────────────────────────
class SttClient:
    """Führt die Wyoming-STT-Sequenz gegen einen Whisper-Server aus.

    Ablauf: ``transcribe`` → ``audio-start`` → ``audio-chunk``* →
    ``audio-stop`` → ``transcript``; Rückgabe ist ``data.text``.
    """

    def __init__(
        self,
        *,
        host: str = STT_HOST,
        port: int = STT_PORT,
        language: Optional[str] = STT_LANGUAGE,
        timeout: float = STT_TIMEOUT_SECONDS,
        connect_timeout: Optional[float] = None,
        chunk_bytes: int = STT_CHUNK_BYTES,
        rate: int = STT_RATE,
        width: int = STT_WIDTH,
        channels: int = STT_CHANNELS,
        client: Optional[AsyncTcpClient] = None,
    ) -> None:
        self.language = language
        self.timeout = float(timeout)
        self.chunk_bytes = int(chunk_bytes)
        self.rate = int(rate)
        self.width = int(width)
        self.channels = int(channels)
        self._client = client or AsyncTcpClient(
            host,
            port,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )

    @property
    def client(self) -> AsyncTcpClient:
        """Der zugrunde liegende `AsyncTcpClient` (für Tests/Diagnose)."""
        return self._client

    async def aclose(self) -> None:
        """Client schließen – idempotent (delegiert an `AsyncTcpClient.aclose`)."""
        await self._client.aclose()

    async def transcribe(
        self,
        audio: Union[_BytesLike, Iterable[_BytesLike]],
        *,
        language: Optional[str] = None,
    ) -> str:
        """PCM (16 kHz/S16_LE/mono) transkribieren und den Text zurückgeben.

        ``audio`` ist ein Byte-Block **oder** ein Iterable von Byte-Stücken;
        in beiden Fällen wird auf `chunk_bytes` normiert.  Nach der Sequenz
        wird die Verbindung in jedem Fall geschlossen; ein Überschreiten von
        `timeout` ergibt `SttTimeoutError`.
        """
        effective_language = self.language if language is None else language
        try:
            return await asyncio.wait_for(
                self._exchange(audio, effective_language), self.timeout
            )
        except asyncio.TimeoutError:
            raise SttTimeoutError(
                f"STT-Timeout nach {self.timeout:g}s (Whisper antwortete nicht)"
            ) from None
        finally:
            await self._client.aclose()

    async def _exchange(
        self, audio: Union[_BytesLike, Iterable[_BytesLike]], language: Optional[str]
    ) -> str:
        client = self._client
        await client.connect()

        if language:
            await client.send(EVENT_TRANSCRIBE, {"language": language})
        else:
            await client.send(EVENT_TRANSCRIBE)

        audio_format: dict[str, Any] = {
            "rate": self.rate,
            "width": self.width,
            "channels": self.channels,
        }
        await client.send(EVENT_AUDIO_START, audio_format)

        chunk_count = 0
        for chunk in _iter_pcm_chunks(audio, self.chunk_bytes):
            await client.send(EVENT_AUDIO_CHUNK, audio_format, payload=chunk)
            chunk_count += 1
        await client.send(EVENT_AUDIO_STOP)
        _LOG.debug(
            "STT: %d Audio-Chunk(s) à %d B gesendet, warte auf transcript",
            chunk_count,
            self.chunk_bytes,
        )

        event = await client.read_event()
        if event.type == EVENT_TRANSCRIPT:
            text = event.data.get("text")
            if not isinstance(text, str):
                raise WyomingProtocolError(
                    f"transcript ohne data.text: {dict(event.data)!r}"
                )
            return text
        if event.type == EVENT_ERROR:
            detail = event.data.get("text") or event.data.get("code") or event.data
            raise SttClientError(f"STT-Server meldet error: {detail!r}")
        raise WyomingProtocolError(
            f"Unerwartetes Event {event.type!r} – erwartet {EVENT_TRANSCRIPT!r}"
        )


async def transcribe(
    audio: Union[_BytesLike, Iterable[_BytesLike]],
    *,
    host: str = STT_HOST,
    port: int = STT_PORT,
    language: Optional[str] = STT_LANGUAGE,
    timeout: float = STT_TIMEOUT_SECONDS,
    chunk_bytes: int = STT_CHUNK_BYTES,
) -> str:
    """Einmalige Transkription (Verbindung aufbauen, sequenzieren, schließen)."""
    client = SttClient(
        host=host,
        port=port,
        language=language,
        timeout=timeout,
        chunk_bytes=chunk_bytes,
    )
    return await client.transcribe(audio)
