"""Wake-Word-Erkennung – `WakeWordDetector` (P2.T4).

**Reine L0-Logik mit genau einer bewussten Ausnahme:** der Aufruf des
openWakeWord-Modells (`OWWModel`).  Der ist so gekapselt, dass die gesamte
*Logik* – Chunk-Accumulator, Warm-up-Gate, Schwellen, Cooldown, Auswertetakt –
**ohne** Modell prüfbar ist: per injizierbarem `score_provider` (oder
`model_factory`) wird das Modell nie konstruiert.  **Kein Netz, keine I/O** –
`openwakeword` wird ausschließlich **lazy** im Ladepfad importiert.

Wiederverwendet statt dupliziert:

* **Chunk-Accumulator** = `app.audio_bridge.MicChunker` (P2.T2): sammelt
  beliebig gestückelte Mic-Payloads zu **2560-Byte**-Chunks
  (`settings.oww_chunk_bytes`) und liefert sie als **`int16`-Roharray**
  (`OWW_DTYPE`, **E46** – *nicht* `float32`, *nicht* normalisiert).  Der
  angebrochene Rest bleibt im Chunker erhalten.
* **Alle Zahlen** aus `app.config.settings`: `OWW_MODEL`, `OWW_THRESHOLD`,
  `OWW_BARGE_IN_THRESHOLD` (**E23**, 0.15 – *nicht* 0.6), `OWW_SPEEX_NS`,
  `OWW_CHUNK_BYTES`, `OWW_WARMUP_CHUNKS`, `OWW_COOLDOWN_MS`,
  `OWW_SCORE_EVERY_N_CHUNKS`, `WAKE_ATTEMPT_KEEP_FLOOR` (**E96**, 0.2).

**E96 (P9.T1) – zwei reine Beobachter-/Konfigurationsänderungen, keine
Erkennungslogik:** (1) `OWW_THRESHOLD` ist **0,80** (E96: datenbasierte
Ableitung aus der P9.T0-Messung) und wird im Vergleich **nicht** aus der
eingefrorenen Modulkonstante gelesen, sondern **beim Vergleich aus den
Settings** – sonst würde ein Wert, den das Dashboard zur Laufzeit ändert, erst
nach einem Neustart wirken (der eigentliche Zweck des Schreibmodus).  Ein
explizit übergebener `threshold=` bleibt unangetastet (so bleiben die L0-Tests
deterministisch).  (2) Der Messring behält **nur** Versuche mit
`score > WAKE_ATTEMPT_KEEP_FLOOR`; darunter liegende Versuche werden
**weiterhin gezählt** (`attempt_summary`), aber nicht mehr gespeichert – live
überschrieb 50× `below_threshold` mit score 0,0 den ganzen Ring, während genau
die interessanten Versuche (0,5–0,9) hinausrutschten.  **Kein** Eingriff in
Reihenfolge, Bedingungen oder Rückgabe der vier Entscheidungszweige.

Modell-Aufruf (wörtlich aus `PLAN.md:428`, mit **E38**):

    OWWModel([OWW_MODEL], enable_speex_noise_suppression=OWW_SPEEX_NS,
             inference_framework="onnx")

`inference_framework="onnx"` ist **explizit** gesetzt (**E38**): der
Paket-Default ist `tflite`, und ohne das Argument hinge die Framework-Wahl an
einem `ImportError` des nicht installierten `tflite_runtime` – also an einem
Zufall statt an einer Entscheidung.  `SessionOptions` wird **nicht** gesetzt:
openWakeWord setzt `intra_op_num_threads`/`inter_op_num_threads` selbst auf 1
(`openwakeword/model.py:149-151`); die einzige Stellschraube ist die ENV
`OMP_NUM_THREADS` (E38).

Warm-up-Gate (Referenz `em_oww_warmup.WarmupGate`, **autoritativ**):  Nach
`Model.reset()` – und schon beim Konstruieren – seedet openWakeWord das
Klassifikator-Fenster mit Embeddings aus **4 s Zufallsrauschen**.  Das Fenster
ist `[1, FEATURE_WINDOW=16, 96]`; erst wenn **16 echte** Chunks nachgefüttert
sind, enthält es keine Reset-Noise mehr.  Die Referenz verwirft die ersten
**15** Chunks und wertet den **16.** erstmals aus – die Formulierung „die ersten
`OWW_WARMUP_CHUNKS` Chunks verwerfen" beschreibt damit die **Fensterlänge**,
nicht die Zahl der verworfenen Scores (Festlegung **E48**, s. STATE §4).

Cooldown – **ab Erkennung**, nicht ab `reset()` (Festlegung, s. u.):  Die
Referenz kennt keinen 1-s-Cooldown; sie ruft nach jeder Erkennung
`model.reset()` + `warmup.reset()` und lässt das Warm-up-Gate (16 Chunks
≈ 1,28 s @ 12,5 Chunks/s) die Wiederauslösung unterdrücken.  EVA verankert
den Cooldown am **Erkennungszeitpunkt** (`Cooldown.arm()` beim Treffer) und
`reset()` **löscht** ihn zusätzlich: `reset()` deklariert eine bewusste
Stream-Diskontinuität, bei der das Warm-up-Gate die Unterdrückung ohnehin
übernimmt; ein veralteter Cooldown über einen Reset hinweg würde Audio
unterdrücken, das die Referenz auswerten würde.

`OWW_SCORE_EVERY_N_CHUNKS` – **n-ter Chunk**, nicht n-te Auswertung:  Gezählt
werden die akkumulierten Chunks (1-basiert); ausgewertet wird genau dann, wenn
`count % n == 0`.  Bei `n=1` (Default) wird jeder Chunk ausgewertet und das
Verhalten ist identisch zur Referenz.  Bei `n>1` sieht das Modell nur jeden
n-ten Chunk – die Auswertungen sinken um Faktor `n`, **und** das Fenster füllt
sich n× langsamer (Warm-up braucht `n·16` akkumulierte Chunks).  Das ist ein
bewusster CPU-/Latenz-Kompromiss (PLAN §9), kein versteckter Bug:  Das Gate
zählt nur die Chunks, die das **Modell gesehen hat** – exakt die Referenzregel
(„exactly once per chunk the MODEL saw").

**Thread-/Task-Safety (begründet):** `process()` läuft im asynchronen
Mikrofon-Pfad (WS-Reader/Turn-Logik), `set_speaking()` und `reset()` ruft die
Pipeline P5 aus einem anderen Task.  Alle zustandsbehafteten
Read-Modify-Write-Operationen (Chunker-Ausgabe, Warm-up-Zähler, Cooldown,
`_speaking`, Modell-Reset) sind deshalb per `threading.Lock` geschützt – wie in
`app/audio_bridge.py`, weil zwischen den Tasks ein `await` liegen kann und der
Aufrufer `process()` zudem in einen Executor legen darf.  **Nicht** geschützt:
das Objekt selbst nach außen (die entkoppelten `WakeEvent`s sind `frozen`), und
nichts am `ipc`-freien Charakter des Moduls.  Der Modellaufruf liegt unter dem
Lock – ein `predict()` von 80 ms Audio kostet ~2,7 ms (P1.T5), das ist als
kurze Sperre vertretbar und verhindert konkurrierende Zugriffe auf den
OpenWW-Feature-Puffer, der nicht threadsicher ist.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Final, Union

import numpy as np

from app.audio_bridge import MicChunker
from app.config import settings
from app.logger import get_logger

__all__ = [
    "WakeWordError",
    "WakeEvent",
    "WarmupGate",
    "Cooldown",
    "WakeWordDetector",
    "OWW_MODEL",
    "OWW_THRESHOLD",
    "OWW_BARGE_IN_THRESHOLD",
    "OWW_BARGE_IN_ENABLED",
    "OWW_SPEEX_NS",
    "OWW_CHUNK_BYTES",
    "OWW_WARMUP_CHUNKS",
    "OWW_COOLDOWN_MS",
    "OWW_SCORE_EVERY_N_CHUNKS",
    "FEATURE_WINDOW",
    "prediction_key",
    "WAKE_ATTEMPTS_MAXLEN",
    "ATTEMPT_REASON_ACCEPTED",
    "ATTEMPT_REASON_BELOW_THRESHOLD",
    "ATTEMPT_REASON_COOLDOWN",
    "ATTEMPT_REASON_WARMUP_GATE",
    "ATTEMPT_LOG_FLOOR_RATIO",
    "WAKE_ATTEMPT_KEEP_FLOOR",
]

_LOG: Final = get_logger("wake_word")

#: PCM akzeptiert `bytes`/`bytearray`/`memoryview` (identisch zu `app.audio_bridge`).
_BytesLike = Union[bytes, bytearray, memoryview]
#: Score-Provider: nimmt einen `int16`-Chunk (1280 Samples) und liefert einen Score.
ScoreProvider = Callable[[np.ndarray], float]
#: Modell-Factory: baut ein Objekt mit `.predict(np.ndarray)->Mapping` und `.reset()`.
ModelFactory = Callable[[], object]


class WakeWordError(ValueError):
    """Verletzung der Wake-Word-Verträge (definiert statt still zu degradieren)."""


def _utc_now_iso() -> str:
    """Aktuelle UTC-Zeit als ISO-8601 (Sekunden-Auflösung, `+00:00`).

    Bewusst **kein** Import aus `app.pipeline` (Pipeline importiert dieses
    Modul) – zwei Zeilen doppelte Logik sind hier billiger als ein Zyklus.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Konstanten (aus `app.config` wiederverwendet, nicht dupliziert) ────────
