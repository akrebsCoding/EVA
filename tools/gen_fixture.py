#!/usr/bin/env python3
"""TTS-Fixture erzeugen und auf 16 kHz/S16_LE/mono verifizieren (P3.T2, `PLAN.md:443`).

Auftrag (wörtlich): `tests/fixtures/sample_text.txt` anlegen („Schalte das Licht
im Wohnzimmer ein") und per **liveem** Piper auf `.123:10200` zu WAV
synthetisieren, via SoXR auf **16 kHz mono S16_LE** normalisieren, **Header +
Rate verifizieren**.

**Wiederverwendung statt Duplikat** (Auftrag §0/§6):

* **Synthese** über `app.tts_client.TtsClient.synthesize(...)` (P3.T1) — das
  Wyoming-JSON-Zeilen-Framing wird **nicht** neu gebaut.
* **Resampling** über `app.audio_bridge.SpeakerResampler` (P2.T2) — er
  implementiert genau den benötigten zustandsbehafteten SoXR-Stream
  (`ResampleStream`, `dtype="int16"`) mit **zur Laufzeit gesetzter
  Eingangsrate** (`set_input_rate()`). Nur die **Ausgangsrate** wird von den
  48000 Hz des Speaker-Pfads auf `settings.audio_mic_rate` (16000) umgestellt.
  Direkter SoXR-Aufbau wäre ein Duplikat derselben verifizierten Logik.

**Die Eingangsrate kommt aus dem `audio-start`-Event** (nicht hart 22050). Für
`de_DE-thorsten-high` ist sie verifiziert 22050 Hz (P0.T3 / STATE §3), für die
`*-low`-Stimmen 16000 Hz. Das Werkzeug protokolliert die tatsächlich gelesene
Rate und prüft die Ausgabe dagegen.

**Idempotenz:** Zielpfade werden aus dem Repo abgeleitet (`__file__` → Wurzel),
es gibt **keine erforderlichen Argumente**; die WAV wird atomar über eine
`.wav.tmp`-Datei ersetzt (`*.wav.tmp` ist git-ignoriert). Mehrfaches Ausführen
liefert jedes Mal eine valide Fixture (die Piper-PCM-Länge variiert pro Lauf,
das ist erwartet — P3.T1).

**Kein Netz außer der Piper-Verbindung, kein `docker`-Eingriff auf `.123`.**

Aufruf::

    /tmp/eva-venv/bin/python tools/gen_fixture.py
    /tmp/eva-venv/bin/python tools/gen_fixture.py --host 10.0.0.10 --port 10200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import wave
from pathlib import Path
from typing import Final

# Projektwurzel in den Importpfad legen, damit `python tools/gen_fixture.py`
# (sys.path[0] = tools/) die `app`-Pakete findet — analog `pytest.ini:pythonpath`.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.audio_bridge import SpeakerResampler  # noqa: E402
from app.config import settings  # noqa: E402
from app.tts_client import TtsClient  # noqa: E402

__all__ = [
    "FixtureVerificationError",
    "FIXTURES_DIR",
    "TEXT_PATH",
    "WAV_PATH",
    "SYNTH_TEXT",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "OUTPUT_RATE",
    "read_or_create_text",
    "synthesize_pcm",
    "resample_to_mic_rate",
    "write_wav",
    "verify_wav",
    "main",
]

# ── Ziele (aus dem Repo abgeleitet, keine Argumente nötig) ────────────────
FIXTURES_DIR: Final[Path] = PROJECT_ROOT / "tests" / "fixtures"
TEXT_PATH: Final[Path] = FIXTURES_DIR / "sample_text.txt"
WAV_PATH: Final[Path] = FIXTURES_DIR / "sample_16k.wav"
TMP_PATH: Final[Path] = FIXTURES_DIR / "sample_16k.wav.tmp"

#: Kanonischer Fixture-Text (eine Zeile) — wird angelegt, falls die Datei fehlt.
SYNTH_TEXT: Final[str] = "Schalte das Licht im Wohnzimmer ein"

#: Live-Piper läuft auf `.123` (HA-Host, `settings.ha_base_url`).  `app.config`
#: führt `piper_host=127.0.0.1`, weil der spätere Manager Piper im selben
#: Compose (network_mode: host) sieht; für die **lokale** Fixture-Erzeugung ist
#: Piper aber nur über `.123:10200` erreichbar (P0.T3).  `--host` überschreibt.
DEFAULT_HOST: Final[str] = "10.0.0.10"
DEFAULT_PORT: Final[int] = settings.piper_port  # 10200
OUTPUT_RATE: Final[int] = settings.audio_mic_rate  # 16000
OUTPUT_CHANNELS: Final[int] = 1
OUTPUT_SAMPLE_WIDTH: Final[int] = 2  # S16_LE = 2 Byte/Sample


class FixtureVerificationError(RuntimeError):
    """Die erzeugte Fixture verletzt das geforderte Format (Header/Rate)."""


def read_or_create_text() -> str:
    """Fixture-Text lesen; fehlende Datei mit dem kanonischen Text anlegen."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    if not TEXT_PATH.exists():
        TEXT_PATH.write_text(SYNTH_TEXT + "\n", encoding="utf-8")
    text = TEXT_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise FixtureVerificationError(f"{TEXT_PATH} ist leer")
    return text


