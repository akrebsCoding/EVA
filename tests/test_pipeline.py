"""Pipeline-Tests (P5.T6, `PLAN.md` §7 → P5.T6, Layer **L0/`unit`**).

Prüfling ist `app/pipeline.py` (P5.T0, Zustandsmaschine `PLAN.md` §5).  Getestet
wird gegen den **verbindlichen Vertrag** aus `STATE.md` §3 („Pipeline (P5.T0)")
und **E60**, nicht gegen eine Wunschfassung.  P5.T6 hielt den Prüfling zunächst
**unangetastet**; der **P5.T6-Nachtrag** behebt den dort gefundenen Bug
(leerer Puffer bei `0x04` ⇒ Hängen in `TRANSCRIBING`) und stellt den Test auf
das korrekte Verhalten um.

Abdeckung der **neun Pflichtfälle** aus `PLAN.md:477`:

1. **Zustandsübergänge** mit Fake-STT/TTS/HA/Router/WakeDetector — jeder
   Fake notiert den Zustand **zum Aufrufzeitpunkt** (STT sieht
   `TRANSCRIBING`, Router `ROUTING`, `execute` `EXECUTING`, TTS `SPEAKING`);
   COMMAND führt zu `EXECUTING`, QUESTION überspringt es →
   `test_state_sequence_*`
2. **Dauer-Stream ohne Turn** — Mic-Frames ohne Wake ⇒ kein Turn, `IDLE`
   bleibt, **kein** Control/Binary gesendet → `test_continuous_stream_*`
3. **Wake→Turn** inkl. **240-ms-Preroll-Verwurf** (3 Chunks, E7) →
   `test_wake_turn_*`, `test_preroll_discard_constant_is_three`
4. **`0x04`** VAD-End ⇒ Transkription → `test_vad_end_*`
5. **`0x05`-Stille** — kein TTS, kein Fehlertext (E6) → `test_no_speech_*`
6. **Hard-Cap** 15 s ⇒ Turn abgebrochen → `test_hard_cap_*`
7. **Barge-in** im `SPEAKING` ⇒ `speaker_flush` + Cancel → `test_barge_in_*`
8. **Mic-Lücke** — Sequenzlücke wird behandelt → `test_mic_gap_*`
9. **Fehlerpfade** STT/TTS/HA(Router)/Router ⇒ definierter Zustand + Cleanup
   (`try/finally`) → `test_*_error_*`, `test_cleanup_*`

**Nachtrag P7.T2 (E88, User-Entscheidung 2026-09-27):** Nach „Hey EVA" gab es
**keinerlei** LED-Feedback. Jetzt: **Bernstein-Blitz `solid[255,170,0]`** für
`WAKE_ACK_FLASH_SECONDS` (0,4 s), danach unverändert das lila `listening` →
Abschnitt 3c (`test_wake_flash_*`, `test_button_turn_has_no_wake_flash`).
Geprüft wird die **Reihenfolge** (Blitz **vor** `listening`, kein Doppelblitz),
die ** Kollisionsfreiheit** (laufender Turn, `0x05`-Abbruch, Gerätetrennung) und
das **Task-Leak**-Verhalten – wieder über die injizierte Uhr, kein reales Warten.

**Kein echtes Netz, kein Gerät, kein Modell.**  Alle Gegenstellen sind
injizierte Fakes (E60); der `wake_detector` liefert skriptete Events, der
`sleep` ist eine **injizierte Uhr** (Gate pro Sekundenwert), damit weder
Hard-Cap (15 s) noch No-Speech (8 s) real gewartet werden.  Die autouse-
Netzsperre aus `tests/conftest.py` (E36) ist aktiv und wird positiv belegt.

**Deterministisch:** kein `random`, kein reales `asyncio.sleep(>0)`; die
Zustandsfolge wird über Aufruf-Snapshots der Fakes geprüft, nicht über Timing.

**Prüfwerte sind Literale** (E56/E59): die Fallback-Texte und LED-Anims sind
lokal definiert, **nicht** aus `app.pipeline`/`app.router` importiert — sonst
wären Mutationen an genau diesen Werten unwirksam.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from app.pipeline import (
    HARD_CAP_SECONDS,
    NO_SPEECH_SECONDS,
    PREROLL_DISCARD_CHUNKS,
    WAKE_ACK_FLASH_SECONDS,
    Pipeline,
    PipelineError,
    PipelineState,
    TurnBuffer,
)
from app.router import (
    INTENT_COMMAND,
    INTENT_ERROR,
    INTENT_QUESTION,
    RouteDecision,
)
from tests.conftest import NetworkAccessBlocked

pytestmark = pytest.mark.unit

# ── Literale (unabhängig vom Prüfling) ───────────────────────────────────
DEV = "dev-A"
CHUNK = 2560
#: Fallback-Texte wörtlich `app/router.py`/v4 §7.2 (bewusst als Literale).
FALLBACK_NOT_UNDERSTOOD = "Ich habe dich nicht verstanden."
FALLBACK_SPEECH = "Spracherkennung fehlgeschlagen."

#: Wire-Nachrichten (wörtlich `app/protocol.py`).
CTRL_MIC_STOP = {"type": "mic_stop"}
CTRL_MIC_START = {"type": "mic_start"}
CTRL_MIC_START_LOCK = {"type": "mic_start", "lock_mic": True}
CTRL_SPEAKER_FLUSH = {"type": "speaker_flush"}
LED_SILENCE = {"type": "led_anim", "anim": {"pattern": "pulse", "periodMs": 900, "ttlSec": 1}}
LED_ERROR = {"type": "led_anim", "anim": {"pattern": "pulse", "periodMs": 220, "ttlSec": 1}}
LED_OFF = {"type": "led_anim", "anim": {"pattern": "off"}}
#: P7.T2 (E88): Wake-Quittierung – Bernstein-Blitz, danach lila `listening`.
#: Literale, **nicht** aus `app.pipeline` importiert (E56/E59-Konvention).
LED_WAKE_ACK = {
    "type": "led_anim",
    "anim": {"pattern": "solid", "colors": [[255, 170, 0]], "ttlSec": 1},
}
#: Denken-Orange (`app/pipeline.THINKING_ANIM`, wörtlich als Literal).
LED_THINKING = {
    "type": "led_anim",
    "anim": {
        "pattern": "spin",
        "colors": [[210, 45, 0], [55, 8, 0]],
        "periodMs": 80,
        "ttlSec": 135,
    },
}
LISTENING_PUSH = {
    "type": "config",
    "listeningAnim": {
        "pattern": "solid",
        "colors": [[110, 0, 45]],
        "listening": True,
        "ttlSec": 30,
    },
}
#: P7.T2b (E89): dasselbe Lila als **`led_anim`** – die Spec aus
#: `LISTENING_PUSH["listeningAnim"]` unter `anim`. Nur `led_anim` malt den Ring
#: neu (`StartAnim`); der `config`-Push cached ihn nur im Gerät.
LED_LISTENING = {
    "type": "led_anim",
    "anim": {
        "pattern": "solid",
        "colors": [[110, 0, 45]],
        "listening": True,
        "ttlSec": 30,
    },
}

EOS = b"\x03"


def pcm(index: int) -> bytes:
    """Eindeutiger 2560-B-Chunk (Inhalt dient nur der Byte-Gleichheit)."""
    return bytes([index & 0xFF]) * CHUNK


def loud() -> bytes:
    """2560-B-Chunk mit RMS 0.5 (Sprache für das Endpointing, P7.T1-Fix)."""
    return b"\x00\x40" * (CHUNK // 2)


# ── Fakes (injiziert, kein Netz) ─────────────────────────────────────────
@dataclass
class FakeEvent:
    """Minimalform eines `WakeEvent` (`.score`, `.barge_in`)."""

    score: float = 0.95
    barge_in: bool = False


class FakeWake:
    """Ersatz für `WakeWordDetector`: skriptete Events + Aufrufzähler."""

    def __init__(self) -> None:
        self._queue: list[FakeEvent] = []
        self.processed: list[bytes] = []
        self.speaking_calls: list[bool] = []
        self.reset_calls = 0

    def queue(self, *events: FakeEvent) -> None:
        self._queue.extend(events)

    def process(self, pcm: bytes) -> list[FakeEvent]:
        self.processed.append(bytes(pcm))
        if self._queue:
            return [self._queue.pop(0)]
        return []

    def set_speaking(self, speaking: bool) -> None:
        self.speaking_calls.append(bool(speaking))

    def reset(self) -> None:
        self.reset_calls += 1


class FakeStt:
    """Ersatz für `SttClient`: liefert ein Transkript oder wirft."""

    def __init__(
        self,
        transcript: str = "schalte das licht ein",
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self.transcript = transcript
        self.error = error
        self.calls: list[bytes] = []
        self.states: list[PipelineState] = []
        self.pipeline: Optional[Pipeline] = None

    def attach(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(bytes(audio))
        if self.pipeline is not None:
            self.states.append(self.pipeline.state_of(DEV))
        if self.error is not None:
            raise self.error
        return self.transcript


class FakeRouter:
    """Ersatz für `Router`: liefert eine feste Entscheidung oder wirft."""

    def __init__(
        self,
        decision: Optional[RouteDecision] = None,
        *,
        route_error: Optional[BaseException] = None,
        execute_error: Optional[BaseException] = None,
    ) -> None:
        self.decision = decision if decision is not None else question_decision()
        self.route_error = route_error
        self.execute_error = execute_error
        self.route_calls: list[str] = []
        self.execute_calls: list[RouteDecision] = []
        self.route_states: list[PipelineState] = []
        self.execute_states: list[PipelineState] = []
        self.pipeline: Optional[Pipeline] = None

    def attach(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    async def route_with_fallback(self, transcript: str) -> RouteDecision:
        self.route_calls.append(transcript)
        if self.pipeline is not None:
            self.route_states.append(self.pipeline.state_of(DEV))
        if self.route_error is not None:
            raise self.route_error
        return self.decision

    async def execute(self, decision: RouteDecision) -> RouteDecision:
        self.execute_calls.append(decision)
        if self.pipeline is not None:
            self.execute_states.append(self.pipeline.state_of(DEV))
        if self.execute_error is not None:
            raise self.execute_error
        return decision


class FakeTts:
    """Ersatz für `TtsClient`: async-Generator `(rate, pcm)`.

    Optional blockiert er an einem Gate (Barge-in) oder wirft beim Iterieren
    (TTS-Fehlerpfad).
    """

    def __init__(
        self,
        *,
        rate: int = 48000,
        pcm_bytes: bytes = b"\x00\x00" * 2048,
        gate: Optional[asyncio.Event] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.rate = rate
        self.pcm = pcm_bytes
        self.gate = gate
        self.error = error
        self.calls: list[str] = []
        self.states: list[PipelineState] = []
        self.pipeline: Optional[Pipeline] = None

    def attach(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    async def synthesize(self, text: str):  # type: ignore[no-untyped-def]
        self.calls.append(text)
        if self.pipeline is not None:
            self.states.append(self.pipeline.state_of(DEV))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        yield (self.rate, self.pcm)


class ManualSleep:
    """Injizierte Uhr: jeder Sekundenwert hat ein eigenes Gate.

    Wird ein Gate gesetzt, läuft der zugehörige Timer (Hard-Cap/No-Speech)
    deterministisch weiter — **kein** reales 15-/8-s-Warten.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._gates: dict[float, asyncio.Event] = {}

    def gate(self, seconds: float) -> asyncio.Event:
        return self._gates.setdefault(float(seconds), asyncio.Event())

    async def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        await self.gate(seconds).wait()