#: Wake-Modellname (`OWW_MODEL`, PLAN §4: `hey_jarvis_v0.1`).
OWW_MODEL: Final[str] = settings.oww_model
#: Auslöseschwelle im Ruhe-/Listen-Zustand (`OWW_THRESHOLD`, **E96** 0.80).
#: ⚠️ Das ist der Wert **beim Import** – der Detektor vergleicht **nicht** gegen
#: diese Konstante, sondern liest die Schwelle **bei jedem Vergleich** aus den
#: Settings (siehe `WakeWordDetector._idle_threshold`).  Nur so wirkt eine zur
#: Laufzeit geänderte Schwelle **ohne** Manager-Neustart; die Konstante bleibt
#: als Default des Konstruktors und als Fallback erhalten.
OWW_THRESHOLD: Final[float] = settings.oww_threshold
#: Barge-in-Schwelle im SPEAKING-Zustand (`OWW_BARGE_IN_THRESHOLD`, **E23** 0.15).
OWW_BARGE_IN_THRESHOLD: Final[float] = settings.oww_barge_in_threshold
#: Barge-in überhaupt aktiv? (`OWW_BARGE_IN_ENABLED`, Default True).
OWW_BARGE_IN_ENABLED: Final[bool] = settings.oww_barge_in_enabled
#: Speex-Noise-Suppression-Boolean (`OWW_SPEEX_NS`, E39: wirksam, kein Modell-Schalter).
OWW_SPEEX_NS: Final[bool] = settings.oww_speex_ns
#: Chunk-Größe (2560 B = 80 ms @ 16 kHz S16_LE mono, K2/E28).
OWW_CHUNK_BYTES: Final[int] = settings.oww_chunk_bytes
#: Warm-up-Fenster in Chunks (`OWW_WARMUP_CHUNKS`, 16).
OWW_WARMUP_CHUNKS: Final[int] = settings.oww_warmup_chunks
#: Cooldown nach einer Erkennung (`OWW_COOLDOWN_MS`, 1000 ms).
OWW_COOLDOWN_MS: Final[int] = settings.oww_cooldown_ms
#: Nur jeden n-ten Chunk auswerten (`OWW_SCORE_EVERY_N_CHUNKS`, Default 1).
OWW_SCORE_EVERY_N_CHUNKS: Final[int] = settings.oww_score_every_n_chunks
#: Embeddings pro Vorhersage / Länge des Reset-Noise-Fensters (Referenz
#: `em_oww_warmup.FEATURE_WINDOW`).  Der Default des Gates ist der Config-Wert
#: `OWW_WARMUP_CHUNKS`; beide sind bei der verifizierten Konfiguration 16.
FEATURE_WINDOW: Final[int] = 16

