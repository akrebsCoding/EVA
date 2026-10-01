"""STT-/TTS-Client-Tests (P3.T3, `PLAN.md` §7 → P3.T3, Layer **L0**).

Prüflinge sind `app/stt_client.py` (P3.T0) und `app/tts_client.py` (P3.T1).
Getestet wird gegen den **verifizierten Vertrag** aus `STATE.md` §3
(„STT-Client (P3.T0)" / „TTS-Client (P3.T1)") und `PLAN.md:441-444`, nicht
gegen eine Wunschfassung.

**Warum ein Fake-Wyoming-Server statt eines echten?**  Whisper ist auf `.123`
gestoppt (P0.T4) und Live-STT wird laut `PLAN.md:444` erst **nach P6**
freigeschaltet; ein echter Piper würde die L1/L0-Tests von `.22` aus an einen
externen Dienst binden.  Beide Clients sprechen aber ein socketbasiertes
JSON-Zeilen-Framing, also wird das Gegenüber **in-process** über ein
`socket.socketpair()` nachgebildet: zwei bereits verbundene Enden, die per
`asyncio.open_connection(sock=…)` zu Streams werden.  `socketpair()` ruft
**kein** `connect`/`getaddrinfo` auf und ist damit trotz der Netzsperre aus
`tests/conftest.py` (E36, §7.1 L0 „rein, kein Netz") erlaubt — es wird
bewusst **kein** Loopback-Port geöffnet.  Der Client ist bereits „verbunden",
sobald seine Streams gesetzt sind; `AsyncTcpClient.connect()` ist idempotent
und wird dadurch zum No-op.

Das Frame-Format kodiert/parst die Testseite **unabhängig** (eigene
Key-Reihenfolge `version` zuerst, eigener Reader) — so wird nicht die zu
prüfende Implementierung gegen sich selbst getestet.

Abdeckung der Pflichtfälle:

1. **STT-Sequenz** `transcribe{language:de}` → `audio-start(16000/2/1)` →
   `audio-chunk`* → `audio-stop`; Antwort `transcript` ⇒ Text korrekt, exakte
   Chunk-Grenzen → `test_stt_sends_expected_event_sequence`
2. **Transcript-Parsing** leer / Umlaute / Langtext → `test_stt_transcript_variants`
3. **TTS-Sequenz** `synthesize{text, voice}`; Antwort `audio-start(rate)` →
   `audio-chunk`* → `audio-stop`; Generator liefert erst `(rate, b"")`, dann je
   Chunk `(rate, pcm)`, Rate konstant → `test_tts_synthesize_sequence_and_generator`
4. **Fehlerfälle** Timeout (stummer Server) ⇒ `SttTimeoutError`/`TtsTimeoutError`;
   ungültige JSON-Zeile ⇒ `WyomingProtocolError`; `aclose()` idempotent
   (2× + nie verbunden) → `test_*timeout_*`, `test_*invalid_json_*`,
   `test_aclose_is_idempotent_twice_and_never_connected`
5. **Fixture** `sample_16k.wav` = 16 kHz/S16_LE/mono (Abnahme P3, `PLAN.md:437`)

**Live-Tests** (echter Whisper/Piper) tragen den Marker `live` **und** sind per
`EVA_LIVE=1` **standardmäßig geskippt** (`PLAN.md:444`: Live-STT erst nach
P6).  **Kein** Netz in den `unit`-Tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
import wave
from pathlib import Path

import pytest

from app.stt_client import (
    EVENT_AUDIO_CHUNK,
    EVENT_AUDIO_START,
    EVENT_AUDIO_STOP,
    EVENT_TRANSCRIPT,
    EVENT_TRANSCRIBE,
    STT_CHANNELS,
    STT_HOST,
    STT_PORT,
    STT_RATE,
    STT_TIMEOUT_SECONDS,
    STT_WIDTH,
    WYOMING_VERSION,
    AsyncTcpClient,
    SttClient,
    SttTimeoutError,
    WyomingProtocolError,
)
from app.tts_client import (
    EVENT_SYNTHESIZE,
    PIPER_VOICE,
    TTS_CHANNELS,
    TTS_HOST,
    TTS_PORT,
    TTS_WIDTH,
    AudioFormat,
    SynthesizeVoice,
    TtsClient,
    TtsClientError,
    TtsTimeoutError,
    build_synthesize_data,
)

#: Fixtures (P3.T2) — relativ zu dieser Datei, kein CWD-Zwang.
FIXTURE_DIR: Path = Path(__file__).resolve().parent / "fixtures"
FIXTURE_WAV: Path = FIXTURE_DIR / "sample_16k.wav"
FIXTURE_TEXT: Path = FIXTURE_DIR / "sample_text.txt"

#: Der Beispieltext, mit dem `sample_16k.wav` synthetisiert wurde (P3.T2).
SAMPLE_TEXT: str = "Schalte das Licht im Wohnzimmer ein"

#: Timeout für die Fehlerfall-Tests — kurz und deterministisch, **kein**
#: `time.sleep`; die Timeouts sind pro Client überschreibbar (`STATE.md` §3).
SHORT_TIMEOUT: float = 0.2

#: Live-Tests laufen nur mit explizitem Opt-in (`PLAN.md:444`).
LIVE_ENABLED: bool = os.environ.get("EVA_LIVE", "") == "1"
requires_live = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason=(
        "Live-Test (echter Wyoming-Server): nur mit EVA_LIVE=1; "
        "Live-STT wird erst nach P6 freigeschaltet (PLAN.md:444)"
    ),
)


# ── Unabhängiges Wyoming-Framing (Testseite) ─────────────────────────────
def _frame(
    event_type: str,
    data: dict | None = None,
    payload: bytes | None = None,
) -> bytes:
    """Event unabhängig kodieren — Key-Reihenfolge bewusst **anders** als der
    Prüfling (`version` zuerst), damit der Reader des Clients gegen ein
    fremdes, aber vertragskonformes Layout geprüft wird."""
    header: dict = {"version": WYOMING_VERSION, "type": event_type}
    body = bytearray()
    if data:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        header["data_length"] = len(encoded)
        body += encoded
    if payload:
        header["payload_length"] = len(payload)
        body += bytes(payload)
    return json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n" + bytes(body)


async def _read_event_from(reader: asyncio.StreamReader) -> tuple[str, dict, bytes | None, str]:
    """Ein Event unabhängig parsen: `(type, data, payload, version)`."""
    line = await reader.readline()
    header = json.loads(line)
    data = header.get("data") if isinstance(header.get("data"), dict) else {}
    data_length = header.get("data_length")
    if data_length:
        parsed = json.loads(await reader.readexactly(int(data_length)))
        if isinstance(parsed, dict):
            data = {**data, **parsed}
    payload: bytes | None = None
    payload_length = header.get("payload_length")
    if payload_length:
        payload = await reader.readexactly(int(payload_length))
    return header["type"], dict(data), payload, header.get("version", "")


class _FakeWyomingServer:
    """In-process Wyoming-Server über ein `socketpair` (kein Netz, L0)."""

    def __init__(self) -> None:
        self.received: list[tuple[str, dict, bytes | None, str]] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.client_reader: asyncio.StreamReader | None = None
        self.client_writer: asyncio.StreamWriter | None = None

    @classmethod
    async def start(cls) -> "_FakeWyomingServer":
        self = cls()
        server_sock, client_sock = socket.socketpair()
        # `sock=` umgeht `getaddrinfo`/`connect` ⇒ trotz Netzsperre (E36) L0.
        self._reader, self._writer = await asyncio.open_connection(sock=server_sock)
        self.client_reader, self.client_writer = await asyncio.open_connection(
            sock=client_sock
        )
        return self

    def attach(self, client: AsyncTcpClient) -> None:
        """Die bereits verbundenen Client-Streams an den Prüfling hängen.

        Bewusst privat: der Vertrag lässt einen `client=`-Injektionspunkt offen,
        aber keinen Stream-Injektionspunkt.  So bleibt `connect()` ein No-op
        (idempotent) und es läuft **kein** echter Socket-Connect.
        """
        client._reader = self.client_reader  # noqa: SLF001 - Testinjektion
        client._writer = self.client_writer  # noqa: SLF001 - Testinjektion

    async def read(self) -> tuple[str, dict, bytes | None, str]:
        assert self._reader is not None
        event = await _read_event_from(self._reader)
        self.received.append(event)
        return event

    async def send(
        self, event_type: str, data: dict | None = None, payload: bytes | None = None
    ) -> None:
        await self.send_raw(_frame(event_type, data, payload))

    async def send_raw(self, raw: bytes) -> None:
        assert self._writer is not None
        self._writer.write(bytes(raw))
        await self._writer.drain()

    async def aclose(self) -> None:
        for writer in (self._writer, self.client_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
        for writer in (self._writer, self.client_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    await writer.wait_closed()


@contextlib.asynccontextmanager
async def _fake_server():
    server = await _FakeWyomingServer.start()
    try:
        yield server
    finally:
        await server.aclose()


@contextlib.asynccontextmanager
async def _server_task(coro):
    """Startet die Server-Gegenstelle als Aufgabe und räumt sie garantiert ab."""
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.exception()  # holt die Ausnahme ab (kein „never retrieved")
        else:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _pcm(total_bytes: int) -> bytes:
    """Deterministisches, nullbyte-freies PCM (deckt stilles Padding auf)."""
    return bytes((i % 251) + 1 for i in range(total_bytes))


# ── Server-Gegenstellen ──────────────────────────────────────────────────
async def _stt_until_audio_stop(server: _FakeWyomingServer) -> None:
    while True:
        event_type, _, _, _ = await server.read()
        if event_type == EVENT_AUDIO_STOP:
            return


async def _stt_respond(server: _FakeWyomingServer, response) -> None:
    await _stt_until_audio_stop(server)
    await response(server)


async def _tts_respond(server: _FakeWyomingServer, plan: list) -> None:
    await server.read()  # erstes Event: synthesize
    for step in plan:
        if isinstance(step, bytes):
            await server.send_raw(step)
        else:
            event_type, data, payload = step
            await server.send(event_type, data, payload)


def _audio_plan(rate: int, chunks: list[bytes], *, start_data=None) -> list:
    start = {"rate": rate, "width": 2, "channels": 1} if start_data is None else start_data
    plan: list = [(EVENT_AUDIO_START, start, None)]
    for chunk in chunks:
        plan.append((EVENT_AUDIO_CHUNK, {"rate": rate}, chunk))
    plan.append((EVENT_AUDIO_STOP, {}, None))
    return plan


# ══ STT ═════════════════════════════════════════════════════════════════
@pytest.mark.unit
def test_stt_sends_expected_event_sequence() -> None:
    """STT-Sequenz + Parameter + Chunk-Grenzen exakt (`PLAN.md:441`)."""
    pcm = _pcm(2560 * 2 + 100)  # 3 Chunks: 2560, 2560, 100

    async def scenario():
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient())
            server.attach(stt.client)
            async with _server_task(
                _stt_respond(server, lambda s: s.send(EVENT_TRANSCRIPT, {"text": "Hallo Welt"}))
            ):
                text = await stt.transcribe(pcm)
            return text, server, stt

    text, server, stt = asyncio.run(scenario())

    assert text == "Hallo Welt"
    types = [event[0] for event in server.received]
    assert types == [
        EVENT_TRANSCRIBE,
        EVENT_AUDIO_START,
        EVENT_AUDIO_CHUNK,
        EVENT_AUDIO_CHUNK,
        EVENT_AUDIO_CHUNK,
        EVENT_AUDIO_STOP,
    ]
    # Sprache wörtlich `de` (Default aus `app.config`).
    assert server.received[0][1] == {"language": "de"}
    # Jeder Header trägt `version` 2.4.3 (P0.T3).
    assert all(event[3] == WYOMING_VERSION for event in server.received)
    # Audio-Format 16000 Hz / 2 Byte / 1 Kanal in `audio-start`.
    assert server.received[1][1] == {"rate": 16000, "width": 2, "channels": 1}
    # Chunk-Grenzen (2560 B, letzter kürzer) und Format pro Chunk.
    chunks = [event for event in server.received if event[0] == EVENT_AUDIO_CHUNK]
    assert [len(chunk[2]) for chunk in chunks] == [2560, 2560, 100]
    assert b"".join(chunk[2] for chunk in chunks) == pcm
    assert all(chunk[1] == {"rate": 16000, "width": 2, "channels": 1} for chunk in chunks)
    # `audio-stop` ist ohne Daten/Payload.
    assert server.received[-1][1] == {}
    assert server.received[-1][2] is None
    assert stt.client.connected is False  # `transcribe` schließt in `finally`


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("empty", ""),
        ("umlauts", "Schöne Grüße aus München – Straße & Café!"),
        ("long", "Wort " * 500),
    ],
)
def test_stt_transcript_variants(label: str, text: str) -> None:
    """Verschiedene `transcript`-Payloads werden korrekt geparst."""
    pcm = _pcm(2560)

    async def scenario():
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient())
            server.attach(stt.client)
            async with _server_task(
                _stt_respond(server, lambda s: s.send(EVENT_TRANSCRIPT, {"text": text}))
            ):
                return await stt.transcribe(pcm)

    assert asyncio.run(scenario()) == text
    assert label  # Param-ID dokumentiert den Fall


@pytest.mark.unit
def test_stt_transcript_without_text_raises_protocol_error() -> None:
    """`transcript` ohne `data.text` ist ein Protokollfehler (STATE §3/P3.T0)."""
    pcm = _pcm(2560)

    async def scenario():
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient())
            server.attach(stt.client)
            async with _server_task(
                _stt_respond(server, lambda s: s.send(EVENT_TRANSCRIPT, {"no_text": 1}))
            ):
                with pytest.raises(WyomingProtocolError):
                    await stt.transcribe(pcm)

    asyncio.run(scenario())


@pytest.mark.unit
def test_stt_timeout_raises_stt_timeout_error() -> None:
    """Stummer Server ⇒ `SttTimeoutError` (Gesamt-Sequenz-Timeout)."""
    pcm = _pcm(2560)

    async def scenario():
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient(), timeout=SHORT_TIMEOUT)
            server.attach(stt.client)
            with pytest.raises(SttTimeoutError):
                await stt.transcribe(pcm)
            assert stt.client.connected is False

    asyncio.run(scenario())


@pytest.mark.unit
def test_stt_invalid_json_line_raises_wyoming_protocol_error() -> None:
    """Ungültige JSON-Zeile ⇒ `WyomingProtocolError` (definierter Framing-Fehler)."""
    pcm = _pcm(2560)

    async def scenario():
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient())
            server.attach(stt.client)
            async with _server_task(_stt_respond(server, lambda s: s.send_raw(b"{ not json }\n"))):
                with pytest.raises(WyomingProtocolError):
                    await stt.transcribe(pcm)

    asyncio.run(scenario())


@pytest.mark.unit
def test_stt_unexpected_event_raises_wyoming_protocol_error() -> None:
    """Unerwartetes Event statt `transcript` ⇒ `WyomingProtocolError`."""
    pcm = _pcm(2560)

    async def scenario():
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient())
            server.attach(stt.client)
            async with _server_task(
                _stt_respond(server, lambda s: s.send(EVENT_AUDIO_START, {"rate": 16000}))
            ):
                with pytest.raises(WyomingProtocolError):
                    await stt.transcribe(pcm)

    asyncio.run(scenario())


# ══ TTS ═════════════════════════════════════════════════════════════════
@pytest.mark.unit
@pytest.mark.parametrize("rate", [22050, 16000])
def test_tts_synthesize_sequence_and_generator(rate: int) -> None:
    """TTS-Sequenz + Generator-Semantik `(rate, pcm)` (`PLAN.md:442`)."""
    chunks = [bytes((i % 251) + 1) * 16 for i in range(3)]  # deterministisch, ≠ 0
    text = SAMPLE_TEXT

    async def scenario():
        async with _fake_server() as server:
            tts = TtsClient(client=AsyncTcpClient())
            server.attach(tts.client)
            async with _server_task(_tts_respond(server, _audio_plan(rate, chunks))):
                items = [item async for item in tts.synthesize(text)]
            return items, server, tts

    items, server, tts = asyncio.run(scenario())

    # Erstes Element = Raten-Ankündigung (leerer PCM), Rate kommt aus audio-start.
    assert items[0] == (rate, b"")
    # Danach ein Element pro Chunk, Rate konstant wiederholt, kein End-Element.
    assert items[1:] == [(rate, chunk) for chunk in chunks]
    assert len(items) == 1 + len(chunks)
    assert {r for r, _ in items} == {rate}
    # Die Rate ist nicht hart auf 22050 verdrahtet.
    assert rate == tts.format.rate
    assert tts.format == AudioFormat(rate=rate, width=2, channels=1)

    # `synthesize`: Text in `data.text`, Voice verschachtelt (P0.T3).
    event_type, data, payload, version = server.received[0]
    assert event_type == EVENT_SYNTHESIZE
    assert data == {"text": text, "voice": {"name": PIPER_VOICE}}
    assert payload is None
    assert version == WYOMING_VERSION


@pytest.mark.unit
def test_tts_audio_start_defaults_width_and_channels() -> None:
    """Fehlen `width`/`channels`, greifen die S16_LE/mono-Defaults (STATE §3)."""
    chunks: list[bytes] = []

    async def scenario():
        async with _fake_server() as server:
            tts = TtsClient(client=AsyncTcpClient())
            server.attach(tts.client)
            plan = _audio_plan(22050, chunks, start_data={"rate": 22050})
            async with _server_task(_tts_respond(server, plan)):
                items = [item async for item in tts.synthesize(SAMPLE_TEXT)]
            return items, tts

    items, tts = asyncio.run(scenario())
    assert items == [(22050, b"")]
    assert tts.format == AudioFormat(rate=22050, width=TTS_WIDTH, channels=TTS_CHANNELS)
    assert TTS_WIDTH == 2 and TTS_CHANNELS == 1


@pytest.mark.unit
def test_tts_audio_start_without_rate_raises_protocol_error() -> None:
    """`audio-start` ohne gültige `rate` ⇒ `WyomingProtocolError`."""
    async def scenario():
        async with _fake_server() as server:
            tts = TtsClient(client=AsyncTcpClient())
            server.attach(tts.client)
            plan = [(EVENT_AUDIO_START, {}, None)]
            async with _server_task(_tts_respond(server, plan)):
                with pytest.raises(WyomingProtocolError):
                    async for _ in tts.synthesize(SAMPLE_TEXT):
                        pass

    asyncio.run(scenario())


@pytest.mark.unit
def test_tts_timeout_raises_tts_timeout_error() -> None:
    """Stummer Server ⇒ `TtsTimeoutError` (Timeout pro `read_event`)."""
    async def scenario():
        async with _fake_server() as server:
            tts = TtsClient(client=AsyncTcpClient(), timeout=SHORT_TIMEOUT)
            server.attach(tts.client)
            with pytest.raises(TtsTimeoutError):
                async for _ in tts.synthesize(SAMPLE_TEXT):
                    pass
            assert tts.client.connected is False

    asyncio.run(scenario())


@pytest.mark.unit
def test_tts_invalid_json_line_raises_wyoming_protocol_error() -> None:
    """Ungültige JSON-Zeile ⇒ `WyomingProtocolError` (geteilter Fehlertyp)."""
    async def scenario():
        async with _fake_server() as server:
            tts = TtsClient(client=AsyncTcpClient())
            server.attach(tts.client)
            async with _server_task(_tts_respond(server, [b"<html>kein json</html>\n"])):
                with pytest.raises(WyomingProtocolError):
                    async for _ in tts.synthesize(SAMPLE_TEXT):
                        pass

    asyncio.run(scenario())


@pytest.mark.unit
def test_tts_unexpected_first_event_raises_wyoming_protocol_error() -> None:
    """Etwas anderes als `audio-start`/`error` ⇒ `WyomingProtocolError`."""
    async def scenario():
        async with _fake_server() as server:
            tts = TtsClient(client=AsyncTcpClient())
            server.attach(tts.client)
            plan = [(EVENT_TRANSCRIPT, {"text": "nope"}, None)]
            async with _server_task(_tts_respond(server, plan)):
                with pytest.raises(WyomingProtocolError):
                    async for _ in tts.synthesize(SAMPLE_TEXT):
                        pass

    asyncio.run(scenario())


# ══ Gemeinsames ═════════════════════════════════════════════════════════
@pytest.mark.unit
def test_aclose_is_idempotent_twice_and_never_connected() -> None:
    """`aclose()` 2× und auf nie verbundenen Clients ist ein no-op (STATE §3)."""
    async def scenario():
        # Nie verbunden.
        raw = AsyncTcpClient()
        await raw.aclose()
        await raw.aclose()
        assert raw.connected is False
        await SttClient(client=AsyncTcpClient()).aclose()
        await SttClient(client=AsyncTcpClient()).aclose()
        await TtsClient(client=AsyncTcpClient()).aclose()
        await TtsClient(client=AsyncTcpClient()).aclose()

        # Einmal verbunden (über das socketpair) und danach 2× schließen.
        async with _fake_server() as server:
            stt = SttClient(client=AsyncTcpClient())
            server.attach(stt.client)
            async with _server_task(
                _stt_respond(server, lambda s: s.send(EVENT_TRANSCRIPT, {"text": "ok"}))
            ):
                assert await stt.transcribe(_pcm(2)) == "ok"
            assert stt.client.connected is False
            await stt.aclose()
            await stt.aclose()

    asyncio.run(scenario())


@pytest.mark.unit
def test_build_synthesize_data_shape_and_voice_coercion() -> None:
    """`build_synthesize_data`: Text in `data.text`, Voice verschachtelt."""
    assert build_synthesize_data("Hallo Welt", "de_DE-thorsten-low") == {
        "text": "Hallo Welt",
        "voice": {"name": "de_DE-thorsten-low"},
    }
    assert build_synthesize_data("Hallo") == {
        "text": "Hallo",
        "voice": {"name": PIPER_VOICE},
    }
    voice = SynthesizeVoice("de_DE-thorsten-low", language="de", speaker="speaker_1")
    assert build_synthesize_data("Hi", voice) == {
        "text": "Hi",
        "voice": {"name": "de_DE-thorsten-low", "language": "de", "speaker": "speaker_1"},
    }


@pytest.mark.unit
def test_synthesize_and_build_reject_empty_text() -> None:
    """Leerer/ungültiger Text ist ein `TtsClientError` — vor jedem Netz-I/O."""
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(TtsClientError):
            build_synthesize_data(bad)
    with pytest.raises(TtsClientError):
        build_synthesize_data(123)  # type: ignore[arg-type]

    async def scenario():
        tts = TtsClient(client=AsyncTcpClient())
        with pytest.raises(TtsClientError):
            async for _ in tts.synthesize("   "):
                pass

    asyncio.run(scenario())


# ══ Fixture (Abnahme P3, `PLAN.md:437`) ═════════════════════════════════
@pytest.mark.unit
def test_sample_16k_fixture_is_16khz_s16le_mono() -> None:
    """`sample_16k.wav` ist **16 kHz / S16_LE / mono** — roher Header + `wave`."""
    raw = FIXTURE_WAV.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    assert struct.unpack_from("<H", raw, 22)[0] == 1  # Kanäle
    rate = struct.unpack_from("<I", raw, 24)[0]
    bits = struct.unpack_from("<H", raw, 34)[0]
    data_size = struct.unpack_from("<I", raw, 40)[0]
    assert rate == 16000
    assert bits == 16
    assert data_size == len(raw) - 44
    assert data_size > 0 and data_size % 2 == 0

    with wave.open(str(FIXTURE_WAV), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == data_size // 2
        assert handle.readframes(handle.getnframes()) == raw[44:]


@pytest.mark.unit
def test_sample_text_fixture_content() -> None:
    """Der Beispieltext lautet wörtlich wie in P3.T2 vereinbart."""
    assert FIXTURE_TEXT.read_text(encoding="utf-8").strip() == SAMPLE_TEXT


# ══ Live (echter Wyoming-Server; standardmäßig geskippt) ═════════════════
@pytest.mark.live
@requires_live
def test_live_stt_transcribes_fixture() -> None:
    """Echtes Whisper auf der Fixture — **erst nach P6** freischalten."""
    host = os.environ.get("EVA_STT_HOST", STT_HOST)
    port = int(os.environ.get("EVA_STT_PORT", str(STT_PORT)))
    with wave.open(str(FIXTURE_WAV), "rb") as handle:
        assert handle.getframerate() == STT_RATE
        assert handle.getnchannels() == STT_CHANNELS
        assert handle.getsampwidth() == STT_WIDTH
        pcm = handle.readframes(handle.getnframes())

    async def scenario() -> str:
        client = SttClient(host=host, port=port, timeout=STT_TIMEOUT_SECONDS)
        try:
            return await client.transcribe(pcm)
        finally:
            await client.aclose()

    text = asyncio.run(scenario())
    assert isinstance(text, str) and text.strip() != ""


@pytest.mark.live
@requires_live
def test_live_tts_synthesizes_text() -> None:
    """Echter Piper auf `.123:10200` — Rate und PCM müssen plausibel sein."""
    host = os.environ.get("EVA_TTS_HOST", TTS_HOST)
    port = int(os.environ.get("EVA_TTS_PORT", str(TTS_PORT)))
    text = FIXTURE_TEXT.read_text(encoding="utf-8").strip()

    async def scenario():
        client = TtsClient(host=host, port=port)
        try:
            return [item async for item in client.synthesize(text)]
        finally:
            await client.aclose()

    items = asyncio.run(scenario())
    assert items[0][1] == b"" and items[0][0] > 0
    assert len({rate for rate, _ in items}) == 1
    assert sum(len(pcm) for _, pcm in items[1:]) > 0
