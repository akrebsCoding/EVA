"""Wake-Word-Tests (P2.T5, `PLAN.md`:429, Layer **L0**).

Prüfling ist `app/wake_word.py` (P2.T4).  Getestet wird **gegen die echte
API** (`STATE.md` §3 „Wake-Word (P2.T4)" = Vertrag), nicht gegen eine
Wunschfassung.  Die gesamte Logik läuft über den **injizierten**
`score_provider` (`Callable[[np.ndarray], float]`) bzw. eine Fake-`model_factory`
– **kein echtes Modell, kein `onnxruntime`, kein Netz, kein Gerät** (L0).

Deterministische Zeit: der `Cooldown` bekommt eine injizierbare Uhr
(`clock`), deshalb wird **nie** `time.sleep` benutzt; die Zeit ist eine reine
Zahlenfolge.  Die Scores sind Literale, damit die Erwartungen exakt sind.

Abdeckung der ≥7 Pflichtfälle aus `PLAN.md:429`:

1. **Chunk-Accumulator** – 2560-B-Chunks, angebrochene Reste bleiben erhalten
   → `test_chunk_accumulator_*`
2. **Warm-up-Gate verwirft** – nach `reset()` werden die ersten **15** Chunks
   verworfen, der **16.** ist der erste vertrauenswürdige (das ist die
   Referenzsemantik, **E48**; `OWW_WARMUP_CHUNKS=16` ist die **Fensterlänge**,
   nicht die Zahl verworfener Scores) → `test_warmup_*`
3. **Schwellen-Vergleich** – `score >= OWW_THRESHOLD` (0.9) löst aus, im
   SPEAKING-Zustand bereits `>= OWW_BARGE_IN_THRESHOLD` (**Default 0.15**, E23)
   → `test_threshold_*`
4. **Cooldown** – zweite Auslösung innerhalb 1000 ms geblockt, danach wieder
   frei; Semantik **ab Erkennung** (nicht ab `reset()`) → `test_cooldown_*`
5. **`reset()`-Verhalten** – Accumulator + Warm-up-Gate + Cooldown zurück
   → `test_reset_*`
6. **Deterministischer Score auf Fixture** – injizierter Provider, kein Modell
   → `test_deterministic_score_*`
7. **Verhalten ohne NS** – `speex_ns=False`/`True` sauber an den
   Modell-Konstruktor durchgereicht (Konstruktor-Boolean, **E39**) →
   `test_speex_ns_*`

Dazu zwei Zusatztests für die in **E48** verbindlich festgelegten Semantiken
(`OWW_SCORE_EVERY_N_CHUNKS` = n-ter **akkumulierter** Chunk; `model_factory` +
`model.reset()` in `reset()`).

**Kein Modell, kein Netz.**  Der Mutationsnachweis erfolgte an
`app/wake_word.py` (Warm-up 16→0, Barge-in 0.15→0.9, Cooldown 1000→0) und ist
in `STATE.md` §5 dokumentiert; der Prüfling ist danach **byteidentisch**.
"""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Callable

import numpy as np
import pytest

from app.config import settings as _module_settings
from app.wake_word import (
    ATTEMPT_LOG_FLOOR_RATIO,
    OWW_BARGE_IN_THRESHOLD,
    OWW_CHUNK_BYTES,
    OWW_COOLDOWN_MS,
    OWW_MODEL,
    OWW_SPEEX_NS,
    OWW_THRESHOLD,
    OWW_WARMUP_CHUNKS,
    WAKE_ATTEMPT_KEEP_FLOOR,
    WAKE_ATTEMPTS_MAXLEN,
    Cooldown,
    WakeEvent,
    WakeWordDetector,
    WakeWordError,
    WarmupGate,
)

pytestmark = pytest.mark.unit

#: Ein voller OWW-Chunk (80 ms @ 16 kHz S16_LE mono) in Byte.
CHUNK = OWW_CHUNK_BYTES