@dataclass
class FakeSettings:
    """Nur die Settings-Attribute, die `Pipeline` liest."""

    mic_start_lock_mic_button: bool = True
    audio_speaker_rate: int = 48000
    speaker_chunk_bytes: int = 4096
    audio_width: int = 2
    audio_channels: int = 1
    # P9.T5 (E100): Audio-Dump – Default **aus** (kein I/O).
    audio_dump_enabled: bool = False
    audio_dump_dir: str = ""
    audio_dump_max_files: int = 50
    audio_dump_max_bytes: int = 100 * 1024 * 1024


@dataclass
class Harness:
    """Bündelt Pipeline + Fakes + aufgezeichnete Ausgänge."""

    stt: FakeStt = field(default_factory=FakeStt)
    tts: FakeTts = field(default_factory=FakeTts)
    router: FakeRouter = field(default_factory=FakeRouter)
    wake: FakeWake = field(default_factory=FakeWake)
    sleep: ManualSleep = field(default_factory=ManualSleep)
    settings: FakeSettings = field(default_factory=FakeSettings)
    hard_cap: float = 15.0
    no_speech: float = 8.0
    control: list[tuple[str, dict]] = field(default_factory=list)
    binary: list[tuple[str, bytes]] = field(default_factory=list)
    pipeline: Pipeline = field(init=False)

    def __post_init__(self) -> None:
        self.pipeline = Pipeline(
            wake_detector=self.wake,
            stt_client=self.stt,
            tts_client=self.tts,
            router=self.router,
            send_control=self._send_control,
            send_binary=self._send_binary,
            sleep=self.sleep,
            settings_obj=self.settings,
            hard_cap_seconds=self.hard_cap,
            no_speech_seconds=self.no_speech,
        )
        for fake in (self.stt, self.tts, self.router):
            fake.attach(self.pipeline)

    async def _send_control(self, device_id: str, message: dict) -> None:
        self.control.append((device_id, dict(message)))

    async def _send_binary(self, device_id: str, data: bytes) -> None:
        self.binary.append((device_id, bytes(data)))

    @property
    def controls(self) -> list[dict]:
        return [message for _, message in self.control]

    @property
    def binaries(self) -> list[bytes]:
        return [data for _, data in self.binary]

    def device(self) -> Any:
        return self.pipeline._devices[DEV]

    def state(self) -> PipelineState:
        return self.pipeline.state_of(DEV)


# ── Helfer ───────────────────────────────────────────────────────────────
def run(coro: Any) -> Any:
    return asyncio.run(coro)


def question_decision(text: str = "Es ist zwölf Uhr.") -> RouteDecision:
    return RouteDecision(intent=INTENT_QUESTION, transcript="frage", response_text=text)


def command_decision(text: str = "Licht an.") -> RouteDecision:
    return RouteDecision(
        intent=INTENT_COMMAND,
        transcript="schalte das licht ein",
        response_text=text,
        entity_id="light.wohnzimmer",
        domain="light",
        service="turn_on",
    )


async def _pump(predicate, *, limit: int = 300) -> bool:
    """Event-Loop so lange ausführen, bis ``predicate()`` wahr ist."""
    for _ in range(limit):
        if predicate():
            return True
        await asyncio.sleep(0)
    return bool(predicate())


async def _arm(harness: Harness, *, audio_chunks: int = 1) -> None:
    """Wake → LISTENING, `audio_chunks` Turn-Chunks füllen."""
    assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
    for index in range(audio_chunks):
        await harness.pipeline.on_mic_frame(DEV, index, pcm(index))


# ═══════════════════════════════════════════════════════════════════════════
# 1. Zustandsübergänge (COMMAND / QUESTION)
# ═══════════════════════════════════════════════════════════════════════════
def test_state_sequence_command_full() -> None:
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    # Jeder Fake sah genau den Zustand, den §5 für seine Phase vorsieht.
    assert harness.stt.states == [PipelineState.TRANSCRIBING]
    assert harness.router.route_states == [PipelineState.ROUTING]
    assert harness.router.execute_states == [PipelineState.EXECUTING]
    assert harness.tts.states == [PipelineState.SPEAKING]
    # Turn sauber beendet.
    assert harness.state() is PipelineState.IDLE
    assert harness.stt.calls and len(harness.stt.calls[0]) == CHUNK
    # Speaker-Ausgang: 0x02-Frames + EOS 0x03.
    assert harness.binaries[-1] == EOS
    assert all(frame[:1] == b"\x02" for frame in harness.binaries[:-1])
    assert harness.binaries


def test_state_sequence_question_skips_execute() -> None:
    harness = Harness(router=FakeRouter(question_decision("Es ist zwölf Uhr.")))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert harness.router.route_states == [PipelineState.ROUTING]
    # QUESTION: kein Service-Call, kein EXECUTING.
    assert harness.router.execute_calls == []
    assert harness.router.execute_states == []
    assert harness.tts.states == [PipelineState.SPEAKING]
    assert harness.tts.calls == ["Es ist zwölf Uhr."]
    assert harness.state() is PipelineState.IDLE


# ═══════════════════════════════════════════════════════════════════════════
# 2. Dauer-Stream ohne Turn
# ═══════════════════════════════════════════════════════════════════════════
def test_continuous_stream_without_turn_keeps_idle() -> None:
    harness = Harness()

    async def scenario() -> None:
        for seq in range(10):
            events = await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq))
            assert events == []

    run(scenario())

    assert harness.state() is PipelineState.IDLE
    assert harness.device().turn_task is None
    # Kein Turn ⇒ keine Ausgänge.
    assert harness.controls == []
    assert harness.binaries == []
    # Jeder Frame ging an den Wake-Detektor (Dauer-Stream, E5).
    assert len(harness.wake.processed) == 10
    # Preroll ist auf 3 Chunks begrenzt (E7/§3).
    assert harness.device().buffer.preroll_count == PREROLL_DISCARD_CHUNKS


def test_preroll_discard_constant_is_three() -> None:
    assert PREROLL_DISCARD_CHUNKS == 3
    assert TurnBuffer().preroll_chunks == 3


# ═══════════════════════════════════════════════════════════════════════════
# 3. Wake → Turn inkl. 240-ms-Preroll-Verwurf (E7)
# ═══════════════════════════════════════════════════════════════════════════
def test_wake_turn_discards_three_preroll_chunks() -> None:
    harness = Harness()

    async def scenario() -> None:
        for seq in range(3):
            await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq))
        assert harness.device().buffer.preroll_count == 3
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        # Preroll (exakt 3) verworfen, Turn-Puffer leer.
        assert harness.device().discarded_preroll == 3
        assert harness.device().buffer.preroll_count == 0
        assert harness.device().buffer.turn_bytes == 0
        assert harness.state() is PipelineState.LISTENING

        for seq in (3, 4):
            await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq))
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    # Nur die zwei Post-Wake-Chunks erreichen die Transkription.
    assert len(harness.stt.calls) == 1
    assert harness.stt.calls[0] == pcm(3) + pcm(4)


def test_wake_turn_via_mic_frame_discards_preroll() -> None:
    harness = Harness()

    async def scenario() -> None:
        # 2 Frames puffern, dann das Wake-Event auf den 3. Frame legen.
        assert await harness.pipeline.on_mic_frame(DEV, 0, pcm(0)) == []
        assert await harness.pipeline.on_mic_frame(DEV, 1, pcm(1)) == []
        harness.wake.queue(FakeEvent(0.99))
        events = await harness.pipeline.on_mic_frame(DEV, 2, pcm(2))
        assert [event.score for event in events] == [0.99]

    run(scenario())

    assert harness.device().discarded_preroll == 3
    assert harness.state() is PipelineState.LISTENING


# ═══════════════════════════════════════════════════════════════════════════
# 3b. Wake-Endpointing ohne Geräte-Sentinel (P7.T1-Fix, E5-Korrektur)
# ═══════════════════════════════════════════════════════════════════════════
def test_wake_turn_endpoints_on_silence_without_vad_sentinel() -> None:
    """Der Wake-Turn endpointet manager-seitig – **ohne** `0x04`/`0x05`.

    Der permanente (`!lockMic`) Stream der Firmware liefert **keine**
    End-of-Speech-Sentinels; nur der `lock_mic`-Turn tut das.  Der Manager
    muss deshalb nach erkannter Sprache und `VAD_SILENCE_MS` (900 ms) Stille
    selbst nach `TRANSCRIBING` wechseln.  Dieser Test löst den Turn **nicht**
    über `on_vad_end`/`on_no_speech` aus – genau das war der Live-Bug
    `LISTENING → No-Speech` (8 s) auf `.123`.
    """
    harness = Harness(router=FakeRouter(question_decision("Antwort.")))

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        frame = 0
        # Die ersten `PREROLL_DISCARD_CHUNKS` (Wake-Wort-Rest) überspringt das
        # Endpointing – wie der Referenz-`preroll_discard`.
        for _ in range(PREROLL_DISCARD_CHUNKS):
            await harness.pipeline.on_mic_frame(DEV, frame, loud())
            frame += 1
        assert harness.device().endpoint_speech_seen is False
        # Echte Sprache (RMS 0.5) ⇒ `speech_seen` nach 1 Frame (80 ms ≥ 32 ms).
        for _ in range(3):
            await harness.pipeline.on_mic_frame(DEV, frame, loud())
            frame += 1
        assert harness.device().endpoint_speech_seen is True
        assert harness.state() is PipelineState.LISTENING
        # Stille ≥ `VAD_SILENCE_MS` (900 ms = 12 Frames) ⇒ Endpointing.
        for _ in range(12):
            await harness.pipeline.on_mic_frame(DEV, frame, pcm(0))
            frame += 1
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    # Transkribiert, obwohl **kein** Sentinel und **kein** expliziter End-Aufruf kam.
    assert harness.stt.calls, "Wake-Turn muss ohne 0x04 transkribieren"
    assert harness.stt.states == [PipelineState.TRANSCRIBING]
    assert harness.tts.calls == ["Antwort."]
    assert harness.state() is PipelineState.IDLE


def test_silence_endpointing_needs_speech_first() -> None:
    """Stille **ohne** vorherige Sprache endpointet nicht (nur K5 greift)."""
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        for seq in range(20):  # 20 × 80 ms = 1,6 s Stille, nie Sprache
            await harness.pipeline.on_mic_frame(DEV, seq, pcm(0))

    run(scenario())

    assert harness.device().endpoint_speech_seen is False
    assert harness.state() is PipelineState.LISTENING
    assert harness.stt.calls == []


