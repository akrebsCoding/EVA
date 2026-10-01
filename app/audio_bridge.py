"""Audio-Bridge – reine L0-Logik für Speaker- und Mic-Pfad (P2.T2).

**Kein Netz, keine I/O**, kein `websockets`/`httpx`/`zeroconf`.  Erlaubt sind
ausschließlich stdlib, `numpy`, `soxr`, `app.config` und `app.protocol`;
Längen und Raten werden dort **wiederverwendet**, nicht neu erfunden.

Drei Aufgaben, klar getrennt:

* **`SpeakerResampler`** – Piper liefert seinen TTS-Strom in der **Voice-Rate**
  (verifiziert: `de_DE-thorsten-high` = **22050 Hz**, P0.T3, STATE §3), das
  Gerät will **48000 Hz S16_LE mono** (Spec §7, `em_controller.py:177-180`).
  Die Rate ist **nicht** hart verdrahtet: sie kommt aus dem Wyoming-
  `AudioStart`-Event (`rate`-Feld) und wird zur Laufzeit per
  `set_input_rate()` gesetzt; Piper kann für andere Stimmen 16000 Hz liefern
  (`*-low`/`*-x_low`, STATE §3/P0.T3).  SoXR `ResampleStream` mit
  `dtype="int16"` (Verhältnisse bleiben ganzzahlig, kein Float-Rundungsverlust).
* **`SpeakerChunker` / `chunk_for_speaker()`** – zerlegt den resampelten Strom
  in **4096-Byte**-Perioden (≈ 42,7 ms @ 48 kHz, `SPEAKER_BYTES`,
  Spec §7.2).  Restbytes bleiben **erhalten** und werden beim Flush
  unpadding zurückgegeben – das Nullen des letzten Frames macht
  `app.protocol.build_speaker_frame(pad=True)`, nicht dieser Code.
* **`AudioRingBuffer`** – begrenzt die gepufferte **Mic**-Audio-Menge
  (Drop-**oldest**, wie das Pacing der Referenz: die Geräte-Queue wirft den
  **ältesten** Frame, nie den neuesten – Spec §7.1/`em_controller.py:4062-4076`).
  Die Max-Dauer ist aus `app.config` **abgeleitet**: die längste mögliche
  Turn-Dauer ist der Hard-Cap `turn_hard_cap_seconds` (K5-Sicherheitsnetz,
  15,0 s), mehr Mikrofon-Audio kann ein Turn niemals brauchen.
* **`MicChunker`** – sammelt eingehende Mic-Payloads zu **2560-Byte**-Chunks
  (80 ms @ 16 kHz, `CHUNK_BYTES`) und liefert sie in der Form, die
  openWakeWord erwartet.

**OWW-Eingangsform – verifiziert, NICHT geraten:** `int16`, **roh**, **nicht**
normalisiert und **nicht** `float32`.  Belegstellen im Referenz-Quelltext:
`em_controller.py:1377` und `:2594` lesen den 2560-B-Chunk mit
``np.frombuffer(frame, dtype=np.int16)`` und reichen das Array **direkt** an
``model.predict(samples)`` weiter (`:1381`, `:2621`).  `em_oww_warmup.py:14`
seedet den Reset-Puffer ebenfalls mit ``np.random.randint(-1000, 1000, …).astype(np.int16)``.
Das passt zur openWakeWord-Implementierung selbst: `AudioFeatures._get_melspectrogram`
verlangt ``x.dtype == np.int16`` und wirft sonst ``ValueError``.  ⇒ Der Plan-
Wortlaut „`float32`-Array für OWW" (§7/P2.T2) ist **falsch** und wird gemäß
„Referenz gewinnt" als **E46** dokumentiert; `MicChunker` liefert `int16`.

**Keine eigene VAD (E5).**  Dieses Modul erkennt **keine** Sprache und keinen
Turn-Grenzpunkt – das Gerät entscheidet (`0x04` VAD_END / `0x05` no-speech,
`app.protocol.SentinelKind`).

**Thread-/Task-Safety (begründet):** Speaker- und Mic-Pfad laufen in
getrennten Kontexten, teilen aber **keine** Objekte.  Die zusammengesetzten
Read-Modify-Write-Operationen von `MicChunker` und `AudioRingBuffer` sind per
`threading.Lock` geschützt, weil der Mic-Pfad (WS-Reader) und die Turn-Logik
in verschiedenen Tasks laufen und ein `asyncio`-`await` dazwischen liegen
kann.  `SpeakerResampler`/`SpeakerChunker` sind **bewusst nicht** gelockt: sie
besitzen einen zustandsbehafteten SoXR-Stream bzw. einen Byte-Puffer und
gehören genau **einem** Aufrufer (dem TTS-Stream-Task); ein Lock würde hier
nur Kosten ohne Schutz erzeugen, weil SoXR selbst keinen internen
Parallelzugriff kennt.
"""

