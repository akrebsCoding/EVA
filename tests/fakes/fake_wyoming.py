"""Deterministische Wyoming-STT-/TTS-Server für den L3-Testlayer (P9.T2, `PLAN.md:540`).

Die beiden Fakes sind **echte asyncio-TCP-Server** (`asyncio.start_server`) und
sprechen **genau** das JSON-Zeilen-Framing, das die echten Clients
`app/stt_client.py` (P3.T0) und `app/tts_client.py` (P3.T1) erwarten.  Es gibt
hier **keine** eigene Framing-Implementierung: Schreiben und Lesen laufen über
die Bausteine aus `app.stt_client` — `WyomingEvent`, `encode_event`, die
Event-Konstanten und der `AsyncTcpClient` (Streams werden wie in P3.T3/E54 in
den Server-Reader/-Writer injiziert).  Damit kann keine Magic Number zwischen
Fake und Prüfling auseinanderdriften.

Was der Fake leistet (Auftrag `PLAN.md:540`):

* **STT-Server** — Protokoll `transcribe` → `audio-start` → `audio-chunk`* →
  `audio-stop`, Antwort `transcript` mit **konfigurierbarem Text** (Default aus
  `tests/fixtures/text/sample_text.txt`; Fallback auf die bestehende Datei
  `tests/fixtures/sample_text.txt`).
* **TTS-Server** — Protokoll `synthesize` → `audio-start` (**Rate aus dem
  Server**) → `audio-chunk`* → `audio-stop`, PCM **deterministisch** (feste
  Rate/Dauer/Ton, Ganzzahl-Rechteckwelle ohne `libm`, **kein Modell, ~0 RAM**).
* **Latenzinjektion** — konfigurierbar (`latency` vor der ersten Antwort,
  optional `chunk_latency` pro PCM-Stück).
* **Lifecycle** — `await start()` / `await stop()` (beide idempotent), **Port 0**
  (das OS vergibt) oder ein fester Port; sauberes Schließen akzeptierter
  Verbindungen inkl. `async with`.

**Kein echtes Piper/Whisper, kein `.123`** — die Server laufen lokal auf `.22`.
Marker-Konvention: die zugehörigen Tests tragen `component` (L1/L3) — nur
dieser Marker hebt die E36-Netzsperre für den Loopback-Verkehr auf.
"""

from __future__ import annotations

import asyncio
import sys
from array import array
from pathlib import Path
from typing import Any, Final, Optional

from app.stt_client import (
    EVENT_AUDIO_CHUNK,
    EVENT_AUDIO_START,
    EVENT_AUDIO_STOP,
    EVENT_ERROR,
    EVENT_TRANSCRIBE,
    EVENT_TRANSCRIPT,
    AsyncTcpClient,
    SttClientError,
    WyomingEvent,
)
from app.tts_client import EVENT_SYNTHESIZE

__all__ = [
    "FakeSttServer",
    "FakeTtsServer",
    "WyomingFakeError",
    "DEFAULT_STT_TEXT_PATH",
    "DEFAULT_TTS_RATE",
    "DEFAULT_TTS_DURATION",
    "DEFAULT_CHUNK_BYTES",
    "load_default_text",
    "tone_pcm",
]

# ── Fixture-Pfade + Defaults ────────────────────────────────────────────────
#: Projektwurzel/`tests/fixtures` — `tests/fakes/fake_wyoming.py` ⇒ `parents[2]`.
FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
#: Default-Textpfad laut `PLAN.md:540`.
DEFAULT_STT_TEXT_PATH: Final[Path] = FIXTURES_DIR / "text" / "sample_text.txt"
#: Bestehende Fixture aus P3.T2 — Fallback, falls das `text/`-Verzeichnis fehlt.
LEGACY_STT_TEXT_PATH: Final[Path] = FIXTURES_DIR / "sample_text.txt"

#: Piper-Rate `de_DE-thorsten-high` (P0.T3/STATE §3) — nur Default, überschreibbar.
DEFAULT_TTS_RATE: Final[int] = 22050
#: Kurze, deterministische Dauer (0,2 s) — schnell und trotzdem real.
DEFAULT_TTS_DURATION: Final[float] = 0.2
#: PCM-Stückgröße auf der Wire (entspricht der Speaker-Periodengröße, P2.T2).
DEFAULT_CHUNK_BYTES: Final[int] = 4096
#: Ton-Amplitude der deterministischen Rechteckwelle (< 32767, S16_LE).
DEFAULT_AMPLITUDE: Final[int] = 8000
#: Ungefähre Tonhöhe des Default-Signals (nur für die Periodenlänge).
DEFAULT_TONE_HZ: Final[int] = 440