# ═══════════════════════════════════════════════════════════════════════════
# 3c. Wake-Quittierung: Bernstein-Blitz, danach lila `listening` (P7.T2, E88)
# ═══════════════════════════════════════════════════════════════════════════
def _led_anims(harness: Harness) -> list[dict]:
    return [m for m in harness.controls if m.get("type") == "led_anim"]


def test_wake_flashes_amber_before_listening_led() -> None:
    """Der User-Wunsch: **unmittelbar** nach dem Wake eine LED-Reaktion.

    Reihenfolge ist exakt `solid[255,170,0]` (Bernstein) → lila `listening`,
    ohne Doppelblitz und ohne Lücke; dazwischen liegt der 400-ms-Blitz, dessen
    Dauer der Manager selbst begrenzt (der Push ist verschoben). Geprüft wird
    über die **injizierte** Uhr (E87) – kein reales Warten, kein `sleep`.

    **P7.T2b/E89:** das Lila kommt **zweimal** und mit zwei Jobs – als
    `config`-Push (cached die Spec im Gerät) **und** als `led_anim` (malt den
    Ring). Vor E89 stand hier nur der `config`-Push, der den Ring nicht anfasst
    ⇒ Blitz → schwarz. Genau das war der Live-Befund.
    """
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        # Sofort nach dem Wake: **nur** der Blitz – kein direkt-lila.
        assert harness.controls == [LED_WAKE_ACK]
        assert harness.state() is PipelineState.LISTENING
        # Der `listening`-Push ist um genau den Blitz verschoben (Gate 0,4 s).
        assert await _pump(lambda: 0.4 in harness.sleep.calls) is True
        assert harness.device().led_task is not None
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: len(harness.controls) == 3) is True

    run(scenario())

    # Bernstein **vor** lila, direkt aufeinanderfolgend (kein Doppelblitz).
    assert harness.controls == [LED_WAKE_ACK, LISTENING_PUSH, LED_LISTENING]
    # Genau **ein** Blitz – das zweite `led_anim` ist das Dauer-Lila (E89).
    assert _led_anims(harness) == [LED_WAKE_ACK, LED_LISTENING]
    assert harness.device().led_task is None  # Task beendet, nichts leakt
    assert WAKE_ACK_FLASH_SECONDS == 0.4


def test_wake_lights_purple_listening_anim_without_any_speech() -> None:
    """**Kerntest des Live-Befunds (P7.T2b/E89):** Lila **vor** jeder Sprache.

    Der User sah „Blitz → nichts". Ursache: der Ring wurde nach dem Blitz nur
    per `config`-Push gefüttert, den das Gerät **nicht** malt. Dieser Test legt
    fest: das dauerhafte Lila muss als `led_anim` rausgehen, **bevor** ein
    einziger Mic-Frame (also **ohne** Spracherkennung, **ohne** Endpointing)
    eingetroffen ist – der Turn steht zu diesem Zeitpunkt noch auf `LISTENING`.

    Ohne den Fix fällt die Assertion `LED_LISTENING in _led_anims(...)` ⇒ rot.
    """
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert await _pump(lambda: 0.4 in harness.sleep.calls) is True
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: len(harness.controls) == 3) is True

    run(scenario())

    # **Kein** Mic-Frame gelaufen: `on_mic_frame` wurde nie aufgerufen.
    assert harness.wake.processed == []
    assert harness.state() is PipelineState.LISTENING
    # Das Lila ist auf dem **Ring** angekommen, nicht nur im Config-Cache.
    assert LED_LISTENING in _led_anims(harness)
    assert _led_anims(harness)[-1] == LED_LISTENING
    # … und die Geräte-Spec ist wörtlich die Referenzform (keine erfundene Farbe).
    assert LED_LISTENING["anim"] == {
        "pattern": "solid",
        "colors": [[110, 0, 45]],
        "listening": True,
        "ttlSec": 30,
    }
    # Der `config`-Push bleibt als Cache-Seed für den **eigenen** Wake-Crossing
    # des Geräts erhalten (unverändert, Reihenfolge Push → `led_anim`).
    assert harness.controls[1:3] == [LISTENING_PUSH, LED_LISTENING]
    # Weder STT noch TTS wurden berührt – es ist reines Overlay.
    assert harness.stt.calls == []
    assert harness.tts.calls == []


def test_wake_led_sequence_flash_listening_think_answer() -> None:
    """Die **verbindliche** Gesamtsequenz (E89): Blitz → Lila → Spin → Antwort.

    `Blitz → dauerhaft Lila → Denken → Sprechen → aus` über einen echten
    Wake-Turn mit manager-seitigem Endpointing (P7.T1-Fix bleibt unberührt):
    3 Wake-Rest-Frames (werden übersprungen) · 3 Sprach-Frames · 12 Stille-
    Frames ⇒ `0x04` ⇒ `thinking` ⇒ TTS ⇒ `off`. Geprüft wird die **Reihenfolge**
    der LED-Nachrichten, nicht das Timing (injizierte Uhr, E87).
    """
    harness = Harness(router=FakeRouter(question_decision("Es ist zwölf Uhr.")))

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert await _pump(lambda: 0.4 in harness.sleep.calls) is True
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: len(harness.controls) == 3) is True
        for _ in range(PREROLL_DISCARD_CHUNKS):  # Wake-Rest (nicht bewertet)
            await harness.pipeline.on_mic_frame(DEV, None, loud())
        for _ in range(3):  # Sprache (RMS 0.5) ⇒ `speech_seen`
            await harness.pipeline.on_mic_frame(DEV, None, loud())
        for _ in range(12):  # 12 · 80 ms = 960 ms Stille ⇒ Endpointing
            await harness.pipeline.on_mic_frame(DEV, None, pcm(0))
        await harness.pipeline.wait_turn(DEV)
        assert await _pump(lambda: harness.controls[-1] == LED_OFF) is True

    run(scenario())

    anims = [m["anim"] for m in _led_anims(harness)]
    patterns = [anim["pattern"] for anim in anims]
    # Blitz → **Lila** → Spin → Meter → aus, in genau dieser Reihenfolge.
    assert patterns == ["solid", "solid", "spin", "meter", "off"]
    assert anims[0]["colors"] == [[255, 170, 0]]  # Bernstein (E88)
    assert anims[1]["colors"] == [[110, 0, 45]]  # Lila, `listening: True`
    assert anims[1]["listening"] is True
    # Das Lila steht **vor** dem Denken – es wird nicht überschrieben.
    assert harness.controls.index(LED_LISTENING) < harness.controls.index(LED_THINKING)
    # Und der Turn lief unverändert durch (P7.T1-Endpointing unberührt).
    assert harness.stt.states == [PipelineState.TRANSCRIBING]
    assert harness.tts.calls == ["Es ist zwölf Uhr."]
    assert harness.state() is PipelineState.IDLE
    assert harness.device().led_task is None  # kein Task-Leak


def test_listening_led_anim_ttl_outlives_turn_hard_cap() -> None:
    """Der Dead-Man des Geräts darf den Ring **nie** mitten im Turn schwärzen.

    `StartAnim` malt `solid` **einmal** und schreibt nach `ttlSec` ohne
    Nachfolger `blackFrame` (Firmware `server/animator.go:113-163`). Damit das
    lila `listening` den ganzen Turn über steht, muss die **tatsächlich
    gesendete** TTL größer sein als der Hard-Cap des längsten zulässigen Turns.

    Geprüft wird die **echte** Nachricht (nicht das Literal `LED_LISTENING`) –
    sonst würde eine Änderung der TTL im Code diesen Test nicht rot machen.
    """
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert await _pump(lambda: 0.4 in harness.sleep.calls) is True
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: len(harness.controls) == 3) is True

    run(scenario())

    sent = _led_anims(harness)[-1]["anim"]
    assert sent["pattern"] == "solid"
    assert sent["ttlSec"] > HARD_CAP_SECONDS
    assert sent["ttlSec"] > NO_SPEECH_SECONDS
    # … und die Wire-Spec bleibt wörtlich die Referenzform (E11/`em_scenes`).
    assert sent == LED_LISTENING["anim"]



def test_wake_flash_does_not_swallow_running_turn() -> None:
    """Der Blitz ist ein Overlay: er darf keinen Turn-Zustand verschlucken.

    Der Turn endet **vor** dem 400-ms-Fenster (`0x04` mit Audio ⇒ sofort
    `TRANSCRIBING`). Der nachgeschobene `listening`-Push darf dann weder
    `thinking`/`meter`/`off` überschreiben noch den Turn beschädigen.
    """
    harness = Harness(router=FakeRouter(question_decision("Antwort.")))

    async def scenario() -> None:
        await _arm(harness, audio_chunks=2)
        assert await _pump(lambda: 0.4 in harness.sleep.calls) is True
        # Turn endet vor dem Blitzfenster ⇒ `thinking` ist der nächste LED.
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)
        # Jetzt das Blitzfenster freigeben: nichts darf mehr kommen.
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: False, limit=20) is False

    run(scenario())

    controls = harness.controls
    assert controls[0] == LED_WAKE_ACK
    thinking_at = controls.index(LED_THINKING)
    # Nach `thinking` folgt **kein** nachgeschobenes `listening` mehr …
    assert LISTENING_PUSH not in controls[thinking_at:]
    # … und auch kein nachgeschobenes Lila-`led_anim` (P7.T2b/E89) – es darf
    # weder `thinking`/`meter`/`off` überschreiben noch den Turn beschädigen.
    assert LED_LISTENING not in controls[thinking_at:]
    assert LED_LISTENING not in controls
    # … und der Turn selbst lief unverändert durch (STT/TTS/Zustand).
    assert harness.stt.states == [PipelineState.TRANSCRIBING]
    assert harness.tts.calls == ["Antwort."]
    assert harness.state() is PipelineState.IDLE
    assert controls[-1] == LED_OFF


def test_wake_flash_is_cancelled_on_fast_silent_abort() -> None:
    """Schneller Abbruch (`0x05`, E6) ⇒ Blitz fällt sauber zurück.

    Der `listening`-Push darf die Stille-LED nicht überschreiben, und es darf
    kein Task übrig bleiben (kein Timer-Leak).
    """
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert await harness.pipeline.on_no_speech(DEV) is True
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: False, limit=20) is False

    run(scenario())

    assert harness.controls == [LED_WAKE_ACK, LED_SILENCE]
    assert harness.stt.calls == []
    assert harness.tts.calls == []
    assert harness.state() is PipelineState.IDLE
    assert harness.device().led_task is None


def test_wake_flash_does_not_leak_on_device_gone() -> None:
    """Gerätetrennung räumt den wartenden Blitz-Push ab (Task ohne Leiche)."""
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert await _pump(lambda: harness.device().led_task is not None) is True
        await harness.pipeline.on_device_gone(DEV)
        assert DEV not in harness.pipeline._devices
        harness.sleep.gate(0.4).set()
        assert await _pump(lambda: False, limit=20) is False

    run(scenario())

    assert harness.controls == [LED_WAKE_ACK, LED_OFF]