from __future__ import annotations

import threading
from typing import Final, Union

import numpy as np
import soxr

from app.config import settings
from app.protocol import CHUNK_BYTES, SPEAKER_BYTES

__all__ = [
    "AudioBridgeError",
    "SPEAKER_RATE",
    "MIC_RATE",
    "SAMPLE_WIDTH",
    "CHANNELS",
    "SPEAKER_CHUNK_BYTES",
    "MIC_CHUNK_BYTES",
    "MIC_MAX_BUFFER_SECONDS",
    "MIC_MAX_BUFFER_BYTES",
    "OWW_DTYPE",
    "SpeakerResampler",
    "SpeakerChunker",
    "chunk_for_speaker",
    "AudioRingBuffer",
    "MicChunker",
]


class AudioBridgeError(ValueError):
    """Verletzung der Audio-Bridge-Verträge (definiert statt still zu degradieren)."""


# ── Konstanten (wiederverwendet, nicht dupliziert) ────────────────────────
#: Ausgaberate des Speaker-Pfads – `AUDIO_SPEAKER_RATE` (PLAN §4, 48000).
SPEAKER_RATE: Final[int] = settings.audio_speaker_rate
#: Eingaberate des Mic-Pfads – `AUDIO_MIC_RATE` (PLAN §4, 16000).
MIC_RATE: Final[int] = settings.audio_mic_rate
#: Bytes pro Sample, S16_LE ⇒ 2 (`AUDIO_WIDTH`).
SAMPLE_WIDTH: Final[int] = settings.audio_width
#: Kanäle ⇒ mono = 1 (`AUDIO_CHANNELS`).
CHANNELS: Final[int] = settings.audio_channels
#: 4096 B Speaker-Periode – dieselbe Konstante wie `app.protocol.SPEAKER_BYTES`.
SPEAKER_CHUNK_BYTES: Final[int] = SPEAKER_BYTES
#: 2560 B Mic-Chunk – dieselbe Konstante wie `app.protocol.CHUNK_BYTES`.
MIC_CHUNK_BYTES: Final[int] = CHUNK_BYTES
#: Max-Dauer des Mic-Ringpuffers = K5-Hard-Cap (`TURN_HARD_CAP_SECONDS`, 15,0 s).
MIC_MAX_BUFFER_SECONDS: Final[float] = settings.turn_hard_cap_seconds
#: Daraus abgeleitete Byte-Obergrenze: 15,0 s · 16 kHz · 2 B · 1 = 480000 B.
MIC_MAX_BUFFER_BYTES: Final[int] = int(
    MIC_MAX_BUFFER_SECONDS * MIC_RATE * SAMPLE_WIDTH * CHANNELS
)
#: OWW-Eingangsdtype – **int16** (E46), siehe Modul-Docstring.
OWW_DTYPE: Final[np.dtype] = np.dtype("int16")

#: PCM akzeptiert `bytes`/`bytearray`/`memoryview`.
_BytesLike = Union[bytes, bytearray, memoryview]


