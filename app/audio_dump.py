"""Audio-Diagnose-Dump pro Turn (P9.T5) – **config-gated, Default AUS**.

Der Schritt P9.T5 ändert **nichts** am Verhalten (keine Schwelle, kein
Endpointing, kein STT).  Dieses Modul erlaubt nur, das **komplette Turn-Audio**
(genau das, was an STT geht, 16 kHz S16_LE mono) zusammen mit einem kleinen
JSON (Endpoint-Stats + Transcript) auf Platte zu legen, damit die
VAD-Distanz-Hypothese („3 m sehr schlecht, langsam reden hilft") offline
geprüft werden kann.

Eigenschaften (bewusst, analog `DASHBOARD_WRITE_ENABLED` aus P9.T1/E96):

* **Default aus** (`AUDIO_DUMP_ENABLED=false`): der erste Zweig ist ein
  earlier return – **kein** I/O, kein Verzeichnis, **null Overhead**.
* **Fehler brechen den Turn nie.**  Jede Ausnahme wird als `WARNING` geloggt
  und geschluckt (gleiche Linie wie `_record_turn`: der Beobachter darf den
  Turn nicht abbrechen).
* **Rotierendes Limit** (`AUDIO_DUMP_MAX_FILES` / `AUDIO_DUMP_MAX_BYTES`,
  älteste zuerst), damit die Platte nicht vollläuft.
* **Datenschutz:** die Dumps enthalten **rohe Sprache** des Nutzers.  Sie
  bleiben auf dem Zielhost (`.123`), werden nicht exportiert und das
  Verzeichnis kann jederzeit geleert werden.

Pfad-Wahrheit im Manager-Container (P9.T5 verifiziert): der Container hat
**keinen** Bind-Mount (`docker inspect .Mounts == []`), daher liegt ein
Default-Verzeichnis `/tmp/audio-dumps` im Container-Dateisystem und geht bei
`up -d --build` (Recreate) **verloren** – für die Diagnostik ausreichend und
in STATE.md §3 dokumentiert.
"""

from __future__ import annotations

import json
import re
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional

from app.logger import get_logger

__all__ = [
    "dump_turn_audio",
    "WAV_RATE",
    "WAV_WIDTH",
    "WAV_CHANNELS",
]

_LOG = get_logger("audio_dump")

#: Das Mic-Format des Dots (K2/E28): 16 kHz, S16_LE, mono – das Turn-Audio
#: kommt bereits in diesem Format aus dem `TurnBuffer`, es wird nichts
#: konvertiert oder neu gesampelt.
WAV_RATE: Final[int] = 16000
WAV_WIDTH: Final[int] = 2
WAV_CHANNELS: Final[int] = 1

#: Zeichenklasse für Dateinamen-Anteile – alles andere wird zu `_` geklart.
_SAFE_CHARS: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")


def _safe_component(raw: Any, fallback: str) -> str:
    """Einen Dateinamen-Anteil klaren: nur `[A-Za-z0-9._-]`, begrenzt auf 64."""
    cleaned = _SAFE_CHARS.sub("_", str(raw or "").strip())
    return cleaned[:64] or fallback


def _rotate(directory: Path, max_files: int, max_bytes: int) -> None:
    """Älteste Dumps löschen, bis Dateianzahl und Bytesatz im Limit liegen.

    Fehler beim Aufräumen sind Diagnostik-Nachrichten, keine Turn-Fehler –
    sie werden geloggt und lassen die frisch geschriebenen Dateien stehen.
    """
    pairs: list[tuple[Path, Path]] = []
    for wav in directory.glob("*.wav"):
        pairs.append((wav, wav.with_suffix(".json")))
    pairs.sort(key=lambda pair: pair[0].stat().st_mtime, reverse=True)

    total = 0
    for wav, sidecar in pairs:
        try:
            total += wav.stat().st_size + (
                sidecar.stat().st_size if sidecar.exists() else 0
            )
        except OSError:
            pass

    kept = 0
    for wav, sidecar in pairs:
        over = kept >= max_files or (kept > 0 and total > max_bytes)
        if not over:
            kept += 1
            continue
        try:
            size = wav.stat().st_size + (
                sidecar.stat().st_size if sidecar.exists() else 0
            )
            wav.unlink()
            if sidecar.exists():
                sidecar.unlink()
            total -= size
            _LOG.info(
                "Audio-Dump rotiert: %s gelöscht (Limit %d Dateien/%d Bytes)",
                wav.name,
                max_files,
                max_bytes,
            )
        except OSError as exc:
            _LOG.warning("Audio-Dump-Rotation scheiterte an %s: %s", wav.name, exc)


def dump_turn_audio(
    *,
    enabled: bool,
    dump_dir: str,
    device_id: str,
    outcome: str,
    transcript: Optional[str],
    stats: dict[str, Any],
    audio: bytes,
    max_files: int = 50,
    max_bytes: int = 100 * 1024 * 1024,
) -> None:
    """**Einen** Turn als WAV + JSON ablegen (oder gar nichts tun).

    Das Gate steht **vor** allem Sonstigen: bei `enabled=False` oder leerem
    `dump_dir` passiert garantiert **kein** I/O.  Der Rest steht in einem
    gemeinsamen `try` – der Dump darf den Turn **nie** brechen.
    """
    if not enabled or not str(dump_dir).strip():
        return
    try:
        directory = Path(str(dump_dir).strip())
        directory.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
        stem = "{}_{}_{}".format(
            ts,
            _safe_component(device_id, "device"),
            _safe_component(outcome, "unknown"),
        )
        wav_path = directory / f"{stem}.wav"
        json_path = directory / f"{stem}.json"

        # WAV erst nebenher, dann atomar an den Platz (kein Halbfabrikat).
        tmp_path = wav_path.with_suffix(".wav.tmp")
        with open(tmp_path, "wb") as raw_file:
            with wave.open(raw_file, "wb") as wav_file:
                wav_file.setnchannels(WAV_CHANNELS)
                wav_file.setsampwidth(WAV_WIDTH)
                wav_file.setframerate(WAV_RATE)
                wav_file.writeframes(bytes(audio))
        tmp_path.replace(wav_path)

        sidecar: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "device_id": device_id,
            "outcome": outcome,
            "transcript": transcript if isinstance(transcript, str) else None,
            "audio_bytes": len(audio),
            **stats,
        }
        json_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        _LOG.info("Audio-Dump geschrieben: %s (%d B Audio)", wav_path.name, len(audio))

        _rotate(directory, int(max_files), int(max_bytes))
    except Exception as exc:  # Beobachter-Regel: der Dump bricht den Turn nie.
        _LOG.warning("Audio-Dump nicht geschrieben (%s): %s", device_id, exc)