def test_button_turn_has_no_wake_flash() -> None:
    """K6/Button: **kein** Blitz – der Button startet den Turn selbst (E88)."""
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 138) is True
        assert harness.state() is PipelineState.LISTENING
        assert await _pump(lambda: False, limit=20) is False

    run(scenario())

    # `mic_stop` → `mic_start{lock_mic:true}` → sofort lila `listening`.
    assert harness.controls[:3] == [CTRL_MIC_STOP, CTRL_MIC_START_LOCK, LISTENING_PUSH]
    assert _led_anims(harness) == []  # kein Blitz, kein Doppelblitz
    assert 0.4 not in harness.sleep.calls
    assert harness.device().led_task is None


# ═══════════════════════════════════════════════════════════════════════════
# 4. 0x04 VAD-End ⇒ Transkription
# ═══════════════════════════════════════════════════════════════════════════
def test_vad_end_transcribes_and_speaks() -> None:
    harness = Harness(router=FakeRouter(question_decision("Antwort.")))

    async def scenario() -> None:
        await _arm(harness, audio_chunks=2)
        assert await harness.pipeline.on_vad_end(DEV) is True
        # Zustand direkt nach dem Handler: Transkription läuft.
        assert harness.state() is PipelineState.TRANSCRIBING
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert len(harness.stt.calls) == 1
    assert harness.stt.calls[0] == pcm(0) + pcm(1)
    assert harness.tts.calls == ["Antwort."]
    assert harness.state() is PipelineState.IDLE


def test_vad_end_without_turn_returns_false() -> None:
    harness = Harness()
    result = run(harness.pipeline.on_vad_end(DEV))
    assert result is False
    assert harness.stt.calls == []
    assert harness.state() is PipelineState.IDLE


def test_vad_end_empty_audio_ends_silent_not_transcribing() -> None:
    """`0x04` ohne gepufferte Mic-Chunks ⇒ stilles Turn-Ende, **kein** Hängen.

    Ersetzt den ehemaligen Ist-Verhalten-Pin aus P5.T6 (dort blieb der Zustand
    in `TRANSCRIBING` hängen). Mit dem Bugfix (P5.T6-Nachtrag) behandelt
    `on_vad_end` den Turn **ohne** Audio analog zur `0x05`-Stille (E6/§5):
    kein STT/TTS/Fehlertext, LED `pulse`, Rückkehr nach `IDLE`.
    """
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        # Kein Audio im Puffer: `on_vad_end` nimmt den stillen Pfad.
        assert await harness.pipeline.on_vad_end(DEV) is True

    run(scenario())

    assert harness.state() is PipelineState.IDLE
    assert harness.device().turn_task is None
    assert harness.stt.calls == []
    assert harness.tts.calls == []  # kein TTS, kein Fehlertext
    assert harness.binaries == []  # kein Speaker-Audio
    assert harness.controls[-1] == LED_SILENCE
    assert LED_ERROR not in harness.controls


def test_vad_end_empty_audio_regression_recovers_and_accepts_new_turn() -> None:
    """Regression (P5.T6-Nachtrag): leerer VAD-End lässt ein benutzbares Gerät
    zurück — kein Hängen in `TRANSCRIBING`, Timer abgeräumt, der nächste Turn
    läuft normal. Der alte Bug hätte hier `on_wake_detected == False` (bzw.
    einen blockierten Folge-`on_vad_end`) ergeben.
    """
    harness = Harness(router=FakeRouter(question_decision("Antwort.")))

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert harness.state() is PipelineState.LISTENING
        assert await harness.pipeline.on_vad_end(DEV) is True
        # Definierter Endzustand statt Hängen in TRANSCRIBING.
        assert harness.state() is PipelineState.IDLE
        assert harness.device().timers == []
        # Zweites 0x04 ohne laufenden Turn ist wirkungslos (nicht LISTENING).
        assert await harness.pipeline.on_vad_end(DEV) is False
        # Der nächste Turn ist ungestört möglich.
        await _arm(harness, audio_chunks=1)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert harness.stt.calls == [pcm(0)]
    assert harness.tts.calls == ["Antwort."]
    assert harness.state() is PipelineState.IDLE


# ═══════════════════════════════════════════════════════════════════════════
# 5. 0x05-Stille: kein TTS, kein Fehlertext (E6)
# ═══════════════════════════════════════════════════════════════════════════
def test_no_speech_silence_no_tts_no_error_text() -> None:
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_no_speech(DEV) is True

    run(scenario())

    assert harness.state() is PipelineState.IDLE
    assert harness.stt.calls == []
    assert harness.tts.calls == []  # kein TTS, kein Fehlertext
    assert harness.binaries == []  # kein Speaker-Audio
    assert harness.controls[-1] == LED_SILENCE
    assert LED_ERROR not in harness.controls


def test_no_speech_without_turn_returns_false() -> None:
    harness = Harness()
    result = run(harness.pipeline.on_no_speech(DEV))
    assert result is False
    assert harness.controls == []


# ═══════════════════════════════════════════════════════════════════════════
# 6. Hard-Cap 15 s
# ═══════════════════════════════════════════════════════════════════════════
def test_hard_cap_aborts_turn() -> None:
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness, audio_chunks=4)
        # Beide Timer sind mit den Config-Werten scharf (K5).
        assert await _pump(lambda: 15.0 in harness.sleep.calls) is True
        assert await _pump(lambda: 8.0 in harness.sleep.calls) is True
        # Hard-Cap feuern ⇔ 15-s-Timer freigeben (kein reales Warten).
        harness.sleep.gate(15.0).set()
        assert await _pump(lambda: harness.state() is PipelineState.IDLE) is True

    run(scenario())

    assert harness.state() is PipelineState.IDLE
    assert harness.stt.calls == []  # Puffer verworfen, keine Transkription
    assert harness.tts.calls == []
    assert harness.device().buffer.turn_bytes == 0
    assert harness.controls[-1] == LED_SILENCE


def test_hard_cap_cancels_no_speech_timer() -> None:
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        await _pump(lambda: len(harness.device().timers) == 2)
        harness.sleep.gate(15.0).set()
        assert await _pump(lambda: harness.state() is PipelineState.IDLE) is True

    run(scenario())

    # Der No-Speech-Timer wurde durch den Hard-Cap mit abgeräumt.
    assert harness.sleep.gate(8.0).is_set() is False
    assert harness.state() is PipelineState.IDLE


# ═══════════════════════════════════════════════════════════════════════════
# 7. Barge-in im SPEAKING
# ═══════════════════════════════════════════════════════════════════════════
def _speaking_harness() -> Harness:
    gate = asyncio.Event()
    return Harness(
        tts=FakeTts(gate=gate),
        router=FakeRouter(question_decision("Lange Antwort.")),
    )


def test_barge_in_flushes_and_starts_new_turn() -> None:
    harness = _speaking_harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        # Turn hängt im SPEAKING am TTS-Gate.
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        assert len(harness.tts.calls) == 1
        # Barge-in per Mic-Frame (Event mit `barge_in=True`) — erst im SPEAKING.
        harness.wake.queue(FakeEvent(score=0.2, barge_in=True))
        await harness.pipeline.on_mic_frame(DEV, 99, pcm(9))
        assert harness.state() is PipelineState.LISTENING

    run(scenario())

    assert CTRL_SPEAKER_FLUSH in harness.controls
    # Neuer Turn wurde scharf (Timer erneut gestartet).
    assert harness.sleep.calls.count(15.0) >= 1
    assert harness.state() is PipelineState.LISTENING


def test_barge_in_via_on_wake_detected_in_speaking() -> None:
    harness = _speaking_harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        assert await harness.pipeline.on_wake_detected(DEV, 0.3) is True
        assert harness.state() is PipelineState.LISTENING

    run(scenario())

    assert CTRL_SPEAKER_FLUSH in harness.controls


# ═══════════════════════════════════════════════════════════════════════════
# 8. Mic-Lücke
# ═══════════════════════════════════════════════════════════════════════════
def test_mic_gap_counted_and_resynced() -> None:
    harness = Harness()

    async def scenario() -> None:
        for seq in (0, 1, 2):
            await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq))
        # Sprung 2 → 5: Lücke, Zähler zieht auf next_sequence(5)=6 nach.
        await harness.pipeline.on_mic_frame(DEV, 5, pcm(5))
        assert harness.device().mic_gaps == 1
        assert harness.device().expected_seq == 6
        # 6 ist wieder lückenlos.
        await harness.pipeline.on_mic_frame(DEV, 6, pcm(6))
        assert harness.device().mic_gaps == 1

    run(scenario())

    assert harness.device().mic_gaps == 1
    assert harness.state() is PipelineState.IDLE


def test_mic_gap_no_false_positive_on_consecutive() -> None:
    harness = Harness()

    async def scenario() -> None:
        for seq in range(5):
            await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq))

    run(scenario())

    assert harness.device().mic_gaps == 0
    assert harness.device().expected_seq == 5


# ═══════════════════════════════════════════════════════════════════════════
# 9. Fehlerpfade + Cleanup (`try/finally`)
# ═══════════════════════════════════════════════════════════════════════════
def _assert_cleaned_up(harness: Harness) -> None:
    assert harness.state() is PipelineState.IDLE
    assert harness.device().turn_task is None
    assert harness.device().lock.locked() is False
    assert harness.device().buffer.turn_bytes == 0
    assert harness.wake.reset_calls >= 1
    assert harness.wake.speaking_calls[-1] is False


def test_stt_error_speaks_fallback_and_cleans_up() -> None:
    harness = Harness(stt=FakeStt(error=RuntimeError("stt kaputt")))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert harness.tts.calls == [FALLBACK_SPEECH]
    assert harness.controls[-1] == LED_ERROR
    _assert_cleaned_up(harness)