def _as_pcm_bytes(data: _BytesLike) -> bytes:
    """Bytes-artigen PCM in `bytes` überführen (identische Verträge wie `app.protocol`)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise AudioBridgeError(
            f"PCM muss bytes-artig sein, ist {type(data).__name__}"
        )
    return bytes(data)


def _require_even(pcm: bytes) -> None:
    """S16_LE verlangt eine ganzzahlige Sample-Anzahl (gerade Byte-Länge)."""
    if len(pcm) % SAMPLE_WIDTH != 0:
        raise AudioBridgeError(
            f"PCM hat {len(pcm)} B – S16_LE braucht ein Vielfaches von {SAMPLE_WIDTH}"
        )


# ── Speaker: Resampling (Piper-Voice-Rate → 48 kHz) ───────────────────────
class SpeakerResampler:
    """Zustandsbehafteter Streaming-Resampler auf SoXR-Basis (`int16`).

    Die **Eingangsrate kommt zur Laufzeit** aus dem Piper-`AudioStart`
    (`set_input_rate()`); die Ausgangsrate ist `AUDIO_SPEAKER_RATE` (48000).
    Ein Ratenwechsel verwirft den alten SoXR-Stream (Stream-Diskontinuität,
    z. B. Wechsel der Stimme) und legt einen frischen an.
    """

    def __init__(
        self,
        out_rate: int = SPEAKER_RATE,
        *,
        in_rate: int | None = None,
        dtype: str | np.dtype = OWW_DTYPE,
        num_channels: int = CHANNELS,
        quality: str = "HQ",
    ) -> None:
        if out_rate <= 0:
            raise AudioBridgeError(f"Ausgangsrate muss > 0 sein, ist {out_rate}")
        self._out_rate = int(out_rate)
        self._in_rate: int | None = None
        self._dtype = np.dtype(dtype)
        self._num_channels = int(num_channels)
        self._quality = quality
        self._stream: soxr.ResampleStream | None = None
        if in_rate is not None:
            self.set_input_rate(in_rate)

    def _require_rate(self) -> int:
        if self._in_rate is None:
            raise AudioBridgeError(
                "Eingangsrate unbekannt – erst `set_input_rate()` aus dem "
                "Piper-`AudioStart` aufrufen (P0.T3: 22050 Hz)"
            )
        return self._in_rate

    def _new_stream(self) -> soxr.ResampleStream:
        return soxr.ResampleStream(
            self._require_rate(),
            self._out_rate,
            self._num_channels,
            dtype=self._dtype.name,
            quality=self._quality,
        )

    def set_input_rate(self, rate: int) -> bool:
        """Eingangsrate setzen.  True = neu/geändert (Stream neu angelegt).

        Rückgabe False bedeutet: identische Rate wie zuvor **und** der Stream
        steht noch (kein Reset nötig).
        """
        if rate is None or int(rate) <= 0:
            raise AudioBridgeError(f"Eingangsrate muss > 0 sein, ist {rate!r}")
        rate = int(rate)
        if self._in_rate == rate and self._stream is not None:
            return False
        self._in_rate = rate
        self._stream = self._new_stream()
        return True

    @property
    def input_rate(self) -> int | None:
        """Aktuell gesetzte Eingangsrate oder `None`."""
        return self._in_rate

    @property
    def output_rate(self) -> int:
        """Feste Ausgangsrate (48000)."""
        return self._out_rate

    @property
    def is_configured(self) -> bool:
        """True, sobald `set_input_rate()` mindestens einmal rief."""
        return self._in_rate is not None and self._stream is not None

    @property
    def dtype(self) -> np.dtype:
        """Verarbeiteter Sample-Typ (int16)."""
        return self._dtype

    def resample(self, pcm: _BytesLike) -> bytes:
        """PCM (S16_LE mono) in einem Stück resampeln, Rest im SoXR-Puffer.

        Rückgabe ist **kein** 4096-B-Vielfaches – das erledigt
        `SpeakerChunker`/`chunk_for_speaker()`.
        """
        self._require_rate()
        data = _as_pcm_bytes(pcm)
        if not data:
            return b""
        _require_even(data)
        if self._stream is None:
            self._stream = self._new_stream()
        x = np.frombuffer(data, dtype=self._dtype)
        y = self._stream.resample_chunk(x, last=False)
        return y.tobytes()

    def flush(self) -> bytes:
        """SoXR-Filter-Tail ausgeben (`last=True`) und den Stream schließen.

        Nach einem Flush ist der Stream verbraucht; der nächste
        `resample()`-Aufruf legt automatisch einen frischen an (neue
        Äußerung).  `flush()` ohne konfigurierte Rate liefert `b""`.
        """
        if self._stream is None:
            return b""
        tail = self._stream.resample_chunk(
            np.empty(0, dtype=self._dtype), last=True
        )
        self._stream = None
        return tail.tobytes()

    def reset(self) -> None:
        """Stream verwerfen; Raten-Konfiguration bleibt erhalten."""
        self._stream = None


# ── Speaker: 4096-B-Chunking ──────────────────────────────────────────────
def chunk_for_speaker(
    pcm: _BytesLike,
    *,
    chunk_bytes: int = SPEAKER_CHUNK_BYTES,
    pending: _BytesLike = b"",
) -> tuple[list[bytes], bytes]:
    """Einen PCM-Strom in 4096-B-Chunks zerlegen.

    Nimmt optional bereits **gepufferte** Restbytes (`pending`) entgegen und
    gibt ein Paar zurück:

    * `list[bytes]` – alle **vollen** `chunk_bytes`-Chunks, in Reihenfolge;
    * `bytes` – der **Rest** (0 … `chunk_bytes`-1 Byte).  Er wird **weder
      verworfen noch genullt**; der Aufrufer hält ihn und gibt ihn beim
      nächsten Aufruf als `pending` wieder hinein.  Das Nullen des letzten
      Frames ist Sache von `app.protocol.build_speaker_frame(pad=True)`.
    """
    if chunk_bytes <= 0:
        raise AudioBridgeError(f"chunk_bytes muss > 0 sein, ist {chunk_bytes}")
    data = _as_pcm_bytes(pending) + _as_pcm_bytes(pcm)
    full = [data[i : i + chunk_bytes] for i in range(0, len(data) - chunk_bytes + 1, chunk_bytes)]
    remainder = data[len(full) * chunk_bytes :]
    return full, remainder


class SpeakerChunker:
    """Zustandsbehaftete 4096-B-Chunkung für den Speaker-Pfad.

    Nicht gelockt – gehört genau einem Aufrufer (TTS-Stream-Task).
    """

    def __init__(self, chunk_bytes: int = SPEAKER_CHUNK_BYTES) -> None:
        if chunk_bytes <= 0:
            raise AudioBridgeError(f"chunk_bytes muss > 0 sein, ist {chunk_bytes}")
        self._chunk_bytes = int(chunk_bytes)
        self._pending = b""

    @property
    def chunk_bytes(self) -> int:
        """Chunk-Größe (4096)."""
        return self._chunk_bytes

    @property
    def pending_bytes(self) -> int:
        """Aktuell gepufferte Restbytes (0 … 4095)."""
        return len(self._pending)

    def feed(self, pcm: _BytesLike) -> list[bytes]:
        """PCM annehmen und alle **vollen** 4096-B-Chunks zurückgeben."""
        full, self._pending = chunk_for_speaker(
            pcm, chunk_bytes=self._chunk_bytes, pending=self._pending
        )
        return full

    def flush(self) -> bytes:
        """Restbytes zurückgeben und den Puffer leeren.

        Der Rest ist absichtlich **ungestreckt/ungestrichen** (kein Padding,
        kein Verwerfen) – nullgepaddet wird erst im
        `build_speaker_frame(pad=True)`.  Leerer Rest ⇒ `b""`.
        """
        rest, self._pending = self._pending, b""
        return rest

    def reset(self) -> None:
        """Gepufferte Restbytes verwerfen (Stream-Diskontinuität)."""
        self._pending = b""


# ── Mic: Ringpuffer mit Max-Dauer (Drop-oldest) ───────────────────────────
class AudioRingBuffer:
    """Byte-Ringpuffer mit harter Dauer-Obergrenze, Drop-**oldest**.

    Die Max-Dauer ist standardmäßig die K5-Turn-Obergrenze
    (`MIC_MAX_BUFFER_SECONDS` = 15,0 s ⇒ 480000 B @ 16 kHz S16_LE mono).
    Läuft der Puffer über, werden die **ältesten** Bytes verworfen – nie die
    neuesten (Referenz-Pacing: die Geräte-Queue droppt den ältesten Frame,
    `em_controller.py:4062-4076`).  Eine einzelne, die Obergrenze
    überschreitende Payload wird auf ihre letzten `max_bytes` gekürzt.
    """

    def __init__(
        self,
        max_duration_seconds: float = MIC_MAX_BUFFER_SECONDS,
        *,
        sample_rate: int = MIC_RATE,
        sample_width: int = SAMPLE_WIDTH,
        channels: int = CHANNELS,
    ) -> None:
        if max_duration_seconds < 0:
            raise AudioBridgeError(
                f"max_duration_seconds muss >= 0 sein, ist {max_duration_seconds}"
            )
        bytes_per_second = int(sample_rate) * int(sample_width) * int(channels)
        if bytes_per_second <= 0:
            raise AudioBridgeError("sample_rate/sample_width/channels müssen > 0 sein")
        self._max_bytes = int(max_duration_seconds * bytes_per_second)
        self._bytes_per_second = bytes_per_second
        self._buf = bytearray()
        self._lock = threading.Lock()

    @property
    def max_bytes(self) -> int:
        """Byte-Obergrenze (480000 bei Default)."""
        return self._max_bytes

    @property
    def max_seconds(self) -> float:
        """Dauer-Obergrenze in Sekunden (15,0 bei Default)."""
        return self._max_bytes / self._bytes_per_second

    @property
    def buffered_bytes(self) -> int:
        """Aktuell gepufferte Bytes."""
        with self._lock:
            return len(self._buf)

    @property
    def buffered_seconds(self) -> float:
        """Aktuell gepufferte Dauer in Sekunden."""
        return self.buffered_bytes / self._bytes_per_second

    def push(self, pcm: _BytesLike) -> int:
        """PCM anhängen; bei Überlauf vorn schneiden.  Gibt verworfene Bytes zurück."""
        data = _as_pcm_bytes(pcm)
        with self._lock:
            self._buf.extend(data)
            dropped = 0
            if len(self._buf) > self._max_bytes:
                dropped = len(self._buf) - self._max_bytes
                del self._buf[:dropped]
            return dropped

    def read(self) -> bytes:
        """Kopie des Pufferinhalts (älteste … neueste), ohne zu leeren."""
        with self._lock:
            return bytes(self._buf)

    def drain(self) -> bytes:
        """Pufferinhalt zurückgeben und leeren."""
        with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
            return data

    def clear(self) -> None:
        """Puffer leeren."""
        with self._lock:
            self._buf.clear()


# ── Mic: Chunking für openWakeWord ────────────────────────────────────────
class MicChunker:
    """Sammelt Mic-Payloads zu 2560-B-Chunks und liefert **int16**-Arrays.

    openWakeWord erwartet rohe int16-Samples (E46, Belege im Modul-Docstring).
    Ein **angebrochener** Chunk bleibt im Puffer und geht beim nächsten
    `feed()` auf; `flush()` gibt ihn als int16-Array aus – **kein Verlust**,
    **kein** Nullen.  Thread-sicher (Lock), weil der Mic-Pfad und die
    Turn-Logik in getrennten Tasks laufen können.
    """

    def __init__(
        self,
        chunk_bytes: int = MIC_CHUNK_BYTES,
        *,
        dtype: str | np.dtype = OWW_DTYPE,
    ) -> None:
        if chunk_bytes <= 0 or chunk_bytes % SAMPLE_WIDTH != 0:
            raise AudioBridgeError(
                f"chunk_bytes muss > 0 und durch {SAMPLE_WIDTH} teilbar sein, "
                f"ist {chunk_bytes}"
            )
        self._chunk_bytes = int(chunk_bytes)
        self._dtype = np.dtype(dtype)
        self._buf = bytearray()
        self._lock = threading.Lock()

    @property
    def chunk_bytes(self) -> int:
        """Chunk-Größe in Byte (2560 = 1280 Samples = 80 ms @ 16 kHz)."""
        return self._chunk_bytes

    @property
    def chunk_samples(self) -> int:
        """Samples pro Chunk (1280)."""
        return self._chunk_bytes // self._dtype.itemsize

    @property
    def pending_bytes(self) -> int:
        """Aktuell gepufferte Restbytes (angebrochener Chunk)."""
        with self._lock:
            return len(self._buf)

    def feed(self, pcm: _BytesLike) -> list[np.ndarray]:
        """Payload anhängen und alle **vollen** Chunks als int16-Arrays liefern.

        Die Arrays sind genau `chunk_samples` lang; die Eingabe darf beliebig
        gestückelt sein (auch < 2560 B).
        """
        data = _as_pcm_bytes(pcm)
        _require_even(data)
        out: list[np.ndarray] = []
        with self._lock:
            self._buf.extend(data)
            while len(self._buf) >= self._chunk_bytes:
                raw = bytes(self._buf[: self._chunk_bytes])
                del self._buf[: self._chunk_bytes]
                out.append(np.frombuffer(raw, dtype=self._dtype).copy())
        return out

    def flush(self) -> np.ndarray | None:
        """Angebrochenen Rest als int16-Array ausgeben und Puffer leeren.

        `None`, wenn nichts gepuffert ist.  Der Rest ist **nicht** auf
        `chunk_samples` genullt oder gekürzt (kein Verlust).
        """
        with self._lock:
            if not self._buf:
                return None
            raw = bytes(self._buf)
            self._buf.clear()
        return np.frombuffer(raw, dtype=self._dtype).copy()

    def reset(self) -> None:
        """Gepufferten Rest verwerfen (Stream-Diskontinuität, z. B. nach Turn)."""
        with self._lock:
            self._buf.clear()