_default_text_cache: Optional[str] = None


class WyomingFakeError(RuntimeError):
    """Programmierfehler/ungültige Konfiguration des Fake-Servers."""


# ── Deterministische PCM-Erzeugung (kein `libm`) ────────────────────────────
def tone_pcm(
    rate: int,
    duration: float,
    *,
    width: int = 2,
    channels: int = 1,
    amplitude: int = DEFAULT_AMPLITUDE,
    period_frames: Optional[int] = None,
) -> bytes:
    """Deterministische S16_LE-Rechteckwelle als Roh-PCM.

    Ganzzahlig gerechnet (kein `math.sin`) und damit auf jeder Plattform
    bytegleich — dieselbe Idee wie `tests/conftest.triangle_pcm`.  ``width``
    ist auf 2 Byte (S16_LE) festgelegt; andere Breiten sind für dieses Fixture
    nicht vorgesehen und werfen `WyomingFakeError`.
    """
    if width != 2:
        raise WyomingFakeError(f"width muss 2 (S16_LE) sein, ist {width!r}")
    if channels < 1:
        raise WyomingFakeError(f"channels muss ≥ 1 sein, ist {channels!r}")
    if rate <= 0 or duration <= 0:
        raise WyomingFakeError(f"rate/duration müssen positiv sein: {rate!r}/{duration!r}")

    frames = int(round(rate * duration))
    if frames < 1:
        frames = 1
    period = int(period_frames) if period_frames else max(1, int(round(rate / DEFAULT_TONE_HZ)))
    if period < 1:
        period = 1

    samples = array("h")
    for index in range(frames):
        value = amplitude if (index // period) % 2 == 0 else -amplitude
        for _ in range(channels):
            samples.append(value)
    if sys.byteorder != "little":  # pragma: no cover - x86/ARM sind little endian
        samples.byteswap()
    return samples.tobytes()


def load_default_text() -> str:
    """Default-Transkript aus der Fixture laden (gecacht, strikt nicht leer).

    Bevorzugt `tests/fixtures/text/sample_text.txt` (Auftrag `PLAN.md:540`),
    fällt auf `tests/fixtures/sample_text.txt` (P3.T2) zurück.  Fehlen beide,
    ist das ein harter Fehler — kein stiller Ersatztext.
    """
    global _default_text_cache
    if _default_text_cache is None:
        for path in (DEFAULT_STT_TEXT_PATH, LEGACY_STT_TEXT_PATH):
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if not text:
                    raise WyomingFakeError(f"Fixture-Text ist leer: {path}")
                _default_text_cache = text
                break
        else:
            raise FileNotFoundError(
                f"Kein Default-Text gefunden (versucht: {DEFAULT_STT_TEXT_PATH}, "
                f"{LEGACY_STT_TEXT_PATH})"
            )
    return _default_text_cache


# ── Server-seitige Verbindung (Framing aus `app.stt_client` wiederverwendet) ─
class _ServerConnection:
    """Dünner Wrapper um `AsyncTcpClient` für server-seitige Streams.

    Der `AsyncTcpClient` ist das **einzige** Framing (JSON-Zeile + Datenblock +
    Roh-Payload).  Wie in P3.T3 (E54) werden Reader/Writer eingesetzt, statt
    das Framing im Fake nachzubauen.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = AsyncTcpClient()
        client._reader = reader  # noqa: SLF001 - E54-Injektionsmuster (Server-Streams).
        client._writer = writer
        self._client = client

    async def read_event(self) -> WyomingEvent:
        """Ein Event vom Client lesen (echtes Wyoming-Framing)."""
        return await self._client.read_event()

    async def send_event(self, event: WyomingEvent) -> None:
        """Ein Event an den Client schreiben (echtes Wyoming-Framing)."""
        await self._client.send_event(event)

    async def close(self) -> None:
        """Verbindung schließen (`aclose` ist idempotent)."""
        await self._client.aclose()


# ── Gemeinsamer Lebenszyklus ────────────────────────────────────────────────
class _BaseWyomingServer:
    """Lifecycle-Basis beider Fakes: `start`/`stop`, Port 0, sauberes Schließen."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, latency: float = 0.0) -> None:
        if port < 0:
            raise WyomingFakeError(f"port muss ≥ 0 sein (0 = OS vergibt), ist {port!r}")
        self.host = host
        self.port = int(port)
        self.latency = float(latency)
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections: set[asyncio.StreamWriter] = set()
        #: Empfangene Event-Typen in Reihenfolge (Diagnose/Assertions).
        self.received_events: list[str] = []
        #: Aufgetretene (nicht-fatale) Handler-Fehler — sichtbar, nicht verschluckt.
        self.errors: list[str] = []

    @property
    def running(self) -> bool:
        """True, solange der Server lauscht."""
        return self._server is not None

    async def start(self) -> "_BaseWyomingServer":
        """Server starten (idempotent). Port 0 ⇒ das OS vergibt einen Port."""
        if self._server is not None:
            return self
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or ()
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        return self

    async def stop(self) -> None:
        """Server + offene Verbindungen schließen (idempotent)."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001 - Best-effort-Teardown.
                pass
        writers = list(self._connections)
        self._connections.clear()
        for writer in writers:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        for writer in writers:
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> "_BaseWyomingServer":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Pro Verbindung: Framing leiten, Fehler sammeln, immer sauber schließen."""
        self._connections.add(writer)
        conn = _ServerConnection(reader, writer)
        try:
            await self._serve(conn)
        except SttClientError as exc:
            # EOF/Verbindungsabbruch ist für einen Fake kein Programmierfehler.
            self.errors.append(f"Verbindung beendet: {exc}")
        except Exception as exc:  # noqa: BLE001 - Handler darf die Loop nicht sprengen.
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            self._connections.discard(writer)
            await conn.close()

    async def _sleep_latency(self) -> None:
        """Konfigurierte Latenz vor der ersten Antwort (falls > 0)."""
        if self.latency > 0:
            await asyncio.sleep(self.latency)

    async def _serve(self, conn: _ServerConnection) -> None:  # pragma: no cover - abstrakt
        raise NotImplementedError


# ── STT-Server ──────────────────────────────────────────────────────────────
class FakeSttServer(_BaseWyomingServer):
    """Deterministischer Wyoming-STT-Server (Antwort ist konfigurierbarer Text).

    Der Server wertet den **Audio-Inhalt nicht aus** (kein Whisper) — er liest
    die Sequenz `transcribe` → `audio-start` → `audio-chunk`* → `audio-stop`
    vollständig und antwortet mit `transcript{text}`.  ``text=None`` nutzt den
    Default aus `tests/fixtures/text/sample_text.txt` (`load_default_text`).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        text: Optional[str] = None,
        latency: float = 0.0,
    ) -> None:
        super().__init__(host=host, port=port, latency=latency)
        self._text = text
        self.audio_chunks = 0
        self.audio_bytes = 0
        self.last_language: Optional[str] = None
        self.last_format: Optional[dict[str, Any]] = None

    @property
    def text(self) -> str:
        """Der konfigurierte Text bzw. der Default aus der Fixture."""
        return self._text if self._text is not None else load_default_text()

    async def _serve(self, conn: _ServerConnection) -> None:
        event = await conn.read_event()
        self.received_events.append(event.type)
        if event.type != EVENT_TRANSCRIBE:
            await self._send_error(conn, f"erwartet {EVENT_TRANSCRIBE!r}, gelesen {event.type!r}")
            return
        language = event.data.get("language")
        if isinstance(language, str):
            self.last_language = language

        event = await conn.read_event()
        self.received_events.append(event.type)
        if event.type != EVENT_AUDIO_START:
            await self._send_error(conn, f"erwartet {EVENT_AUDIO_START!r}, gelesen {event.type!r}")
            return
        self.last_format = dict(event.data)

        chunks = 0
        total = 0
        while True:
            event = await conn.read_event()
            self.received_events.append(event.type)
            if event.type == EVENT_AUDIO_CHUNK:
                chunks += 1
                total += len(event.payload or b"")
            elif event.type == EVENT_AUDIO_STOP:
                break
            else:
                await self._send_error(
                    conn, f"unerwartetes Event {event.type!r} während des Audio-Stroms"
                )
                return

        self.audio_chunks += chunks
        self.audio_bytes += total
        await self._sleep_latency()
        await conn.send_event(WyomingEvent(EVENT_TRANSCRIPT, {"text": self.text}))

    async def _send_error(self, conn: _ServerConnection, message: str) -> None:
        """Protokollfehler sichtbar als Wyoming-`error`-Event melden."""
        self.errors.append(message)
        await conn.send_event(WyomingEvent(EVENT_ERROR, {"text": message}))


# ── TTS-Server ──────────────────────────────────────────────────────────────
class FakeTtsServer(_BaseWyomingServer):
    """Deterministischer Wyoming-TTS-Server (fester Ton, kein Modell, ~0 RAM).

    Antwortet auf `synthesize` mit `audio-start{rate,width,channels}` → einem
    `audio-chunk` je ``chunk_bytes`` → `audio-stop`.  Das PCM wird pro Anfrage
    frisch aus `tone_pcm` erzeugt (kein Puffer im RAM, kein Piper).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        rate: int = DEFAULT_TTS_RATE,
        duration: float = DEFAULT_TTS_DURATION,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        width: int = 2,
        channels: int = 1,
        amplitude: int = DEFAULT_AMPLITUDE,
        period_frames: Optional[int] = None,
        latency: float = 0.0,
        chunk_latency: float = 0.0,
    ) -> None:
        super().__init__(host=host, port=port, latency=latency)
        if rate <= 0:
            raise WyomingFakeError(f"rate muss positiv sein, ist {rate!r}")
        if duration <= 0:
            raise WyomingFakeError(f"duration muss positiv sein, ist {duration!r}")
        if chunk_bytes <= 0:
            raise WyomingFakeError(f"chunk_bytes muss positiv sein, ist {chunk_bytes!r}")
        self.rate = int(rate)
        self.duration = float(duration)
        self.chunk_bytes = int(chunk_bytes)
        self.width = int(width)
        self.channels = int(channels)
        self.amplitude = int(amplitude)
        self.period_frames = period_frames
        self.chunk_latency = float(chunk_latency)
        self.last_text: Optional[str] = None
        self.last_voice: Optional[dict[str, Any]] = None
        self.chunks_sent = 0
        self.pcm_bytes_sent = 0

    @property
    def frame_count(self) -> int:
        """Anzahl PCM-Frames des deterministischen Signals."""
        return max(1, int(round(self.rate * self.duration)))

    @property
    def pcm_bytes(self) -> int:
        """Erwartete Gesamt-PCM-Länge in Byte."""
        return self.frame_count * self.width * self.channels

    def build_pcm(self) -> bytes:
        """Das deterministische PCM-Signal (identisch bei gleicher Konfiguration)."""
        return tone_pcm(
            self.rate,
            self.duration,
            width=self.width,
            channels=self.channels,
            amplitude=self.amplitude,
            period_frames=self.period_frames,
        )

    async def _serve(self, conn: _ServerConnection) -> None:
        event = await conn.read_event()
        self.received_events.append(event.type)
        if event.type != EVENT_SYNTHESIZE:
            await self._send_error(conn, f"erwartet {EVENT_SYNTHESIZE!r}, gelesen {event.type!r}")
            return
        text = event.data.get("text")
        if isinstance(text, str):
            self.last_text = text
        voice = event.data.get("voice")
        if isinstance(voice, dict):
            self.last_voice = dict(voice)

        await self._sleep_latency()
        audio_format = {"rate": self.rate, "width": self.width, "channels": self.channels}
        await conn.send_event(WyomingEvent(EVENT_AUDIO_START, dict(audio_format)))

        pcm = self.build_pcm()
        for offset in range(0, len(pcm), self.chunk_bytes):
            piece = pcm[offset : offset + self.chunk_bytes]
            await conn.send_event(WyomingEvent(EVENT_AUDIO_CHUNK, dict(audio_format), payload=piece))
            self.chunks_sent += 1
            self.pcm_bytes_sent += len(piece)
            if self.chunk_latency > 0:
                await asyncio.sleep(self.chunk_latency)
        await conn.send_event(WyomingEvent(EVENT_AUDIO_STOP))

    async def _send_error(self, conn: _ServerConnection, message: str) -> None:
        """Protokollfehler sichtbar als Wyoming-`error`-Event melden."""
        self.errors.append(message)
        await conn.send_event(WyomingEvent(EVENT_ERROR, {"text": message}))
