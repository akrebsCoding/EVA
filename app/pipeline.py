"""EVA-Pipeline – Zustandsmaschine (§5) + Dauer-Mic-Empfang (P5.T0).

Reine Orchestrierungs-Logik (L0): **kein** Netz-Aufruf, **kein** WebSocket-Import.
Alle Gegenstellen sind **injiziert** (STT/TTS/HA/Router/WakeWordDetector sowie
die beiden ausgehenden Transport-Callables `send_control`/`send_binary`), damit
`tests/test_pipeline.py` (P5.T6) ohne Netz/Gerät/Modell deterministisch läuft.

Umgesetzte Festlegungen (jede Zahl belegt):

* **E5 (korrigiert, P7.T1-Fix)** – Der Dot streamt permanent. `on_mic_frame`
  reicht **jeden** PCM-Block an `wake_word` **und** in den Turn-Puffer. Das
  Gerät sendet `0x04`/`0x05` **nur** im `lock_mic`-Turn; der **permanente**
  Stream (Wake-Pfad) hat **keine** End-of-Speech-Sentinels (Firmware
  `device/internal/client/data.go`). Deshalb endpointet der **Wake-/Barge-in-
  Turn** manager-seitig (Silence-Endpointing, Referenz `em_esphome._is_speech`
  + `docs/device-config-reference.json`); der **Button-Pfad (K6)** nutzt
  weiterhin den `lock_mic`-Turn und dessen `0x04`/`0x05`.
* **E7** – Beim Wake werden **`oww_preroll_discard_chunks` = 3 Chunks (240 ms)**
  aus dem Puffer verworfen (Wake-Wort-Rest). Nur der Wake-Pfad; Button-Turns
  verwerfen nichts (Referenz `em_controller.py` „button and continuation turns
  pass 0").
* **E6/K5** – `0x05` (`on_no_speech`) ⇒ **stillschweigen**: kein STT, kein TTS,
  kein Fehlertext, LED `pulse`, zurück nach `IDLE`.
* **K5** – Sicherheitsnetze aus `app.config` (**nicht** hart verdrahtet):
  Hard-Cap `turn_hard_cap_seconds` (15 s) und No-Speech
  `turn_no_speech_seconds` (8 s). Sie feuern **ohne** eingehende Frames
  („Mic-Lücke") und verwerfen den Puffer.
* **Barge-in** – im Zustand `SPEAKING` löst ein Wake-Event mit
  `barge_in=True` (Schwelle `oww_barge_in_threshold`, vom Detektor gesetzt)
  `speaker_flush` + Cancel aus; danach ein **neuer** Turn.
* **E29/K6** – Button: jedes `clickType == 138` mit `down:false` zählt. Läuft
  ein Turn ⇒ `speaker_flush` + Cancel; sonst Button-Turn
  `mic_stop → mic_start{lock_mic:true} → Turn → mic_stop → mic_start{}`.
  `heldMs`/`muted` werden geparst, ändern das Verhalten aber nicht (K6 bleibt).
* **E11/K7** – LED folgt dem **Turn**. Turn-Start
  `listeningAnim` = `solid[110,0,45]`+`listening` (ttl 30), Denken `spin`
  `[[210,45,0],[55,8,0]]` 80 ms ttl 135, Sprechen `meter` `[[210,45,0]]`
  ttl `max(30, 2·audio+20)` (erst bei Audio-Start), Stille/Fehler `pulse`
  (900/220 ms, ttl 1), IDLE `off`. Serializer aus `app/protocol.py`.
* **P7.T2/E88** – **Wake-Quittierung:** der User wollte unmittelbar nach „Hey
  EVA" eine LED-Reaktion (vorher keinerlei Feedback). Verbindlich: kurzer
  **Bernstein-Blitz `solid[255,170,0]`** für `WAKE_ACK_FLASH_SECONDS` (0,4 s),
  danach unverändert das lila `listening`. Der Blitz ist ein **transientes
  Overlay**, kein neuer Zustand: er wird nur beim `wake`-Trigger gesendet
  (nicht Button/K6, nicht Barge-in – dort läuft bereits ein Turn), und der
  nachgeschobene `listening`-Push prüft vor dem Senden, ob der Turn noch
  `LISTENING` ist – sonst würde er `thinking`/`speaking`/`off` überschreiben.
* **P7.T2b/E89** – **Warum das Lila ausblieb: der falsche Transport.** Nur ein
  `led_anim` malt den Ring neu (Firmware `control.go:692` → `cmd/server.go:250`
  → `StartAnim`); ein `{"type":"config","listeningAnim":…}`-Push **cached** die
  Spec nur (`config/config.go:291-292`) und wird genau **ein** Mal konsumiert:
  im Gerät selbst, wenn es den Wake-Crossing erkennt (`cmd/server.go:1328`).
  Ein Config-Push malt also **nie**. Der Blitz (`led_anim`, `ttlSec: 1`) hielt
  den Ring deshalb 1 s bernstein und der nachgeschobene Config-Push war für den
  Ring ein No-op ⇒ `animExpiry` schwärzte ihn (`server/animator.go:154-163`):
  **Blitz → schwarz → später Spin.** Das Lila muss daher als **`led_anim`**
  kommen – genau wie die Referenz `em_controller.leds_listening` (`:1231-1232`)
  es mit der Spec aus `em_scenes.py:199-204` tut. Reihenfolge danach:
  **Blitz → dauerhaft lila → `thinking` → Antwort**, unabhängig davon, ob
  schon Sprache erkannt wurde (E89).
* **§5** – Pro Gerät **ein** aktiver Turn (per-Device-`asyncio.Lock`); jeder
  Turn räumt im `try/finally` auf (LED, Puffer, Timer, Lock, Task).
* **P8.D3 – Turn-Historie (reiner Beobachter).** Die Pipeline führt jetzt einen
  **begrenzten In-Memory-Ringpuffer** `Pipeline.turn_history`
  (`deque(maxlen=TURN_HISTORY_MAXLEN)` = 100 Einträge), den `app/dashboard.py`
  seit P8.D1 als Allowlist-Projektion liest. Der Puffer ist **ausschließlich
  Beobachter**:
  - Er ändert **keinen** Return-Wert, **keinen** Zustandsübergang, **keine**
    Fehlerbehandlung und **kein** Timing. Der einzige Eingriffspunkt ist ein
    **synchroner** Aufruf von `self._record_turn(...)` – kein `await`, also
    weder unterbrechbar noch blockierend.
  - Er schreibt **nicht** in `DeviceState`; die Turn-Startzeit liegt in einem
    eigenen `self._turn_started` (ein `float` pro Gerät, bei jedem Turn-Ende
    und bei `on_device_gone` wieder entfernt).
  - **Kein** `try/except` um bestehende Fehlerbehandlung; `_record_turn` fängt
    **nur seine eigenen** Fehler und gibt sie als `WARNING` auf `_LOG` aus.
  - **Nebenläufigkeit:** die Pipeline läuft im asyncio-Event-Loop; `deque.append`
    ist unter dem GIL atomar, und der Turn eines Geräts läuft unter
    `state.lock` (ein Turn pro Gerät). `_end_silent`/`_cancel_active_turn`/
    `on_device_gone` laufen in **anderen** Tasks, rufen `_record_turn` aber
    **nach** dem Turn-Task (`_cleanup_turn` im `finally`) auf ⇒ es gibt pro
    Turn genau einen Aufruf. **Kein** `threading.Lock`, keiner nötig.
  - **Erfasste Endpfade:** Erfolg (`ok`), Router-Fallback/HA-Fehler (`error`),
    STT-Fehler (`error`), unerwartete Ausnahme (`error`), leeres Transkript
    (`silence`), `0x05`-Stille, `0x04` ohne Audio, Hard-Cap, No-Speech,
    Abbruch/Cancel (Barge-in, Button, `on_device_gone`) und Gerät weg im
    `LISTENING`. **Nicht** erfasst: ein Turn, der nie endet (Prozess lebt) –
    das ist per Definition kein Endpfad.
  - **Datenschutz:** nur eine **Allowlist** von Feldern (siehe
    `Pipeline.turn_history` in der Klassen-Doku). `transcript` und `error` laufen
    durch `redact()` und sind auf `HISTORY_TEXT_MAXLEN` (500) Zeichen gekürzt.
    **Nie** enthalten: Audio/Chunks/Rohframes, Secrets, `response_text` (der
    komplette DeepSeek-/Template-Text), `raw` (Jev-Rohantwort), `service_data`
    (nur der **eine** E92-Allowlist-Key als `key=wert`), Systemprompts.
  - **Nur Arbeitsspeicher:** nichts wird persistiert, keine Datei, keine
    Retention über den Prozess hinaus – nach einem Neustart ist die Historie
    leer (`/api/history` meldet dann `available: true`, `count: 0`).
* **P9.T0 – Phasen-Timing (reiner Beobachter, wie P8.D3).** Zusätzlich zum
  Ringpuffer führt die Pipeline je Gerät eine Liste von **Phasen-Zeitpunkten**
  (`self._phase_marks`, `time.monotonic()`), aus der am Turn-Ende **neun
  additive Felder** in denselben Historieneintrag fallen
  (`TURN_LATENCY_FIELDS`, Reihenfolge = Vertrag für `app/dashboard.py`):
  - `latency_after_speech_seconds` = `ha_done − speech_end` ist die
    **Schlüsselzahl** („Licht ist an" ab Sprechende). Für einen QUESTION-/
    TEXT-Turn gibt es keinen HA-Call – das Feld bleibt dann `None`, es wird
    **keine** Antwortzeit erfunden.
  - `latency_after_wake_seconds` = `ha_done − wake_detected` (nur Wake-Turns;
    der Button (K6) hat kein Wake-Wort ⇒ `None`), `phase_listen_seconds` =
    Zuhören, `phase_stt_seconds` = STT, `phase_route_seconds` = Routing,
    `phase_execute_seconds` = HA-Ausführung, `phase_tts_first_audio_seconds` =
    erstes **Audio von Piper**, `phase_tts_first_frame_seconds` = erster
    **gesendeter** Frame (der Chunkerpuffer ist damit sichtbar),
    `phase_tts_total_seconds` = ganze TTS-Phase inkl. EOS.
  - **Eine nicht erreichte Phase ist `None`, niemals 0.** `CancelledError`
    markiert bewusst **nicht** (unvollendeter Versuch, keine Dauer), ein
    STT-/HA-Fehler **doch** (beendet, mit Fehler). `_phase_span` rundet auf 3
    Stellen und klemmt Negative (kaputte Uhr ⇒ keine erfundene Zahl).
  - **Kein** neuer `await`, **kein** I/O, **kein** Log an einer Messstelle;
    `_mark` ist ein reiner `monotonic()`-Aufruf, damit die Zustandsfolge in §5
    nachweislich unverändert bleibt (keine zusätzliche Yield-Möglichkeit).
  - `_phase_marks` wird in **jedem** Endpfad (`_run_turn`/`_end_silent`/
    `on_device_gone`) in `_record_turn` geleert ⇒ kein `dict` bleibt liegen und
    der nächste Turn erbt keine fremde Marke.
* **P9.T0 – Wake-Diagnose (nur lesend).** `wake_device_ids()`,
  `wake_attempts(device_id)` und `wake_threshold()` sind die einzige
  Schnittstelle zu `app/dashboard.py::collect_wake`. Sie lesen ausschließlich
  den fertigen Ringpuffer des Detektors (Datenstruktur, **keine**
  Modellauswertung), geben **Kopien** nach außen und liefern bei unbekanntem
  Gerät `[]`/`None` statt einer Zahl aus der Config.
"""