# ── Wake-Versuche (P9.T0 – Evidenz, reiner Beobachter) ────────────────────
#: **Harter Cap** des Ringpuffers `WakeWordDetector.wake_attempts`.  Jeder
#: **bewertete** Chunk ist genau ein Versuch, deshalb entspricht die
#: Kapazität bei `OWW_SCORE_EVERY_N_CHUNKS=1` und 80-ms-Chunks
#: 50 × 80 ms ≈ **4 s** Ringfenster.  Das ist bewusst kurz: der Ring ist der
#: Beweis für den *aktuellen* Zustand, das `INFO`-Log der Langzeitbeleg.
WAKE_ATTEMPTS_MAXLEN: Final[int] = 50
#: Gründe – **wörtlich** die vier Entscheidungszweige in `process()`.  Kein
#: erfundener Grund, und keiner, der dort nicht wirklich stehen kann:
ATTEMPT_REASON_ACCEPTED: Final[str] = "accepted"
ATTEMPT_REASON_BELOW_THRESHOLD: Final[str] = "below_threshold"
ATTEMPT_REASON_COOLDOWN: Final[str] = "cooldown"
ATTEMPT_REASON_WARMUP_GATE: Final[str] = "warmup_gate"
#: **Beobachtbarkeits-Boden** für die `INFO`-Zeile bei `below_threshold`.
#: Hintergrund: Der Mic-Stream bewertet dauerhaft ~12,5 Chunks/s.  Jede
#: Unterschreitung zu loggen wäre ein Dauerstrom von ~12,5 Zeilen/s und damit
#: ein **Log-Spam**, der die eigentliche Beobachtung (Wake/kein Wake, Abstand
#: zur Schwelle) unlesbar macht.  Deshalb gilt die Vorgabe „jede Ablehnung
#: sichtbar" so: **jede** Ablehnung steht im Ringpuffer, und auf `INFO` wird
#: jede strukturelle Ablehnung (Warm-up, Cooldown) **sowie** jede
#: schwellennahe Unterschreitung geloggt.  Der Boden ist **kein** Filter der
#: Messung: `score`/`threshold`/`accepted` werden **immer** aufgezeichnet,
#: unabhängig von dieser Kennzahl.  0.5 ⇒ geloggt wird ab der halben
#: effektiven Schwelle (bei **E96** 0.80 also ab 0.40); der Wert liegt bewusst
#: **unter** der Barge-in-Schwelle (0.15 → 0.075), damit Barge-in-Phasen
#: nicht völlig stumm werden.
ATTEMPT_LOG_FLOOR_RATIO: Final[float] = 0.5
#: **E96 (P9.T1) – Speicher-Boden des Messrings** (`WAKE_ATTEMPT_KEEP_FLOOR`).
#: Der Ring ist **nur** das Diagnosefenster der letzten N Versuche; im
#: Live-Betrieb (P9.T0) füllten ihn 50× `below_threshold` mit **score 0,0**
#: (Stille-Chunks bei ~12,5/s) in vier Sekunden – die Karte war damit
#: unbrauchbar und die interessanten Versuche (0,5–0,9) waren längst
#: überschrieben.  Versuche **unter** diesem Boden werden deshalb **nicht
#: gespeichert**, aber **weiterhin gezählt** (`attempt_summary`: `total`,
#: `suppressed`, `reasons` je Grund).  Damit bleibt die Aussage „alle
#: Ablehnungsgründe sind vollständig gezählt" erhalten, während der Ring die
#: *messbaren* Versuche zeigt.  **Rein beobachtend** – Score, Schwelle und
#: Entscheidung werden unverändert berechnet.
#:
#: Warum **0,2** und nicht etwa 0,45 (= `ATTEMPT_LOG_FLOOR_RATIO · 0.8`)?  Der
#: Ring soll **auch** unterhalb der Log-Schwelle sichtbar bleiben (sonst wären
#: die Scores 0,25–0,40, also der untere Rand des Verlaufs, nur im Log zu
#: sehen) und **oberhalb** des Barge-in-Niveaus (0.15), damit der Ring
#: Barge-in-Phasen nicht still wird.  0,2 liegt außerdem weit unter dem
#: ungünstigsten *echten* Sprachscore der P9.T0-Messung (0,526) – ein
#: Fehlversuch wird also **nie** durch diesen Boden verschluckt.
WAKE_ATTEMPT_KEEP_FLOOR: Final[float] = float(settings.wake_attempt_keep_floor)
#: Präzision der abgelegten Scores/Schwellen (3 Nachkommastellen).
_ATTEMPT_ROUND: Final[int] = 3

