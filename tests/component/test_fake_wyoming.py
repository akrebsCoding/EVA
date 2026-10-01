"""L1/L3-Component-Tests der Fake-Wyoming-Server (P9.T2, `PLAN.md:540`, §7.1).

Prüfling: `tests/fakes/fake_wyoming.py`.  Zwei Ebenen:

* **Roundtrip-Beweis (der eigentliche L3-Beleg):** die **echten** Clients
  `app/stt_client.SttClient` bzw. `app/tts_client.TtsClient` sprechen gegen die
  Fakes über echtes Loopback-TCP.
* **Server-Eigenschaften:** Protokoll-Reihenfolge, konfigurierbarer Text,
  deterministischer PCM (feste Rate/Länge/Ton), Latenzinjektion und der
  Start-/Stop-Lebenszyklus (Port 0, idempotent, sauberes Schließen).

Marker `component` — nur dieser Marker hebt die E36-Netzsperre für den
Test-Prozess gezielt auf (Loopback).  Kein echtes Piper/Whisper, kein `.123`.
"""

from __future__ import annotations

import asyncio
import math
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from app.stt_client import (
    EVENT_AUDIO_CHUNK,
    EVENT_AUDIO_START,
    EVENT_AUDIO_STOP,
    EVENT_TRANSCRIBE,
    EVENT_TRANSCRIPT,
    AsyncTcpClient,
    SttClient,
)
from app.tts_client import EVENT_SYNTHESIZE, TtsClient

from tests.fakes.fake_wyoming import (
    DEFAULT_STT_TEXT_PATH,
    DEFAULT_TTS_DURATION,
    DEFAULT_TTS_RATE,
    FakeSttServer,
    FakeTtsServer,
    WyomingFakeError,
    load_default_text,
    tone_pcm,
)

pytestmark = pytest.mark.component

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _PROJECT_ROOT / "tests" / "fixtures"

#: STT-Chunk-Größe des echten Clients (P3.T0, `oww_chunk_bytes` = 2560).
_STT_CHUNK_BYTES = 2560


def _run(coro: Any) -> Any:
    """Synchroner pytest-Test-Wrapper (kein `pytest-asyncio` im Projekt)."""
    return asyncio.run(coro)


def _sample_pcm() -> bytes:
    """PCM aus `tests/fixtures/sample_16k.wav` (16 kHz/S16_LE/mono)."""
    with wave.open(str(_FIXTURES / "sample_16k.wav"), "rb") as handle:
        return handle.readframes(handle.getnframes())


# ── STT-Server ──────────────────────────────────────────────────────────────
def test_stt_roundtrip_real_client_returns_default_text() -> None:
    """Echter `SttClient` gegen den Fake ⇒ Default-Text + korrekte Sequenz."""

    async def scenario() -> None:
        server = FakeSttServer()
        await server.start()
        try:
            pcm = _sample_pcm()
            client = SttClient(host=server.host, port=server.port, language="de")
            text = await client.transcribe(pcm)

            assert text == load_default_text()
            assert server.last_language == "de"
            assert server.last_format == {"rate": 16000, "width": 2, "channels": 1}
            assert server.audio_chunks == math.ceil(len(pcm) / _STT_CHUNK_BYTES)
            assert server.audio_bytes == len(pcm)
            assert server.received_events == (
                [EVENT_TRANSCRIBE, EVENT_AUDIO_START]
                + [EVENT_AUDIO_CHUNK] * server.audio_chunks
                + [EVENT_AUDIO_STOP]
            )
            assert server.errors == []
        finally:
            await server.stop()

    _run(scenario())