from __future__ import annotations

import asyncio
import math
import time
from array import array
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Final, Optional

from app.audio_bridge import SpeakerChunker, SpeakerResampler
from app.audio_dump import dump_turn_audio
from app.config import settings
from app.device_config import (
    DeviceConfigError,
    build_listening_anim,
    build_listening_anim_push,
)
from app.logger import get_logger
from app.protocol import (
    build_speaker_eos,
    build_speaker_frame,
    next_sequence,
    serialize_led_anim,
    serialize_mic_start,
    serialize_mic_stop,
    serialize_speaker_flush,
)
from app.router import (
    ENTITY_PARAM_ALLOWLIST,
    FALLBACK_NOT_UNDERSTOOD,
    FALLBACK_SPEECH,
)

__all__ = [
    "PipelineError",
    "PipelineState",
    "TurnBuffer",
    "DeviceState",
    "Pipeline",
    "PREROLL_DISCARD_CHUNKS",
    "HARD_CAP_SECONDS",
    "NO_SPEECH_SECONDS",
    "VAD_THRESHOLD",
    "VAD_SPEECH_MS",
    "VAD_SILENCE_MS",
    "BUTTON_CLICK_TYPE",
    "HOLD_MS_THRESHOLD",
    "THINKING_ANIM",
    "METER_COLOR",
    "SILENCE_ANIM",
    "ERROR_ANIM",
    "OFF_ANIM",
    "WAKE_ACK_ANIM",
    "WAKE_ACK_FLASH_SECONDS",
    "HISTORY_TEXT_MAXLEN",
    "TURN_HISTORY_MAXLEN",
    "TURN_LATENCY_FIELDS",
    "ENDPOINT_HISTORY_FIELDS",
]

_LOG = get_logger("pipeline")

#: 2560 B = 80 ms @ 16 kHz S16_LE mono (K2/E28) – aus `app.config`.
CHUNK_BYTES: Final[int] = settings.oww_chunk_bytes
#: 3 Chunks = 240 ms Wake-Wort-Rest (E7, `VOICE_PREROLL_DISCARD`).
PREROLL_DISCARD_CHUNKS: Final[int] = settings.oww_preroll_discard_chunks
#: Sicherheitsnetz (K5) – aus der Config, nie hart 15.0.
HARD_CAP_SECONDS: Final[float] = settings.turn_hard_cap_seconds
#: Sicherheitsnetz (K5) – aus der Config, nie hart 8.0.
NO_SPEECH_SECONDS: Final[float] = settings.turn_no_speech_seconds
#: E29: nur dieser `clickType` erreicht den Controller (Dot).
BUTTON_CLICK_TYPE: Final[int] = 138
#: E29: geräteseitige Hold-Schwelle – nur diagnostisch (K6 bleibt).
HOLD_MS_THRESHOLD: Final[int] = 750

#: ── Silence-Endpointing des Wake-/Barge-in-Turns (P7.T1-Fix, E5-Korrektur) ──
#: Der permanente Stream hat **keine** `0x04`/`0x05`-Sentinels; der Manager
#: endpointet selbst (Referenz `em_esphome._is_speech`: SNR-relative Schwelle
#: `rms >= max(3·noise_floor, 0.004)`; gemessene Dauerparameter aus der
#: verifizierten Geräte-Config `docs/device-config-reference.json:11-13`).
#: Absolut-Untergrenze der Sprache (em_esphome.py:1661) – **nicht** erfunden.
VAD_THRESHOLD: Final[float] = 0.004
#: `vadSpeechMs` = 32 ms Sprache, bevor `speech_seen` gilt (Geräte-Config).
VAD_SPEECH_MS: Final[int] = 32
#: `vadSilenceMs` = 900 ms Stille nach Sprache ⇒ Turn-Ende (Geräte-Config).
VAD_SILENCE_MS: Final[int] = 900
#: Per-Raum-Rauschboden-EWMA (em_controller.py:2613-2619), asymmetrisch:
#: fällt schnell (α=0.3), steigt langsam (α=0.008 ≈ 10 s @ 12,5 Chunks/s).
_NOISE_FLOOR_SNR: Final[float] = 3.0
_NOISE_FLOOR_DOWN: Final[float] = 0.3
_NOISE_FLOOR_UP: Final[float] = 0.008

#: Farben/Raten sind Referenzwerte aus STATE §4/E11 – **nicht** erfunden.
_SOLID_COLOR: Final[list[list[int]]] = [[110, 0, 45]]
#: TTL des lila `listening` (E11: 30 s). Bewusst **größer** als
#: `HARD_CAP_SECONDS` (15 s): das Gerät schwärzt einen `solid`-Ring, dessen
#: `ttlSec` ohne Nachfolger verfällt (`server/animator.go:154-163`) – der
#: Dead-Man darf den Ring also **nie** mitten im Turn löschen (P7.T2b/E89).
_LISTENING_TTL_SEC: Final[int] = 30
THINKING_ANIM: Final[dict[str, Any]] = {
    "pattern": "spin",
    "colors": [[210, 45, 0], [55, 8, 0]],
    "periodMs": 80,
    "ttlSec": 135,
}
METER_COLOR: Final[list[list[int]]] = [[210, 45, 0]]
#: Stille/Fehler tragen die Bedeutung im Rhythmus (E11); Farbe nicht belegt.
SILENCE_ANIM: Final[dict[str, Any]] = {"pattern": "pulse", "periodMs": 900, "ttlSec": 1}
ERROR_ANIM: Final[dict[str, Any]] = {"pattern": "pulse", "periodMs": 220, "ttlSec": 1}
OFF_ANIM: Final[dict[str, Any]] = {"pattern": "off"}

#: ── Wake-Quittierung (P7.T2, E88, User-Entscheidung 2026-09-27) ──────────
#: Nach „Hey EVA" gab es **keinerlei** Feedback (kein LED). Verbindlich ist
#: ein **kurzer Bernstein-Blitz** von `WAKE_ACK_FLASH_SECONDS`, danach das
#: bestehende lila `listening` (kein Doppelblitz, kein direkt-lila).
#:
#: **Farbwahl (RGB, dokumentiert):** Bernstein = `255,170,0`.
#: * klar getrennt vom Denken-Orange `210,45,0` (Farbton ~17° vs. ~38°) und von
#:   dessen Schweif `55,8,0` – zusätzlich rund 3× heller (Luminanz ~140 vs. ~47),
#: * klar getrennt vom lila `listening` `110,0,45` (Farbton ~332°) und vom
#:   Mute-Ring (rot) bzw. dem Link-Orange `255,40,0` aus der Referenz,
#: * `green`/weiß sind belegt (Szene `standard` `0,180,0`, Ring voll an).
#:
#: **Effekt `solid`, nicht `pulse`:** die Dauer des Blitzes begrenzt der
#: Manager selbst (`WAKE_ACK_FLASH_SECONDS`); `periodMs` ist laut Katalog ein
#: **ganzer** Throb-Zyklus (15 %→100 %), ein `pulse` würde im sichtbaren
#: Fenster also nur heller werden, nicht blinken. `ttlSec` 1 ist der
#: Dead-Man-Timer (E11): bleibt der Folge-Push aus, wird der Ring nach 1 s
#: schwarz statt dauerhaft bernstein — **Firmware-belegt** (P7.T2b/E89):
#: `StartAnim` malt `solid` **einmal** und armiert `animExpiry`; verfällt die
#: TTL ohne Nachfolger, schreibt sie `blackFrame` (`server/animator.go:113-126`
#: und `:154-163`).
WAKE_ACK_COLOR: Final[list[int]] = [255, 170, 0]
WAKE_ACK_FLASH_SECONDS: Final[float] = 0.4
WAKE_ACK_ANIM: Final[dict[str, Any]] = {
    "pattern": "solid",
    "colors": [list(WAKE_ACK_COLOR)],
    "ttlSec": 1,
}

#: Meter-TTL-Untergrenze aus E11 (`max(30, 2·audio+20)`).
_METER_TTL_FLOOR: Final[int] = 30

# ── Turn-Historie (P8.D3 – reiner Beobachter, nur im Arbeitsspeicher) ───
#: **Harter Cap** des Ringpuffers `Pipeline.turn_history`.  `deque(maxlen=…)`
#: ⇒ die ältesten Einträge werden verworfen, das Wachstum ist begrenzt. 100
#: Einträge ≈ 2–3 h bei aktivem Geradebetrieb; Speicher-Obergrenze grob
#: 100 × (2×500 Zeichen Text + ~200 B Overhead) ≈ **< 200 kB**.
TURN_HISTORY_MAXLEN: Final[int] = 100
#: Längen-Cap für die **einzigen** zwei Freitextfelder des Puffers
#: (`transcript`, `error`) – nach `redact()`, mit `…` als Abschneidemarker.
HISTORY_TEXT_MAXLEN: Final[int] = 500
#: Schwelle, ab der eine `noul`-Antwort als „ja" gilt (E58: `NoulResult.value`
#: = `score >= 0.5`). Nur für die Historie-Interpretation von `needs_param`.
_NOUL_TRUE_THRESHOLD: Final[float] = 0.5
#: Platzhalter, falls `redact()` nicht importierbar wäre (Importzyklus). Dann
#: wird **kein** Text gespeichert – lieber ehrlich leer als unredigiert.
_HISTORY_REDACTION_UNAVAILABLE: Final[str] = "[Text nicht ausgegeben]"

_BYTES_PER_SECOND: Final[int] = (
    settings.audio_mic_rate * settings.audio_width * settings.audio_channels
)

#: Dauer eines Mic-Chunks (2560 B = 80 ms @ 16 kHz S16_LE mono) – Basis des
#: frame-getriebenen Endpointings (kein Uhr-Warten nötig, deterministisch).
_CHUNK_SECONDS: Final[float] = CHUNK_BYTES / _BYTES_PER_SECOND


