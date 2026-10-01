"""Nachweis der P1.T4-Fixtures (`tests/conftest.py`) — Layer **L0**, kein Netz.

Geprüft wird **nicht** der Manager, sondern das Fundament selbst: die
Fixtures müssen die in P0.T6/E28 und `docs/ECOMUSE_PROTOCOL.md`
**verifizierten** Audio-Werte liefern — Sample-Rate, Kanalzahl, Bitbreite,
80 ms ⇒ 2560 Byte und Mic-Frame exakt 2563 Byte inklusive der
uint16-Sequenzbytes (big-endian) samt Überlauf-Kante.

Ohne diesen Test wäre „die Fixtures existieren" nur eine Behauptung: Ein
stillschweigend auf 22050 Hz (Piper-nativ) oder 2558 Byte gesetztes Fixture
würde in P2 (Resampling, MicChunker) falsche Tests grün färben.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import wave
from pathlib import Path

import pytest

from app.config import settings
from tests.conftest import (
    CHANNELS,
    CHUNK_BYTES,
    MIC_CHUNK_MS,
    MIC_FRAME_BYTES,
    MIC_FRAME_TYPE,
    MIC_HEADER_LEN,
    MIC_RATE,
    MIC_SEQ_MAX,
    SAMPLE_WIDTH,
    SPEAKER_RATE,
    TMP_WAV_FRAMES,
    NetworkAccessBlocked,
    purge_test_artifacts,
    triangle_pcm,
    write_wav,
)

pytestmark = pytest.mark.unit


# ── tmp_wav ─────────────────────────────────────────────────────────────
def test_tmp_wav_matches_the_speaker_side_settings(tmp_wav: Path) -> None:
    """48 kHz / S16_LE / mono — die Speaker-Seite laut PLAN §4."""
    assert settings.audio_speaker_rate == SPEAKER_RATE == 48000
    assert settings.audio_width == SAMPLE_WIDTH == 2
    assert settings.audio_channels == CHANNELS == 1
    assert tmp_wav.is_file()
    with wave.open(str(tmp_wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 48000
        assert handle.getnframes() == TMP_WAV_FRAMES == 9600  # 200 ms
        assert len(handle.readframes(handle.getnframes())) == 9600 * 2


def test_tmp_wav_size_is_header_plus_pcm(tmp_wav: Path) -> None:
    """44-Byte-RIFF-Header + 19 200 Byte PCM, und nichts darüber hinaus."""
    assert tmp_wav.stat().st_size == 44 + 9600 * 2
    assert tmp_wav.stat().st_size == 19244


def test_tmp_wav_pcm_is_deterministic_and_not_silent(tmp_wav: Path) -> None:
    """Ganzzahliges Muster: reproduzierbar, bytegleich, nicht still."""
    with wave.open(str(tmp_wav), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    assert pcm == triangle_pcm(TMP_WAV_FRAMES)
    assert set(pcm) != {0}  # nicht still
    # Erneut erzeugen ⇒ bytegleich (kein Zufall, keine Uhr, kein Gerät).
    again = write_wav(tmp_wav.with_name("again.wav"))
    assert again.read_bytes() == tmp_wav.read_bytes()


# ── mic_frame ───────────────────────────────────────────────────────────
def test_mic_chunk_is_eighty_milliseconds_at_16k() -> None:
    """Die 80-ms-Kette: 16000 Hz × 2 B × 0,080 s = 2560 B (E28)."""
    assert settings.audio_mic_rate == MIC_RATE == 16000
    assert settings.oww_chunk_bytes == CHUNK_BYTES == 2560
    assert MIC_CHUNK_MS == pytest.approx(80.0)
    assert MIC_CHUNK_MS * MIC_RATE * SAMPLE_WIDTH / 1000.0 == CHUNK_BYTES


def test_mic_frame_is_exactly_2563_bytes(mic_frame: MicFrameFactory) -> None:
    """3-Byte-Header + 2560 B PCM = **2563 B** (K2, `STATE.md` §4/E28)."""
    frame = mic_frame()
    assert len(frame) == MIC_FRAME_BYTES == 2563
    assert MIC_HEADER_LEN == 3
    assert frame[0] == MIC_FRAME_TYPE == 0x01
    assert len(frame) - MIC_HEADER_LEN == CHUNK_BYTES == 2560
    assert frame[MIC_HEADER_LEN:] == triangle_pcm(CHUNK_BYTES // SAMPLE_WIDTH)


@pytest.mark.parametrize("seq", [0, 1, 2, 255, 256, 257, 4095, 65534, 65535])
def test_mic_frame_sequence_bytes_are_big_endian(
    mic_frame: MicFrameFactory, seq: int
) -> None:
    """`[seq_hi][seq_lo]` = uint16 **big-endian** (Gerät: `BigEndian.PutUint16`)."""
    header = mic_frame(seq=seq)[:MIC_HEADER_LEN]
    assert header[1:3] == struct.pack(">H", seq)
    assert (header[1] << 8) | header[2] == seq
    assert header[1] == (seq >> 8) & 0xFF
    assert header[2] == seq & 0xFF


def test_mic_frame_sequence_wraps_at_uint16_edge(mic_frame: MicFrameFactory) -> None:
    """Überlauf-Kante explizit: Gerät wrappt 65535 → 0, die Factory nicht.

    Geräteseite ist `var seqNum uint16; seqNum++` — auf der Wire gibt es also
    keinen Fehlerfall, der Zähler springt still auf 0.  Die Factory weist
    Werte außerhalb uint16 ab, damit ein Test nicht mit einem kaputten
    Zähler rechnet.
    """
    last = mic_frame(seq=MIC_SEQ_MAX)
    first = mic_frame(seq=0)
    assert last[1:3] == b"\xff\xff"
    assert first[1:3] == b"\x00\x00"
    assert MIC_SEQ_MAX == 0xFFFF
    assert (MIC_SEQ_MAX + 1) % (MIC_SEQ_MAX + 1) == 0  # der echte Wrap des Geräts
    with pytest.raises(ValueError, match="uint16"):
        mic_frame(seq=MIC_SEQ_MAX + 1)
    with pytest.raises(ValueError, match="uint16"):
        mic_frame(seq=-1)
    with pytest.raises(TypeError):
        mic_frame(seq="1")  # type: ignore[arg-type]


def test_mic_frame_rejects_a_payload_of_the_wrong_length(
    mic_frame: MicFrameFactory,
) -> None:
    """Ein Frame mit falscher Payload-Länge ist ein Protokollfehler."""
    for bad in (b"", b"\x00" * (CHUNK_BYTES - 1), b"\x00" * (CHUNK_BYTES + 1)):
        with pytest.raises(ValueError, match="Byte"):
            mic_frame(pcm=bad)
    with pytest.raises(TypeError):
        mic_frame(pcm=CHUNK_BYTES)  # type: ignore[arg-type]


def test_mic_frame_sequence_is_contiguous_over_several_frames(
    mic_frame: MicFrameFactory,
) -> None:
    """Drei Chunks ⇒ 3×2560 B PCM und lückenlose Sequenz 0,1,2.

    Das ist die Form, in der P2 den MicChunker (2560 B → `float32`) prüft.
    Achtung: das Fixture-Muster beginnt pro Frame neu, der Strom ist also
    dreimal dasselbe Muster hintereinander, **nicht** ein durchgehender Ton.
    """
    frames = [mic_frame(seq=seq) for seq in range(3)]
    assert [f[1:3] for f in frames] == [b"\x00\x00", b"\x00\x01", b"\x00\x02"]
    joined = b"".join(f[MIC_HEADER_LEN:] for f in frames)
    assert len(joined) == 3 * CHUNK_BYTES
    assert joined == triangle_pcm(CHUNK_BYTES // SAMPLE_WIDTH) * 3
    assert 3 * MIC_CHUNK_MS == pytest.approx(240.0)


def test_mic_frame_is_deterministic(mic_frame: MicFrameFactory) -> None:
    """Gleiche Eingabe ⇒ gleiche Bytes (Layer L0 ist reproduzierbar)."""
    assert mic_frame(seq=7) == mic_frame(seq=7)
    assert mic_frame(seq=7, pcm=b"\x00" * CHUNK_BYTES) != mic_frame(seq=7)


# ── Netz-Sperre ─────────────────────────────────────────────────────────
def test_network_is_blocked_in_unit_tests() -> None:
    """L0 = „rein, kein Netz" (PLAN §7.1): Verbindung und DNS müssen scheitern."""
    with pytest.raises(NetworkAccessBlocked):
        socket.create_connection(("127.0.0.1", 1), timeout=0.01)
    with socket.socket() as sock:
        with pytest.raises(NetworkAccessBlocked):
            sock.connect(("127.0.0.1", 1))
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("example.invalid", 80)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(NetworkAccessBlocked):
            udp.sendto(b"x", ("127.0.0.1", 9))