def test_stt_server_custom_text_low_level_sequence() -> None:
    """Konfigurierbarer Text; Sequenz unabhängig über `AsyncTcpClient` gefahren."""

    async def scenario() -> None:
        server = FakeSttServer(text="Hallo EVA")
        await server.start()
        try:
            client = AsyncTcpClient(host=server.host, port=server.port)
            await client.connect()
            fmt = {"rate": 16000, "width": 2, "channels": 1}
            await client.send(EVENT_TRANSCRIBE, {"language": "de"})
            await client.send(EVENT_AUDIO_START, fmt)
            await client.send(EVENT_AUDIO_CHUNK, fmt, payload=b"\x00\x00" * 1280)
            await client.send(EVENT_AUDIO_CHUNK, fmt, payload=b"\x00\x00" * 10)
            await client.send(EVENT_AUDIO_STOP)

            event = await client.read_event()
            assert event.type == EVENT_TRANSCRIPT
            assert event.data.get("text") == "Hallo EVA"
            await client.aclose()

            assert server.audio_chunks == 2
            assert server.audio_bytes == (1280 + 10) * 2
            assert server.received_events == [
                EVENT_TRANSCRIBE,
                EVENT_AUDIO_START,
                EVENT_AUDIO_CHUNK,
                EVENT_AUDIO_CHUNK,
                EVENT_AUDIO_STOP,
            ]
        finally:
            await server.stop()

    _run(scenario())


def test_stt_latency_injection_is_measurable() -> None:
    """Konfigurierte Latenz verzögert die Antwort messbar."""

    async def scenario() -> None:
        latency = 0.15
        server = FakeSttServer(latency=latency)
        await server.start()
        try:
            client = SttClient(host=server.host, port=server.port)
            start = time.monotonic()
            await client.transcribe(b"\x00\x00" * 100)
            elapsed = time.monotonic() - start
            assert elapsed >= latency, f"nur {elapsed:.3f}s < {latency}s"
        finally:
            await server.stop()

    _run(scenario())


def test_stt_server_lifecycle_and_clean_close() -> None:
    """Port 0, idempotentes start/stop, nach stop keine Verbindung mehr."""

    async def scenario() -> None:
        server = FakeSttServer()
        assert server.running is False
        await server.start()
        assert server.running is True
        assert server.port > 0
        first_port = server.port
        await server.start()  # idempotent
        assert server.port == first_port

        await server.stop()
        assert server.running is False
        await server.stop()  # idempotent/no-op

        with pytest.raises(OSError):
            await asyncio.open_connection(server.host, first_port)

    _run(scenario())


# ── TTS-Server ──────────────────────────────────────────────────────────────
def test_tts_roundtrip_real_client_rate_and_pcm() -> None:
    """Echter `TtsClient` gegen den Fake ⇒ Rate-Ankündigung + exakte PCM-Länge."""

    async def scenario() -> None:
        server = FakeTtsServer(rate=22050, duration=0.2)
        await server.start()
        try:
            client = TtsClient(host=server.host, port=server.port)
            items = [item async for item in client.synthesize("Schalte das Licht ein")]

            assert items, "kein Audio geliefert"
            rate, announcement = items[0]
            assert rate == 22050
            assert announcement == b""  # Raten-Ankündigung (P3.T1)
            assert all(item_rate == 22050 for item_rate, _ in items)

            pcm = b"".join(piece for _, piece in items)
            assert len(pcm) == 4410 * 2  # 22050 Hz * 0,2 s, S16_LE
            assert pcm == server.build_pcm()
            assert server.last_text == "Schalte das Licht ein"
            assert server.last_voice is not None
            assert server.last_voice.get("name") == "de_DE-thorsten-high"
            assert server.chunks_sent == math.ceil(len(pcm) / 4096)
            assert server.pcm_bytes_sent == len(pcm)
        finally:
            await server.stop()

    _run(scenario())


def test_tts_pcm_is_deterministic_across_requests() -> None:
    """Zwei Synthesen ergeben bytegleiches PCM (kein Modell, kein Zufall)."""

    async def scenario() -> None:
        server = FakeTtsServer(rate=16000, duration=0.25)
        await server.start()
        try:
            client = TtsClient(host=server.host, port=server.port)

            async def collect() -> bytes:
                chunks = [item async for item in client.synthesize("Test")]
                return b"".join(piece for _, piece in chunks)

            first = await collect()
            second = await collect()
            assert first == second
            assert len(first) == 4000 * 2  # 16000 Hz * 0,25 s
        finally:
            await server.stop()

    _run(scenario())