def test_router_error_speaks_fallback_and_cleans_up() -> None:
    from app.router import RouterTurnError

    harness = Harness(
        router=FakeRouter(route_error=RouterTurnError("x"))
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert harness.tts.calls == [FALLBACK_NOT_UNDERSTOOD]
    assert harness.controls[-1] == LED_ERROR
    _assert_cleaned_up(harness)


def test_execute_error_speaks_fallback_and_cleans_up() -> None:
    from app.router import RouterTurnError

    harness = Harness(
        router=FakeRouter(
            command_decision(), execute_error=RouterTurnError("x")
        )
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    # `execute` lief (EXECUTING), schlug fehl ⇒ definierter Fehlertext.
    assert harness.router.execute_calls
    assert harness.tts.calls == [FALLBACK_NOT_UNDERSTOOD]
    assert harness.controls[-1] == LED_ERROR
    _assert_cleaned_up(harness)


def test_tts_error_is_swallowed_and_cleans_up() -> None:
    harness = Harness(
        tts=FakeTts(error=RuntimeError("tts kaputt")),
        router=FakeRouter(question_decision("Antwort.")),
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    # Erst der Antworttext, dann der Fehler-Fallback — beide scheitern am TTS.
    assert harness.tts.calls == ["Antwort.", FALLBACK_NOT_UNDERSTOOD]
    assert harness.controls[-1] == LED_ERROR
    _assert_cleaned_up(harness)


def test_cleanup_releases_lock_after_successful_turn() -> None:
    harness = Harness(router=FakeRouter(question_decision()))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    _assert_cleaned_up(harness)
    assert harness.controls[-1] == LED_OFF


# ═══════════════════════════════════════════════════════════════════════════
# Zusatz: Button (K6/E29), Gerätetrennung, Ist-Verhalten Button/LISTENING
# ═══════════════════════════════════════════════════════════════════════════
def test_button_turn_starts_with_lock_mic_and_closes_pair() -> None:
    harness = Harness(router=FakeRouter(question_decision("Antwort.")))

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 138) is True
        # Button-Turns verwerfen **nichts** (E7/K6).
        assert harness.device().discarded_preroll == 0
        assert harness.state() is PipelineState.LISTENING
        for seq in (0,):
            await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq))
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    controls = harness.controls
    # Start: mic_stop → mic_start{lock_mic:true} (K6).
    assert controls[0] == CTRL_MIC_STOP
    assert controls[1] == CTRL_MIC_START_LOCK
    # Abschluss-Paar aus dem Cleanup: mic_stop → mic_start{} (ohne lock),
    # danach der Erfolgs-LED-`off`.
    assert controls[-3] == CTRL_MIC_STOP
    assert controls[-2] == CTRL_MIC_START
    assert controls[-1] == LED_OFF


def test_button_ignores_foreign_click_type_and_down_true() -> None:
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 115) is False
        assert await harness.pipeline.on_button(DEV, 138, down=True) is False
        # `muted` blockiert nur den Turn-Start (E29).
        assert await harness.pipeline.on_button(DEV, 138, muted=True) is False

    run(scenario())

    assert harness.state() is PipelineState.IDLE
    assert harness.controls == []


def test_button_during_listening_sends_flush_but_keeps_state_ist_verhalten() -> None:
    """Pinnt das **Ist-Verhalten**: Button während `LISTENING` beendet den Turn
    nicht (nur `speaker_flush`), weil noch kein `turn_task` existiert.

    Beobachtet in P5.T6 und als Abweichung von der Referenzabsicht dokumentiert
    („der Button ist genau das Mittel, wenn das Wake-Wort nichts getan hat" —
    `docs/reference/em_controller.py:3043-3060`).  Der Prüfling bleibt
    unangetastet (P2.T1-Präzedenz: der Test hält den Ist-Zustand fest).
    """
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        assert harness.state() is PipelineState.LISTENING
        assert await harness.pipeline.on_button(DEV, 138) is True

    run(scenario())

    assert CTRL_SPEAKER_FLUSH in harness.controls
    assert harness.state() is PipelineState.LISTENING


def test_button_cancels_active_turn_with_flush() -> None:
    harness = _speaking_harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        assert await harness.pipeline.on_button(DEV, 138) is True
        assert await _pump(lambda: harness.state() is PipelineState.IDLE) is True

    run(scenario())

    assert CTRL_SPEAKER_FLUSH in harness.controls
    assert harness.state() is PipelineState.IDLE


def test_device_gone_cancels_and_removes() -> None:
    harness = _speaking_harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        await harness.pipeline.on_device_gone(DEV)
        assert DEV not in harness.pipeline._devices

    run(scenario())

    assert harness.controls[-1] == LED_OFF


# ═══════════════════════════════════════════════════════════════════════════
# 11. Turn-Historie (P8.D3 – **reiner Beobachter**)
# ═══════════════════════════════════════════════════════════════════════════
# Diese Sektion beweist die drei Zusagen des P8.D3-Auftrags:
#   (a) **Genau ein** Eintrag pro Turn – über **alle** Endpfade, ohne Lücken
#       und ohne Dubletten;
#   (b) **Keine** Nebenwirkung auf die Steuerung (identisches Verhalten wie
#       ohne Historie: Zustände, Ausgänge, Timer, Lock, Return-Werte);
#   (c) **Bounded** (100) und **gefiltert** (rotiert, gekürzt, Allowlist).
#
# Literale für Erwartungen (E56/E59) – `duration_ms`/`target_confidence`/
# `HISTORY_TEXT_MAXLEN` werden bewusst als Literal 100/1000, 0.87, 500 geprüft,
# damit eine Mutation an genau diesen Werten auffällt.


def entity_decision(
    *, confidence: float = 0.87, service_data: Any = None, raw: Any = None
) -> RouteDecision:
    """COMMAND mit E92-Daten (Param + `needs_param` in `raw`)."""
    return RouteDecision(
        intent=INTENT_COMMAND,
        transcript="sage nur zum Test",
        response_text="Licht an.",
        entity_id="light.wohnzimmer",
        domain="light",
        service="turn_on",
        service_data=service_data if service_data is not None else {"brightness_pct": 40},
        confidence=confidence,
        raw=raw
        if raw is not None
        else {"target": {"choice": "light.wohnzimmer"}, "needs_param": {"noul": 0.8}},
    )


def error_decision() -> RouteDecision:
    """Router-Fallback (INTENT_ERROR) mit Fehlercode + `detail` in `raw`."""
    return RouteDecision(
        intent=INTENT_ERROR,
        response_text=FALLBACK_NOT_UNDERSTOOD,
        error_code="ENTITY_PARAM_MISSING",
        raw={"detail": "brightness_pct fehlt"},
    )


def _entries(harness: Harness) -> list[dict]:
    return list(harness.pipeline.turn_history)


# ── (a) Erfolgs-/Fehlerpfade ─────────────────────────────────────────────
def test_history_records_one_entry_for_successful_command() -> None:
    harness = Harness(router=FakeRouter(entity_decision()))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["device_id"] == DEV
    assert entry["outcome"] == "ok"
    assert entry["state"] == "speaking"  # Zustand am Turn-Ende, vor IDLE
    assert entry["intent"] == INTENT_COMMAND
    assert entry["service"] == "turn_on"
    assert entry["domain"] == "light"
    assert entry["target_entity_id"] == "light.wohnzimmer"
    assert entry["confidence"] == 0.87  # P8.D3: `target_confidence`
    assert entry["score"] == 0.95  # Wake-Score
    assert entry["needs_param"] is True  # noul 0.8 >= 0.5
    assert entry["extracted_param"] == "brightness_pct=40"
    # Der `transcript` ist das **STT**-Ergebnis (was der Nutzer wirklich
    # sagte) – nicht das `transcript`-Feld der Router-Entscheidung.
    assert entry["transcript"] == "schalte das licht ein"
    assert entry["error_code"] is None
    assert entry["error"] is None
    assert entry["barge_in"] is False
    assert entry["ts"].endswith("+00:00")


def test_history_outcome_error_for_router_fallback() -> None:
    """INTENT_ERROR ⇒ `outcome="error"` + Fehlercode/`detail`, kein Exception-Pfad."""
    harness = Harness(router=FakeRouter(error_decision()))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "error"
    assert entries[0]["error_code"] == "ENTITY_PARAM_MISSING"
    assert entries[0]["error"] == "brightness_pct fehlt"
    # Der Fehlertext steht **nicht** im Transkript-Feld.
    assert entries[0]["transcript"] == "schalte das licht ein"


def test_history_records_stt_error_with_exception_text() -> None:
    """STT-Exception ⇒ `outcome="error"`, `error` mit Typ, Transcript bleibt leer."""
    harness = Harness(stt=FakeStt(error=RuntimeError("stt kaputt")))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "error"
    assert entries[0]["state"] == "speaking"  # STT fiel aus ⇒ Fallback gesprochen
    assert entries[0]["transcript"] is None
    assert entries[0]["error"] == "STT: RuntimeError: stt kaputt"
    assert entries[0]["target_entity_id"] is None


def test_history_records_unexpected_exception() -> None:
    """Unerwartete Ausnahme im Turn ⇒ `outcome="error"` + Typ/Text."""
    harness = Harness(router=FakeRouter(route_error=RuntimeError("router explodiert")))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "error"
    assert entries[0]["error"] == "RuntimeError: router explodiert"
    assert entries[0]["transcript"] == "schalte das licht ein"


# ── (a) Stille-Endpfade: 0x05 / 0x04 leer / Hard-Cap / No-Speech ─────────
def test_history_records_no_speech_0x05() -> None:
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_no_speech(DEV) is True

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "silence"
    assert entries[0]["state"] == "listening"  # vor dem Wechsel auf IDLE
    assert entries[0]["transcript"] is None
    assert entries[0]["error"] == "still beendet: no_speech"
    assert entries[0]["score"] == 0.95


def test_history_records_vad_end_without_audio() -> None:
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        assert await harness.pipeline.on_vad_end(DEV) is True

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "silence"
    assert entries[0]["error"] == "still beendet: vad_end"


def test_history_records_hard_cap() -> None:
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness, audio_chunks=4)
        assert await _pump(lambda: 15.0 in harness.sleep.calls) is True
        harness.sleep.gate(15.0).set()
        assert await _pump(lambda: harness.state() is PipelineState.IDLE) is True

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "silence"
    assert entries[0]["error"] == "still beendet: hard_cap"


def test_history_records_empty_transcript_from_stt() -> None:
    """STT liefert `""` ⇒ `silence` (kein `error`), Transcript `None`."""
    harness = Harness(stt=FakeStt(transcript="   "))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "silence"
    assert entries[0]["state"] == "transcribing"
    assert entries[0]["transcript"] is None
    assert entries[0]["error"] is None  # Stille ist kein Fehler


# ── (a) Abbruch / Trennung: **keine** Dubletten ──────────────────────────
def test_history_records_barge_in_cancel_once() -> None:
    """Barge-in im SPEAKING ⇒ alter Turn `cancelled`, neuer Turn eigener Eintrag."""
    harness = _speaking_harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        # Barge-in: Flush, alter Turn abgebrochen, neuer Turn per `_arm_listening`.
        assert await harness.pipeline.on_wake_detected(DEV, 0.9) is True
        assert harness.state() is PipelineState.LISTENING
        # Zweiten Turn beenden (stille), damit **er** einen Eintrag bekommt.
        assert await harness.pipeline.on_no_speech(DEV) is True

    run(scenario())

    entries = _entries(harness)
    # Genau zwei: der abgebrochene Sprech-Turn und der per Barge-in gestartete.
    assert len(entries) == 2
    assert entries[0]["outcome"] == "cancelled"
    assert entries[0]["barge_in"] is False
    assert entries[0]["state"] == "speaking"
    assert entries[1]["barge_in"] is True
    assert entries[1]["score"] == 0.9
    assert entries[1]["outcome"] == "silence"