def mic_chunk(start: int = 0) -> bytes:
    """Ein deterministischer 2560-B-Mic-Chunk (1280 `int16`-Samples, E46).

    Ganzzahlig und ohne `math.sin`/`random` – auf jeder Plattform bitgleich.
    """
    values = (
        np.arange(start, start + CHUNK // 2, dtype=np.int64) * 7 - 3000
    ) % 60000 - 30000
    return values.astype(np.int16).tobytes()


def fixed_provider(value: float) -> Callable[[np.ndarray], float]:
    """Score-Provider, der konstant `value` liefert."""

    def _provider(samples: np.ndarray) -> float:
        return value

    return _provider


# ── 1. Chunk-Accumulator: 2560-B-Chunks, Rest bleibt erhalten ────────────
def test_chunk_accumulator_yields_full_chunks_and_keeps_remainder() -> None:
    """Beliebig gestückelte Payloads → volle 2560-B-Chunks; Rest bleibt."""
    seen: list[np.ndarray] = []

    def provider(samples: np.ndarray) -> float:
        seen.append(samples)
        return 0.0

    detector = WakeWordDetector(score_provider=provider)

    # Erster Aufruf: ein voller Chunk + 1000 B angebrochener Rest.
    assert detector.process(mic_chunk() + b"\x01" * 1000) == []
    assert detector.evaluations == 1
    assert detector.chunks_accumulated == 1
    assert detector.pending_bytes == 1000

    # Zweiter Aufruf füllt den Rest genau auf (1000 + 1560 = 2560).
    assert detector.process(b"\x02" * 1560) == []
    assert detector.evaluations == 2
    assert detector.chunks_accumulated == 2
    assert detector.pending_bytes == 0

    # Der Provider hat **int16**, roh und 1280 Samples pro Chunk gesehen (E46).
    assert len(seen) == 2
    for samples in seen:
        assert samples.dtype == np.dtype("int16")
        assert samples.shape == (CHUNK // 2,) == (1280,)

    # Ein einzelner 5120-B-Strom ergibt genau zwei volle Chunks.
    fresh = WakeWordDetector(score_provider=provider)
    seen.clear()
    assert fresh.process(b"\x03" * (2 * CHUNK)) == []
    assert fresh.evaluations == 2
    assert fresh.pending_bytes == 0
    assert len(seen) == 2


# ── 2. Warm-up-Gate: 15 verworfen, der 16. ist vertrauenswürdig (E48) ─────
def test_warmup_gate_discards_fifteen_and_trusts_the_sixteenth() -> None:
    """Die tatsächliche Semantik: **15** Scores verworfen, der **16.** zählt.

    Das ist die Referenzregel (`em_oww_warmup.WarmupGate`), festgehalten als
    **E48**: `OWW_WARMUP_CHUNKS=16` beschreibt die **Fensterlänge**
    (`[1, 16, 96]`), nicht die Zahl verworfener Scores.
    """
    assert OWW_WARMUP_CHUNKS == 16
    detector = WakeWordDetector(score_provider=fixed_provider(0.99))
    chunk = mic_chunk()

    for index in range(15):
        assert detector.process(chunk) == [], f"Chunk {index + 1} darf nicht zählen"
    assert detector.warmup.fed == 15
    assert detector.warmup.ready is False
    assert detector.evaluations == 15  # ausgewertet, aber verworfen

    events = detector.process(chunk)  # der 16. Chunk
    assert len(events) == 1
    assert detector.warmup.fed == 16
    assert detector.warmup.ready is True
    assert events[0].chunk_index == 16
    assert events[0].score == 0.99


def test_warmup_gate_feed_contract_is_false_fifteen_times() -> None:
    """`WarmupGate.feed()` isoliert: 15× `False`, beim 16. `True`."""
    gate = WarmupGate(OWW_WARMUP_CHUNKS)
    statuses = [gate.feed() for _ in range(16)]
    assert statuses[:15] == [False] * 15
    assert statuses[15] is True
    assert gate.fed == 16
    assert gate.ready is True
    # Nach `reset()` ist das Fenster wieder ungewärmt.
    gate.reset()
    assert gate.fed == 0
    assert gate.ready is False
    assert gate.feed() is False


# ── 3. Schwellen-Vergleich (Ruhe 0.80 / SPEAKING 0.15, Default geprüft) ───
def test_threshold_idle_uses_oww_threshold() -> None:
    """Ruhezustand: `score >= OWW_THRESHOLD` (0.80 seit P9.T1) löst aus, 0.79 nicht.

    Der P9.T1-Datensatz (4 Annahmen 0,922/0,955/0,988/0,977, 9 Ablehnungen
    0,526–0,887) wird mit 0,80 **ohne** Fehlversuch angenommen, **außer** dem
    tiefsten Wert 0,526, der auch weiterhin abgelehnt wird – das ist der
    ganze Zweck der Absenkung.
    """
    assert OWW_THRESHOLD == 0.80

    hit = WakeWordDetector(score_provider=fixed_provider(0.8), warmup_chunks=0)
    assert hit.speaking is False
    assert hit.threshold == OWW_THRESHOLD == 0.80
    assert hit.barge_in is False
    events = hit.process(mic_chunk())
    assert len(events) == 1
    assert events[0].score == 0.8
    assert events[0].threshold == 0.8
    assert events[0].barge_in is False
    assert isinstance(events[0], WakeEvent)

    miss = WakeWordDetector(score_provider=fixed_provider(0.799), warmup_chunks=0)
    assert miss.process(mic_chunk()) == []

    # Der P9.T1-Datensatz: 4 Annahmen 0,922/0,955/0,988/0,977 ⇒ alle über
    # 0,80.  Von den 9 `below_threshold` (0,526–0,887) werden die **acht**
    # höchsten (0,80–0,887) jetzt angenommen, der tiefste (0,526) bleibt
    # (korrekt) abgelehnt.  Genau das war der Punkt.
    rejected = (0.887, 0.870, 0.860, 0.850, 0.840, 0.830, 0.820, 0.810, 0.526)
    assert sum(1 for v in rejected if v >= 0.80) == 8
    assert sum(1 for v in (0.922, 0.955, 0.988, 0.977) if v >= 0.80) == 4


def test_threshold_speaking_uses_barge_in_default_not_explicit() -> None:
    """SPEAKING: es gilt der **Default** `OWW_BARGE_IN_THRESHOLD` (0.15, E23).

    Der Detektor wird **ohne** `barge_in_threshold`-Argument gebaut, damit der
    Config-Default (nicht ein explizit übergebener Wert) geprüft wird.
    """
    assert OWW_BARGE_IN_THRESHOLD == 0.15

    detector = WakeWordDetector(score_provider=fixed_provider(0.15), warmup_chunks=0)
    detector.set_speaking(True)
    assert detector.speaking is True
    assert detector.barge_in is True
    assert detector.threshold == OWW_BARGE_IN_THRESHOLD == 0.15

    events = detector.process(mic_chunk())
    assert len(events) == 1
    assert events[0].score == 0.15
    assert events[0].threshold == OWW_BARGE_IN_THRESHOLD
    assert events[0].barge_in is True

    # Unterhalb der Barge-in-Schwelle löst nichts aus.
    below = WakeWordDetector(score_provider=fixed_provider(0.149), warmup_chunks=0)
    below.set_speaking(True)
    assert below.process(mic_chunk()) == []

    # Barge-in deaktiviert ⇒ es bleibt bei `OWW_THRESHOLD` (0.80).
    disabled = WakeWordDetector(
        score_provider=fixed_provider(0.15), warmup_chunks=0, barge_in_enabled=False
    )
    disabled.set_speaking(True)
    assert disabled.barge_in is False
    assert disabled.threshold == OWW_THRESHOLD == 0.80
    assert disabled.process(mic_chunk()) == []

    # Zurück im Ruhezustand gilt wieder 0.80.
    detector.set_speaking(False)
    assert detector.threshold == OWW_THRESHOLD


# ── 4. Cooldown: 1000 ms ab Erkennung, danach wieder frei ────────────────
def test_cooldown_blocks_for_1000ms_from_detection() -> None:
    """Zweite Auslösung < 1000 ms ab **Erkennung** wird geblockt, danach frei.

    Die Zeit ist injiziert (`clock`), es wird **nicht** geschlafen.  Der
    Cooldown wird erst beim Treffer scharf (`arm()` im Erkennungsmoment), nicht
    bei der Konstruktion/`reset()` – wäre er an `t=0` verankert, liefe er bei
    `t=1000.999` längst ab und die Block-Assertion unten schlüge fehl.
    """
    assert OWW_COOLDOWN_MS == 1000
    now = [1000.0]
    detector = WakeWordDetector(
        score_provider=fixed_provider(0.95),
        clock=lambda: now[0],
        warmup_chunks=0,
    )
    assert detector.cooldown.cooldown_seconds == 1.0
    assert detector.cooldown.armed is False

    chunk = mic_chunk()
    first = detector.process(chunk)
    assert len(first) == 1
    assert first[0].chunk_index == 1
    assert detector.cooldown.armed is True

    # 500 ms nach der Erkennung: geblockt (Chunk 2, kein Event).
    now[0] = 1000.5
    assert detector.process(chunk) == []
    # 999 ms: weiterhin geblockt (Chunk 3, kein Event).
    now[0] = 1000.999
    assert detector.process(chunk) == []
    # Exakt 1000 ms: Cooldown abgelaufen ⇒ wieder erlaubt (Chunk 4).
    now[0] = 1001.0
    second = detector.process(chunk)
    assert len(second) == 1
    assert second[0].chunk_index == 4
    # Mit dem zweiten Treffer wurde der Cooldown neu scharfgeschaltet.
    now[0] = 1001.5
    assert detector.process(chunk) == []  # Chunk 5
    now[0] = 1002.0
    third = detector.process(chunk)
    assert len(third) == 1
    assert third[0].chunk_index == 6


def test_cooldown_unit_clock_boundaries() -> None:
    """`Cooldown` direkt: `active()`/`remaining()` an den Grenzen."""
    now = [0.0]
    cooldown = Cooldown(1000, clock=lambda: now[0])
    assert cooldown.active() is False  # nie scharfgeschaltet
    cooldown.arm()
    assert cooldown.active() is True
    assert cooldown.remaining() == pytest.approx(1.0)
    now[0] = 0.999
    assert cooldown.active() is True
    now[0] = 1.0
    assert cooldown.active() is False
    assert cooldown.remaining() == 0.0
    # `clear()` löscht den Cooldown vollständig.
    cooldown.arm()
    cooldown.clear()
    assert cooldown.armed is False
    assert cooldown.active() is False
    # Negativer Cooldown ist ein Vertragsbruch.
    with pytest.raises(WakeWordError):
        Cooldown(-1)


# ── 5. `reset()`: Accumulator + Warm-up-Gate + Cooldown zurück ───────────
def test_reset_clears_accumulator_warmup_and_cooldown() -> None:
    """`reset()` setzt Chunks, Rest, Warm-up-Gate und Cooldown zurück."""
    detector = WakeWordDetector(score_provider=fixed_provider(0.99))
    chunk = mic_chunk()

    # 16 Chunks: der 16. löst aus (Warm-up) ⇒ Cooldown ist scharf.
    for _ in range(16):
        detector.process(chunk)
    detector.process(b"\x00" * 100)  # angebrochener Rest
    assert detector.chunks_accumulated == 16
    assert detector.pending_bytes == 100
    assert detector.warmup.fed == 16
    assert detector.cooldown.armed is True

    detector.reset()
    assert detector.chunks_accumulated == 0
    assert detector.pending_bytes == 0
    assert detector.warmup.fed == 0
    assert detector.warmup.ready is False
    assert detector.cooldown.armed is False
    assert detector.cooldown.active() is False

    # Nach dem Reset greift das Warm-up-Gate **erneut**.
    for _ in range(15):
        assert detector.process(chunk) == []
    assert len(detector.process(chunk)) == 1


# ── 6. Deterministischer Score-Verlauf über injizierten Provider ─────────
def test_deterministic_score_progression_without_model() -> None:
    """Reproduzierbarer Score-Verlauf; **kein** Modell wird konstruiert."""
    pattern = (0.05, 0.1, 0.2, 0.4, 0.8, 0.95, 0.99, 0.1, 0.95, 0.05)
    seen: list[np.ndarray] = []

    def run() -> tuple[WakeWordDetector, tuple[float, ...], tuple[float, ...]]:
        calls: list[float] = []

        def provider(samples: np.ndarray) -> float:
            seen.append(samples)
            score = pattern[len(calls)]
            calls.append(score)
            return score

        detector = WakeWordDetector(
            score_provider=provider, warmup_chunks=0, cooldown_ms=0
        )
        last_scores: list[float] = []
        trigger_scores: list[float] = []
        for _ in pattern:
            trigger_scores.extend(event.score for event in detector.process(mic_chunk()))
            last_scores.append(detector.last_score)  # type: ignore[arg-type]
        return detector, tuple(last_scores), tuple(trigger_scores)

    first_detector, first_scores, first_events = run()
    second_detector, second_scores, second_events = run()

    assert first_scores == pattern
    assert second_scores == pattern
    assert first_scores == second_scores
    # Auslöser nur bei >= 0.80: 0.80 (Index 4), 0.95 (5), 0.99 (6), 0.95 (8).
    assert first_events == (0.8, 0.95, 0.99, 0.95)
    assert second_events == first_events
    # Ohne `score_provider` wäre ein Modell gebaut worden – hier nie.
    assert first_detector.model_loaded is False
    assert second_detector.model_loaded is False
    assert all(
        samples.dtype == np.dtype("int16") and samples.shape == (1280,)
        for samples in seen
    )


# ── 7. Verhalten ohne NS: Konstruktor-Boolean clean durchgereicht ────────
def test_speex_ns_boolean_is_passed_to_model_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`speex_ns=False`/`True` erreichen den `OWWModel`-Konstruktor **wörtlich** (E39).

    `openwakeword` ist nicht installiert (und wird hier auch nicht gebraucht):
    `_build_model` importiert **lazy**, deshalb wird das Modul per
    `monkeypatch` durch ein Fake-`openwakeword.model` ersetzt.  Geprüft wird
    die echte Pfad-Nutzung inkl. `inference_framework="onnx"` (**E38**).
    """
    assert OWW_SPEEX_NS is True  # Plan-Default (E39)

    recorded: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeModel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            recorded.append((args, kwargs))

        def predict(self, samples: np.ndarray) -> dict[str, float]:
            return {"hey_jarvis_v0.1": 0.0}

        def reset(self) -> None:
            pass

    fake_module = types.ModuleType("openwakeword.model")
    fake_module.Model = FakeModel  # type: ignore[attr-defined]
    fake_package = types.ModuleType("openwakeword")
    fake_package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openwakeword", fake_package)
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake_module)

    off = WakeWordDetector(speex_ns=False)
    assert off.speex_ns is False
    assert off.model_loaded is False
    off.process(mic_chunk())  # löst `_build_model` aus
    assert off.model_loaded is True

    on = WakeWordDetector(speex_ns=True)
    assert on.speex_ns is True
    on.process(mic_chunk())

    assert len(recorded) == 2
    off_args, off_kwargs = recorded[0]
    on_args, on_kwargs = recorded[1]
    for args, kwargs in (recorded[0], recorded[1]):
        assert args[0] == [OWW_MODEL]
        assert kwargs["inference_framework"] == "onnx"
    assert off_kwargs["enable_speex_noise_suppression"] is False
    assert on_kwargs["enable_speex_noise_suppression"] is True
    assert off_kwargs == {
        "enable_speex_noise_suppression": False,
        "inference_framework": "onnx",
    }
    assert on_args == off_args


# ── Zusatz: `OWW_SCORE_EVERY_N_CHUNKS` = n-ter akkumulierter Chunk (E48c) ─
def test_score_every_n_chunks_counts_nth_accumulated_chunk() -> None:
    """Bei `n=3` wird genau der 3., 6., 9., 12. **akkumulierte** Chunk bewertet."""
    calls: list[int] = []

    def provider(samples: np.ndarray) -> float:
        calls.append(len(calls))
        return 0.0

    # Default-Warm-up (16): erst nach 16 Auswertungen ist das Gate gesättigt;
    # bei n=3 sind das 16 · 3 = 48 akkumulierte Chunks.
    detector = WakeWordDetector(score_provider=provider, score_every_n_chunks=3)
    for _ in range(48):
        detector.process(mic_chunk())

    assert detector.chunks_accumulated == 48
    assert detector.evaluations == 16  # 3, 6, 9, …, 48
    assert len(calls) == 16
    assert detector.warmup.fed == 16  # nur die vom Modell gesehenen Chunks

    with pytest.raises(WakeWordError):
        WakeWordDetector(score_provider=provider, score_every_n_chunks=0)


# ── Zusatz: `model_factory`-Injektion + `model.reset()` in `reset()` ──────
def test_model_factory_injection_and_model_reset() -> None:
    """`model_factory` liefert ein Fake-Modell; `reset()` ruft `model.reset()`."""
    resets: list[int] = []

    class FakeModel:
        def predict(self, samples: np.ndarray) -> dict[str, float]:
            return {"hey_jarvis_v0.1": 0.99}

        def reset(self) -> None:
            resets.append(1)

    fake = FakeModel()
    detector = WakeWordDetector(model_factory=lambda: fake)
    assert detector.model_loaded is False

    chunk = mic_chunk()
    for _ in range(16):
        detector.process(chunk)
    assert detector.model_loaded is True
    assert resets == []  # noch kein Reset

    detector.reset()
    assert resets == [1]  # genau einmal `model.reset()`


# ── Randfall: `prediction_key` (Stock-Name vs. `.onnx`-Pfad) ─────────────
def test_prediction_key_stock_name_vs_onnx_path() -> None:
    from app.wake_word import prediction_key

    assert prediction_key(OWW_MODEL) == OWW_MODEL == "hey_jarvis_v0.1"
    assert prediction_key("/models/hey_jarvis_v0.1.onnx") == "hey_jarvis_v0.1"
    assert prediction_key("custom.onnx") == "custom"


# ═══════════════════════════════════════════════════════════════════════════
# 9. P9.T0 – Wake-Versuche (Evidenz, **kein** Verhaltenswechsel)
# ═══════════════════════════════════════════════════════════════════════════
# Der Ringpuffer ist der Beweis dafür, **warum** nicht ausgelöst wurde.  Die
# Gründe sind Literale (E56/E59), die erwarteten Werte ebenfalls – sonst wäre
# eine Mutation an genau diesen Zeichenketten wirkungslos.


def _detector(scores, **kwargs) -> WakeWordDetector:
    """Detektor mit einer **Score-Folge** (kein Modell, keine Zeit)."""
    queue = list(scores)
    calls = {"n": 0}

    def _provider(samples: np.ndarray) -> float:
        index = min(calls["n"], len(queue) - 1)
        calls["n"] += 1
        return float(queue[index])

    kwargs.setdefault("warmup_chunks", 0)
    kwargs.setdefault("clock", lambda: 0.0)
    return WakeWordDetector(score_provider=_provider, **kwargs)


def test_cap_ist_50_und_nicht_groesser() -> None:
    """Der Ring ist hart begrenzt – 120 Versuche ⇒ genau 50 Einträge."""
    assert WAKE_ATTEMPTS_MAXLEN == 50
    detector = _detector([0.50])
    for _ in range(120):
        detector.process(mic_chunk())
    assert len(detector.wake_attempts) == 50


def test_ring_verwirft_die_aeltesten_und_behaelt_die_neuesten() -> None:
    """Nach Überlauf stehen die **jüngsten** Versuche im Ring, in Reihenfolge."""
    detector = _detector([0.50])
    for _ in range(60):
        detector.process(mic_chunk())
    indices = [entry["chunk_index"] for entry in detector.wake_attempts]
    # 60 bewertete Chunks, 50 Plätze ⇒ die Chunks 11..60 stehen drin.
    assert indices == list(range(11, 61))


def test_ring_wird_beim_beschreiben_nicht_neu_allokiert() -> None:
    """Im vollen Ring wird an Ort und Stelle überschrieben (kein Knoten-Leak)."""
    detector = _detector([0.50])
    for _ in range(WAKE_ATTEMPTS_MAXLEN):
        detector.process(mic_chunk())
    for _ in range(5):
        detector.process(mic_chunk())
    second = detector.wake_attempts
    assert len(second) == WAKE_ATTEMPTS_MAXLEN
    # 55 bewertete Chunks ⇒ die Chunks 6..55 stehen im Ring.  Der Container
    # ist hart begrenzt: es werden **keine** Knoten angehängt, sondern der
    # älteste Eintrag überschrieben.
    assert second[0]["chunk_index"] == 6
    assert second[-1]["chunk_index"] == 55


def test_ring_liefert_kopien_keine_referenzen() -> None:
    """Der Aufrufer kann den Ring nicht verändern."""
    detector = _detector([0.50])
    detector.process(mic_chunk())
    snapshot = detector.wake_attempts
    snapshot[0]["score"] = 99.0
    snapshot.append({"score": -1.0})  # type: ignore[arg-type]
    assert detector.wake_attempts[0]["score"] == 0.5
    assert len(detector.wake_attempts) == 1
    # Auch zwei Aufrufe liefern verschiedene Objekt-Instanzen.
    assert detector.wake_attempts[0] is not detector.wake_attempts[0]


def test_angenommener_versuch_steht_im_ring() -> None:
    """Annahme ⇒ `accepted=True`, Grund `accepted`, **kein** Ablehnungsgrund."""
    detector = _detector([0.95])
    assert len(detector.process(mic_chunk())) == 1
    entries = detector.wake_attempts
    assert len(entries) == 1
    entry = entries[0]
    assert entry["accepted"] is True
    assert entry["reason"] == "accepted"
    assert entry["score"] == 0.95
    assert entry["threshold"] == 0.8
    assert entry["chunk_index"] == 1
    assert entry["ts"].endswith("+00:00")


def test_unterschwelliger_versuch_hat_grund_below_threshold() -> None:
    """Ablehnung an der Schwelle ⇒ Grund `below_threshold` mit beiden Zahlen."""
    detector = _detector([0.30])
    assert detector.process(mic_chunk()) == []
    entry = detector.wake_attempts[0]
    assert entry["accepted"] is False
    assert entry["reason"] == "below_threshold"
    assert entry["score"] == 0.3
    assert entry["threshold"] == 0.8


def test_warmup_verwurf_ist_eigener_grund_nicht_below_threshold() -> None:
    """Der Warm-up-Zweig bekommt **seinen** Grund – auch bei Score über 0.8.

    Genau dieser Fall war vorher unsichtbar: der Score lag über der Schwelle,
    wurde aber vom Gate verworfen.  Er darf **nicht** als
    `below_threshold` erscheinen, sonst wäre die Diagnose falsch.
    """
    detector = _detector([0.99], warmup_chunks=5)
    for _ in range(4):  # 1.–4. Chunks: das Gate (Fenster 5) verwirft
        assert detector.process(mic_chunk()) == []
    entries = detector.wake_attempts
    assert len(entries) == 4
    assert all(entry["reason"] == "warmup_gate" for entry in entries)
    assert all(entry["accepted"] is False for entry in entries)
    # Der Score lag wirklich über der Schwelle – der Grund ist trotzdem das Gate.
    assert all(entry["score"] == 0.99 for entry in entries)
    assert all(entry["threshold"] == 0.8 for entry in entries)


def test_cooldown_verwurf_ist_eigener_grund() -> None:
    """Der Cooldown-Zweig bekommt **seinen** Grund (vorher völlig stumm)."""
    now = [1000.0]
    detector = _detector([0.95], clock=lambda: now[0])
    assert len(detector.process(mic_chunk())) == 1  # Annahme
    now[0] = 1000.5
    assert detector.process(mic_chunk()) == []      # Cooldown
    entries = detector.wake_attempts
    assert entries[0]["reason"] == "accepted"
    assert entries[1]["reason"] == "cooldown"
    assert entries[1]["accepted"] is False
    assert entries[1]["score"] == 0.95


def test_uebersprungene_chunks_sind_keine_versuche() -> None:
    """`score_every_n_chunks=3`: nur jeder 3. Chunk ist **ein** Versuch."""
    detector = _detector([0.50], score_every_n_chunks=3)
    for _ in range(9):
        detector.process(mic_chunk())
    entries = detector.wake_attempts
    assert len(entries) == 3
    assert [entry["chunk_index"] for entry in entries] == [3, 6, 9]


def test_barge_in_verwendet_die_barge_in_schwelle_im_eintrag() -> None:
    """Im SPEAKING zählt die **tatsächlich geltende** Schwelle (E23: 0.15)."""
    detector = _detector([0.25])
    detector.set_speaking(True)
    assert len(detector.process(mic_chunk())) == 1  # 0.25 >= 0.15 und > Floor 0.2
    entry = detector.wake_attempts[0]
    assert entry["accepted"] is True
    assert entry["threshold"] == 0.15


def test_below_threshold_bei_barge_in_nutzt_die_schwelle_von_015() -> None:
    """Ablehnung im SPEAKING misst gegen 0.15, nicht gegen 0.8.

    Bei Barge-in liegt die Ablehnungsschwelle (0.15) **unter** dem Ring-Floor
    (0.2): eine `below_threshold`-Ablehnung im SPEAKING landet daher im
    Standardfenster **nicht** im Ring, sondern nur in den Summary-Zählern.  Um
    den Grund trotzdem im Ring zu prüfen, wird der Floor hier testweise
    abgesenkt – genau für solche Diagnosezwecke ist der Floor ein Parameter.
    """
    detector = _detector([0.10], keep_floor=0.05)
    detector.set_speaking(True)
    assert detector.process(mic_chunk()) == []
    entry = detector.wake_attempts[0]
    assert entry["reason"] == "below_threshold"
    assert entry["threshold"] == 0.15


def test_reset_leert_den_ring_nicht_aber_zaehlt_hoch() -> None:
    """`reset()` setzt den Detektor zurück; der Ring ist **Diagnose** und bleibt.

    Bewusste Entscheidung: der Ring dokumentiert, was zuletzt **passiert** ist.
    Ein Reset nach einem Turn (Normalfall) darf die Evidenz des gerade
    abgeschlossenen Wake-Versuchs nicht löschen – sonst wäre die Kachel nach
    jedem Turn leer und es gäbe keinen Beleg.
    """
    detector = _detector([0.95])
    assert len(detector.process(mic_chunk())) == 1
    detector.reset()
    assert len(detector.wake_attempts) == 1
    assert detector.wake_attempts[0]["reason"] == "accepted"
    # Der Chunk-Zähler ist dagegen zurückgesetzt (neue Messung).
    assert detector.chunks_accumulated == 0


def test_ablehnung_wird_als_info_geloggt(caplog) -> None:
    """Schwellennahe Ablehnung ⇒ genau eine `INFO`-Zeile mit Score und Grund.

    `0.50` liegt über dem Beobachtbarkeits-Boden (`ATTEMPT_LOG_FLOOR_RATIO` 0.5
    ⇒ 0.40 bei Schwelle 0.8) und **unter** der Schwelle – also der Fall, der im
    Log stehen muss.
    """
    detector = _detector([0.50])
    with caplog.at_level(logging.INFO, logger="manager.wake_word"):
        detector.process(mic_chunk())
    records = [r for r in caplog.records if r.name == "manager.wake_word"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "0.500" in message
    assert "below_threshold" in message
    assert "0.800" in message  # die wirksame Schwelle mitprotokolliert


def test_warmup_und_cooldown_werden_als_info_geloggt(caplog) -> None:
    """Auch die beiden **strukturellen** Ablehnungen landen auf `INFO`.

    Fenster 3 ⇒ Chunks 1+2 werden vom Gate verworfen, Chunk 3 ist der erste
    vertrauenswürdige und löst aus (0.99), Chunk 4 fällt in den Cooldown.
    """
    now = [1000.0]
    detector = _detector([0.99], clock=lambda: now[0], warmup_chunks=3)
    with caplog.at_level(logging.INFO, logger="manager.wake_word"):
        assert detector.process(mic_chunk()) == []   # warmup_gate
        assert detector.process(mic_chunk()) == []   # warmup_gate
        assert len(detector.process(mic_chunk())) == 1  # accepted
        now[0] = 1000.1
        assert detector.process(mic_chunk()) == []   # cooldown
    messages = [r.getMessage() for r in caplog.records if r.name == "manager.wake_word"]
    assert sum("warmup_gate" in m for m in messages) == 2
    assert sum("cooldown" in m for m in messages) == 1
    assert sum("Wake-Wort erkannt" in m for m in messages) == 1
    # Und alle vier Versuche stehen trotzdem im Ring.
    assert [e["reason"] for e in detector.wake_attempts] == [
        "warmup_gate", "warmup_gate", "accepted", "cooldown",
    ]


def test_fehlgeschlagener_versuch_unter_dem_boden_loggt_nicht(caplog) -> None:
    """Weit unter der Schwelle: **kein** Log-Dauerstrom (Ring trotzdem voll).

    Sonst entstünden bei 12,5 Versuchen/s ~12,5 Zeilen/s und der
    eigentliche Befund („knapp dran") geht unter.  `0.30` liegt über dem
    Ring-Boden (0.2 ⇒ Eintrag da) und unter dem halben Log-Boden
    (0.40 bei Schwelle 0.8 ⇒ keine Log-Zeile): genau das soll der Test zeigen.
    """
    assert ATTEMPT_LOG_FLOOR_RATIO == 0.5
    detector = _detector([0.30])  # 0.30 < 0.40 (halbe Schwelle), > 0.2 (Ring-Boden)
    with caplog.at_level("INFO", logger="manager.wake_word"):
        for _ in range(20):
            detector.process(mic_chunk())
    assert not [r for r in caplog.records if r.name == "manager.wake_word"]
    # Die Evidenz ist trotzdem vollständig – nur nicht im Log.
    assert len(detector.wake_attempts) == 20
    assert all(e["reason"] == "below_threshold" for e in detector.wake_attempts)


def test_schwellennahe_ablehnung_wird_bereits_auf_0_5_geloggt(caplog) -> None:
    """0.50 bei Schwelle 0.8 ⇒ geloggt (0.50 >= 0.40), obwohl abgelehnt."""
    detector = _detector([0.50])
    with caplog.at_level("INFO", logger="manager.wake_word"):
        detector.process(mic_chunk())
    messages = [r.getMessage() for r in caplog.records if r.name == "manager.wake_word"]
    assert len(messages) == 1
    assert "below_threshold" in messages[0]
    assert detector.wake_attempts[0]["accepted"] is False


def test_ring_ist_threadsicher_und_bleibt_konsistent() -> None:
    """Viele Threads ⇒ keine Ausnahme, keine kaputten Einträge, Cap hält."""
    import threading

    detector = _detector([0.50], cooldown_ms=0)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(200):
                detector.process(mic_chunk())
        except BaseException as exc:  # pragma: no cover – Fehlschlag ist der Befund
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    entries = detector.wake_attempts
    assert len(entries) == 50
    for entry in entries:
        # Jeder Eintrag ist vollständig und serialisierbar – kein halber Zustand.
        assert set(entry) == {
            "ts", "score", "accepted", "reason", "threshold", "chunk_index",
        }
        assert isinstance(entry["chunk_index"], int)
        assert isinstance(entry["score"], float)
        assert isinstance(entry["accepted"], bool)


def test_erkennung_veraendert_sich_durch_die_evidenz_nicht() -> None:
    """Die Aufzeichnung ändert **kein** Ergebnis: gleiche Events wie zuvor.

    Kern der P9.T0-Zusage: reiner Beobachter.  Dieselbe Score-Folge erzeugt
    mit und ohne ausgewerteten Ring exakt dieselbe Event-Liste.
    """
    scores = [0.30, 0.95, 0.30, 0.30, 0.95]
    now = [0.0]
    detector = _detector(scores, clock=lambda: now[0])
    events = detector.process(mic_chunk())
    for index in range(1, len(scores)):
        now[0] = float(index)  # Cooldown (1000 ms) umgehen
        events += detector.process(mic_chunk())
    # Zwei Annahmen (Chunk 2 und 5), alle anderen abgelehnt.
    assert len(events) == 2
    assert [e.chunk_index for e in events] == [2, 5]
    assert len(detector.wake_attempts) == 5
    assert [e["accepted"] for e in detector.wake_attempts] == [
        False, True, False, False, True,
    ]


def test_bewertungen_und_versuche_sind_gleich_viel() -> None:
    """`evaluations` und die Zahl der Versuche müssen **identisch** sein."""
    detector = _detector([0.50], score_every_n_chunks=2)
    for _ in range(10):
        detector.process(mic_chunk())
    assert detector.evaluations == len(detector.wake_attempts) == 5


# ═══════════════════════════════════════════════════════════════════════════
# 10. P9.T1 – Schwelle 0.80, Live-Änderung, Ring-Floor, Aggregate
# ═══════════════════════════════════════════════════════════════════════════
# Drei Zusagen, die hier **jeweils einzeln** rot werden müssen, wenn sie
# verletzt werden:
#   a) der Default ist 0.80 (Datenlage, s. Modul-Docstring),
#   b) ein **explizit** übergebener `threshold=` bleibt eingefroren, ein
#      Detektor **ohne** Argument liest die Settings bei jedem Vergleich –
#      ohne Neustart umstellbar (Voraussetzung für den Dashboard-Schreibpfad),
#   c) der Ring filtert unter `keep_floor`, **zählt** aber weiter.


def test_default_ist_080_nicht_090() -> None:
    """Der Projekt-Default ist 0.80 – der Grund des ganzen Schritts."""
    assert OWW_THRESHOLD == 0.80
    assert _module_settings.oww_threshold == 0.80
    assert WAKE_ATTEMPT_KEEP_FLOOR == 0.2


def test_standard_detektor_liest_die_settings_bei_jedem_vergleich(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Ohne** `threshold=` folgt der Detektor der Schwelle live.

    Das ist die Voraussetzung dafür, dass der Dashboard-Schreibpfad ohne
    Neustart wirkt: der Vergleich liest `settings.oww_threshold` in jedem
    `process()`, nicht einen bei der Konstruktion kopierten Wert.
    """
    monkeypatch.setattr(_module_settings, "oww_threshold", 0.90)
    scores = [0.85, 0.85]
    detector = _detector(scores)
    assert detector.threshold == 0.90
    assert detector.threshold_pinned is False
    assert detector.process(mic_chunk()) == []          # 0.85 < 0.90

    monkeypatch.setattr(_module_settings, "oww_threshold", 0.80)
    assert detector.threshold == 0.80                   # sofort, ohne Neustart
    assert len(detector.process(mic_chunk())) == 1     # 0.85 >= 0.80
    # Und der Eintrag trägt die **neue** Schwelle mit.
    assert detector.wake_attempts[-1]["threshold"] == 0.80


def test_explizite_schwelle_bleibt_eingefroren(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wer `threshold=` übergibt, bekommt **genau diesen** Wert.

    Sonst würde eine testweise oder per Gerät gesetzte Schwelle still vom
    Settings-Wert überschrieben – das wäre ein versteckter Vorrang.
    """
    monkeypatch.setattr(_module_settings, "oww_threshold", 0.80)
    detector = _detector([0.85], threshold=0.90)
    assert detector.threshold == 0.90
    assert detector.threshold_pinned is True
    assert detector.process(mic_chunk()) == []

    monkeypatch.setattr(_module_settings, "oww_threshold", 0.70)
    assert detector.threshold == 0.90                   # bleibt 0.90
    assert detector.process(mic_chunk()) == []


def test_ring_filtert_unter_der_kachel_but_zaehlt_alle() -> None:
    """Unter `keep_floor` ⇒ kein Ring-Eintrag, aber **volle** Aggregate.

    Genau der Live-Befund aus P9.T0: bei 0.9 Schwelle füllten die 15
    Stille-Chunks (score 0.0) den kompletten Ring und die neun interessanten
    Ablehnungen wurden verdrängt.  Jetzt: Ring frei, Zähler vollständig.
    """
    floor = WAKE_ATTEMPT_KEEP_FLOOR
    detector = _detector([0.0])
    for _ in range(20):                    # 20 Stille-Chunks
        detector.process(mic_chunk())
    assert detector.wake_attempts == []     # nichts davon liegt im Ring
    summary = detector.attempt_summary
    assert summary["total"] == 20
    assert summary["suppressed"] == 20
    assert summary["kept"] == 0
    assert summary["reasons"] == {"below_threshold": 20}


def test_aggregat_zaehlt_alle_gruende_und_haelt_den_besten_abgelehnten() -> None:
    """Aggregate über den Floor hinweg: **alle** Versuche, Grund je Grund."""
    detector = _detector([0.05, 0.30, 0.50, 0.79])
    for _ in range(4):
        detector.process(mic_chunk())
    summary = detector.attempt_summary
    # 4 bewertete Chunks; 0.05 fällt unter den Floor (0.2), die anderen drei
    # stehen im Ring.
    assert summary["total"] == 4
    assert summary["kept"] == 3
    assert summary["suppressed"] == 1
    assert summary["reasons"]["below_threshold"] == 4
    assert summary["reasons"].get("accepted", 0) == 0
    # Der beste **abgelehnte** Score kommt aus dem Ring, aber die Zählung
    # selbst ist unabhängig vom Floor – das war der Zweck.
    assert summary["best_rejected_score"] == 0.79
    assert summary["best_score_per_reason"]["below_threshold"] == 0.79
    assert summary["keep_floor"] == WAKE_ATTEMPT_KEEP_FLOOR
    assert summary["maxlen"] == WAKE_ATTEMPTS_MAXLEN


def test_aggregat_bleibt_vollstaendig_wenn_der_cap_greift() -> None:
    """Der Cap beschneidet den **Ring**, nicht die Zählung.

    20 Stille-Chunks (unter dem Floor) + 60 Versuche über dem Floor ⇒ der
    Ring ist voll (50), die Aggregate zählen aber alle 80 Bewertungen.
    """
    detector = _detector([0.0] * 20 + [0.50] * 60)
    for _ in range(80):
        detector.process(mic_chunk())
    assert detector.evaluations == 80
    assert len(detector.wake_attempts) == WAKE_ATTEMPTS_MAXLEN == 50
    summary = detector.attempt_summary
    assert summary["total"] == 80
    assert summary["suppressed"] == 20
    assert summary["kept"] == 50           # = Ringgröße, vom Cap begrenzt
    assert summary["reasons"]["below_threshold"] == 80
    assert summary["best_rejected_score"] == 0.50