def test_tts_server_records_synthesize_data() -> None:
    """`data.text` und verschachtelte `data.voice` kommen unverändert an."""

    async def scenario() -> None:
        server = FakeTtsServer()
        await server.start()
        try:
            client = AsyncTcpClient(host=server.host, port=server.port)
            await client.connect()
            await client.send(
                EVENT_SYNTHESIZE,
                {"text": "Moin", "voice": {"name": "de_DE-thorsten-high"}},
            )
            event = await client.read_event()
            assert event.type == EVENT_AUDIO_START
            assert event.data.get("rate") == DEFAULT_TTS_RATE
            while True:
                nxt = await client.read_event()
                if nxt.type == EVENT_AUDIO_STOP:
                    break
                assert nxt.type == EVENT_AUDIO_CHUNK
            await client.aclose()

            assert server.received_events[0] == EVENT_SYNTHESIZE
            assert server.last_text == "Moin"
            assert server.last_voice == {"name": "de_DE-thorsten-high"}
        finally:
            await server.stop()

    _run(scenario())


def test_tts_latency_injection_is_measurable() -> None:
    """Konfigurierte Latenz verzögert auch den TTS-Start messbar."""

    async def scenario() -> None:
        latency = 0.15
        server = FakeTtsServer(latency=latency)
        await server.start()
        try:
            client = TtsClient(host=server.host, port=server.port)
            start = time.monotonic()
            items = [item async for item in client.synthesize("T")]
            elapsed = time.monotonic() - start
            assert items
            assert elapsed >= latency, f"nur {elapsed:.3f}s < {latency}s"
        finally:
            await server.stop()

    _run(scenario())


def test_tts_server_lifecycle_and_clean_close() -> None:
    """Port 0, idempotentes start/stop, nach stop keine Verbindung mehr."""

    async def scenario() -> None:
        server = FakeTtsServer()
        assert server.running is False
        await server.start()
        assert server.running is True
        assert server.port > 0
        first_port = server.port
        await server.start()  # idempotent
        assert server.port == first_port

        await server.stop()
        assert server.running is False
        await server.stop()

        with pytest.raises(OSError):
            await asyncio.open_connection(server.host, first_port)

    _run(scenario())


# ── Bausteine ───────────────────────────────────────────────────────────────
def test_default_text_fixture_matches_plan_path() -> None:
    """Der Default kommt aus `tests/fixtures/text/sample_text.txt` (PLAN:540)."""
    assert DEFAULT_STT_TEXT_PATH.is_file()
    assert load_default_text() == DEFAULT_STT_TEXT_PATH.read_text(encoding="utf-8").strip()
    assert load_default_text()  # nicht leer


def test_tone_pcm_is_deterministic_and_validates_width() -> None:
    """`tone_pcm` ist bytegleich und lehnt fremde Sample-Breiten ab."""
    first = tone_pcm(8000, 0.1)
    second = tone_pcm(8000, 0.1)
    assert first == second
    assert len(first) == 800 * 2  # 8000 Hz * 0,1 s, S16_LE
    assert first != tone_pcm(8000, 0.1, amplitude=4000)
    with pytest.raises(WyomingFakeError):
        tone_pcm(8000, 0.1, width=4)
    with pytest.raises(WyomingFakeError):
        tone_pcm(0, 0.1)


def test_default_tts_config_matches_documented_defaults() -> None:
    """Die dokumentierten TTS-Defaults sind Rate 22050 und 0,2 s."""
    server = FakeTtsServer()
    assert server.rate == DEFAULT_TTS_RATE == 22050
    assert server.duration == DEFAULT_TTS_DURATION == 0.2
    assert server.pcm_bytes == 4410 * 2
    assert server.frame_count == 4410