async def synthesize_pcm(
    host: str, port: int, text: str
) -> tuple[int, bytes, int]:
    """Text über `TtsClient` synthetisieren → ``(input_rate, pcm, chunks)``.

    Das erste Generator-Element ist die Raten-Ankündigung ``(rate, b"")``
    (P3.T1); danach folgt ein Element pro `audio-chunk`.  Die Rate ist über den
    ganzen Strom konstant; die PCM-Teilstücke werden für die WAV-Ausgabe
    gesammelt (die Streaming-Semantik des Clients bleibt unberührt).
    """
    client = TtsClient(host=host, port=port)
    input_rate: int | None = None
    parts: list[bytes] = []
    chunks = 0
    try:
        async for rate, pcm in client.synthesize(text):
            if input_rate is None:
                input_rate = int(rate)
            elif int(rate) != input_rate:
                raise FixtureVerificationError(
                    f"Rate wechselte im Strom: {input_rate} → {rate}"
                )
            if pcm:
                parts.append(pcm)
                chunks += 1
    finally:
        await client.aclose()
    if input_rate is None or input_rate <= 0:
        raise FixtureVerificationError("audio-start lieferte keine gültige Rate")
    return input_rate, b"".join(parts), chunks


def resample_to_mic_rate(pcm: bytes, input_rate: int) -> bytes:
    """PCM (S16_LE mono) per `SpeakerResampler` auf `OUTPUT_RATE` normalisieren.

    Die Eingangsrate stammt aus dem `audio-start` (`set_input_rate`), die
    Ausgangsrate ist `settings.audio_mic_rate` (16000) — nicht die 48000 Hz des
    Speaker-Pfads.  SoXR wird stückweise gespeist und mit `flush()` sauber
    abgeschlossen (Filter-Tail wird ausgegeben, kein Verlust).
    """
    if len(pcm) % OUTPUT_SAMPLE_WIDTH != 0:
        raise FixtureVerificationError(
            f"Piper-PCM hat {len(pcm)} B — S16_LE braucht ein Vielfaches von 2"
        )
    resampler = SpeakerResampler(
        out_rate=OUTPUT_RATE,
        dtype="int16",
        num_channels=OUTPUT_CHANNELS,
    )
    resampler.set_input_rate(input_rate)
    out: list[bytes] = [resampler.resample(pcm), resampler.flush()]
    return b"".join(out)


def write_wav(path: Path, pcm: bytes) -> None:
    """PCM als WAV (RIFF/WAVE, PCM fmt, rate/channels/bits) atomar schreiben."""
    with wave.open(str(TMP_PATH), "wb") as wav:
        wav.setnchannels(OUTPUT_CHANNELS)
        wav.setsampwidth(OUTPUT_SAMPLE_WIDTH)
        wav.setframerate(OUTPUT_RATE)
        wav.writeframes(pcm)
    TMP_PATH.replace(path)


def _parse_header(path: Path) -> dict[str, object]:
    """Erste 44 Byte als PCM-RIFF-Header parsen — **unabhängig** vom `wave`-Modul."""
    raw = path.read_bytes()
    if len(raw) < 44:
        raise FixtureVerificationError(f"{path.name} hat nur {len(raw)} B (< 44)")
    (
        riff,
        riff_size,
        wave_id,
        fmt_id,
        fmt_size,
        audio_format,
        channels,
        rate,
        byte_rate,
        block_align,
        bits,
        data_id,
        data_size,
    ) = struct.unpack("<4sI4s4sIHHIIHH4sI", raw[:44])
    return {
        "riff": riff,
        "riff_size": riff_size,
        "wave_id": wave_id,
        "fmt_id": fmt_id,
        "fmt_size": fmt_size,
        "audio_format": audio_format,
        "channels": channels,
        "rate": rate,
        "byte_rate": byte_rate,
        "block_align": block_align,
        "bits": bits,
        "data_id": data_id,
        "data_size": data_size,
        "file_size": len(raw),
    }


