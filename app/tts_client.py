"""Wyoming-TTS-Client (P3.T1, `PLAN.md:442`) – reine Client-Logik, kein `wyoming`-Paket.

Auftrag (wörtlich): ``Synthesize(voice=SynthesizeVoice(PIPER_VOICE))`` →
``AudioStart`` (**Rate!**) → ``AudioChunk``* → ``AudioStop``; als
**async-Generator ``(rate, pcm)``**.

**Wiederverwendung statt Duplikat:** Das JSON-Zeilen-Framing, das Event-Modell
(``WyomingEvent``), ``encode_event`` und der ``AsyncTcpClient`` stammen **1:1**
aus ``app/stt_client.py`` (P3.T0).  Dieses Modul dupliziert **kein** Framing;
es importiert die Bausteine und implementiert nur die TTS-Sequenz.  Der
zugrunde liegende ``AsyncTcpClient`` spricht das in P0.T3 live verifizierte
``wyoming`` 2.4.3 (JSON-Zeile + ``data_length`` JSON-Block + ``payload_length``
Roh-PCM).

**Konstanten – belegt, nicht erfunden** (alle aus `app.config`):

* Voice: ``piper_voice`` ``de_DE-thorsten-high`` (P0.T3: die im Container
  konfigurierte Stimme).
* Host/Port: ``piper_host`` 127.0.0.1 · ``piper_port`` 10200 (P0.T3: Port
  erreichbar).  Die **Live-Verifikation** dieses Schritts lief gegen
  ``10.0.0.10:10200`` (Host explizit übergeben), wie im Auftrag gefordert.
* S16_LE/mono-Fallback: ``audio_width`` 2 · ``audio_channels`` 1.  Diese Werte
  werden nur benutzt, wenn das ``audio-start`` sie **nicht** mitsendet; live
  sendet Piper ``width=2``/``channels=1`` (P0.T3).
* **Timeout:** In `app/config.py` existiert **kein** TTS-/Request-Timeout-Key
  (alle Felder geprüft, analog P3.T0).  Deshalb derselbe **benannte** Default
  wie beim STT-Client: ``TTS_TIMEOUT_SECONDS = max(30.0, 2 · turn_hard_cap_seconds)``
  = **30,0 s**; pro Client überschreibbar.  Der Timeout gilt **pro Event-Read**
  (streaming-freundlich), nicht als Sammel-Timeout über den ganzen Strom.
* Wyoming-``version``: ``WYOMING_VERSION`` **2.4.3** (STATE §3/P0.T3).

**Die Rate ist NICHT hart verdrahtet.**  Sie kommt ausschließlich aus dem
``audio-start``-Event (verifiziert: **22050 Hz** für ``de_DE-thorsten-high``,
16 kHz bei den ``*-low``-Stimmen) und wird an den Aufrufer weitergereicht.  Der
Speaker-Pfad (`app/audio_bridge.py`, P2.T2) resampelt 22050→48000 und ruft
dafür ``set_input_rate(rate)`` mit genau dem hier gelieferten Wert.

**Generator-Semantik ``(rate, pcm)`` – hier verbindlich festgelegt:**
Jedes Element ist ein Tupel ``(rate: int, pcm: bytes)``.  Das **erste** Element
ist die **Raten-Ankündigung** ``(rate, b"")`` (leerer PCM), damit der Aufrufer
die Rate erfährt, **bevor** der erste Ton anliegt.  Danach folgt **ein Element
pro ``audio-chunk``** ``(rate, <PCM-Stück>)``; die Rate ist über den ganzen
Strom konstant und wird bei jedem Element wiederholt (die Signatur ist
vorgegeben).  Der Strom endet **ohne** eigenes End-Element, sobald der Server
``audio-stop`` sendet.  Die Gesamt-PCM-Länge ist die Summe aller ``pcm``-
Teilstücke (das leere Ankündigungs-Element trägt nichts bei).

**Streaming, nicht Puffern:** Jeder ``audio-chunk`` wird sofort weitergereicht;
das Modul sammelt die PCM **nicht** im RAM.

**Nicht** hier: Fixture-Erzeugung (`tools/gen_fixture.py`, P3.T2) und Tests
(`tests/test_stt_tts.py`, P3.T3).  Dieses Modul importiert bewusst **nichts**
aus einer Pipeline o. Ä.; nur stdlib, `app.config`, `app.logger` und
`app.stt_client`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Final, Optional, Union

from app.config import settings
from app.logger import get_logger
from app.stt_client import (
    EVENT_AUDIO_CHUNK,
    EVENT_AUDIO_START,
    EVENT_AUDIO_STOP,
    EVENT_ERROR,
    WYOMING_VERSION,
    AsyncTcpClient,
    SttClientError,
    SttTimeoutError,
    WyomingEvent,
    WyomingProtocolError,
)

__all__ = [
    "TtsClientError",
    "TtsTimeoutError",
    "WyomingProtocolError",
    "AudioFormat",
    "SynthesizeVoice",
    "build_synthesize_data",
    "TtsClient",
    "synthesize",
    "WYOMING_VERSION",
    "TTS_HOST",
    "TTS_PORT",
    "PIPER_VOICE",
    "TTS_WIDTH",
    "TTS_CHANNELS",
    "TTS_TIMEOUT_SECONDS",
    "EVENT_SYNTHESIZE",
    "EVENT_AUDIO_START",
    "EVENT_AUDIO_CHUNK",
    "EVENT_AUDIO_STOP",
    "EVENT_ERROR",
]

_LOG: Final = get_logger("tts_client")

# ── Wyoming-Event-Typen (tts/audio, `wyoming/tts.py` + `wyoming/audio.py`) ──
#: TTS-Anfrage: `{"data":{"text":…,"voice":{"name":…}}}` (Text in `data.text`, P0.T3).
EVENT_SYNTHESIZE: Final[str] = "synthesize"
# (audio-start/-chunk/-stop/error werden aus `app.stt_client` wiederverwendet.)

# ── Verbindungs- und Audio-Parameter (alle aus `app.config`, P0.T3/§4) ─────
TTS_HOST: Final[str] = settings.piper_host
TTS_PORT: Final[int] = settings.piper_port
PIPER_VOICE: Final[str] = settings.piper_voice
#: S16_LE/mono – nur Fallback, wenn `audio-start` die Felder nicht mitsendet.
TTS_WIDTH: Final[int] = settings.audio_width
TTS_CHANNELS: Final[int] = settings.audio_channels
#: Benannter Default (kein Config-Key vorhanden): 2 × Turn-Hard-Cap (15 s).
TTS_TIMEOUT_SECONDS: Final[float] = max(30.0, 2.0 * settings.turn_hard_cap_seconds)


# ── Fehler ────────────────────────────────────────────────────────────────
class TtsClientError(SttClientError):
    """Basisklasse: Verbindungs-/Clientfehler des TTS-Clients.

    Erbt von `SttClientError`, damit gemeinsame Framing-/Transport-Fehler
    beider Wyoming-Clients unter einer Wurzel fangbar bleiben.
    """


class TtsTimeoutError(TtsClientError, SttTimeoutError):
    """Timeout – die Gegenstelle hat nicht (rechtzeitig) geantwortet."""


# ── Voice + Audio-Format ──────────────────────────────────────────────────
@dataclass(frozen=True)
class SynthesizeVoice:
    """Stimme einer ``synthesize``-Anfrage (Wyoming ``SynthesizeVoice``).

    ``name`` ist die Piper-Voice (z. B. ``de_DE-thorsten-high``); ``language``
    und ``speaker`` sind optional und werden nur mitgesendet, wenn gesetzt.
    """

    name: str
    language: Optional[str] = None
    speaker: Optional[str] = None

    def to_data(self) -> dict[str, Any]:
        """Als JSON-Datenobjekt (leere Felder werden weggelassen)."""
        data: dict[str, Any] = {"name": self.name}
        if self.language is not None:
            data["language"] = self.language
        if self.speaker is not None:
            data["speaker"] = self.speaker
        return data


@dataclass(frozen=True)
class AudioFormat:
    """Aus dem ``audio-start`` gelesenes Audio-Format (Rate ist **nicht** fix)."""

    rate: int
    width: int = TTS_WIDTH
    channels: int = TTS_CHANNELS


_VoiceLike = Union[str, SynthesizeVoice, None]


def _coerce_voice(voice: _VoiceLike) -> SynthesizeVoice:
    """Voice-Eingabe (``str``/``SynthesizeVoice``/``None``) normalisieren."""
    if voice is None:
        voice = PIPER_VOICE
    if isinstance(voice, SynthesizeVoice):
        if not voice.name or not voice.name.strip():
            raise TtsClientError("SynthesizeVoice.name darf nicht leer sein")
        return voice
    if isinstance(voice, str):
        if not voice.strip():
            raise TtsClientError("Voice-Name darf nicht leer sein")
        return SynthesizeVoice(voice.strip())
    raise TtsClientError(
        f"Voice muss str/SynthesizeVoice/None sein, ist {type(voice).__name__}"
    )


def build_synthesize_data(
    text: str, voice: _VoiceLike = PIPER_VOICE
) -> dict[str, Any]:
    """``data`` einer ``synthesize``-Anfrage bauen.

    Form (P0.T3 verifiziert): ``{"text": <str>, "voice": {"name": <str>}}`` –
    der Text liegt in **``data.text``**, die Voice als verschachteltes Objekt.
    """
    if not isinstance(text, str):
        raise TtsClientError(
            f"synthesize-Text muss str sein, ist {type(text).__name__}"
        )
    if not text.strip():
        raise TtsClientError("synthesize-Text darf nicht leer sein")
    return {"text": text, "voice": _coerce_voice(voice).to_data()}


def _error_detail(event: WyomingEvent) -> Any:
    """Kurzbezeichnung eines ``error``-Events (Text, sonst Code, sonst Daten)."""
    return event.data.get("text") or event.data.get("code") or dict(event.data)


def _parse_audio_start(event: WyomingEvent) -> AudioFormat:
    """``audio-start``-Event in ein `AudioFormat` überführen (Rate Pflicht)."""
    rate = event.data.get("rate")
    if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0:
        raise WyomingProtocolError(
            f"audio-start ohne gültige data.rate: {dict(event.data)!r}"
        )
    width = event.data.get("width", TTS_WIDTH)
    channels = event.data.get("channels", TTS_CHANNELS)
    if not isinstance(width, int) or width <= 0:
        raise WyomingProtocolError(f"audio-start mit ungültiger width: {width!r}")
    if not isinstance(channels, int) or channels <= 0:
        raise WyomingProtocolError(f"audio-start mit ungültigen channels: {channels!r}")
    return AudioFormat(rate=rate, width=width, channels=channels)


# ── Protokoll-spezifischer TTS-Client ─────────────────────────────────────
class TtsClient:
    """Führt die Wyoming-TTS-Sequenz gegen einen Piper-Server aus.

    Ablauf: ``synthesize`` → ``audio-start`` (**Rate**) → ``audio-chunk``* →
    ``audio-stop``.  Rückgabe ist ein Async-Generator ``(rate, pcm)`` (s. o.).
    """

    def __init__(
        self,
        *,
        host: str = TTS_HOST,
        port: int = TTS_PORT,
        voice: _VoiceLike = PIPER_VOICE,
        timeout: float = TTS_TIMEOUT_SECONDS,
        connect_timeout: Optional[float] = None,
        client: Optional[AsyncTcpClient] = None,
    ) -> None:
        self.voice = _coerce_voice(voice) if voice is not None else None
        self.timeout = float(timeout)
        self._client = client or AsyncTcpClient(
            host,
            port,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )
        self._format: Optional[AudioFormat] = None

    @property
    def client(self) -> AsyncTcpClient:
        """Der zugrunde liegende `AsyncTcpClient` (für Tests/Diagnose)."""
        return self._client

    @property
    def format(self) -> Optional[AudioFormat]:
        """Zuletzt im ``audio-start`` gesehenes Format (None vor dem Strom)."""
        return self._format

    async def aclose(self) -> None:
        """Client schließen – idempotent (delegiert an `AsyncTcpClient.aclose`)."""
        await self._client.aclose()

    # ── Öffentlicher Einstieg ──────────────────────────────────────────
    async def synthesize(
        self, text: str, *, voice: _VoiceLike = None
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Text synthetisieren und ``(rate, pcm)`` streamen.

        Erstes Element = ``(rate, b"")`` (Raten-Ankündigung), dann ein Element
        pro PCM-Chunk.  Nach ``audio-stop`` endet der Generator; die Verbindung
        wird in **jedem** Fall geschlossen (auch bei Abbruch/Fehler/Timeout).
        """
        effective_voice = voice if voice is not None else self.voice
        if not isinstance(text, str) or not text.strip():
            raise TtsClientError("synthesize-Text muss ein nicht-leerer str sein")
        try:
            async for item in self._stream(text, effective_voice):
                yield item
        finally:
            await self._client.aclose()

    # ── Interne Sequenz ────────────────────────────────────────────────
    async def _stream(
        self, text: str, voice: _VoiceLike
    ) -> AsyncIterator[tuple[int, bytes]]:
        client = self._client
        try:
            await client.connect()
        except SttTimeoutError as exc:
            raise TtsTimeoutError(str(exc)) from exc
        except SttClientError as exc:
            raise TtsClientError(str(exc)) from exc

        try:
            await client.send(EVENT_SYNTHESIZE, build_synthesize_data(text, voice))
        except WyomingProtocolError:
            raise
        except SttClientError as exc:
            raise TtsClientError(str(exc)) from exc

        event = await self._read_event()
        if event.type == EVENT_ERROR:
            raise TtsClientError(f"TTS-Server meldet error: {_error_detail(event)!r}")
        if event.type != EVENT_AUDIO_START:
            raise WyomingProtocolError(
                f"Unerwartetes Event {event.type!r} – erwartet {EVENT_AUDIO_START!r}"
            )

        fmt = _parse_audio_start(event)
        self._format = fmt
        # Erstes Element: Rate bekannt geben, bevor der erste Ton anliegt.
        yield (fmt.rate, b"")

        while True:
            event = await self._read_event()
            if event.type == EVENT_AUDIO_CHUNK:
                if not isinstance(event.payload, (bytes, bytearray)):
                    raise WyomingProtocolError(
                        "audio-chunk ohne Byte-Payload"
                    )
                yield (fmt.rate, bytes(event.payload))
            elif event.type == EVENT_AUDIO_STOP:
                return
            elif event.type == EVENT_ERROR:
                raise TtsClientError(
                    f"TTS-Server meldet error: {_error_detail(event)!r}"
                )
            else:
                raise WyomingProtocolError(
                    f"Unerwartetes Event {event.type!r} während des Audio-Stroms"
                )

    async def _read_event(self) -> WyomingEvent:
        """Ein Event lesen – mit **Timeout pro Read** (definierte Fehler)."""
        try:
            return await asyncio.wait_for(self._client.read_event(), self.timeout)
        except asyncio.TimeoutError:
            raise TtsTimeoutError(
                f"TTS-Timeout nach {self.timeout:g}s (Piper antwortete nicht)"
            ) from None
        except WyomingProtocolError:
            raise
        except SttTimeoutError as exc:
            raise TtsTimeoutError(str(exc)) from exc
        except SttClientError as exc:
            raise TtsClientError(str(exc)) from exc


async def synthesize(
    text: str,
    *,
    host: str = TTS_HOST,
    port: int = TTS_PORT,
    voice: _VoiceLike = PIPER_VOICE,
    timeout: float = TTS_TIMEOUT_SECONDS,
) -> AsyncIterator[tuple[int, bytes]]:
    """Einmalige Synthese (Client aufbauen, streamen, in jedem Fall schließen)."""
    client = TtsClient(host=host, port=port, voice=voice, timeout=timeout)
    try:
        async for item in client.synthesize(text):
            yield item
    finally:
        await client.aclose()