def test_history_records_device_gone_while_listening_once() -> None:
    """Trennung im `LISTENING` ⇒ **ein** `device_gone`-Eintrag (kein Hängen)."""
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        assert harness.state() is PipelineState.LISTENING
        await harness.pipeline.on_device_gone(DEV)
        assert DEV not in harness.pipeline._devices

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "device_gone"
    assert entries[0]["state"] == "listening"
    assert "on_device_gone" in entries[0]["error"]


def test_history_device_gone_during_speaking_has_no_duplicate() -> None:
    """Trennung im `SPEAKING` ⇒ der `_run_turn`-Pfad erfasst **allein**."""
    harness = _speaking_harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        await harness.pipeline.on_device_gone(DEV)

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "cancelled"


def test_history_empty_before_any_turn() -> None:
    """Ohne Turn bleibt der Puffer leer – es wird **nichts** vorab erfunden."""
    harness = Harness()

    async def scenario() -> None:
        for seq in range(3):
            assert await harness.pipeline.on_mic_frame(DEV, seq, pcm(seq)) == []

    run(scenario())

    assert _entries(harness) == []


# ── (b) Nebenwirkungsfreiheit ─────────────────────────────────────────────
def test_history_does_not_change_control_flow() -> None:
    """Derselbe Turn **mit** Historien-Aufzeichnung liefert Zustände, Ausgänge,
    Timer und Return-Werte, die exakt dem vertraglichen Verhalten entsprechen –
    die Mitschrift ist damit nachweislich nicht in der Steuerung wirksam.
    """
    harness = Harness(router=FakeRouter(entity_decision()))

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True
        for index in range(2):
            await harness.pipeline.on_mic_frame(DEV, index, pcm(index))
        assert harness.state() is PipelineState.LISTENING
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    # Zustandsfolge unverändert (STT/Router/HA/TTS je ihr Zustand).
    assert harness.stt.states == [PipelineState.TRANSCRIBING]
    assert harness.router.route_states == [PipelineState.ROUTING]
    assert harness.router.execute_states == [PipelineState.EXECUTING]
    assert harness.tts.states == [PipelineState.SPEAKING]
    # Aufgeräumt wie im Vertragstest.
    _assert_cleaned_up(harness)
    # Return-Wert: `0x04` ⇒ `True`, ein zweites `0x04` ohne Turn ⇒ `False`.
    assert run(harness.pipeline.on_vad_end(DEV)) is False
    # Und die Historie hat genau einen Eintrag – nicht mehr, nicht weniger.
    assert len(_entries(harness)) == 1