def verify_wav(path: Path) -> dict[str, object]:
    """Header + Rate **programmatisch** prüfen (roher Header **und** `wave`).

    Prüft: RIFF/WAVE, PCM (`audio_format == 1`), `rate == OUTPUT_RATE`,
    `channels == OUTPUT_CHANNELS`, `bits == 16`, `data`-Chunk, `data_size > 0`,
    gerade Byte-Länge und `data_size == file_size - 44`.  Zusätzlich liest das
    **stdlib-`wave`-Modul** dieselbe Datei (zweiter, unabhängiger Weg).  Jede
    Abweichung ⇒ `FixtureVerificationError` (Exit-Code 1).
    """
    header = _parse_header(path)
    expected_bytes = OUTPUT_RATE * OUTPUT_CHANNELS * OUTPUT_SAMPLE_WIDTH

    if header["riff"] != b"RIFF" or header["wave_id"] != b"WAVE":
        raise FixtureVerificationError("kein RIFF/WAVE-Header")
    if header["fmt_id"] != b"fmt " or header["data_id"] != b"data":
        raise FixtureVerificationError("fmt-/data-Chunk fehlt")
    if header["fmt_size"] != 16:
        raise FixtureVerificationError(f"fmt-Chunk-Größe {header['fmt_size']} ≠ 16")
    if header["audio_format"] != 1:
        raise FixtureVerificationError(
            f"audio_format {header['audio_format']} ≠ 1 (PCM)"
        )
    if header["rate"] != OUTPUT_RATE:
        raise FixtureVerificationError(
            f"Header-Rate {header['rate']} ≠ {OUTPUT_RATE}"
        )
    if header["channels"] != OUTPUT_CHANNELS:
        raise FixtureVerificationError(
            f"Header-Kanäle {header['channels']} ≠ {OUTPUT_CHANNELS}"
        )
    if header["bits"] != OUTPUT_SAMPLE_WIDTH * 8:
        raise FixtureVerificationError(
            f"Header-Bits {header['bits']} ≠ {OUTPUT_SAMPLE_WIDTH * 8}"
        )
    if header["data_size"] == 0 or header["data_size"] % OUTPUT_SAMPLE_WIDTH != 0:
        raise FixtureVerificationError(
            f"data-Chunk {header['data_size']} B leer oder ungerade"
        )
    if header["data_size"] != header["file_size"] - 44:
        raise FixtureVerificationError(
            f"data_size {header['data_size']} ≠ Datei {header['file_size']} − 44"
        )
    if header["block_align"] != OUTPUT_CHANNELS * OUTPUT_SAMPLE_WIDTH:
        raise FixtureVerificationError(
            f"block_align {header['block_align']} ≠ {OUTPUT_CHANNELS * OUTPUT_SAMPLE_WIDTH}"
        )
    if header["byte_rate"] != expected_bytes:
        raise FixtureVerificationError(
            f"byte_rate {header['byte_rate']} ≠ {expected_bytes}"
        )

    # Zweiter, unabhängiger Weg: stdlib `wave` liest die Datei selbst.
    with wave.open(str(path), "rb") as wav:
        w_rate = wav.getframerate()
        w_channels = wav.getnchannels()
        w_width = wav.getsampwidth()
        w_frames = wav.getnframes()
        w_pcm = wav.readframes(w_frames)
    if w_rate != OUTPUT_RATE or w_channels != OUTPUT_CHANNELS:
        raise FixtureVerificationError(
            f"wave-Modul liest rate={w_rate}/channels={w_channels}"
        )
    if w_width != OUTPUT_SAMPLE_WIDTH:
        raise FixtureVerificationError(f"wave-Modul liest width={w_width}")
    if len(w_pcm) != header["data_size"]:
        raise FixtureVerificationError(
            f"wave-PCM {len(w_pcm)} B ≠ data_size {header['data_size']}"
        )

    return {
        "file": str(path),
        "file_bytes": header["file_size"],
        "rate": header["rate"],
        "channels": header["channels"],
        "bits": header["bits"],
        "pcm_bytes": header["data_size"],
        "frames": w_frames,
        "duration_seconds": round(w_frames / header["rate"], 4),
        "header_verified": True,
    }


async def _run(host: str, port: int) -> int:
    text = read_or_create_text()
    input_rate, pcm, chunks = await synthesize_pcm(host, port, text)
    pcm_16k = resample_to_mic_rate(pcm, input_rate)
    if not pcm_16k:
        raise FixtureVerificationError("normalisiertes PCM ist leer")
    write_wav(WAV_PATH, pcm_16k)
    result = verify_wav(WAV_PATH)
    result.update(
        {
            "text": text,
            "input_rate": input_rate,
            "input_pcm_bytes": len(pcm),
            "input_chunks": chunks,
            "output_rate": OUTPUT_RATE,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Kommandozeilen-Einstieg — ohne Argumente lauffähig (Defaults aus Repo)."""
    parser = argparse.ArgumentParser(
        description="TTS-Fixture über liveem Piper erzeugen und verifizieren (P3.T2)"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Piper-Host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Piper-Port")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.host, args.port))
    except FixtureVerificationError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Verbindungs-/Protokollfehler des Clients
        print(f"FEHLER ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
