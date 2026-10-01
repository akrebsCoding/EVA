"""Audio-Bridge-Tests (P2.T3, `PLAN.md` §7 → P2.T3, Layer **L0**).

Prüfling ist `app/audio_bridge.py` (P2.T2).  Getestet wird **gegen die echte
API** (`STATE.md` §3 „Audio-Bridge (P2.T2)" = Vertrag), nicht gegen eine
Wunschfassung.  Synthetisches PCM ist deterministisch (ganzzahliges
Rampenmuster, **kein** `random`, **keine** Zeitabhängigkeit), damit die
Byte-Längen und Muster exakt reproduzierbar sind.

Abdeckung der ≥5 Pflichtfälle aus `PLAN.md:427`:

1. **Resampling-Regression** 1 s 22050→48000 = **exakt 96000 B**, inkl.
   Chunk-Aufteilung + Rest ohne stilles Padding/Verlust → `test_resampling_*`
2. `set_input_rate()` wirkt (16000→48000 korrekte Länge, **nicht** auf 22050
   verdrahtet) → `test_set_input_rate_*`, `test_resampler_requires_*`
3. `chunk_for_speaker()`: 4096-B-Chunks, Restbytes erhalten, keine
   erfundenen Nullbytes → `test_chunk_for_speaker_*`, `test_speaker_chunker_*`
4. Ringpuffer-**Drop-oldest** bei Überschreiten der Max-Dauer → `test_ring_buffer_*`
5. `MicChunker`: 2560-B-Chunks, korrekte Anzahl/Länge, **`int16` roh** (E46),
   angebrochener Chunk bleibt gepuffert → `test_mic_chunker_*`

**Kein Netz, kein Gerät, kein Modell** (L0).
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio_bridge import (
    CHANNELS,
    MIC_CHUNK_BYTES,
    MIC_MAX_BUFFER_BYTES,
    MIC_MAX_BUFFER_SECONDS,
    MIC_RATE,
    OWW_DTYPE,
    SAMPLE_WIDTH,
    SPEAKER_CHUNK_BYTES,
    SPEAKER_RATE,
    AudioBridgeError,
    AudioRingBuffer,
    MicChunker,
    SpeakerChunker,
    SpeakerResampler,
    chunk_for_speaker,
)

pytestmark = pytest.mark.unit

#: Bytes pro Sekunde auf der Mic-Seite (S16_LE mono) – aus den verifizierten
#: Konstanten abgeleitet, nicht erfunden.
MIC_BYTES_PER_SECOND = MIC_RATE * SAMPLE_WIDTH * CHANNELS


def pcm_ramp(frames: int, *, start: int = 0, span: int = 60000) -> bytes:
    """Deterministisches S16_LE-Rampenmuster (little endian, je 2 Byte).

    Ganzzahlig und ohne `math.sin`/`random` – auf jeder Plattform bitgleich.
    Die Werte liegen in ``[-30000, 29999]`` (int16-sicher) und wiederholen
    sich nach ``span`` Samples.
    """
    values = ((np.arange(start, start + frames, dtype=np.int64) % span) - span // 2)
    return values.astype(np.int16).tobytes()


def resample_whole(pcm: bytes, in_rate: int) -> bytes:
    """Resampelt den kompletten Strom in **einem** `resample()` + `flush()`."""
    resampler = SpeakerResampler(in_rate=in_rate)
    return resampler.resample(pcm) + resampler.flush()


def resample_pieces(pcm: bytes, in_rate: int, piece_bytes: int) -> bytes:
    """Resampelt denselben Strom in `piece_bytes`-Stücken + `flush()`."""
    resampler = SpeakerResampler(in_rate=in_rate)
    out = b""
    for offset in range(0, len(pcm), piece_bytes):
        out += resampler.resample(pcm[offset : offset + piece_bytes])
    return out + resampler.flush()


# ── 1. Resampling-Regression 22050→48000 = exakt 96000 B ──────────────────
def test_resampling_22050_to_48000_is_exactly_96000_bytes() -> None:
    """Die zentrale Phasen-Abnahme: 1 s 22050→48000 = **96000 B** S16_LE mono."""
    one_second = pcm_ramp(22050)  # 1,0 s @ 22050 Hz = 44100 B S16_LE mono
    assert len(one_second) == 22050 * SAMPLE_WIDTH == 44100

    resampler = SpeakerResampler(in_rate=22050)
    assert resampler.input_rate == 22050
    assert resampler.output_rate == SPEAKER_RATE == 48000
    output = resampler.resample(one_second) + resampler.flush()

    # 48000 Samples · 2 Byte = 96000 Byte exakt; kein Pad, kein Verlust.
    assert len(output) == 96000
    assert len(resample_whole(one_second, 22050)) == 96000


def test_resampling_chunk_split_and_remainder_lose_no_bytes() -> None:
    """Stückgröße und Chunk-Aufteilung ändern die **Gesamtlänge nicht** (kein
    Padding/Verlust).  Ehrliche Abgrenzung: SoXR liefert bei stückweiser
    Zuführung **denselben** 96000-B-Output, aber wegen des zustandsbehafteten
    Filters nicht **bytegleich** zum Ein-Stück-Lauf — geprüft wird deshalb die
    exakte Länge über mehrere Stückgrößen und die verlustfreie Rekonstruktion."""
    one_second = pcm_ramp(22050)
    whole = resample_whole(one_second, 22050)
    for piece_bytes in (1000, 4096):
        assert len(resample_pieces(one_second, 22050, piece_bytes)) == 96000

    full_chunks, remainder = chunk_for_speaker(whole)
    assert len(full_chunks) == 23
    assert all(len(chunk) == SPEAKER_CHUNK_BYTES == 4096 for chunk in full_chunks)
    assert len(remainder) == 1792
    # 96000 mod 4096 = 1792 – der Rest ist exakt der ungepolsterte Schluss.
    assert 96000 % SPEAKER_CHUNK_BYTES == 1792
    assert remainder == whole[-1792:]
    # Rekonstruktion ist bytegleich ⇒ nichts verworfen, nichts erfunden.
    assert b"".join(full_chunks) + remainder == whole


# ── 2. `set_input_rate()` wirkt (nicht auf 22050 verdrahtet) ──────────────
def test_set_input_rate_16000_to_48000_yields_correct_length() -> None:
    """1 s 16000→48000 ergibt **96000 B** – die Rate stammt aus `set_input_rate`."""
    one_second = pcm_ramp(16000)  # 1,0 s @ 16000 Hz = 32000 B

    resampler = SpeakerResampler()
    assert resampler.is_configured is False
    assert resampler.output_rate == 48000
    assert resampler.set_input_rate(16000) is True

    output = resampler.resample(one_second) + resampler.flush()
    assert len(output) == 96000
    # Wäre die Eingangsrate hart auf 22050 verdrahtet, lieferte derselbe
    # 16000-Sample-Strom ~69 659 B statt 96000 B ⇒ die Rate wirkt wirklich.
    assert len(output) != 69659
    assert resampler.is_configured is False  # Stream nach `flush()` verbraucht


def test_resampler_requires_input_rate_and_reports_changes() -> None:
    resampler = SpeakerResampler(in_rate=22050)
    assert resampler.input_rate == 22050
    assert resampler.dtype == OWW_DTYPE == np.dtype("int16")
    # Identische Rate ⇒ kein neuer Stream (False), geändert ⇒ True.
    assert resampler.set_input_rate(22050) is False
    assert resampler.set_input_rate(16000) is True
    assert resampler.input_rate == 16000
    with pytest.raises(AudioBridgeError):
        SpeakerResampler().resample(pcm_ramp(16))  # ohne Rate
    with pytest.raises(AudioBridgeError):
        SpeakerResampler().set_input_rate(0)
    with pytest.raises(AudioBridgeError):
        SpeakerResampler(in_rate=22050).resample(b"\x01")  # ungerade Byte-Länge


# ── 3. `chunk_for_speaker()`: 4096-B-Chunks, Rest erhalten ───────────────
def test_chunk_for_speaker_splits_into_4096_and_keeps_remainder() -> None:
    # Muster ohne Nullbytes, damit ein „erfundener Nullbyte" sofort auffiele.
    pcm = bytes((i % 251) + 1 for i in range(3 * SPEAKER_CHUNK_BYTES + 100))
    full, remainder = chunk_for_speaker(pcm)
    assert len(full) == 3
    assert all(len(chunk) == 4096 for chunk in full)
    assert remainder == pcm[-100:]
    assert all(byte != 0 for byte in remainder)
    assert b"".join(full) + remainder == pcm

    # Explizites `pending` (Rest vom Voraufruf) wird **vorangestellt**: aus
    # 100 + (3·4096 + 100) = 12488 B werden 3 volle Chunks + 200 B Rest.
    carried = pcm[-100:]
    full2, remainder2 = chunk_for_speaker(pcm, pending=carried)
    assert len(full2) == 3
    assert remainder2 == (carried + pcm)[3 * SPEAKER_CHUNK_BYTES :]
    assert len(remainder2) == 200

    with pytest.raises(AudioBridgeError):
        chunk_for_speaker(b"", chunk_bytes=0)


def test_speaker_chunker_buffers_partial_remainder_without_padding() -> None:
    chunker = SpeakerChunker()
    assert chunker.chunk_bytes == 4096
    pcm = bytes((i % 251) + 1 for i in range(4096 + 77))
    assert chunker.feed(pcm) == [pcm[:4096]]
    assert chunker.pending_bytes == 77
    # Der angebrochene Rest bleibt ungepolstert erhalten – kein Auffüllen.
    rest = chunker.flush()
    assert rest == pcm[4096:]
    assert len(rest) == 77
    assert chunker.pending_bytes == 0
    assert chunker.flush() == b""


# ── 4. Ringpuffer Drop-**oldest** (nicht neueste) ─────────────────────────
def test_ring_buffer_drops_oldest_not_newest() -> None:
    # 0,001 s @ 16 kHz S16_LE mono = 32 B Obergrenze.
    ring = AudioRingBuffer(max_duration_seconds=0.001)
    assert ring.max_bytes == int(0.001 * MIC_BYTES_PER_SECOND) == 32

    oldest = b"A" * 20
    newest = b"B" * 20
    assert ring.push(oldest) == 0
    dropped = ring.push(newest)
    assert dropped == 8  # 40 − 32
    buffered = ring.read()
    assert len(buffered) == ring.buffered_bytes == 32
    # Genau die **ältesten** 8 Byte („A") fielen weg, die neuesten 20 B stehen.
    assert buffered == b"A" * 12 + newest
    assert buffered.endswith(newest)
    assert not buffered.endswith(oldest)  # Drop-newest wäre b"…BBBB…" von vorn gewesen
    assert buffered.startswith(b"A" * 12)
    assert ring.drain() == buffered
    assert ring.read() == b""


def test_ring_buffer_truncates_single_oversized_payload_to_newest() -> None:
    ring = AudioRingBuffer(max_duration_seconds=0.001)
    # Eine einzelne, die Obergrenze überschreitende Payload wird auf ihre
    # **letzten** 32 Byte gekürzt – wieder Drop-oldest.
    payload = b"".join(bytes([i % 251]) + b"Z" for i in range(50))
    dropped = ring.push(payload)
    assert dropped == len(payload) - 32
    assert ring.read() == payload[-32:]
    # Default-Grenze entspricht dem K5-Hard-Cap: 15 s = 480000 B.
    assert MIC_MAX_BUFFER_SECONDS == 15.0
    assert MIC_MAX_BUFFER_BYTES == 480000
    assert AudioRingBuffer().max_bytes == MIC_MAX_BUFFER_BYTES
    assert AudioRingBuffer().max_seconds == 15.0


# ── 5. `MicChunker`: 2560-B-Chunks, int16 roh, Rest gepuffert ────────────
def test_mic_chunker_yields_raw_int16_chunks() -> None:
    # 1280 Samples = 2560 B = genau **ein** Mic-Chunk (80 ms @ 16 kHz).
    samples = (((np.arange(1280, dtype=np.int64) * 7) - 3000) % 60000 - 30000).astype(
        np.int16
    )
    payload = samples.tobytes()
    chunker = MicChunker()
    assert chunker.chunk_bytes == MIC_CHUNK_BYTES == 2560
    assert chunker.chunk_samples == 1280

    chunks = chunker.feed(payload)
    assert len(chunks) == 1
    chunk = chunks[0]
    # E46: **int16**, roh – nicht float32 und nicht auf [-1,1] normalisiert.
    assert chunk.dtype == OWW_DTYPE == np.dtype("int16")
    assert chunk.dtype != np.dtype("float32")
    assert chunk.shape == (1280,)
    assert np.array_equal(chunk, samples)

    with pytest.raises(AudioBridgeError):
        MicChunker(chunk_bytes=2561)  # nicht durch SAMPLE_WIDTH teilbar
    with pytest.raises(AudioBridgeError):
        MicChunker(chunk_bytes=0)


def test_mic_chunker_buffers_partial_chunk() -> None:
    chunker = MicChunker()
    total_bytes = MIC_CHUNK_BYTES * 2 + 500  # 5620 B = 2810 Samples
    data = pcm_ramp(total_bytes // SAMPLE_WIDTH)
    chunks = chunker.feed(data)
    assert len(chunks) == 2
    assert all(chunk.shape == (1280,) for chunk in chunks)
    assert chunker.pending_bytes == 500
    # Der angebrochene Rest wird **nicht** genullt/verworfen.
    assert chunker.flush().tobytes() == data[-500:]
    assert chunker.pending_bytes == 0
    assert chunker.flush() is None

    # Gestückelte Zuführung: Rest wandert in den nächsten vollen Chunk.
    chunker.reset()
    assert chunker.feed(data[:1000]) == []
    assert chunker.pending_bytes == 1000
    second = chunker.feed(data[1000 : 1000 + 1560])
    assert len(second) == 1
    assert second[0].tobytes() == data[:2560]
    assert chunker.pending_bytes == 0