def _frame_rms(pcm: bytes) -> float:
    """RMS eines Mic-Chunks als normierter Pegel (0.0–1.0), int16 roh (E46)."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    acc = 0
    for sample in samples:
        acc += sample * sample
    return math.sqrt(acc / len(samples)) / 32768.0


def _listening_anim_spec() -> dict[str, Any]:
    """Spec des lila `listening` (E11) – für **beide** Transportwege.

    `app/device_config.build_listening_anim` liefert genau die Referenzform
    (`em_scenes.py:199-204`): `solid`, `colors`, `listening: True`, `ttlSec`.
    P7.T2b/E89: dieselbe Spec geht als `config`-Push (cached das Gerät, damit es
    den Ring beim **eigenen** Wake-Crossing selbst malt) **und** als `led_anim`
    (malt den Ring sofort). Unsupported-Szenen werfen weiterhin
    `DeviceConfigError` – statt eine Farbe zu erfinden, greift der Fallback auf
    den `_SOLID_COLOR`-Referenzwert zurück.
    """
    try:
        return build_listening_anim()
    except DeviceConfigError:
        return {
            "pattern": "solid",
            "colors": list(_SOLID_COLOR),
            "listening": True,
            "ttlSec": _LISTENING_TTL_SEC,
        }


#: ── Phasen-Zeitpunkte (P9.T0 – **reiner Beobachter**) ────────────────────
#: Interne Marken.  Sie sind **kein** Zustand und nichts liest sie aus außer
#: `_phase_fields`; `self._phase_marks` wird pro Turn geleert.  Die Namen der
#: Historie-Felder stehen unten als Literale – eine Mutation an genau diesen
#: Feldern soll die Tests treffen (E56/E59).
_PHASE_WAKE_DETECTED: Final[str] = "wake_detected"
_PHASE_SPEECH_END: Final[str] = "speech_end"
_PHASE_STT_START: Final[str] = "stt_start"
_PHASE_STT_DONE: Final[str] = "stt_done"
_PHASE_JEV_DONE: Final[str] = "jev_done"
_PHASE_HA_DONE: Final[str] = "ha_done"
_PHASE_TTS_START: Final[str] = "tts_start"
_PHASE_TTS_FIRST_AUDIO: Final[str] = "tts_first_audio"
_PHASE_TTS_FIRST_FRAME: Final[str] = "tts_first_frame"
_PHASE_TTS_DONE: Final[str] = "tts_done"

#: Die neun additiven History-Felder, in **fester** Reihenfolge.  Reihenfolge
#: ist Teil des Vertrags: `app/dashboard.py` iteriert darüber, und die Reihenfolge
#: der Karten im Dashboard folgt ihr.
TURN_LATENCY_FIELDS: Final[tuple[str, ...]] = (
    "latency_after_speech_seconds",
    "latency_after_wake_seconds",
    "phase_stt_seconds",
    "phase_route_seconds",
    "phase_tts_first_audio_seconds",
    "phase_tts_total_seconds",
    "phase_listen_seconds",
    "phase_execute_seconds",
    "phase_tts_first_frame_seconds",
)

#: P9.T5 (E100): die sechs additiven **Endpoint-Diagnosefelder**.  Reihenfolge
#: ist (wie bei `TURN_LATENCY_FIELDS`) Teil des Vertrags für
#: `app/dashboard.py`.  `endpoint_threshold` ist die **wirksame** Schwelle
#: `max(_NOISE_FLOOR_SNR · noise_floor, VAD_THRESHOLD)` am Turn-Ende – sie
#: steht heute sonst nirgends.  Button-Turns (K6) laufen über Geräte-Sentinel
#: ⇒ manager-seitiges Endpointing lief dort nie ⇒ **alles `None`** statt
#: erfundener 0er (gleiche ehrliche Grundregel wie P9.T0).
ENDPOINT_HISTORY_FIELDS: Final[tuple[str, ...]] = (
    "endpoint_noise_floor",
    "endpoint_threshold",
    "endpoint_speech_frames",
    "endpoint_silence_frames",
    "endpoint_skip_frames",
    "endpoint_speech_seconds",
)


def _state_value(state: Any) -> Optional[str]:
    """`PipelineState`/str → String; unbekannt ⇒ `None` statt Exception."""
    raw = getattr(state, "value", state)
    return str(raw) if isinstance(raw, str) else None


def _history_text(value: Any, *, limit: int = HISTORY_TEXT_MAXLEN) -> Optional[str]:
    """Text für die Historie: **`redact()`** zuerst, dann längenbegrenzt.

    Leer/whitespace ⇒ `None`. Ist `redact()` nicht importierbar (der Import
    ist bewusst **lokal**, weil `app.dashboard` `app.pipeline` importiert), wird
    der Text **verworfen** statt unredigiert gespeichert.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        from app.dashboard import redact  # lokal: app.dashboard importiert app.pipeline
    except Exception:  # pragma: no cover – Importzyklus/Teilausfall
        return _HISTORY_REDACTION_UNAVAILABLE
    text = redact(text)
    if len(text) > limit:
        text = text[: max(1, limit - 1)] + "…"
    return text


def _optional_str(value: Any) -> Optional[str]:
    """`str`/Enum → nicht-leerer String, sonst `None` (keine Zahlen im Allowlist)."""
    raw = getattr(value, "value", value)
    return str(raw) if isinstance(raw, str) and raw else None


def _optional_float(value: Any) -> Optional[float]:
    """Zahl → `float`; `bool`/Nicht-Zahlen ⇒ `None` (`bool` ist keine Messung)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _needs_param_flag(decision: Any) -> Optional[bool]:
    """E92: hat Jev die `needs_param`-Frage bejaht? ⇒ `bool`, sonst `None`.

    Quelle ist **nur** `decision.raw["needs_param"]["noul"]` (die Jev-Antwort,
    vom Router als `dict(...)` abgelegt). `None` heißt „Jev hat die dritte
    Frage nicht beantwortet" – das ist ein *unbekannter* Wert, kein `False`.
    Interpretation als Bool: `score >= 0.5` (E58, `_NOUL_TRUE_THRESHOLD`).
    """
    raw = getattr(decision, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    answer = raw.get("needs_param")
    if not isinstance(answer, Mapping):
        return None
    score = _optional_float(answer.get("noul"))
    if score is None:
        return None
    return score >= _NOUL_TRUE_THRESHOLD


def _extracted_param(decision: Any) -> Optional[str]:
    """E92: **nur** der eine erlaubte Param-Key als ``key=wert`` (z. B. ``brightness_pct=40``).

    `service_data` wird **nicht** durchgereicht: der Key muss in
    `ENTITY_PARAM_ALLOWLIST` stehen (die E92-Sicherheits-Allowlist des Routers,
    pro `(domain, service)` genau ein Key), und der Wert muss eine Zahl sein.
    Alles andere wird stillschweigend verworfen – es gibt kein freies
    Durchreichen von DeepSeek-JSON.
    """
    service_data = getattr(decision, "service_data", None)
    if not isinstance(service_data, Mapping) or not service_data:
        return None
    allowed = set(ENTITY_PARAM_ALLOWLIST.values())
    for key, value in service_data.items():
        if not isinstance(key, str) or key not in allowed:
            continue
        number = _optional_float(value)
        if number is None:
            return None
        rendered = str(int(number)) if number.is_integer() else f"{round(number, 2):g}"
        return f"{key}={rendered}"
    return None


def _decision_detail(decision: Any) -> Optional[str]:
    """`raw["detail"]` der Router-Fehlerentscheidung (kurz, router-eigen)."""
    raw = getattr(decision, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    detail = raw.get("detail")
    return detail if isinstance(detail, str) and detail.strip() else None


def _utc_now_iso() -> str:
    """Aktuelle UTC-Zeit als ISO-8601 (Sekunden-Auflösung, `+00:00`)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PipelineError(ValueError):
    """Fehler in der Orchestrierung (definiert statt still zu degradieren)."""


class PipelineState(str, Enum):
    """Zustände der Zustandsmaschine `PLAN.md` §5 (vollständig, nichts erfunden)."""

    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    ROUTING = "routing"
    EXECUTING = "executing"
    ANSWERING = "answering"
    SPEAKING = "speaking"


# ── Puffer ────────────────────────────────────────────────────────────────
class TurnBuffer:
    """Dauer-Puffer pro Gerät: Preroll-Fenster (3 Chunks) + Turn-Audio.

    Während `IDLE`/`SPEAKING` landen Frames im **Preroll**-Fenster (max.
    `preroll_chunks`); beim Wake/Button wird es verworfen und ab dann füllt
    sich das Turn-Audio bis zum Hard-Cap (Drop-oldest, kein Blockieren).
    """

    def __init__(
        self,
        *,
        preroll_chunks: int = PREROLL_DISCARD_CHUNKS,
        max_bytes: Optional[int] = None,
    ) -> None:
        if preroll_chunks < 0:
            raise PipelineError("preroll_chunks muss >= 0 sein")
        self._preroll_chunks = int(preroll_chunks)
        self._preroll: Deque[bytes] = deque(maxlen=self._preroll_chunks)
        self._turn = bytearray()
        self._max_bytes = (
            int(max_bytes)
            if max_bytes is not None
            else int(HARD_CAP_SECONDS * _BYTES_PER_SECOND)
        )

    @property
    def preroll_chunks(self) -> int:
        return self._preroll_chunks

    @property
    def preroll_count(self) -> int:
        return len(self._preroll)

    @property
    def turn_bytes(self) -> int:
        return len(self._turn)

    @property
    def buffered_seconds(self) -> float:
        return len(self._turn) / _BYTES_PER_SECOND

    def push_preroll(self, pcm: bytes) -> None:
        self._preroll.append(pcm)

    def push_turn(self, pcm: bytes) -> None:
        self._turn.extend(pcm)
        overflow = len(self._turn) - self._max_bytes
        if overflow > 0:  # Drop-oldest, wie der Referenz-Ringpuffer (E5)
            del self._turn[:overflow]

    def discard_preroll(self) -> int:
        """Preroll-Fenster leeren und die Zahl verworfener Chunks liefern (E7)."""
        dropped = len(self._preroll)
        self._preroll.clear()
        return dropped

    def begin_turn(self) -> None:
        """Preroll **und** Turn-Audio verwerfen (Turn-Start)."""
        self._preroll.clear()
        self._turn.clear()

    def clear_turn(self) -> None:
        """Nur das Turn-Audio verwerfen (Preroll bleibt für einen neuen Wake)."""
        self._turn.clear()

    def drain(self) -> bytes:
        """Turn-Audio als ein Byte-Block, Puffer danach leer."""
        audio = bytes(self._turn)
        self._turn.clear()
        return audio

    def clear(self) -> None:
        self._preroll.clear()
        self._turn.clear()


