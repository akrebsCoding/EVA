"""Unit-Tests für `app/audio_dump.py` (P9.T5, E100 – Layer **L0**).

Prüfling ist der config-gated Audio-Dump: **Default aus** (kein I/O), sonst
WAV + JSON mit denselben Endpoint-Stats, rotierendes Limit (Dateien **und**
Bytes, älteste zuerst) und die Beobachter-Regel: **jeder** Fehler wird
geschluckt und darf den Turn nie brechen.

Kein Netz, kein Gerät, kein Modell – nur stdlib `wave`/`json` und `tmp_path`.
"""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from typing import Any

import pytest

from app.audio_dump import dump_turn_audio

pytestmark = pytest.mark.unit

#: Endpoint-Stats, wie `_endpoint_fields` sie liefert (Literale, unabhängig).
STATS: dict[str, Any] = {
    "endpoint_noise_floor": 0.001002,
    "endpoint_threshold": 0.004,
    "endpoint_speech_frames": 5,
    "endpoint_silence_frames": 12,
    "endpoint_skip_frames": 3,
    "endpoint_speech_seconds": 0.4,
}
AUDIO: bytes = b"\x00\x01\x00\x02" * 40  # 160 B S16_LE mono


def _dump(target: Path, **overrides: Any) -> None:
    kwargs: dict[str, Any] = dict(
        enabled=True,
        dump_dir=str(target),
        device_id="dev-A",
        outcome="ok",
        transcript="schalte das licht ein",
        stats=dict(STATS),
        audio=AUDIO,
    )
    kwargs.update(overrides)
    dump_turn_audio(**kwargs)


# ── Gate ──────────────────────────────────────────────────────────────────
def test_aus_ist_kein_io(tmp_path: Path) -> None:
    """`enabled=False` ⇒ garantiert kein Verzeichnis, keine Datei."""
    dump_dir = tmp_path / "nope"
    _dump(dump_dir, enabled=False)
    assert not dump_dir.exists()


def test_leerer_dump_dir_ist_kein_io(tmp_path: Path) -> None:
    """`dump_dir=""` ⇒ kein Dump, selbst wenn enabled."""
    dump_dir = tmp_path / "nope"
    _dump(dump_dir, dump_dir="")
    assert not dump_dir.exists()


# ── WAV + JSON ────────────────────────────────────────────────────────────
def test_an_schreibt_wav_und_json_paar(tmp_path: Path) -> None:
    """Gate auf: exakt ein WAV (16 kHz/S16_LE/mono) + JSON am selben Stem."""
    _dump(tmp_path)
    wavs = list(tmp_path.glob("*.wav"))
    jsons = list(tmp_path.glob("*.json"))
    assert len(wavs) == 1 and len(jsons) == 1
    assert jsons[0].stem == wavs[0].stem

    with wave.open(str(wavs[0]), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() == len(AUDIO) // 2

    sidecar = json.loads(jsons[0].read_text(encoding="utf-8"))
    assert sidecar["outcome"] == "ok"
    assert sidecar["transcript"] == "schalte das licht ein"
    assert sidecar["audio_bytes"] == len(AUDIO)
    for name, value in STATS.items():
        assert sidecar[name] == value


def test_dateinamen_sind_geklart(tmp_path: Path) -> None:
    """Fremdzeichen in device_id/outcome werden zu `_` (kein Pfad-Escape)."""
    _dump(tmp_path, device_id="../evil/..", outcome="ok/with/slash")
    wavs = list(tmp_path.glob("*.wav"))
    assert len(wavs) == 1
    assert "/" not in wavs[0].name


# ── Rotation ──────────────────────────────────────────────────────────────
def test_rotation_loescht_aelteste_zuerst(tmp_path: Path) -> None:
    """Limit 3 Dateien: 5 Dumps ⇒ 3 übrig, die ältesten (mit JSON) weg."""
    for index in range(5):
        _dump(tmp_path, outcome=f"ok{index}", max_files=3)
        for wav in tmp_path.glob("*.wav"):
            os.utime(wav, (index + 1, index + 1))  # eindeutige mtimes
    wavs = sorted(tmp_path.glob("*.wav"))
    assert len(wavs) == 3
    # Die drei jüngsten (ok2, ok3, ok4) überleben; ok0/ok1 sind weg.
    stems = {wav.stem for wav in wavs}
    assert any("ok4" in stem for stem in stems)
    assert any("ok3" in stem for stem in stems)
    assert any("ok2" in stem for stem in stems)
    assert not any("ok0" in stem for stem in stems)
    assert not any("ok1" in stem for stem in stems)
    # Kein verwaistes JSON: genausoviele Sidecars wie WAVs.
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_rotation_bytes_limit_bewahrt_mindestens_eins(tmp_path: Path) -> None:
    """Bytes-Limit: es wird immer **mindestens** der jüngste Dump behalten."""
    for _ in range(3):
        _dump(tmp_path, max_bytes=1)  # 1 Byte Limit ⇒ jedes Mal über dem Limit
    assert len(list(tmp_path.glob("*.wav"))) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


# ── Beobachter-Regel ──────────────────────────────────────────────────────
def test_fehler_wird_geschluckt_und_steigt_nicht_hoch(tmp_path: Path) -> None:
    """Ziel ist eine **Datei** ⇒ `mkdir` scheitert ⇒ WARNING, keine Ausnahme."""
    blocker = tmp_path / "kaputt"
    blocker.write_text("ich bin keine directory")
    _dump(blocker)  # darf nicht werfen
    assert blocker.is_file()