_MODEL_FALLBACK: Final[str] = "hey_jarvis_v0.1"


class WarmupGate:
    """Verfolgt, ob das Klassifikator-Fenster noch Reset-Noise enthält.

    **1:1 die Referenzsemantik** (`em_oww_warmup.WarmupGate`, autoritativ):
    `reset()` bei jedem `Model.reset()`, `feed()` genau einmal pro Chunk, den
    das **Modell gesehen hat**.  `feed()` gibt zurück, ob der Score dieses
    Chunks vertrauenswürdig ist; es **muss** für jeden gesehenen Chunk gerufen
    werden, sonst füllt sich das Fenster nie.  Der Zähler sättigt bei `window`
    (kein Überlauf bei Dauerbetrieb).

    Ein **neues** Gate startet ungewärmt – wie ein neues Modell, dessen
    `AudioFeatures.__init__` denselben Rausch-Puffer seedet.  Deshalb braucht
    die Konstruktion von Modell und Gate keine Abstimmung.
    """

    def __init__(self, window: int = OWW_WARMUP_CHUNKS) -> None:
        if window < 0:
            raise WakeWordError("window darf nicht negativ sein")
        self._window = int(window)
        self._fed = 0

    def reset(self) -> None:
        """Merken, dass die Modellpuffer gerade mit Noise geseedet wurden."""
        self._fed = 0

    def feed(self) -> bool:
        """Einen gesehenen Chunk zählen.  True = dessen Score ist vertrauenswürdig."""
        if self._fed < self._window:
            self._fed += 1
        return self._fed >= self._window

    @property
    def ready(self) -> bool:
        """True, sobald Scores vertrauenswürdig sind (ohne zu feeden)."""
        return self._fed >= self._window

    @property
    def fed(self) -> int:
        """Gesehene Chunks seit dem letzten Reset, bei `window` gesättigt."""
        return self._fed

    @property
    def window(self) -> int:
        """Fenstergröße in Chunks."""
        return self._window

    def progress(self) -> str:
        """`N/16` für eine Log-Zeile bei unterdrückter Erkennung."""
        return f"{self._fed}/{self._window}"