# ── Per-Device-Zustand ────────────────────────────────────────────────────
@dataclass
class DeviceState:
    """Zustandsmaschinen-Kontext eines Geräts (ein aktiver Turn, §5)."""

    device_id: str
    wake: Any
    buffer: TurnBuffer
    lock: asyncio.Lock
    state: PipelineState = PipelineState.IDLE
    turn_task: Optional[asyncio.Task[Any]] = None
    timers: list[asyncio.Task[Any]] = field(default_factory=list)
    trigger: str = "idle"
    button_turn: bool = False
    cancelled: bool = False
    outcome: Optional[str] = None
    expected_seq: Optional[int] = None
    mic_gaps: int = 0
    discarded_preroll: int = 0
    last_score: float = 0.0
    #: P7.T1-Fix: manager-seitiges Endpointing des Wake-/Barge-in-Turns.
    endpoint_noise_floor: float = 0.0
    endpoint_speech_seen: bool = False
    endpoint_speech_frames: int = 0
    endpoint_silence_frames: int = 0
    endpoint_skip_frames: int = 0
    #: P7.T2: Task des nachgeschobenen `listening`-Push (Wake-Blitz) und seine
    #: Generation – der Task sendet **nur**, wenn er noch der aktuelle ist.
    led_task: Optional[asyncio.Task[Any]] = None
    led_epoch: int = 0


# ── Pipeline ──────────────────────────────────────────────────────────────
_ControlSender = Callable[[str, dict], Awaitable[None]]
_BinarySender = Callable[[str, bytes], Awaitable[None]]
_Sleep = Callable[[float], Awaitable[None]]


async def _null_control(device_id: str, message: dict) -> None:
    _LOG.debug("kein send_control injiziert – Nachricht %s verworfen", message.get("type"))


async def _null_binary(device_id: str, data: bytes) -> None:
    _LOG.debug("kein send_binary injiziert – %d B verworfen", len(data))