def test_asyncio_event_loop_still_works() -> None:
    """Die Sperre darf asyncio nicht brechen (Self-Pipe = `socketpair`).

    `asyncio` baut beim Anlegen eines Event-Loops eine Self-Pipe über
    `socket.socketpair()`, das selbst `socket.socket`-Objekte erzeugt.  Hätte
    die Sperre pauschal `socket.socket` verboten, wären die **asynchronen**
    L0-Tests (Pipeline, P2/P5) schon beim Erstellen des Loops kaputt.
    """

    async def _noop() -> str:
        await asyncio.sleep(0)
        return "ok"

    async def main() -> str:
        return await _noop()

    left, right = socket.socketpair()
    try:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
    finally:
        left.close()
        right.close()
    assert asyncio.run(main()) == "ok"


# ── Cleanup ─────────────────────────────────────────────────────────────
def test_purge_removes_temp_artifacts_but_keeps_reports(tmp_path: Path) -> None:
    """Nur `.tmp`-Muster werden entfernt, und nur im Wurzel-/reports-Bereich."""
    stray_root = tmp_path / "wurzel.wav.tmp"
    stray_reports = tmp_path / "reports" / "junit" / "kaputt.raw.tmp"
    keep_reports = tmp_path / "reports" / "TEST-REPORT.md"
    keep_other = tmp_path / "wichtig.txt"
    for path in (stray_root, stray_reports, keep_reports, keep_other):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    removed = purge_test_artifacts(tmp_path)

    assert set(removed) >= {stray_root, stray_reports}
    assert not stray_root.exists() and not stray_reports.exists()
    assert keep_reports.exists(), "ein echter Report darf nicht weggeräumt werden"
    assert keep_other.exists(), "außerhalb von reports/ wird nichts angefasst"
    assert not (tmp_path / "reports" / "junit").exists(), "leerer Ordner wird entfernt"
    assert (tmp_path / "reports").exists(), "das Ausgabeziel reports/ bleibt"