class ExplodingRaw(dict):
    """`raw` einer Entscheidung, deren `.get()` zur Laufzeit explodiert.

    Simuliert einen kaputten/unerwarteten Jev-Payload **innerhalb** der
    Aufzeichnung – der Beobachter muss das abfangen, ohne den Turn zu
    beeinflussen und **ohne** einen halben Eintrag zu hinterlassen.
    """

    def get(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("raw kaputt")


def exploding_decision() -> RouteDecision:
    return RouteDecision(
        intent=INTENT_QUESTION,
        response_text="Antwort.",
        raw=ExplodingRaw(),
    )


def test_history_internal_error_never_breaks_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kaputter Payload ⇒ Turn läuft normal, **kein** Eintrag, `WARNING` sichtbar.

    Beweist beide Zusagen gleichzeitig: der Beobachter bricht den Turn **nicht**
    ab, und er scheitert auch **nicht still** (der Fehler landet im Logpuffer,
    den `/api/logs` ausliefert).
    """
    harness = Harness(router=FakeRouter(exploding_decision()))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    with caplog.at_level("WARNING", logger="manager.pipeline"):
        run(scenario())

    # Der Turn ist unbeeinflusst: Antwort gesprochen, sauber aufgeräumt.
    assert harness.tts.calls == ["Antwort."]
    _assert_cleaned_up(harness)
    # Kein halber Eintrag, und der Fehler ist **nicht** still verschwunden.
    assert _entries(harness) == []
    assert any(
        "Turn-Historie" in record.getMessage() for record in caplog.records
    )


# ── (c) Bounded + gefiltert ──────────────────────────────────────────────
def test_history_is_bounded_to_100_entries() -> None:
    """Harter Cap: nach 105 erzwungenen Einträgen bleiben **genau 100** übrig.

    Die ältesten gehen zuerst weg (Ringpuffer ⇒ FIFO), die neuesten bleiben.
    """
    harness = Harness()
    for index in range(105):
        harness.pipeline._record_turn(
            DEV, state=harness.pipeline._state(DEV), outcome="silence", transcript=f"t{index}"
        )

    entries = _entries(harness)
    assert len(entries) == 100
    assert entries[0]["transcript"] == "t5"
    assert entries[-1]["transcript"] == "t104"


def test_history_duration_is_seconds_in_range() -> None:
    """`duration_seconds` ist **Sekunden** (P8.D3) und liegt in [0, hard_cap]."""
    harness = Harness(router=FakeRouter(question_decision()))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    duration = _entries(harness)[0]["duration_seconds"]
    assert isinstance(duration, float)
    assert 0.0 <= duration < 15.0  # < HARD_CAP_SECONDS (Literal, E56)
    assert harness.pipeline._turn_started == {}  # Startzeit aufgeräumt


def test_history_transcript_is_truncated_at_500_chars() -> None:
    """`transcript` wird auf 500 Zeichen gekürzt (mit `…`), Roh-Audio nie."""
    harness = Harness()
    long_text = "A" * 5000
    harness.pipeline._record_turn(
        DEV, state=harness.pipeline._state(DEV), outcome="ok", transcript=long_text
    )

    entry = _entries(harness)[0]
    assert len(entry["transcript"]) == 500  # Literal, E56
    assert entry["transcript"].endswith("…")
    assert entry["transcript"] == "A" * 499 + "…"


def test_history_redacts_secrets_in_transcript_and_error() -> None:
    """Secrets in Transcript/Fehlertext landen **nicht** im Puffer (P8.D3 Privacy).

    Geprüft werden die Muster, die `dashboard.redact()` vertraglich abdeckt
    (`ha_token=…`, `Authorization: …`, `Bearer …`, URL-Userinfo). Bewusst *kein*
    generischer Token-Regex – den gibt es in `redact()` absichtlich nicht.
    """
    harness = Harness()
    harness.pipeline._record_turn(
        DEV,
        state=harness.pipeline._state(DEV),
        outcome="error",
        transcript="mein ha_token=sup3rs3cret und mehr",
        error="Authorization: Bearer eyJhbGciOi.abc.def",
    )

    entry = _entries(harness)[0]
    assert "sup3rs3cret" not in entry["transcript"]
    assert "eyJhbGciOi.abc.def" not in entry["error"]
    # Der Schlüsselname bleibt (Diagnose), nur der Wert ist maskiert.
    assert "ha_token" in entry["transcript"]


def test_history_never_stores_response_text_or_raw() -> None:
    """`response_text` und `raw` landen **nie** im Puffer (Allowlist)."""
    harness = Harness(
        router=FakeRouter(
            entity_decision(raw={"secret": "geheim", "needs_param": {"noul": 0.8}})
        )
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entry = _entries(harness)[0]
    assert "response_text" not in entry
    assert "raw" not in entry
    assert "Licht an." not in str(entry)  # vollständiger Piper-Text fehlt
    assert "geheim" not in str(entry)


def test_history_needs_param_unknown_is_none_not_false() -> None:
    """Ohne Jev-Antwort ⇒ `needs_param=None` (unbekannt), nicht `False`."""
    harness = Harness(router=FakeRouter(entity_decision(raw={"target": {}})))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert _entries(harness)[0]["needs_param"] is None


def test_history_noul_below_threshold_is_false() -> None:
    """`noul=0.2` ⇒ `needs_param=False` (E58-Schwelle 0.5)."""
    harness = Harness(
        router=FakeRouter(entity_decision(raw={"needs_param": {"noul": 0.2}}))
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert _entries(harness)[0]["needs_param"] is False


def test_history_drops_service_data_key_outside_allowlist() -> None:
    """Ein Key **außerhalb** der E92-Allowlist wird nicht übernommen."""
    harness = Harness(
        router=FakeRouter(entity_decision(service_data={"transition": 3, "code": "abc"}))
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert _entries(harness)[0]["extracted_param"] is None


def test_history_button_turn_has_no_wake_score() -> None:
    """Button-Turn: `last_score` bleibt 0.0 ⇒ `score=None` (keine Messung)."""
    harness = Harness(router=FakeRouter(question_decision()))

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 138) is True
        assert harness.state() is PipelineState.LISTENING
        # Button-Turn beenden (still) – er endet nicht von selbst.
        assert await harness.pipeline.on_no_speech(DEV) is True

    run(scenario())

    entries = _entries(harness)
    assert len(entries) == 1
    assert entries[0]["score"] is None
    assert entries[0]["barge_in"] is False


def test_history_start_tracking_is_per_device_and_leak_free() -> None:
    """`_turn_started` wird pro Turn gesetzt und restlos wieder entfernt."""
    harness = Harness(router=FakeRouter(question_decision()))

    async def scenario() -> None:
        await _arm(harness)
        assert DEV in harness.pipeline._turn_started
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert harness.pipeline._turn_started == {}


# ═══════════════════════════════════════════════════════════════════════════
# Konstanten + Netz-Nachweis
# ═══════════════════════════════════════════════════════════════════════════
def test_safety_net_constants_from_config() -> None:
    assert HARD_CAP_SECONDS == 15.0
    assert NO_SPEECH_SECONDS == 8.0


def test_invalid_mic_payload_raises_pipeline_error() -> None:
    harness = Harness()
    with pytest.raises(PipelineError):
        run(harness.pipeline.on_mic_frame(DEV, 0, "kein bytes"))  # type: ignore[arg-type]


def test_pipeline_runs_while_network_block_is_active() -> None:
    """Die Netzsperre (E36) ist aktiv; die Pipeline läuft vollständig in-process.

    Ein echter Socket-Kontakt (DNS) blutet als `NetworkAccessBlocked` hoch —
    genau das würde passieren, wenn die Pipeline das Netz träfe.  Der
    vollständige Turn aus reinen Fakes ist damit ein **positiver** Beweis für
    Netzfreiheit (L0/§7.1).
    """
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("mock-pipeline.invalid", 80)

    harness = Harness(router=FakeRouter(question_decision("Antwort.")))

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    assert harness.tts.calls == ["Antwort."]
    assert harness.state() is PipelineState.IDLE


# ═══════════════════════════════════════════════════════════════════════════
# 11. P9.T0 – Phasen-Timing (Messung, **kein** Verhaltenswechsel)
# ═══════════════════════════════════════════════════════════════════════════
# Die Feldnamen stehen hier als **Literale** (E56/E59-Konvention: sonst wäre
# eine Mutation an genau diesen Namen wirkungslos).  Erwartet wird die
# Semantik, nicht ein exakter Zahlenwert:
#   * erreichte Phase   ⇒ Zahl (float), >= 0
#   * nicht erreichte    ⇒ **None**, nie 0
#   * Reihenfolge        ⇒ verschachtelte Phasen: Ende >= Ende der inneren
# Die Arithmetik selbst wird mit synthetischen Zeitpunkten exakt geprüft
# (`test_phase_span_*`), damit der Test nicht von der echten Uhr abhängt.
LATENCY_ALL = (
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


def _entry(harness: Harness) -> dict:
    entries = _entries(harness)
    assert len(entries) == 1, f"genau ein Turn erwartet, war {len(entries)}"
    return entries[0]


def _command_turn(harness: Harness) -> None:
    """Kompletter COMMAND-Turn: Wake → VAD-End → STT → Routing → HA → TTS."""

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())


def test_latency_felder_sind_additiv_vorhanden() -> None:
    """Alle neun Felder sind im Eintrag – **kein** altes Feld fehlt."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)

    entry = _entry(harness)
    for field in LATENCY_ALL:
        assert field in entry, f"{field} fehlt im History-Eintrag"
    # Nichts, was vor P9.T0 drin war, ist verschwunden.
    for field in ("ts", "device_id", "state", "intent", "outcome",
                  "duration_seconds", "score", "confidence", "service",
                  "domain", "target_entity_id", "needs_param",
                  "extracted_param", "transcript", "error_code", "error",
                  "barge_in"):
        assert field in entry, f"{field} (Bestand) fehlt"


def test_command_turn_misst_alle_phasen_als_zahlen() -> None:
    """COMMAND: **alle** Phasen werden erreicht ⇒ überall eine Zahl >= 0."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)

    entry = _entry(harness)
    for field in LATENCY_ALL:
        value = entry[field]
        assert isinstance(value, float), f"{field}={value!r} ist keine Zahl"
        assert value >= 0.0, f"{field}={value!r} ist negativ"
    # Verschachtelung: die innere Phase endet nicht **später** als die äußere.
    assert entry["phase_tts_first_audio_seconds"] <= entry["phase_tts_total_seconds"]
    assert entry["phase_tts_first_frame_seconds"] >= entry["phase_tts_first_audio_seconds"]
    # Die Phasen sind **aneinandergehängt**, nicht verschachtelt: von
    # `speech_end` bis `ha_done` liegen genau STT → Routing → Ausführung.
    # Deshalb müssen sie zusammen die Schlüsselzahl ergeben (Toleranz für
    # die 3-stellige Rundung: bis zu 0,0005 s je Phase).
    assert entry["phase_stt_seconds"] + entry["phase_route_seconds"] + entry[
        "phase_execute_seconds"
    ] == pytest.approx(entry["latency_after_speech_seconds"], abs=0.002)
    # Ab Wake kommt das Zuhören dazu, sonst nichts.
    assert entry["phase_listen_seconds"] + entry["phase_stt_seconds"] + entry[
        "phase_route_seconds"
    ] + entry["phase_execute_seconds"] == pytest.approx(
        entry["latency_after_wake_seconds"], abs=0.003
    )


def test_erstes_gesendetes_frame_kommt_nach_erstem_pcm_chunk() -> None:
    """Der Puffer zwischen Piper und dem Gerät ist **sichtbar**, nicht weg."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)

    entry = _entry(harness)
    assert entry["phase_tts_first_audio_seconds"] is not None
    assert entry["phase_tts_first_frame_seconds"] is not None
    assert entry["phase_tts_first_frame_seconds"] >= entry["phase_tts_first_audio_seconds"]


def test_question_turn_ohne_ha_hat_keine_latenz_nach_speech() -> None:
    """QUESTION: kein HA-Call ⇒ **kein** „Licht ist an" – `None`, nicht `0`."""
    harness = Harness(router=FakeRouter(question_decision("Es ist zwölf Uhr.")))
    _command_turn(harness)

    entry = _entry(harness)
    assert entry["latency_after_speech_seconds"] is None
    assert entry["latency_after_wake_seconds"] is None
    assert entry["phase_execute_seconds"] is None
    # STT und Routing wurden sehr wohl gemessen.
    assert isinstance(entry["phase_stt_seconds"], float)
    assert isinstance(entry["phase_route_seconds"], float)
    # TTS gab es (Antwort gesprochen).
    assert isinstance(entry["phase_tts_total_seconds"], float)


def test_button_turn_ohne_wake_wort_hat_keine_wake_latenz() -> None:
    """K6/Button: es gab kein Wake-Wort ⇒ `latency_after_wake` ist `None`."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 138) is True
        await harness.pipeline.on_mic_frame(DEV, 0, pcm(0))
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entry = _entry(harness)
    assert entry["barge_in"] is False
    assert entry["latency_after_wake_seconds"] is None
    assert entry["phase_listen_seconds"] is None
    # Der HA-Call fand statt – die Schlüsselzahl ist also **nicht** leer.
    assert isinstance(entry["latency_after_speech_seconds"], float)


def test_stt_fehler_misst_stt_aber_keine_ha_latenz() -> None:
    """STT bricht ab: `phase_stt` ist eine Zahl, HA-/TTS-Phasen bleiben leer."""
    harness = Harness(stt=FakeStt(error=RuntimeError("stt kaputt")))
    _command_turn(harness)

    entry = _entry(harness)
    assert entry["outcome"] == "error"
    assert isinstance(entry["phase_stt_seconds"], float)
    assert entry["phase_route_seconds"] is None
    assert entry["latency_after_speech_seconds"] is None
    assert entry["latency_after_wake_seconds"] is None


def test_stt_liefert_leeren_text_ohne_route_und_ha() -> None:
    """Leeres Transkript ⇒ `silence`: kein Routing, kein HA – `None`."""
    harness = Harness(stt=FakeStt(transcript="   "))
    _command_turn(harness)

    entry = _entry(harness)
    assert entry["outcome"] == "silence"
    assert isinstance(entry["phase_stt_seconds"], float)
    assert entry["phase_route_seconds"] is None
    assert entry["latency_after_speech_seconds"] is None
    assert entry["phase_tts_total_seconds"] is None


def test_kein_tts_audio_lässt_erstes_audio_null_aber_gesamt_gemessen() -> None:
    """TTS liefert **keinen** PCM-Chunk: „1. Audio" `None`, „gesamt" eine Zahl.

    Genau der Fall, in dem eine erfundene 0 am meisten täuschen würde: es gab
    TTS-Aufrufe, aber **kein** Audio.  Nur der EOS-Frame ging raus.
    """

    class EmptyTts:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.pipeline: Optional[Pipeline] = None

        def attach(self, pipeline: Pipeline) -> None:
            self.pipeline = pipeline

        async def synthesize(self, text: str):  # type: ignore[no-untyped-def]
            self.calls.append(text)
            if False:  # pragma: no cover – leere Sequenz, nie ein Chunk
                yield (48000, b"")

    harness = Harness(router=FakeRouter(command_decision("Licht an.")), tts=EmptyTts())
    _command_turn(harness)

    entry = _entry(harness)
    assert entry["phase_tts_first_audio_seconds"] is None
    assert entry["phase_tts_first_frame_seconds"] is None
    assert isinstance(entry["phase_tts_total_seconds"], float)
    # „Licht ist an" war trotzdem messbar – der HA-Call lief ja.
    assert isinstance(entry["latency_after_speech_seconds"], float)
    # Nur der EOS-Frame ging raus, kein Audio.
    assert harness.binaries == [EOS]


def test_abgebrochener_turn_hat_keine_tts_gesamtdauer() -> None:
    """Barge-in im TTS ⇒ `phase_tts_total` bleibt `None` (unvollendet)."""
    gate = asyncio.Event()
    harness = Harness(
        tts=FakeTts(gate=gate), router=FakeRouter(command_decision("Licht an."))
    )

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_vad_end(DEV) is True
        assert await _pump(lambda: harness.state() is PipelineState.SPEAKING) is True
        # Barge-in = der reale Abbruchpfad: `speaker_flush` + Turn-Cancel.
        assert await harness.pipeline.on_wake_detected(DEV, 0.2) is True

    run(scenario())

    entry = _entry(harness)
    assert entry["outcome"] == "cancelled"
    assert entry["phase_tts_total_seconds"] is None
    assert entry["phase_tts_first_audio_seconds"] is None
    # STT/Routing/HA waren abgeschlossen ⇒ echte Zahlen.
    assert isinstance(entry["phase_stt_seconds"], float)
    assert isinstance(entry["latency_after_speech_seconds"], float)


def test_messung_veraendert_die_zustandsfolge_nicht() -> None:
    """Die Instrumentierung fügt **keinen** Schritt in §5 ein.

    Geprüft wird die vom Router/STT/TTS jeweils gesehene Zustandsfolge: sie
    muss ohne jede Zeitmarke exakt dieselbe sein wie mit ihr – ein `await` an
    einer Messstelle hätte hier eine zusätzliche Yield-Möglichkeit erzeugt.
    """
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)

    assert harness.stt.states == [PipelineState.TRANSCRIBING]
    assert harness.router.route_states == [PipelineState.ROUTING]
    assert harness.router.execute_states == [PipelineState.EXECUTING]
    assert harness.tts.states == [PipelineState.SPEAKING]
    # HA **vor** TTS – die Reihenfolge, die P9.T0 ausdrücklich nicht ändern durfte.
    assert harness.router.execute_calls and harness.tts.calls
    assert _entry(harness)["state"] == "speaking"


def test_messung_addiert_keinen_turn_zur_historie() -> None:
    """Genau **ein** Eintrag, genau ein Turn – die Messung dupliziert nichts."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)
    assert len(_entries(harness)) == 1
    assert len(harness.stt.calls) == 1
    assert len(harness.router.route_calls) == 1
    assert len(harness.router.execute_calls) == 1
    assert len(harness.tts.calls) == 1


def test_phase_marks_werden_nach_turn_freigegeben() -> None:
    """`_phase_marks` leert sich – kein `dict` pro Turn bleibt liegen."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)
    assert harness.pipeline._phase_marks == {}


def test_phase_marks_ueberleben_keinen_stillen_abbruch() -> None:
    """Auch der Stille-Pfad räumt seine Zeitpunkte ab (kein Leck)."""
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_no_speech(DEV) is True

    run(scenario())

    assert harness.pipeline._phase_marks == {}
    entry = _entry(harness)
    assert entry["outcome"] == "silence"
    # Kein `speech_end` ⇒ nichts „gemessen" statt „0".
    for field in LATENCY_ALL:
        assert entry[field] is None, f"{field}={entry[field]!r} hätte None sein müssen"


def test_button_turn_mit_0x04_ohne_auf_speech_wartet() -> None:
    """Button-Turn ohne Sprache (leeres Audio) ⇒ alle Phasen `None`."""
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 138) is True
        assert await harness.pipeline.on_vad_end(DEV) is True

    run(scenario())

    entry = _entry(harness)
    for field in LATENCY_ALL:
        assert entry[field] is None, f"{field}={entry[field]!r} hätte None sein müssen"
    # Die Gesamtdauer bleibt trotzdem gemessen (P8.D3, unverändert).
    assert isinstance(entry["duration_seconds"], float)


def test_stt_dauer_ist_eine_echte_messung_nicht_konstruiert() -> None:
    """Ein bewusst langsames STT ergibt eine **echte** Zahl > 0 (kein 0,0)."""

    class SlowStt(FakeStt):
        async def transcribe(self, audio: bytes) -> str:
            self.calls.append(bytes(audio))
            await asyncio.sleep(0.02)  # einziges echtes Warten in diesem Modul
            return self.transcript

    harness = Harness(stt=SlowStt(), router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)

    entry = _entry(harness)
    # Großzügige Untergrenze: beweist die Messung, ohne flakig zu sein.
    assert entry["phase_stt_seconds"] >= 0.01


# ── Arithmetik (`_phase_span`) – exakt, ohne echte Uhr ───────────────────
def test_phase_span_rechnet_aus_zwei_zeitpunkten() -> None:
    marks = {"speech_end": 10.0, "ha_done": 12.3456}
    assert Pipeline._phase_span(marks, "speech_end", "ha_done") == 2.346


def test_phase_span_ist_none_wenn_ein_punkt_fehlt() -> None:
    """Fehlender Endpunkt ⇒ `None`.  Ein fehlender Punkt darf **keine** 0 liefern."""
    assert Pipeline._phase_span({"speech_end": 1.0}, "speech_end", "ha_done") is None
    assert Pipeline._phase_span({"ha_done": 1.0}, "speech_end", "ha_done") is None
    assert Pipeline._phase_span({}, "speech_end", "ha_done") is None
    assert Pipeline._phase_span(None, "speech_end", "ha_done") is None


def test_phase_span_behaelt_eine_echte_null() -> None:
    """Zwei gleiche Zeitpunkte sind eine Messung von 0,0 – **kein** `None`."""
    assert Pipeline._phase_span({"a": 5.0, "b": 5.0}, "a", "b") == 0.0


def test_phase_span_klemmt_negative_werte_ab() -> None:
    """Eine kaputte Uhr darf keinen negativen Wert in die Historie schreiben."""
    assert Pipeline._phase_span({"a": 7.0, "b": 5.0}, "a", "b") == 0.0


def test_phase_fields_ohne_marks_sind_alle_none() -> None:
    """Keine Zeitpunkte ⇒ **alle** neun Felder `None` (nie 0)."""
    harness = Harness()
    fields = harness.pipeline._phase_fields(DEV)
    assert set(fields) == set(LATENCY_ALL)
    assert all(value is None for value in fields.values())


def test_phase_fields_rechnen_aus_synthetischen_marks() -> None:
    """Exakte Rechnung aus einer Mark-Liste – ohne echte Uhr, ohne Turn."""
    harness = Harness()
    harness.pipeline._phase_marks[DEV] = {
        "wake_detected": 100.0,
        "speech_end": 101.0,
        "stt_start": 101.0,
        "stt_done": 103.0,
        "jev_done": 104.0,
        "ha_done": 104.5,
        "tts_start": 104.6,
        "tts_first_audio": 106.6,
        "tts_first_frame": 106.8,
        "tts_done": 108.0,
    }
    fields = harness.pipeline._phase_fields(DEV)
    assert fields["phase_listen_seconds"] == 1.0
    assert fields["phase_stt_seconds"] == 2.0
    assert fields["phase_route_seconds"] == 1.0
    assert fields["phase_execute_seconds"] == 0.5
    assert fields["latency_after_speech_seconds"] == 3.5
    assert fields["latency_after_wake_seconds"] == 4.5
    assert fields["phase_tts_first_audio_seconds"] == 2.0
    assert fields["phase_tts_first_frame_seconds"] == 2.2
    assert fields["phase_tts_total_seconds"] == 3.4
    # `_phase_fields` liest **nur** – der Ring bleibt unangetastet.
    assert DEV in harness.pipeline._phase_marks


def test_wake_detected_nutzt_die_turn_startzeit() -> None:
    """`_arm_listening` setzt Wake und Turn-Start aus **einer** Uhrlesung."""
    harness = Harness()

    async def scenario() -> None:
        assert await harness.pipeline.on_wake_detected(DEV, 0.95) is True

    run(scenario())

    marks = harness.pipeline._phase_marks[DEV]
    assert marks["wake_detected"] == harness.pipeline._turn_started[DEV]


def test_wake_zugriff_liefert_kopien_mit_geräte_id() -> None:
    """`wake_attempts(device_id)` hängt die ID an und kopiert die Einträge."""
    harness = Harness()
    # Attrappe mit Ringpuffer – wie `WakeWordDetector.wake_attempts`.
    class Attrappe:
        def __init__(self) -> None:
            self.wake_attempts = [{"ts": "t", "score": 0.9, "accepted": True}]

    state = harness.pipeline._state(DEV)
    state.wake = Attrappe()  # type: ignore[assignment]
    entries = harness.pipeline.wake_attempts(DEV)
    assert entries == [
        {"ts": "t", "score": 0.9, "accepted": True, "device_id": DEV}
    ]
    entries[0]["score"] = 0.1
    # Der Aufrufer bekommt eine **Kopie** – der Ring bleibt unverändert.
    assert state.wake.wake_attempts[0]["score"] == 0.9


def test_wake_zugriff_bei_unbekanntem_geraet_ist_leer() -> None:
    harness = Harness()
    assert harness.pipeline.wake_attempts("gibt-es-nicht") == []
    assert harness.pipeline.wake_threshold() is None


# ═══════════════════════════════════════════════════════════════════════════
# P9.T5 (E100): Endpoint-Diagnostik in der Historie + Audio-Dump
# ═══════════════════════════════════════════════════════════════════════════
def test_history_wake_turn_hat_endpoint_felder_mit_wirksamer_schwelle() -> None:
    """Wake-Turn: alle sechs Felder da; Absolut-Boden `0.004` greift (floor=0)."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))
    _command_turn(harness)

    entry = _entry(harness)
    assert entry["endpoint_noise_floor"] == 0.0
    # Wirksame Schwelle = max(3·floor, 0.004) = 0.004 – der **Absolut-Boden**.
    assert entry["endpoint_threshold"] == 0.004
    # `_arm` legte 1 Turn-Chunk an: von 3 Preroll-Frames wurde 1 übersprungen.
    assert entry["endpoint_skip_frames"] == PREROLL_DISCARD_CHUNKS - 1
    assert entry["endpoint_speech_frames"] == 0
    assert entry["endpoint_silence_frames"] == 0
    assert entry["endpoint_speech_seconds"] == 0.0


def test_history_endpoint_threshold_nutzt_raumboden_wenn_er_dominant_ist() -> None:
    """Hoher Raum-Boden: Schwelle = 3·floor (> 0.004), floor unverändert gelesen."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))

    async def scenario() -> None:
        await _arm(harness)
        # Raum-Boden „während" des Turns gesetzt (reiner Test-Stupser –
        # `_update_noise_floor` läuft nur im IDLE und wird hier nicht fällig).
        harness.device().endpoint_noise_floor = 0.01
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entry = _entry(harness)
    assert entry["endpoint_noise_floor"] == 0.01
    assert entry["endpoint_threshold"] == 0.03


def test_history_button_turn_endpoint_felder_sind_none() -> None:
    """Button-Turn (K6): kein manager-seitiges Endpointing ⇒ alles `None`."""
    harness = Harness(router=FakeRouter(command_decision("Licht an.")))

    async def scenario() -> None:
        assert await harness.pipeline.on_button(DEV, 138) is True
        await harness.pipeline.on_mic_frame(DEV, 0, pcm(0))
        assert await harness.pipeline.on_vad_end(DEV) is True
        await harness.pipeline.wait_turn(DEV)

    run(scenario())

    entry = _entry(harness)
    for name in (
        "endpoint_noise_floor",
        "endpoint_threshold",
        "endpoint_speech_frames",
        "endpoint_silence_frames",
        "endpoint_skip_frames",
        "endpoint_speech_seconds",
    ):
        assert entry[name] is None, f"{name} muss beim Button-Turn None sein"


def test_history_endpoint_felder_auch_am_stillen_endpfad() -> None:
    """Auch ein Turn, der still endet (0x05), zeigt seine Endpoint-Zahlen."""
    harness = Harness()

    async def scenario() -> None:
        await _arm(harness)
        assert await harness.pipeline.on_no_speech(DEV) is True

    run(scenario())

    entry = _entry(harness)
    assert entry["outcome"] == "silence"
    assert entry["endpoint_threshold"] == 0.004
    assert isinstance(entry["endpoint_skip_frames"], int)


def test_dump_aus_ist_wirklich_kein_io(tmp_path: "Any") -> None:
    """Gate zu (`Default`): kein Verzeichnis, keine Datei, kein I/O."""
    dump_dir = tmp_path / "dumps"
    harness = Harness(
        settings=FakeSettings(audio_dump_enabled=False, audio_dump_dir=str(dump_dir)),
        router=FakeRouter(command_decision("Licht an.")),
    )
    _command_turn(harness)

    assert not dump_dir.exists()
    assert _entry(harness)["outcome"] == "ok"


def test_dump_an_schreibt_wav_und_json_mit_endpoint_stats(tmp_path: "Any") -> None:
    """Gate auf: exakt ein WAV (16 kHz/S16_LE/mono) + JSON mit denselben Stats."""
    import json
    import wave

    dump_dir = tmp_path / "dumps"
    harness = Harness(
        settings=FakeSettings(audio_dump_enabled=True, audio_dump_dir=str(dump_dir)),
        router=FakeRouter(command_decision("Licht an.")),
    )
    _command_turn(harness)

    wavs = list(dump_dir.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() == CHUNK // 2  # 1 Turn-Chunk à 80 ms

    jsons = list(dump_dir.glob("*.json"))
    assert len(jsons) == 1
    sidecar = json.loads(jsons[0].read_text(encoding="utf-8"))
    assert sidecar["outcome"] == "ok"
    assert sidecar["transcript"] == "schalte das licht ein"
    assert sidecar["endpoint_threshold"] == 0.004
    assert sidecar["audio_bytes"] == CHUNK
    assert jsons[0].stem == wavs[0].stem  # Paar am selben Stem erkennbar


def test_dump_fehler_bricht_den_turn_nie(tmp_path: "Any") -> None:
    """Dump-Ziel ist eine **Datei** ⇒ Dump scheitert, Turn läuft trotzdem sauber."""
    blocker = tmp_path / "kaputt"
    blocker.write_text("ich bin keine directory")
    harness = Harness(
        settings=FakeSettings(audio_dump_enabled=True, audio_dump_dir=str(blocker)),
        router=FakeRouter(command_decision("Licht an.")),
    )
    _command_turn(harness)

    assert harness.state() is PipelineState.IDLE
    entry = _entry(harness)
    assert entry["outcome"] == "ok"
    # Der Turn selbst hat sein Audio an STT geliefert – der Dump ist nur weg.
    assert harness.stt.calls