class Pipeline:
    """Zustandsmaschine pro Gerät mit injizierten Clients (P5.T0).

    Die sechs öffentlichen Handler entsprechen wörtlich `PLAN.md` §5:
    ``on_mic_frame`` · ``on_wake_detected`` · ``on_vad_end`` · ``on_no_speech``
    · ``on_button`` · ``on_device_gone``.  Sie sind **Coroutinen** – der
    WS-Server (P5.T1/T2) awaited sie; alle Werte kommen aus `app.config`.

    ``turn_history`` (P8.D3)
    ------------------------
    Begrenzter In-Memory-Ringpuffer (``deque(maxlen=TURN_HISTORY_MAXLEN)``) mit
    **genau einem** Eintrag pro abgeschlossenem Turn – gelesen von
    `app/dashboard.py` (``/api/history``). **Allowlist**, kein Eject, keine
    Steuerungswirkung, **nur RAM** (nach Neustart leer):

    =======================  ==================================================
    Feld                     Inhalt
    =======================  ==================================================
    ``ts``                   ISO-8601 UTC (Ende des Turns)
    ``device_id``            Gerät
    ``state``                Zustand **am Ende** des Turns (§5-Enum-Wert)
    ``outcome``              ``ok`` / ``error`` / ``silence`` / ``cancelled`` /
                             ``device_gone`` / ``unknown``
    ``duration_seconds``     Gesamtturn (float, s) – P8.D3-Auftrag ``duration_ms``
    ``score``                Wake-Score, ``None`` wenn keiner gemessen wurde
    ``confidence``           Entity-Konfidenz – P8.D3-Auftrag ``target_confidence``
    ``service`` / ``domain`` HA-Dienst/Domain der Entscheidung
    ``target_entity_id``     aufgelöstes Zielgerät
    ``needs_param``          E92: Jev bejahte die Param-Frage (``bool``/``None``)
    ``extracted_param``      E92: ``key=wert`` des **einen** erlaubten Keys
    ``transcript``           **rotiert + gekürzt** (500 Z.), nie ein Prompt
    ``error_code``           Router-Code (``ENTITY_PARAM_MISSING``, …)
    ``error``                **rotiert + gekürzt** (500 Z.) Fehler-/Abbruchgrund
    ``barge_in``             ``True``, wenn der Turn als Barge-in startete

    **P9.T5 (E100)**: zusätzlich sechs Endpoint-Diagnosefelder
    (`endpoint_noise_floor`, `endpoint_threshold`, `endpoint_speech_frames`,
    `endpoint_silence_frames`, `endpoint_skip_frames`, `endpoint_speech_seconds`)
    – Button-Turn ⇒ alles `None` (kein manager-seitiges Endpointing).
    =======================  ==================================================

    **Nicht** enthalten (bewusst): Audio/Chunks/Rohframes, `response_text` (der
    komplette DeepSeek-/Template-Text – auch nicht als Länge), `raw` (Jev-Roh),
    `service_data` (nur der eine Allowlist-Key), Secrets, Systemprompts.
    """

    def __init__(
        self,
        *,
        wake_detector: Any = None,
        wake_detector_factory: Optional[Callable[[str], Any]] = None,
        stt_client: Any = None,
        tts_client: Any = None,
        router: Any = None,
        ha_client: Any = None,
        send_control: Optional[_ControlSender] = None,
        send_binary: Optional[_BinarySender] = None,
        sleep: Optional[_Sleep] = None,
        settings_obj: Any = None,
        hard_cap_seconds: Optional[float] = None,
        no_speech_seconds: Optional[float] = None,
    ) -> None:
        self._settings = settings_obj if settings_obj is not None else settings
        self._wake_detector = wake_detector
        self._wake_factory = wake_detector_factory
        self._stt = stt_client
        self._tts = tts_client
        self._router = router
        self._ha_client = ha_client
        self._send_control = send_control or _null_control
        self._send_binary = send_binary or _null_binary
        self._sleep = sleep
        #: K5-Sicherheitsnetze – Default aus der Config, überschreibbar (Tests).
        self._hard_cap = (
            float(hard_cap_seconds) if hard_cap_seconds is not None else HARD_CAP_SECONDS
        )
        self._no_speech = (
            float(no_speech_seconds) if no_speech_seconds is not None else NO_SPEECH_SECONDS
        )
        self._devices: dict[str, DeviceState] = {}
        #: P8.D3: Ringpuffer der abgeschlossenen Turns – gelesen von
        #: `app/dashboard.py` (`Pipeline.turn_history` ist genau der dort
        #: gelesene Attributname).  `deque(maxlen=…)` = harter Cap; **nur RAM**.
        self.turn_history: Deque[dict[str, Any]] = deque(maxlen=TURN_HISTORY_MAXLEN)
        #: P8.D3: Turn-Startzeit (monoton) je Gerät – bewusst **außerhalb** von
        #: `DeviceState`, damit die Steuerung nichts davon liest.  Ein `float`
        #: pro Gerät, bei jedem Turn-Ende und bei `on_device_gone` entfernt.
        self._turn_started: dict[str, float] = {}
        #: P9.T0: Phasen-Zeitpunkte (monoton) je Gerät.  Wie
        #: `_turn_started` bewusst **außerhalb** von `DeviceState`; ein `dict`
        #: mit den Marken des **laufenden** Turns, das `_record_turn` in
        #: jedem Endpfad wieder leert.  Nie über einen Turn hinweg bestehen
        #: gelassen – sonst würde der nächste Turn fremde Phasen erben.
        self._phase_marks: dict[str, dict[str, float]] = {}

    # ── Clients (lazy, keine I/O bei Konstruktion) ────────────────────
    def _stt_client(self) -> Any:
        if self._stt is None:
            from app.stt_client import SttClient

            self._stt = SttClient()
        return self._stt

    def _tts_client(self) -> Any:
        if self._tts is None:
            from app.tts_client import TtsClient

            self._tts = TtsClient()
        return self._tts

    def _router_client(self) -> Any:
        if self._router is None:
            from app.router import Router

            self._router = Router(ha_client=self._ha_client)
        return self._router

    def _make_wake(self, device_id: str) -> Any:
        if self._wake_detector is not None:
            return self._wake_detector
        if self._wake_factory is not None:
            return self._wake_factory(device_id)
        from app.wake_word import WakeWordDetector

        return WakeWordDetector()

    # ── Zustands-Helfer ───────────────────────────────────────────────
    def _state(self, device_id: str) -> DeviceState:
        if not isinstance(device_id, str) or not device_id:
            raise PipelineError("device_id muss ein nicht-leerer str sein")
        state = self._devices.get(device_id)
        if state is None:
            state = DeviceState(
                device_id=device_id,
                wake=self._make_wake(device_id),
                buffer=TurnBuffer(),
                lock=asyncio.Lock(),
            )
            self._devices[device_id] = state
        return state

    def state_of(self, device_id: str) -> PipelineState:
        """Aktueller Zustand eines Geräts (für ws_server/Tests)."""
        state = self._devices.get(device_id)
        return state.state if state is not None else PipelineState.IDLE

    # ── Diagnose-Zugriff (P10.T3, additiv – **nur lesend**) ────────────
    def mic_synced(self, device_id: str) -> Optional[bool]:
        """True, sobald mindestens ein Mic-Frame (0x01) mit seq gesehen wurde.

        Grundlage ist `DeviceState.expected_seq` (von `_track_sequence` geführt):
        ``None`` = unbekanntes Gerät oder noch kein Frame, ``True`` = Sequenz-
        Tracker synchron („verbunden" erst nach Mic-Seq-Sync).
        Rein lesend, kein `await`, kein I/O.
        """
        state = self._devices.get(device_id)
        if state is None:
            return None
        return state.expected_seq is not None

    # ── Diagnose-Zugriffe (P9.T0 – **nur lesend**) ─────────────────────
    # Diese drei Methoden sind die einzige Schnittstelle, die das Dashboard
    # (`app/dashboard.py::collect_wake`) benutzt.  Bewusste Eigenschaften:
    #   * **kein `await`**, kein I/O, kein Modell – der Detektor-Ring wird
    #     hier nur als Datenstruktur gelesen, nie ausgewertet;
    #   * **Kopien** nach außen, damit ein HTTP-Aufrufer den Ring des
    #     laufenden Detektors nicht verändern kann;
    #   * **leer statt geraten**: unbekanntes Gerät ⇒ `[]`/`None`.
    def wake_device_ids(self) -> list[str]:
        """Geräte-IDs, für die derzeit ein Wake-Detektor existiert."""
        return [device_id for device_id, state in self._devices.items()
                if state.wake is not None]

    def wake_attempts(self, device_id: str) -> list[dict[str, Any]]:
        """Die letzten Wake-Versuche eines Geräts, jeweils mit `device_id`.

        Liest `WakeWordDetector.wake_attempts`.  Das ist im Repo eine
        **Property** (nicht Methode); eine Attrappe im Test darf sie als
        Methode anbieten – beide Formen werden akzeptiert, damit die
        Diagnose nicht an eine konkrete Implementierung gebunden ist.
        """
        state = self._devices.get(device_id)
        if state is None or state.wake is None:
            return []
        raw = getattr(state.wake, "wake_attempts", None)
        if callable(raw):
            try:
                raw = raw()
            except Exception as exc:  # pragma: no cover – Diagnose, nie der Turn
                _LOG.debug("wake_attempts(%s) nicht lesbar: %s", device_id, exc)
                return []
        if not isinstance(raw, (list, tuple, deque)):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            entry = dict(item)
            # Die Pipeline kennt die Geräte-Zuordnung – der Detektor nicht.
            entry["device_id"] = device_id
            out.append(entry)
        return out

    def wake_threshold(self) -> Optional[float]:
        """Die gerade **wirksame** Wake-Schwelle (repräsentatives Gerät).

        Barge-in senkt die Schwelle zeitweise auf `OWW_BARGE_IN_THRESHOLD`; der
        Detektor wertet gerade diese ab.  Ohne Gerät ⇒ `None` statt einer Zahl
        aus der Config (die wäre eine Behauptung, keine Messung).
        """
        for state in self._devices.values():
            threshold = getattr(state.wake, "threshold", None)
            if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
                return float(threshold)
        return None

    async def wait_turn(self, device_id: str, timeout: Optional[float] = None) -> None:
        """Auf den laufenden Turn eines Geräts warten (Test-/Shutdown-Hilfe)."""
        state = self._devices.get(device_id)
        task = state.turn_task if state is not None else None
        if task is None:
            return
        if timeout is None:
            await asyncio.shield(task)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            return

    # ── Öffentliche Handler (§5) ──────────────────────────────────────
    async def on_mic_frame(
        self,
        device_id: str,
        seq: Optional[int],
        pcm: Any,
    ) -> list[Any]:
        """Dauer-Empfang: **immer** an `wake_word` + Puffer (E5).

        Zusätzlich endpointet der **Wake-/Barge-in-Turn** manager-seitig
        (P7.T1-Fix): der permanente Stream des Geräts liefert **keine**
        `0x04`/`0x05`-Sentinels, deshalb wird hier Sprache/Stille erkannt und
        beim Sprachende `TRANSCRIBING` ausgelöst. Der Button-Pfad (K6) bleibt
        unberührt – er nutzt den `lock_mic`-Turn mit echten Sentinels.
        """
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise PipelineError(
                f"Mic-Payload muss bytes-artig sein, ist {type(pcm).__name__}"
            )
        state = self._state(device_id)
        data = bytes(pcm)
        self._track_sequence(state, seq)

        was_listening = state.state is PipelineState.LISTENING
        was_idle = state.state is PipelineState.IDLE
        if was_listening:
            state.buffer.push_turn(data)
        else:
            state.buffer.push_preroll(data)

        # Rauschboden nur im Ruhezustand nachführen (Referenz: Frames während
        # Turn/TTS werden nicht zur Schätzung herangezogen).
        if was_idle:
            self._update_noise_floor(state, data)

        events = state.wake.process(data)
        if events:
            state.last_score = events[-1].score
        for event in events:
            if state.state is PipelineState.IDLE:
                await self.on_wake_detected(device_id, event.score)
            elif state.state is PipelineState.SPEAKING and getattr(event, "barge_in", False):
                await self._barge_in(device_id, event.score)

        # Silence-Endpointing nur für den permanenten Wake-/Barge-in-Turn –
        # **nicht** für den Button-Turn (K6, `lock_mic` ⇒ Geräte-Sentinels).
        if (
            was_listening
            and state.state is PipelineState.LISTENING
            and not state.button_turn
        ):
            await self._maybe_endpoint(device_id, data)
        return events

    async def on_wake_detected(self, device_id: str, score: float) -> bool:
        """Wake ⇒ Turn: 240-ms-Preroll-Verwurf (E7), LED-Start, Timer (K5)."""
        state = self._state(device_id)
        # P8.D3: der übergebene Score wird im **bestehenden** Diagnosefeld
        # `DeviceState.last_score` abgelegt. Vor P8.D3 setzte das nur der
        # Mic-Frame-Pfad (`on_mic_frame`); ein direkt aufgerufenes
        # `on_wake_detected` (und damit auch `_barge_in`) verlor den Score.
        # Reines Diagnosefeld – **kein** Steuerungsfeld, kein Leser außer der
        # Turn-Historie, daher keine Verhaltensänderung.
        state.last_score = score
        if state.state is PipelineState.SPEAKING:
            await self._barge_in(device_id, score)
            return True
        if state.state is not PipelineState.IDLE:
            return False
        await self._arm_listening(
            device_id, trigger="wake", preroll=PREROLL_DISCARD_CHUNKS
        )
        return True

    async def on_vad_end(self, device_id: str) -> bool:
        """`0x04` VAD_END ⇒ Puffer an STT, Zustand `TRANSCRIBING`.

        Ohne gepufferte Mic-Chunks gibt es nichts zu transkribieren: analog zur
        `0x05`-Stille (E6/§5) endet der Turn **still** – kein STT/TTS/Fehlertext,
        zurück nach `IDLE` (nie in `TRANSCRIBING` hängen bleiben).
        """
        state = self._state(device_id)
        if state.state is not PipelineState.LISTENING:
            return False
        await self._cancel_timers(state)
        # P7.T2: der Turn verlässt `LISTENING` ⇒ ein wartender Blitz-Push (und
        # damit das nachgeschobene lila `listening`) ist nicht mehr fällig.
        await self._cancel_led_task(state)
        audio = state.buffer.drain()
        if not audio:
            await self._end_silent(device_id, reason="vad_end")
            return True
        # P9.T0: Ende der Sprache.  **Erst** hier – ein Turn ohne Audio hat
        # kein `speech_end`, und `latency_after_speech_seconds` bleibt dann
        # `None` statt einer erfundenen Zahl.
        self._mark(device_id, _PHASE_SPEECH_END)
        state.state = PipelineState.TRANSCRIBING
        state.cancelled = False
        state.turn_task = self._spawn(self._run_turn(device_id, audio))
        return True

    async def on_no_speech(self, device_id: str) -> bool:
        """`0x05` no-speech ⇒ **stillschweigen** (E6): kein TTS/Fehlertext."""
        state = self._state(device_id)
        if state.state is not PipelineState.LISTENING:
            return False
        await self._cancel_timers(state)
        await self._end_silent(device_id)
        return True

    async def on_button(
        self,
        device_id: str,
        click_type: int,
        *,
        held_ms: Optional[int] = None,
        muted: bool = False,
        down: bool = False,
    ) -> bool:
        """Button (E29/K6): `138/down:false` ⇒ Turn/Cancel."""
        state = self._state(device_id)
        if down or click_type != BUTTON_CLICK_TYPE:
            return False
        if held_ms is not None and held_ms >= HOLD_MS_THRESHOLD:
            _LOG.debug("Button-Hold %s ms – K6 bleibt Turn/Cancel", held_ms)

        if state.state is not PipelineState.IDLE:
            await self._send_control(device_id, serialize_speaker_flush())
            await self._cancel_active_turn(state)
            return True
        if muted:  # E29: muted blockiert nur den Turn
            return False

        await self._send_control(device_id, serialize_mic_stop())
        await self._send_control(
            device_id,
            serialize_mic_start(lock_mic=bool(self._settings.mic_start_lock_mic_button)),
        )
        await self._arm_listening(device_id, trigger="button", preroll=0)
        return True

    async def on_device_gone(self, device_id: str) -> None:
        """Gerät getrennt: laufenden Turn/Timer abbrechen, LED aus, Zustand weg."""
        state = self._devices.get(device_id)
        if state is None:
            return
        # P8.D3 (Beobachter): stand der Turn noch im `LISTENING`, endet er hier
        # **ohne** `_run_turn` (kein STT/TTS).  Ein bereits laufender Turn wird
        # von `_cancel_active_turn` ⇒ `_cleanup_turn` erfasst – deshalb wird
        # nur der `LISTENING`-Fall zusätzlich protokolliert (kein Doppeleintrag).
        was_listening = state.state is PipelineState.LISTENING
        await self._cancel_led_task(state)
        await self._cancel_active_turn(state)
        await self._cancel_timers(state)
        state.buffer.clear()
        await self._send_led(device_id, OFF_ANIM)
        self._devices.pop(device_id, None)
        if was_listening:
            self._record_turn(
                device_id,
                state=state,
                outcome="device_gone",
                error="still beendet: Gerät getrennt (on_device_gone)",
            )

    # ── Intern: Zustandsübergänge ─────────────────────────────────────
    def _track_sequence(self, state: DeviceState, seq: Optional[int]) -> None:
        if seq is None:
            return
        if state.expected_seq is not None and int(seq) != state.expected_seq:
            state.mic_gaps += 1
            _LOG.warning(
                "Mic-Lücke bei %s: seq=%s erwartet=%s",
                state.device_id,
                seq,
                state.expected_seq,
            )
        state.expected_seq = next_sequence(int(seq))

    async def _arm_listening(self, device_id: str, *, trigger: str, preroll: int) -> None:
        state = self._state(device_id)
        # P7.T2: ein noch wartender Blitz-Push eines Vor-Turns wird abgebrochen,
        # bevor der neue Turn seinen Zustand/LED setzt (kein doppelter Blitz).
        await self._cancel_led_task(state)
        available = state.buffer.preroll_count
        state.buffer.begin_turn()
        state.discarded_preroll = available if preroll > 0 else 0
        state.trigger = trigger
        state.button_turn = trigger == "button"
        state.cancelled = False
        state.outcome = None
        # P7.T1-Fix: Endpointing-Zähler pro Turn zurücksetzen (Rauschboden bleibt,
        # er ist eine Raum-Eigenschaft wie in der Referenz). Die ersten
        # `preroll`-Frames (Wake-Wort-Rest) überspringt das Endpointing – genau
        # wie die Referenz ihren `preroll_discard` (em_esphome._stream_mic_audio).
        state.endpoint_speech_seen = False
        state.endpoint_speech_frames = 0
        state.endpoint_silence_frames = 0
        state.endpoint_skip_frames = int(preroll)
        state.state = PipelineState.LISTENING
        # P8.D3 (Beobachter): Turn-Startzeit merken.  Reine Mitschrift in
        # `self._turn_started` – `DeviceState` und der Zustandsübergang bleiben
        # unberührt.
        # P9.T0: **eine** Uhrlesung für Turn-Start und Wake-Marke – sonst
        # wäre `latency_after_wake_seconds` um einen `monotonic()`-Aufruf
        # (≈ ns) größer als `duration_seconds`, und die Differenz wäre eine
        # Scheingenauigkeit, die ein Rundungsfehler sichtbar macht.
        started = time.monotonic()
        self._turn_started[device_id] = started
        # P9.T0: neue Phasenliste ⇒ der nächste Turn erbt **keine** Marke.
        # Bei Barge-in wird die alte Liste bewusst verworfen: der abgebrochene
        # Turn hat sie bereits in `_record_turn` geleert.
        self._phase_marks[device_id] = {}
        if trigger == "wake":
            # Wake-Marke nur für den echten Wake-Turn: der Button (K6) hat kein
            # Wake-Wort, und `latency_after_wake_seconds`/`phase_listen_seconds`
            # müssen dort ehrlich `None` bleiben.
            self._phase_marks[device_id][_PHASE_WAKE_DETECTED] = started
        if trigger == "wake":
            # P7.T2: der User-Wunsch gilt **nur** für das Wake-Wort aus `IDLE`.
            # Button (K6) und Barge-in haben den Turn schon per Push gestartet
            # bzw. laufen mitten in einem Turn – dort blitzt nichts (E88).
            await self._acknowledge_wake(device_id, state)
        else:
            await self._send_turn_start_led(device_id)
        await self._start_timers(device_id)

    def _update_noise_floor(self, state: DeviceState, pcm: bytes) -> None:
        """Per-Raum-Rauschboden, asymmetrischer EWMA (em_controller.py:2613-2619)."""
        rms = _frame_rms(pcm)
        floor = state.endpoint_noise_floor
        if floor == 0.0:
            state.endpoint_noise_floor = rms
        elif rms < floor:
            state.endpoint_noise_floor = floor + _NOISE_FLOOR_DOWN * (rms - floor)
        else:
            state.endpoint_noise_floor = floor + _NOISE_FLOOR_UP * (rms - floor)

    async def _maybe_endpoint(self, device_id: str, pcm: bytes) -> None:
        """Silence-Endpointing des Wake-/Barge-in-Turns (P7.T1-Fix).

        Frame-getrieben (jeder Mic-Chunk = 80 ms, kein Uhr-Warten):
        Sprache, sobald `rms >= max(3·noise_floor, VAD_THRESHOLD)` für
        `VAD_SPEECH_MS`; Turn-Ende, sobald nach erkannter Sprache
        `VAD_SILENCE_MS` Stille anhalten (Referenz-Parameter, s. Modul oben).
        """
        state = self._devices.get(device_id)
        if state is None or state.state is not PipelineState.LISTENING:
            return
        if state.endpoint_skip_frames > 0:
            # Wake-Wort-Rest (preroll_discard) – wie die Referenz nicht bewerten.
            state.endpoint_skip_frames -= 1
            return
        rms = _frame_rms(pcm)
        threshold = max(_NOISE_FLOOR_SNR * state.endpoint_noise_floor, VAD_THRESHOLD)
        is_speech = rms >= threshold

        if not state.endpoint_speech_seen:
            if is_speech:
                state.endpoint_speech_frames += 1
                if state.endpoint_speech_frames * _CHUNK_SECONDS * 1000.0 >= VAD_SPEECH_MS:
                    state.endpoint_speech_seen = True
                    state.endpoint_silence_frames = 0
            else:
                state.endpoint_speech_frames = 0
            return

        if is_speech:
            state.endpoint_silence_frames = 0
            return
        state.endpoint_silence_frames += 1
        if state.endpoint_silence_frames * _CHUNK_SECONDS * 1000.0 >= VAD_SILENCE_MS:
            _LOG.info(
                "Wake-Turn %s: %.0f ms Stille nach Sprache – Endpointing, "
                "kein 0x04/0x05 im permanenten Stream (P7.T1-Fix)",
                device_id,
                state.endpoint_silence_frames * _CHUNK_SECONDS * 1000.0,
            )
            await self.on_vad_end(device_id)

    async def _end_silent(self, device_id: str, *, reason: str = "no_speech") -> None:
        state = self._devices.get(device_id)
        if state is None or state.state is not PipelineState.LISTENING:
            return
        _LOG.info("Turn %s endet still (%s) – kein TTS (E6/K5)", device_id, reason)
        state.buffer.clear()
        await self._cancel_led_task(state)
        await self._cancel_timers(state, skip=asyncio.current_task())
        # P8.D3 (Beobachter): der **stille** Turn-Endpfad (`0x05`, Hard-Cap,
        # No-Speech, `0x04` ohne Audio) wird hier erfasst – vor dem Wechsel auf
        # `IDLE`, damit `state` den Zustand am Turn-Ende zeigt.  Reiner
        # Aufruf, keine Änderung an LED/Timern/Puffer.
        self._record_turn(
            device_id, state=state, outcome="silence", error=f"still beendet: {reason}"
        )
        state.state = PipelineState.IDLE
        await self._send_led(device_id, SILENCE_ANIM)

    async def _barge_in(self, device_id: str, score: float) -> None:
        _LOG.info("Barge-in bei %s (score=%.3f) – speaker_flush + neuer Turn", device_id, score)
        await self._send_control(device_id, serialize_speaker_flush())
        await self._cancel_active_turn(self._state(device_id))
        await self._arm_listening(
            device_id, trigger="barge_in", preroll=PREROLL_DISCARD_CHUNKS
        )

    async def _cancel_active_turn(self, state: DeviceState) -> None:
        task = state.turn_task
        if task is None or task.done():
            state.turn_task = None
            return
        state.cancelled = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("Turn-Abbruch %s: %s", state.device_id, exc)
        state.turn_task = None

    # ── Intern: Timer (Hard-Cap/No-Speech, K5) ────────────────────────
    async def _start_timers(self, device_id: str) -> None:
        state = self._state(device_id)
        state.timers = [
            self._spawn(self._timer(device_id, self._hard_cap, "hard_cap")),
            self._spawn(self._timer(device_id, self._no_speech, "no_speech")),
        ]

    async def _timer(self, device_id: str, seconds: float, reason: str) -> None:
        try:
            await self._sleep_seconds(seconds)
        except asyncio.CancelledError:
            raise
        await self._on_timeout(device_id, reason)

    async def _on_timeout(self, device_id: str, reason: str) -> None:
        state = self._devices.get(device_id)
        if state is None or state.state is not PipelineState.LISTENING:
            return
        _LOG.info("Turn %s Timeout (%s, %.1fs) – Puffer verwerfen (K5)",
                  device_id, reason, self._hard_cap if reason == "hard_cap" else self._no_speech)
        await self._end_silent(device_id, reason=reason)

    async def _cancel_timers(
        self, state: DeviceState, *, skip: Optional[asyncio.Task[Any]] = None
    ) -> None:
        tasks = list(state.timers)
        state.timers = []
        for task in tasks:
            if task is skip or task.done():
                continue
            task.cancel()
        for task in tasks:
            if task is skip or task.done():
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover – defensiv
                pass

    async def _sleep_seconds(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    # ── Intern: Phasen-Zeitpunkte (P9.T0 – **reiner Beobachter**) ──────
    def _mark(self, device_id: str, name: str) -> None:
        """Einen Phasen-Zeitpunkt (monoton) für dieses Gerät mitschreiben.

        Bewusste Eigenschaften (P9.T0):

        * **Kein ``await``**, kein I/O, kein Log – ein reiner
          ``time.monotonic()``-Aufruf.  Damit kann die Marke keinen Zustands-
          übergang verschieben und keine neue Yield-Möglichkeit erzeugen
          (E94 (c): der Messpunkt darf den Ablauf nicht verändern).
        * **Ein `float` je Marke**: `_arm_listening` setzt die Marken eines
          neuen Turns zurück, ein Turn kann also nie eine alte Marke erben.
        * Fehler werden hier bewusst **nicht** abgefangen: `monotonic()` ist
          die verlässlichste Funktion der Standardbibliothek.  Eine Ausnahme
          hieße ein kaputter Prozess, kein Messproblem – und der wäre im
          Dashboard sowieso nicht mehr sinnvoll darstellbar.
        """
        self._phase_marks.setdefault(device_id, {})[name] = time.monotonic()

    @staticmethod
    def _phase_span(
        marks: Optional[Mapping[str, float]], start: str, end: str
    ) -> Optional[float]:
        """Dauer `end - start` in Sekunden, auf 3 Nachkommastellen gerundet.

        Die ehrliche Grundregel von P9.T0: **eine nicht erreichte Phase ist
        ``None``, niemals 0.**  Fehlt einer der beiden Zeitpunkte, wird nichts
        gerechnet und nichts geschätzt.  Vorhandene Werte werden geklemmt
        (defensiv gegen eine kaputte/gesprungene Uhr – ein negativer Wert in
        der Historie wäre eine erfundene Aussage) und gerundet; eine echte
        Dauer von 0,0 bleibt dabei 0,0.
        """
        if not marks:
            return None
        first = marks.get(start)
        last = marks.get(end)
        if first is None or last is None:
            return None
        return round(max(0.0, float(last) - float(first)), 3)

    def _phase_fields(self, device_id: str) -> dict[str, Optional[float]]:
        """Die neun P9.T0-Felder für dieses Gerät – **nur** gelesen.

        Die Zeilen sind absichtlich einzeln geschrieben statt in einer
        Schleife über `TURN_LATENCY_FIELDS`: der Feldname steht dabei je Zeile
        als **Literal** an beiden Stellen (Start-/Endmarke und Historienname),
        nicht als indirekter Schlüssel.  Sonst wäre eine Mutation an genau
        diesem Mapping wirkungslos (E56/E59) und ein Tippfehler fiele erst im
        Dashboard auf.
        """
        marks = self._phase_marks.get(device_id)
        return {
            "latency_after_speech_seconds": self._phase_span(
                marks, _PHASE_SPEECH_END, _PHASE_HA_DONE
            ),
            "latency_after_wake_seconds": self._phase_span(
                marks, _PHASE_WAKE_DETECTED, _PHASE_HA_DONE
            ),
            "phase_stt_seconds": self._phase_span(
                marks, _PHASE_STT_START, _PHASE_STT_DONE
            ),
            "phase_route_seconds": self._phase_span(marks, _PHASE_STT_DONE, _PHASE_JEV_DONE),
            "phase_tts_first_audio_seconds": self._phase_span(
                marks, _PHASE_TTS_START, _PHASE_TTS_FIRST_AUDIO
            ),
            "phase_tts_total_seconds": self._phase_span(
                marks, _PHASE_TTS_START, _PHASE_TTS_DONE
            ),
            "phase_listen_seconds": self._phase_span(
                marks, _PHASE_WAKE_DETECTED, _PHASE_SPEECH_END
            ),
            "phase_execute_seconds": self._phase_span(marks, _PHASE_JEV_DONE, _PHASE_HA_DONE),
            "phase_tts_first_frame_seconds": self._phase_span(
                marks, _PHASE_TTS_START, _PHASE_TTS_FIRST_FRAME
            ),
        }

    # ── Intern: Endpoint-Diagnostik (P9.T5, E100 – **reiner Beobachter**) ──
    @staticmethod
    def _endpoint_fields(state: DeviceState) -> dict[str, Any]:
        """Die sechs P9.T5-Felder aus dem **bereits vorhandenen** Turn-State.

        Bewusste Eigenschaften (exakt dem P9.T0-Muster folgend):

        * **Nur lesen.**  `DeviceState` wurde in `_arm_listening`/`_maybe_
          endpoint` gefüllt; hier wird nichts zurückgeschrieben, gerundet
          wird nur in den **Historienwerten**, nie im State.
        * `endpoint_threshold` ist die **wirksame** Schwelle des Endpointings
          (`max(_NOISE_FLOOR_SNR · endpoint_noise_floor, VAD_THRESHOLD)`) am
          Turn-Ende – dieselbe Formel wie `_maybe_endpoint`, damit Messwert
          und Verhalten garantiert übereinstimmen.
        * **Button-Turn ⇒ alles `None`**: der K6-Pfad endpointet manager-
          seitig nie (`on_mic_frame` überspringt ihn bewusst, Geräte-
          Sentinels 0x04/0x05), also wäre jede Zahl eine Erfindung.  Der
          Rauschboden ist zwar eine Raum-Eigenschaft, die auch beim Button-
          Turn einen Wert hätte – aber ohne bewertete Frames wäre
          `endpoint_threshold` kein Messwert, sondern eine Rechnung ohne
          Anlass; ehrlich `None`.
        """
        if state.button_turn:
            return {name: None for name in ENDPOINT_HISTORY_FIELDS}
        threshold = max(_NOISE_FLOOR_SNR * state.endpoint_noise_floor, VAD_THRESHOLD)
        return {
            "endpoint_noise_floor": round(float(state.endpoint_noise_floor), 6),
            "endpoint_threshold": round(float(threshold), 6),
            "endpoint_speech_frames": int(state.endpoint_speech_frames),
            "endpoint_silence_frames": int(state.endpoint_silence_frames),
            "endpoint_skip_frames": int(state.endpoint_skip_frames),
            "endpoint_speech_seconds": round(
                float(state.endpoint_speech_frames) * _CHUNK_SECONDS, 3
            ),
        }

    def _dump_turn_audio(
        self,
        device_id: str,
        *,
        state: DeviceState,
        transcript: Optional[str],
        audio: bytes,
    ) -> None:
        """P9.T5: das Turn-Audio config-gated ablegen (**nie** in den Turn).

        Der Gate-Zugriff ist absichtlich `getattr`-defensiv: Fakes in den
        Tests bauen ihre Settings als schlanke Dataclasses, und der Dump
        darf **keine** einzige Zeile der Turn-Fehlerbehandlung berühren.
        Das `outcome` wird wie in `_record_turn` abgeleitet, damit WAV-/JSON-
        Name und Historieneintrag zusammenpassen.
        """
        enabled = bool(getattr(self._settings, "audio_dump_enabled", False))
        if not enabled or not audio:
            return
        dump_dir = str(getattr(self._settings, "audio_dump_dir", "") or "")
        outcome = (
            "cancelled"
            if state.cancelled
            else (str(state.outcome) if state.outcome else "unknown")
        )
        dump_turn_audio(
            enabled=True,
            dump_dir=dump_dir,
            device_id=device_id,
            outcome=outcome,
            transcript=transcript,
            stats=self._endpoint_fields(state),
            audio=audio,
            max_files=int(
                getattr(self._settings, "audio_dump_max_files", 50) or 50
            ),
            max_bytes=int(
                getattr(self._settings, "audio_dump_max_bytes", 100 * 1024 * 1024)
                or 100 * 1024 * 1024
            ),
        )

    # ── Intern: Turn-Historie (P8.D3 – **reiner Beobachter**) ──────────
    def _record_turn(
        self,
        device_id: str,
        *,
        state: DeviceState,
        outcome: Optional[str] = None,
        transcript: Optional[str] = None,
        decision: Any = None,
        error: Optional[str] = None,
    ) -> None:
        """**Einen** Turn in `self.turn_history` mitschreiben (Allowlist).

        Bewusste Eigenschaften (P8.D3):

        * **Synchron** – kein ``await``, also weder unterbrechbar noch ein
          Zeitpunkt, an dem die Steuerung auf diesen Aufruf warten könnte.
        * **Kein Eingriff** in `state`, `DeviceState`, Return-Werten oder
          Fehlerbehandlung. Es wird **gelesen**, nie geschrieben.
        * **Genau ein Aufruf pro Turn**: `_run_turn` im `finally` **vor**
          `_cleanup_turn` (dort wird `state.outcome` genullt), `_end_silent`
          vor dem Wechsel auf `IDLE`, `on_device_gone` nur, wenn der Turn noch
          im `LISTENING` stand (sonst hat `_cancel_active_turn` schon erfasst).
        * **Eigene Fehler** werden abgefangen und als `WARNING` gemeldet – der
          Beobachter darf einen Turn **nie** abbrechen. Umgekehrt wird kein
          `try`/`except` **um** die bestehende Fehlerbehandlung gelegt.
        * `outcome` wird **nur** überschrieben, wenn der Aufrufer es explizit
          kennt (Stille-/Gerät-weg-Pfade); sonst gilt `cancelled` bzw. der
          vom Turn gesetzte `state.outcome`.
        """
        try:
            started = self._turn_started.pop(device_id, None)
            # P9.T0: Phasen **vor** dem `append` lesen und die Liste sofort
            # freigeben – `pop` in jedem Endpfad (`_run_turn`/`_end_silent`/
            # `on_device_gone`), damit kein `dict` pro Turn liegen bleibt und
            # der nächste Turn nicht fremde Marken erbt.  Wird die
            # Historie-Aufzeichnung selbst scheitern, ist der Puffer trotzdem
            # frei – ein Leck wäre schlimmer als ein fehlender Eintrag.
            phase_fields = self._phase_fields(device_id)
            self._phase_marks.pop(device_id, None)
            if outcome is None:
                if state.cancelled:
                    outcome = "cancelled"
                elif state.outcome:
                    outcome = str(state.outcome)
                else:
                    outcome = "unknown"
            # `last_score == 0.0` heißt "kein Wake-Event gesehen" (z. B.
            # Button-Turn) – das ist *keine* Messung und wird als `None` gemeldet.
            score = _optional_float(state.last_score)
            self.turn_history.append(
                {
                    "ts": _utc_now_iso(),
                    "device_id": device_id,
                    "state": _state_value(state.state),
                    "outcome": outcome,
                    "duration_seconds": (
                        round(max(0.0, time.monotonic() - started) / 1000.0, 3)
                        if started is not None
                        else None
                    ),
                    "intent": _optional_str(getattr(decision, "intent", None)),
                    "service": _optional_str(getattr(decision, "service", None)),
                    "domain": _optional_str(getattr(decision, "domain", None)),
                    "target_entity_id": _optional_str(
                        getattr(decision, "entity_id", None)
                    ),
                    "confidence": _optional_float(getattr(decision, "confidence", None)),
                    "score": score if (score is None or score > 0.0) else None,
                    "needs_param": _needs_param_flag(decision),
                    "extracted_param": _extracted_param(decision),
                    "transcript": _history_text(transcript),
                    "error_code": _optional_str(getattr(decision, "error_code", None)),
                    "error": _history_text(error if error else _decision_detail(decision)),
                    "barge_in": state.trigger == "barge_in",
                    # P9.T0: die neun additiven Phasenfelder.  `None` heißt
                    # „nicht erreicht/erreichbar" – z. B. `latency_after_wake`
                    # bei einem Button-Turn oder `latency_after_speech` bei
                    # einer QUESTION ohne HA.  Vorhandene Felder aus
                    # P8.D3 bleiben unverändert.
                    **phase_fields,
                    # P9.T5 (E100): die sechs additiven Endpoint-Diagnosefelder
                    # (reines Beobachten des bereits geführten Turn-States;
                    # Button-Turn ⇒ alles `None`).  `noise_floor`/Frames sind
                    # keine personenbezogenen Daten – keine Redaction nötig.
                    **self._endpoint_fields(state),
                }
            )
        except Exception as exc:  # pragma: no cover – Beobachter, nie der Turn
            _LOG.warning("Turn-Historie %s nicht erfasst: %s", device_id, exc)

    # ── Intern: Turn-Ausführung ───────────────────────────────────────
    async def _run_turn(self, device_id: str, audio: bytes) -> None:
        state = self._state(device_id)
        # P8.D3 (Beobachter): Mitschrift-Lokale.  Reine Zuweisungen – sie
        # ändern keinen Zustandsübergang, keinen Return-Wert und keine
        # Fehlerbehandlung; sie werden ausschließlich von `_record_turn` gelesen.
        transcript: Optional[str] = None
        decision: Any = None
        error: Optional[str] = None
        async with state.lock:  # per-Device-Lock: kein Parallel-Turn (§5)
            try:
                state.state = PipelineState.TRANSCRIBING
                await self._send_led(device_id, THINKING_ANIM)
                # P9.T0: STT-Marke **vor** dem Aufruf – der Aufruf ist die
                # gemessene Arbeit.  Sie steht vor dem LED-Push, damit die
                # Zeit von „der Manager hat den Turn übernommen" bis
                # „Transkript da" gemessen wird.
                self._mark(device_id, _PHASE_STT_START)
                try:
                    transcript = await self._stt_client().transcribe(audio)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # P9.T0: `stt_done` auch im **Fehler**fall – der STT-Versuch
                    # ist beendet (mit Fehler), nicht ungeschehen.  Abbruch
                    # (`CancelledError`) markiert bewusst **nicht**: dort ist
                    # keine Dauer gemessen, sondern ein unvollendeter Versuch.
                    self._mark(device_id, _PHASE_STT_DONE)
                    _LOG.warning("STT-Fehler %s: %s", device_id, exc)
                    error = f"STT: {type(exc).__name__}: {exc}"
                    state.outcome = "error"
                    await self._safe_speak(device_id, FALLBACK_SPEECH)
                    return
                # P9.T0: Transkript ist da – STT ist beendet.  Steht **vor**
                # der Leerprüfung, denn ein leeres Transkript ist ein
                # abgeschlossener STT (nur ohne Routing).
                self._mark(device_id, _PHASE_STT_DONE)
                if not isinstance(transcript, str) or not transcript.strip():
                    state.outcome = "silence"
                    return

                state.state = PipelineState.ROUTING
                decision = await self._router_client().route_with_fallback(
                    transcript.strip()
                )
                # P9.T0: Routing ist beendet (ob COMMAND oder QUESTION) – ab
                # hier beginnt für einen COMMAND erst die Ausführung.
                self._mark(device_id, _PHASE_JEV_DONE)
                if getattr(decision, "is_command", False):
                    state.state = PipelineState.EXECUTING
                    decision = await self._router_client().execute(decision)
                    # P9.T0: `ha_done` **nach** `execute` ⇒ nur ein erfolgreicher
                    # HA-Call erzeugt die Schlüsselzahl.  Wirft `execute`, bleibt
                    # sie `None` – „nicht gemessen", nicht „0 s".
                    self._mark(device_id, _PHASE_HA_DONE)
                else:
                    state.state = PipelineState.ANSWERING

                text = getattr(decision, "response_text", None) or FALLBACK_NOT_UNDERSTOOD
                state.outcome = "error" if getattr(decision, "is_error", False) else "ok"
                await self._speak(device_id, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Programmfehler/unerwartete Client-Fehler
                _LOG.exception("Turn-Fehler %s: %s", device_id, exc)
                error = f"{type(exc).__name__}: {exc}"
                state.outcome = "error"
                await self._safe_speak(device_id, FALLBACK_NOT_UNDERSTOOD)
            finally:
                # P8.D3: **vor** `_cleanup_turn` – dort werden `state.outcome`
                # genullt und der Zustand auf `IDLE` gezogen.  Der synchrone
                # Aufruf kann den Aufräum-Pfad nicht stören; der `finally`
                # bleibt auch bei Abbruch/Cancel erhalten.
                self._record_turn(
                    device_id, state=state, transcript=transcript,
                    decision=decision, error=error,
                )
                # P9.T5 (E100): config-gated Audio-Dump – **nach** `_record_turn`
                # (outcome/Stats konsistent zum Historieneintrag), **vor**
                # `_cleanup_turn` (das `state.outcome` nullt).  Wirft der Dump,
                # fängt er sich selbst (`audio_dump.py`); der Turn bleibt unberührt.
                self._dump_turn_audio(
                    device_id, state=state, transcript=transcript, audio=audio
                )
                await self._cleanup_turn(device_id)

    async def _speak(self, device_id: str, text: str) -> None:
        state = self._state(device_id)
        state.state = PipelineState.SPEAKING
        if not state.cancelled:
            state.wake.set_speaking(True)
        # P9.T0: TTS-Phase.  `tts_start` **vor** `synthesize`, damit die reine
        # Piper-/Netzwerkzeit im Feld steckt.  Ein Abbruch (`CancelledError`)
        # lässt `tts_done` leer ⇒ `phase_tts_total_seconds` bleibt `None`
        # (unvollendet, nicht „0 s").
        self._mark(device_id, _PHASE_TTS_START)

        chunks: list[tuple[int, bytes]] = []
        first_audio_marked = False
        async for rate, pcm in self._tts_client().synthesize(text):
            # P9.T0: „erstes Audio von Piper" = der erste **nichtleere** PCM
            # Chunk.  Leere Chunks (Heartbeat/Flush ohne Daten) zählen nicht –
            # sonst stünde die Zahl dort, wo das Gerät noch nichts hört.
            if pcm and not first_audio_marked:
                self._mark(device_id, _PHASE_TTS_FIRST_AUDIO)
                first_audio_marked = True
            chunks.append((int(rate), bytes(pcm)))

        resampler = SpeakerResampler(out_rate=self._settings.audio_speaker_rate)
        rate: Optional[int] = None
        parts: list[bytes] = []
        for item_rate, item_pcm in chunks:
            if item_rate != rate:
                resampler.set_input_rate(item_rate)
                rate = item_rate
            if item_pcm:
                parts.append(resampler.resample(item_pcm))
        parts.append(resampler.flush())
        chunker = SpeakerChunker(self._settings.speaker_chunk_bytes)

        total_bytes = sum(len(part) for part in parts)
        seconds = total_bytes / self._speaker_bytes_per_second()
        ttl = max(_METER_TTL_FLOOR, int(2 * seconds + 20))
        await self._send_led(
            device_id,
            {"pattern": "meter", "colors": list(METER_COLOR), "ttlSec": ttl},
        )

        # P9.T0: „erstes Frame raus" = der erste **tatsächlich gesendete**
        # Audio-Frame.  Der Chunker sammelt erst `speaker_chunk_bytes`; genau
        # diese Verzögerung ist die Zahl, die auf dem Dot hörbar ist.  Sie ist
        # deshalb **später** als `tts_first_audio` und wird nie aus der
        # Pufferlänge geschätzt.
        first_frame_marked = False

        for part in parts:
            for chunk in chunker.feed(part):
                if not first_frame_marked:
                    self._mark(device_id, _PHASE_TTS_FIRST_FRAME)
                    first_frame_marked = True
                await self._send_binary(device_id, build_speaker_frame(chunk))
        tail = chunker.flush()
        if tail:
            if not first_frame_marked:
                self._mark(device_id, _PHASE_TTS_FIRST_FRAME)
                first_frame_marked = True
            await self._send_binary(device_id, build_speaker_frame(tail))
        # P9.T0: erst **nach** dem EOS-Frame ist die TTS-Phase beendet.  Gab es
        # kein Audio (nur EOS), bleiben „erstes Audio"/„erstes Frame" `None`,
        # die Gesamtdauer ist trotzdem eine Zahl – es gab ja einen TTS-Aufruf.
        await self._send_binary(device_id, build_speaker_eos())
        self._mark(device_id, _PHASE_TTS_DONE)

    async def _safe_speak(self, device_id: str, text: str) -> None:
        try:
            await self._speak(device_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("TTS-Fehler %s: %s – Turn endet ohne Ton", device_id, exc)

    async def _cleanup_turn(self, device_id: str) -> None:
        state = self._devices.get(device_id)
        if state is None:
            return
        await self._cancel_timers(state)
        # P7.T2: nach dem Turn ist ein wartender Blitz-Push überholt.
        await self._cancel_led_task(state)
        state.buffer.clear_turn()
        try:
            state.wake.set_speaking(False)
            state.wake.reset()
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("Wake-Reset %s: %s", device_id, exc)

        if state.button_turn:
            await self._send_control(device_id, serialize_mic_stop())
            await self._send_control(device_id, serialize_mic_start())
            state.button_turn = False

        if not state.cancelled:
            await self._send_outcome_led(device_id, state.outcome)
        state.outcome = None
        state.turn_task = None
        state.state = PipelineState.IDLE

    # ── Intern: LED (E11/K7) ──────────────────────────────────────────
    async def _acknowledge_wake(self, device_id: str, state: DeviceState) -> None:
        """P7.T2/E88: Bernstein-Blitz, danach das lila `listening`.

        Der Blitz ist ein **transientes Overlay**, kein eigener Zustand – er
        ändert `state.state` nicht und beendet den Turn nicht. Weil das Gerät
        jede `led_anim` sofort anwendet, muss der `listening`-Push um
        `WAKE_ACK_FLASH_SECONDS` **verschoben** werden, sonst wäre der Blitz
        nicht sichtbar. Der Task sendet deshalb nur, wenn
        (a) er noch der aktuelle Blitz ist (`led_epoch`),
        (b) der Turn noch `LISTENING` ist – sonst hat der Zustandsautomat
            (`thinking`/`speaking`/`off`) bereits gesendet und dürfte nicht
            überschrieben werden.

        **P7.T2b/E89 (Korrektur):** das `ttlSec: 1` des Blitzes ist ein echter
        Dead-Man – verfällt er ohne Nachfolger, schwärzt **das Gerät** den Ring
        (`server/animator.go:154-163`). Genau deshalb ist der Nachfolger zwingend
        (→ `_send_listening_led`) und muss auf dem Ring ankommen, nicht nur im
        Config-Cache des Geräts.
        """
        state.led_epoch += 1
        epoch = state.led_epoch
        await self._send_led(device_id, WAKE_ACK_ANIM)
        state.led_task = self._spawn(self._delayed_turn_start_led(device_id, epoch))

    async def _delayed_turn_start_led(self, device_id: str, epoch: int) -> None:
        """Nach dem Blitz das lila `listening` – oder gar nichts (P7.T2/E88).

        P7.T2b/E89: das Lila geht als **`led_anim`** auf den Ring, weil nur das
        den Ring tatsächlich neu malt (Firmware `StartAnim`); der
        `config`-Push allein cached nur. Der Wächter bleibt unverändert: ist der
        Turn nicht mehr `LISTENING`, geht **nichts** raus (`thinking`/`speaking`/
        `off` dürfen nicht überschrieben werden).
        """
        try:
            await self._sleep_seconds(WAKE_ACK_FLASH_SECONDS)
            state = self._devices.get(device_id)
            if (
                state is not None
                and state.led_epoch == epoch
                and state.state is PipelineState.LISTENING
            ):
                await self._send_listening_led(device_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("Wake-Blitz %s abgebrochen: %s", device_id, exc)
        finally:
            state = self._devices.get(device_id)
            if state is not None and state.led_task is asyncio.current_task():
                state.led_task = None

    async def _cancel_led_task(self, state: DeviceState) -> None:
        """P7.T2: wartenden Blitz-Push abbrechen (Task lebt nur in `led_task`).

        Bewusst getrennt von `_cancel_timers` (K5-Sicherheitsnetze) und mit
        demselben Muster inkl. `skip` – der Task darf sich nie selbst abwarten.
        """
        task = state.led_task
        state.led_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover – defensiv
            _LOG.warning("Blitz-Abbruch %s: %s", state.device_id, exc)

    async def _send_listening_led(self, device_id: str) -> None:
        """Wake-Overlay → **dauerhaft lila auf dem Ring** (P7.T2b, E89).

        Zwei Nachrichten, zwei Jobs – genau wie die Referenz sie trennt:

        1. `config.listeningAnim` (unverändert `_send_turn_start_led`): cached
           die Spec im Gerät, damit es den Ring beim **eigenen** Wake-Crossing
           selbst malt (`cmd/server.go:1328`).
        2. **`led_anim` mit derselben Spec**: malt den Ring **jetzt** und hält
           ihn bis zum nächsten Anim (`ttlSec` 30 > Hard-Cap 15 s). Ohne diese
        zweite Nachricht blieb der Ring nach dem Blitz schwarz, weil der
        `config`-Push den Ring nicht anfasst (P7.T2b, E89).

        Der Button-Pfad (K6) ruft weiterhin nur `_send_turn_start_led` – er ist
        vom P7.T2b-Fix bewusst nicht betroffen.
        """
        await self._send_turn_start_led(device_id)
        await self._send_control(device_id, serialize_led_anim(_listening_anim_spec()))

    async def _send_turn_start_led(self, device_id: str) -> None:
        """Turn-Start: `solid[110,0,45]`+`listening` (E11), auch nach Blitz (P7.T2)."""
        try:
            message = build_listening_anim_push()
        except DeviceConfigError:
            message = serialize_led_anim(
                {"pattern": "solid", "colors": list(_SOLID_COLOR), "ttlSec": 30}
            )
        await self._send_control(device_id, message)

    async def _send_outcome_led(self, device_id: str, outcome: Optional[str]) -> None:
        if outcome == "error":
            await self._send_led(device_id, ERROR_ANIM)
        elif outcome == "silence":
            await self._send_led(device_id, SILENCE_ANIM)
        else:
            await self._send_led(device_id, OFF_ANIM)

    async def _send_led(self, device_id: str, anim: dict) -> None:
        await self._send_control(device_id, serialize_led_anim(anim))

    # ── Intern: Sonstiges ─────────────────────────────────────────────
    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        return asyncio.ensure_future(coro)

    def _speaker_bytes_per_second(self) -> int:
        return (
            int(self._settings.audio_speaker_rate)
            * int(self._settings.audio_width)
            * int(self._settings.audio_channels)
        )