class Cooldown:
    """Zeitbasierter Cooldown, **verankert am Auslösezeitpunkt**.

    Die Uhr ist injizierbar (`clock`, Default `time.monotonic`), damit die
    Logik ohne Schlafen/Sekunden testbar ist.  `arm()` startet den Cooldown
    (Aufruf beim Treffer), `active()` sagt, ob er noch läuft, `clear()` löscht
    ihn (Aufruf aus `reset()`).
    """

    def __init__(
        self,
        cooldown_ms: int = OWW_COOLDOWN_MS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cooldown_ms < 0:
            raise WakeWordError(f"cooldown_ms muss >= 0 sein, ist {cooldown_ms}")
        self._cooldown_s = cooldown_ms / 1000.0
        self._clock = clock
        self._armed_at: float | None = None

    def arm(self, now: float | None = None) -> None:
        """Cooldown starten (Zeitpunkt = `now` oder Uhr)."""
        self._armed_at = self._clock() if now is None else now

    def clear(self) -> None:
        """Cooldown löschen."""
        self._armed_at = None

    def active(self, now: float | None = None) -> bool:
        """True, solange eine erneute Auslösung unterdrückt wird."""
        if self._armed_at is None or self._cooldown_s <= 0.0:
            return False
        t = self._clock() if now is None else now
        return (t - self._armed_at) < self._cooldown_s

    def remaining(self, now: float | None = None) -> float:
        """Restsekunden des Cooldowns (0.0, wenn nicht aktiv)."""
        if self._armed_at is None:
            return 0.0
        t = self._clock() if now is None else now
        return max(0.0, self._cooldown_s - (t - self._armed_at))

    @property
    def armed(self) -> bool:
        """True, sobald mindestens einmal scharfgeschaltet wurde."""
        return self._armed_at is not None

    @property
    def cooldown_seconds(self) -> float:
        """Cooldown-Dauer in Sekunden."""
        return self._cooldown_s


@dataclass(frozen=True)
class WakeEvent:
    """Eine erkannte Auslösung – `frozen`, damit sie gefahrlos weitergereicht wird."""

    score: float
    threshold: float
    barge_in: bool
    chunk_index: int


def prediction_key(model_name: str) -> str:
    """Schlüssel, unter dem openWakeWord den Score liefert (Referenz
    `em_oww_models.prediction_key`):  Stock-Namen keyen als sie selbst, ein
    Dateipfad dagegen als Dateiname-Stem.
    """
    if model_name.endswith(".onnx"):
        from pathlib import Path

        return Path(model_name).stem
    return model_name


class WakeWordDetector:
    """Manager-seitige Wake-Word-Erkennung (L0, E5).

    Eingehende Mic-Payloads beliebiger Stückelung gehen durch den
    `MicChunker`; jeder volle 2560-B-Chunk wird (je nach
    `OWW_SCORE_EVERY_N_CHUNKS`) ausgewertet, durch das `WarmupGate` gefiltert,
    gegen die zustandsabhängige Schwelle geprüft und durch den `Cooldown`
    entschärft.  Das Modell wird **lazy** geladen (oder per Injektion
    ersetzt); ohne Modell läuft die gesamte Logik.

    Parameter (alle Defaults aus `app.config`):

    * `score_provider` – `Callable[[np.ndarray], float]`.  Ist er gesetzt, wird
      **nie** ein Modell konstruiert (Testbarkeit ohne Modell).
    * `model_factory` – `Callable[[], obj]`, `obj` mit `.predict()`/`.reset()`.
      Nur genutzt, wenn kein `score_provider` gesetzt ist.
    * `clock` – monoton steigende Uhr für den Cooldown.
    * `model`, `threshold`, `barge_in_threshold`, `barge_in_enabled`,
      `speex_ns`, `chunk_bytes`, `warmup_chunks`, `cooldown_ms`,
      `score_every_n_chunks`, `keep_floor` – übersteuerbare Werte
      (Default = Config).  **`threshold=None`** (der Default) heißt: **kein**
      Fixwert – der Detektor liest die Schwelle bei jedem Vergleich aus den
      Settings (`_idle_threshold`), damit eine zur Laufzeit geänderte Schwelle
      **ohne** Neustart wirkt (E96).  Ein **explizites** `threshold=` friert den
      Wert ein (L0-Tests bleiben deterministisch).
    """

    def __init__(
        self,
        *,
        score_provider: ScoreProvider | None = None,
        model_factory: ModelFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        model: str = OWW_MODEL,
        threshold: float | None = None,
        barge_in_threshold: float = OWW_BARGE_IN_THRESHOLD,
        barge_in_enabled: bool = OWW_BARGE_IN_ENABLED,
        speex_ns: bool = OWW_SPEEX_NS,
        chunk_bytes: int = OWW_CHUNK_BYTES,
        warmup_chunks: int = OWW_WARMUP_CHUNKS,
        cooldown_ms: int = OWW_COOLDOWN_MS,
        score_every_n_chunks: int = OWW_SCORE_EVERY_N_CHUNKS,
        keep_floor: float = WAKE_ATTEMPT_KEEP_FLOOR,
    ) -> None:
        if score_every_n_chunks < 1:
            raise WakeWordError(
                f"score_every_n_chunks muss >= 1 sein, ist {score_every_n_chunks}"
            )
        self._model_name = model
        #: `None` ⇒ Schwelle **live** aus den Settings (E96); sonst Fixwert.
        self._threshold_override: float | None = (
            None if threshold is None else float(threshold)
        )
        self._barge_in_threshold = float(barge_in_threshold)
        self._barge_in_enabled = bool(barge_in_enabled)
        self._speex_ns = bool(speex_ns)
        self._score_every_n_chunks = int(score_every_n_chunks)
        self._keep_floor = float(keep_floor)

        self._score_provider = score_provider
        self._model_factory = model_factory
        self._model: object | None = None
        self._prediction_key = prediction_key(model)

        self._chunker = MicChunker(chunk_bytes)
        self._warmup = WarmupGate(warmup_chunks)
        self._cooldown = Cooldown(cooldown_ms, clock=clock)

        self._speaking = False
        self._chunks_accumulated = 0
        self._evaluations = 0
        self._last_score: float | None = None
        self._lock = threading.Lock()
        #: P9.T0: die letzten N **bewerteten** Chunks mit Score und
        #: Entscheidungsgrund.  `deque(maxlen=…)` = harter Cap, nur RAM, keine
        #: Persistenz (identisch zur Turn-Historie, E94 (b)).  Wird **nur**
        #: unter `self._lock` geschrieben (`process()` bzw. `_record_attempt`).
        self._attempts: Deque[dict[str, Any]] = deque(maxlen=WAKE_ATTEMPTS_MAXLEN)
        #: E96: **alle** Versuche werden gezählt, auch die, die der Ring wegen
        #: `keep_floor` nicht speichert.  Ohne diese Zähler wäre „nur RAM und
        #: Floor" ein stiller Datenverlust – die Aggregate beweisen das Gegenteil.
        self._attempt_total = 0
        self._attempt_reasons: dict[str, int] = {}
        self._attempt_suppressed = 0
        self._best_rejected_score: float | None = None
        self._best_score_per_reason: dict[str, float] = {}

    # ── Modell-Ladepfad (die eine bewusste Ausnahme) ──────────────────────
    @property
    def model_loaded(self) -> bool:
        """True, sobald ein Modell (Default oder injiziert) konstruiert wurde."""
        return self._model is not None

    def _build_model(self) -> object:
        """Baut das openWakeWord-Modell – **lazy**, mit explizitem Framework (E38)."""
        from openwakeword.model import Model as OWWModel

        _LOG.info(
            "Lade OWW-Modell %s (speex_ns=%s, inference_framework=onnx)",
            self._model_name,
            self._speex_ns,
        )
        return OWWModel(
            [self._model_name],
            enable_speex_noise_suppression=self._speex_ns,
            inference_framework="onnx",
        )

    def _ensure_model(self) -> object:
        """Modell bei Bedarf (einmalig) konstruieren."""
        if self._model is None:
            factory = self._model_factory or self._build_model
            self._model = factory()
        return self._model

    def _score(self, samples: np.ndarray) -> float:
        """Chunk bewerten – über den injizierten Provider oder das Modell."""
        if self._score_provider is not None:
            return float(self._score_provider(samples))
        model = self._ensure_model()
        prediction = model.predict(samples)  # type: ignore[attr-defined]
        return float(prediction.get(self._prediction_key, 0.0))

    # ── Zustand der Pipeline (P5 kennt SPEAKING) ──────────────────────────
    def set_speaking(self, speaking: bool) -> None:
        """SPEAKING-Zustand setzen – steuert die Barge-in-Schwelle."""
        with self._lock:
            self._speaking = bool(speaking)

    @property
    def speaking(self) -> bool:
        """Aktueller SPEAKING-Zustand."""
        with self._lock:
            return self._speaking

    @property
    def threshold(self) -> float:
        """Aktuell wirksame Schwelle (Barge-in im SPEAKING-Zustand)."""
        with self._lock:
            return self._effective_threshold()

    def _effective_threshold(self) -> float:
        if self._speaking and self._barge_in_enabled:
            return self._barge_in_threshold
        return self._idle_threshold()

    def _idle_threshold(self) -> float:
        """Die **Ruhe-/Listen**-Schwelle – **live** aus den Settings (E96).

        Ohne expliziten `threshold=` wird `app.config.settings.oww_threshold`
        **bei jedem Vergleich** gelesen und nicht die Modulkonstante
        `OWW_THRESHOLD` (die wäre beim Import eingefroren).  Genau das macht
        den Zweck des Dashboard-Schreibmodus aus: die Schwelle ist zur Laufzeit
        nachjustierbar, **ohne** Manager-Neustart.

        Fällt der Wert aus (fehlendes Attribut, unbrauchbarer Typ), gilt
        `OWW_THRESHOLD` – der Wert beim Import.  **Kein** Exception-Pfad im
        heißen Mic-Pfad: `_idle_threshold` wird unter `self._lock` in
        `process()` aufgerufen und darf dort nichts werfen.
        """
        if self._threshold_override is not None:
            return self._threshold_override
        value = getattr(settings, "oww_threshold", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return OWW_THRESHOLD
        return float(value)

    @property
    def threshold_pinned(self) -> bool:
        """True, wenn die Schwelle per Konstruktor **eingefroren** wurde."""
        return self._threshold_override is not None

    @property
    def keep_floor(self) -> float:
        """Unterhalb dieses Scores landet ein Versuch **nicht** im Ring (E96)."""
        with self._lock:
            return self._keep_floor

    @property
    def barge_in(self) -> bool:
        """True, wenn gerade die Barge-in-Schwelle gilt."""
        with self._lock:
            return self._speaking and self._barge_in_enabled

    # ── Verarbeitung ──────────────────────────────────────────────────────
    def _record_attempt(
        self, score: float, accepted: bool, reason: str, threshold: float
    ) -> None:
        """**Einen** bewerteten Chunk als Versuch festhalten (P9.T0/E96).

        **Nur** unter `self._lock` aufrufen (`process()` hält ihn bereits).
        Kein ``await``/I/O in diesem Pfad, keine Abhängigkeit vom
        Detektor-Zustand: der Aufruf darf die Entscheidung **nicht** ändern,
        sondern nur bezeugen.

        **E96:** Zuerst wird **jeder** Versuch gezählt (`total`, Grund, bester
        abgelehnter Score, bester Score je Grund).  *Gespeichert* wird er nur,
        wenn `score > keep_floor`; darunter zählt `suppressed`.  Damit ist die
        Zählung vollständig, auch wenn der Ring die Stille nicht zeigt – der
        Ring ist Diagnosefenster, die Zähler sind der Beleg.

        Ist der Ring voll, wird der **älteste** Eintrag an Ort und Stelle
        überschrieben (`rotate(-1)`), statt ein neues `dict` zu allozieren –
        ein verketteter Knoten pro Versuch wäre bei 12,5 Versuchen/s genau
        die stille Lecke, die P9.T0 nicht braucht.  Danach ist der Eintrag der
        **neueste** (deque-Invariant: `append`/`rotate` am rechten Ende).
        """
        value = float(score)
        self._attempt_total += 1
        self._attempt_reasons[reason] = self._attempt_reasons.get(reason, 0) + 1
        previous_best = self._best_score_per_reason.get(reason)
        if previous_best is None or value > previous_best:
            self._best_score_per_reason[reason] = value
        if not accepted:
            if self._best_rejected_score is None or value > self._best_rejected_score:
                self._best_rejected_score = value
        if value <= self._keep_floor:
            self._attempt_suppressed += 1
            return
        buffer = self._attempts
        limit = buffer.maxlen
        if limit and len(buffer) >= limit:
            entry: dict[str, Any] = buffer[0]
            buffer.rotate(-1)
        else:
            entry = {}
            buffer.append(entry)
        entry["ts"] = _utc_now_iso()
        entry["score"] = round(value, _ATTEMPT_ROUND)
        entry["accepted"] = bool(accepted)
        entry["reason"] = reason
        entry["threshold"] = round(float(threshold), _ATTEMPT_ROUND)
        entry["chunk_index"] = self._chunks_accumulated

    def process(self, pcm: _BytesLike) -> list[WakeEvent]:
        """Mic-Payload verarbeiten; erkannte Auslösungen zurückgeben (0..n).

        Der `MicChunker` puffert angebrochene Reste; bewertet wird nur jeder
        `score_every_n_chunks`-te volle Chunk (Default jeder).  Das Warm-up-Gate
        wird genau einmal pro **bewertetem** Chunk gefüttert (das ist die Menge,
        die das Modell gesehen hat).  Ein Treffer schaltet den Cooldown scharf.

        **P9.T0 (reiner Beobachter, kein Eingriff):** Jeder bewertete Chunk wird
        zusätzlich in `wake_attempts` festgehalten – mit `score`, `accepted`,
        `reason` (`accepted`/`warmup_gate`/`cooldown`/`below_threshold`) und der
        **tatsächlich wirksamen** Schwelle.  Die Reihenfolge der Zweige, ihre
        Bedingungen und die Rückgabe bleiben **unverändert**.  Ein per
        `score_every_n_chunks` **übersprungener** Chunk wird *nicht* bewertet
        und ist deshalb **kein** Versuch – er hätte keinen Score.
        Ablehnungen erscheinen auf `INFO` (Boden: `ATTEMPT_LOG_FLOOR_RATIO`,
        siehe dort für die Begründung), Annahmen auf der **bestehenden** Zeile.

        **E96:** (a) verglichen wird gegen `_idle_threshold()` – die Schwelle
        kommt **live** aus den Settings, nicht aus der eingefrorenen
        Modulkonstante; (b) gespeichert wird ein Versuch nur, wenn
        `score > keep_floor` – **gezählt** wird jeder (siehe `_record_attempt`).
        Die **Entscheidung** ändert sich dadurch nicht: der Floor greift erst
        **nach** der Entscheidung, ausschließlich im Beobachter.
        """
        chunks = self._chunker.feed(pcm)
        events: list[WakeEvent] = []
        with self._lock:
            for samples in chunks:
                self._chunks_accumulated += 1
                if self._chunks_accumulated % self._score_every_n_chunks != 0:
                    continue
                score = self._score(samples)
                self._evaluations += 1
                self._last_score = score
                trusted = self._warmup.feed()
                if not trusted:
                    # P9.T0: der Grund ist der Gate-Zweig, **nicht**
                    # `below_threshold` – auch dann nicht, wenn der Score über
                    # der Schwelle lag (genau das war bisher schwer zu sehen).
                    self._record_attempt(
                        score,
                        False,
                        ATTEMPT_REASON_WARMUP_GATE,
                        self._effective_threshold(),
                    )
                    _LOG.info(
                        "Wake-Versuch abgelehnt: score=%.3f Grund=%s Schwelle=%.3f"
                        " (Warm-up, %s Chunks seit Reset)",
                        score,
                        ATTEMPT_REASON_WARMUP_GATE,
                        self._effective_threshold(),
                        self._warmup.progress(),
                    )
                    continue
                if self._cooldown.active():
                    # P9.T0: Ablehnung durch den Cooldown war bisher
                    # **unsichtbar** (es stand keine Zeile dort) – jetzt mit
                    # Score und Grund.
                    self._record_attempt(
                        score,
                        False,
                        ATTEMPT_REASON_COOLDOWN,
                        self._effective_threshold(),
                    )
                    _LOG.info(
                        "Wake-Versuch abgelehnt: score=%.3f Grund=%s Schwelle=%.3f"
                        " (Cooldown, Restzeit=%.3fs)",
                        score,
                        ATTEMPT_REASON_COOLDOWN,
                        self._effective_threshold(),
                        self._cooldown.remaining(),
                    )
                    continue
                threshold = self._effective_threshold()
                if score >= threshold:
                    self._cooldown.arm()
                    self._record_attempt(
                        score, True, ATTEMPT_REASON_ACCEPTED, threshold
                    )
                    events.append(
                        WakeEvent(
                            score=score,
                            threshold=threshold,
                            barge_in=self._speaking and self._barge_in_enabled,
                            chunk_index=self._chunks_accumulated,
                        )
                    )
                    _LOG.info(
                        "Wake-Wort erkannt (score=%.3f >= %.3f, barge_in=%s)",
                        score,
                        threshold,
                        self._speaking and self._barge_in_enabled,
                    )
                else:
                    # P9.T0: der häufigste Fall.  Immer **aufgezeichnet**, auf
                    # `INFO` nur oberhalb des Beobachtbarkeits-Bodens – sonst
                    # ~12,5 Zeilen/s Dauerstrom (Begründung dort).
                    self._record_attempt(
                        score, False, ATTEMPT_REASON_BELOW_THRESHOLD, threshold
                    )
                    if score >= ATTEMPT_LOG_FLOOR_RATIO * threshold:
                        _LOG.info(
                            "Wake-Versuch abgelehnt: score=%.3f Grund=%s"
                            " Schwelle=%.3f (Abstand=%.3f)",
                            score,
                            ATTEMPT_REASON_BELOW_THRESHOLD,
                            threshold,
                            threshold - score,
                        )
        return events

    def reset(self) -> None:
        """Accumulator, Warm-up-Gate und Cooldown zurücksetzen; Modell resetten.

        Anbindung an die Pipeline (P5):  nach einem Turn / einer
        Stream-Diskontinuität.  Ein neues Modell-Fenster ist noise-seeded, also
        startet das Warm-up-Gate ungewärmt.  Der Cooldown wird **gelöscht** –
        die Unterdrückung übernimmt danach das Warm-up-Gate (Begründung im
        Modul-Docstring).
        """
        with self._lock:
            self._chunker.reset()
            self._warmup.reset()
            self._cooldown.clear()
            self._chunks_accumulated = 0
            if self._model is not None:
                self._model.reset()  # type: ignore[attr-defined]

    # ── Diagnose (für Tests/Smoke) ────────────────────────────────────────
    @property
    def warmup(self) -> WarmupGate:
        """Das Warm-up-Gate (Read-only-Zugriff für Tests/Diagnose)."""
        return self._warmup

    @property
    def cooldown(self) -> Cooldown:
        """Der Cooldown (Read-only-Zugriff für Tests/Diagnose)."""
        return self._cooldown

    @property
    def chunks_accumulated(self) -> int:
        """Seit dem letzten Reset akkumulierte volle Chunks."""
        with self._lock:
            return self._chunks_accumulated

    @property
    def evaluations(self) -> int:
        """Zahl der Modell-/Provider-Auswertungen (Diagnose)."""
        with self._lock:
            return self._evaluations

    @property
    def last_score(self) -> float | None:
        """Zuletzt ermittelter Score (Diagnose/Smoke), `None` vor der ersten Auswertung."""
        with self._lock:
            return self._last_score

    @property
    def wake_attempts(self) -> list[dict[str, Any]]:
        """Die letzten Wake-Versuche als **Kopie** (P9.T0, `/api/wake`).

        Ältester Eintrag zuerst, höchstens `WAKE_ATTEMPTS_MAXLEN`.  Jeder Eintrag
        ist ein **eigenes** `dict` (flache Kopie) – der Aufrufer kann den Ring
        nicht verändern und keine Referenz auf die internen Objekte behalten.
        Der Zugriff läuft unter demselben `Lock` wie `process()`; bei
        gleichzeitigem Schreiben wartet er höchstens einen Chunk
        (Dauer einer Modell-Auswertung), es wird **nichts** blockiert und
        nichts angehalten, solange der Aufrufer nur liest.

        **E96:** enthalten sind nur Versuche mit `score > keep_floor` (Stille
        und sehr tiefe Scores werden nicht gespeichert) – die **vollständige**
        Zählung steht in :attr:`attempt_summary`.
        """
        with self._lock:
            return [dict(entry) for entry in self._attempts]

    @property
    def attempt_summary(self) -> dict[str, Any]:
        """Aggregierte Kennzahlen über **alle** Versuche (E96, Detektor-Diagnose).

        Der Ring ist ein Fenster; diese Kennzahlen sind der **vollständige**
        Beleg, weil sie auch die Versuche zählen, die der Ring wegen
        `keep_floor` nicht speichert:

        * `total` – **alle** bewerteten Chunks (jeder ist genau ein Versuch),
        * `kept` – Einträge, die im Ring stehen,
        * `suppressed` – Versuche **unter/gleich** `keep_floor` (nicht
          gespeichert, aber gezählt),
        * `reasons` – Anzahl je Entscheidungsgrund, über **alle** Versuche,
        * `best_rejected_score` – der höchste abgelehnte Score („so knapp war
          es"), `None`, wenn nichts abgelehnt wurde,
        * `best_score_per_reason` – der höchste Score je Grund,
        * `keep_floor`/`maxlen` – die beiden Filter, damit die Zahlen im
          Dashboard lesbar bleiben.

        Die Werte sind **Kopien**; die Dicts werden unter demselben `Lock`
        gelesen wie `process()` ihn hält.

        **Bewusste Grenze (E96):** dieses Dict ist **Detektor-Diagnose**, kein
        API-Vertrag. `/api/wake` liefert die **Ring-Fensterstatistik**
        (`keep_floor`, `default_threshold`, `threshold_source`, Fenster-Mittel)
        und nicht diese Zähler – sie über einen Pipeline-Lesezugriff
        (`wake_attempt_summary()`) zu veröffentlichen, war in P9.T1 **nicht**
        Teil des Auftrags (`app/pipeline.py` bleibt unangetastet). Wer sie
        später im Dashboard sehen will, braucht diesen einen Lesezugriff plus
        je ein Feld in `/api/wake` – der Zählerstand selbst ist bereits
        korrekt und getestet.
        """
        with self._lock:
            return {
                "total": self._attempt_total,
                "kept": len(self._attempts),
                "suppressed": self._attempt_suppressed,
                "reasons": dict(self._attempt_reasons),
                "best_rejected_score": (
                    None
                    if self._best_rejected_score is None
                    else round(self._best_rejected_score, _ATTEMPT_ROUND)
                ),
                "best_score_per_reason": {
                    reason: round(score, _ATTEMPT_ROUND)
                    for reason, score in self._best_score_per_reason.items()
                },
                "keep_floor": round(self._keep_floor, _ATTEMPT_ROUND),
                "maxlen": WAKE_ATTEMPTS_MAXLEN,
            }

    @property
    def pending_bytes(self) -> int:
        """Angebrochener Rest im Chunk-Accumulator."""
        return self._chunker.pending_bytes

    @property
    def model_name(self) -> str:
        """Verwendeter Modellname."""
        return self._model_name

    @property
    def speex_ns(self) -> bool:
        """Speex-NS-Boolean, mit dem das Modell gebaut wird."""
        return self._speex_ns
