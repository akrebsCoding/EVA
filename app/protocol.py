"""EchoMuse-Wire-Protokoll – korrigierte Konstanten, Parser und Serializer (P2.T0).

Reine L0-Logik: **kein Netz, keine I/O**, nur stdlib und `app.config`.
Autoritativ ist `docs/ECOMUSE_PROTOCOL.md` (P0.T6, verifiziert gegen den
Referenz-Controller v2.22.0); wo `PLAN.md` abweicht, gewinnt die Spec.

Umgesetzte Korrekturen:

* **K1 / E27** – `ack` ist wörtlich `{"type": "ack", "device_id": …}`, genau
  **2 Keys**. Ein `features`-Feld existiert nicht und wird nie erwartet.
* **K2 / E28** – Mic-Frame = `[0x01][seq_hi][seq_lo]` + 2560 B PCM = **2563 B**;
  die Sequenznummer ist **uint16 big-endian**, Wrap `65535 → 0`.
* **E28** – `0x04`/`0x05` sind **4-Byte-Sentinel** (`[0x01][0x00][0x00][0x04|0x05]`),
  keine eigenständigen 1-Byte-Typen.
* **Kollision** – die Typcodes sind **richtungs-namespaced**: auf `/data`
  D→C ist `0x04`/`0x05` das **vierte Byte** eines Mic-Sentinels; auf `/data`
  C→D ist `0x04`/`0x05` das **erste Byte** von Music-PCM/EOS. Deshalb gibt es
  je Richtung einen eigenen Parser plus `parse_data_frame(raw, direction)`.
* **E11** – `led_anim` ist **JSON auf `/control`**, kein binärer `0x08`-Frame.
  `0x07 leds` (RGB) ist der binäre Fallback auf `/control`.

Richtungen in den Kommentaren: ``D→C`` = Device → Controller (Manager),
``C→D`` = Controller → Device.  Alle Zahlen sind in
`docs/ECOMUSE_PROTOCOL.md` §7 bzw. im Referenz-Quelltext belegt
(`em_controller.py:165,276-290`, `em_player.py:56-68`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Mapping, Union

from app.config import settings

__all__ = [
    "Direction",
    "SentinelKind",
    "Ack",
    "MicFrame",
    "MicSentinel",
    "SpeakerFrame",
    "SpeakerEos",
    "MusicFrame",
    "MusicEos",
    "ProtocolError",
    "MIC_FRAME_TYPE",
    "MIC_HEADER_LEN",
    "MIC_FRAME_BYTES",
    "MIC_SENTINEL_LEN",
    "VAD_END_TYPE",
    "VAD_NO_SPEECH_TIMEOUT_TYPE",
    "SPEAKER_FRAME_TYPE",
    "SPEAKER_EOS_TYPE",
    "MUSIC_FRAME_TYPE",
    "MUSIC_EOS_TYPE",
    "CHUNK_BYTES",
    "SPEAKER_BYTES",
    "SPEAKER_FRAME_BYTES",
    "NUM_LEDS",
    "SEQ_MODULUS",
    "CTRL_ACK",
    "CTRL_PENDING",
    "CTRL_CONFIG",
    "CTRL_MIC_START",
    "CTRL_MIC_STOP",
    "CTRL_SPEAKER_FLUSH",
    "CTRL_LEDS",
    "CTRL_LED_ANIM",
    "CTRL_PING",
    "CTRL_PONG",
    "CTRL_REGISTER",
    "CTRL_BUTTON",
    "next_sequence",
    "SequenceCounter",
    "build_mic_frame",
    "build_mic_sentinel",
    "build_vad_end_frame",
    "build_no_speech_frame",
    "build_speaker_frame",
    "build_speaker_eos",
    "build_music_frame",
    "build_music_eos",
    "parse_device_data_frame",
    "parse_controller_data_frame",
    "parse_data_frame",
    "parse_control_message",
    "parse_ack",
    "serialize_ack",
    "serialize_pending",
    "serialize_mic_start",
    "serialize_mic_stop",
    "serialize_config",
    "serialize_listening_anim",
    "serialize_speaker_flush",
    "serialize_ping",
    "serialize_leds",
    "serialize_led_anim",
]


class ProtocolError(ValueError):
    """Verletzung des Wire-Protokolls (definiert statt still zu degradieren)."""


# ── Richtungen ────────────────────────────────────────────────────────────
class Direction(Enum):
    """Namespacing der `/data`-Typcodes (Spec §7)."""

    #: Device → Controller (Manager): Mic hoch, VAD-Sentinel.
    DEVICE_TO_CONTROLLER = "D→C"
    #: Controller (Manager) → Device: Speaker/Music runter.
    CONTROLLER_TO_DEVICE = "C→D"


class SentinelKind(Enum):
    """Turn-Ende-Sentinels, vom **Gerät** entschieden (K5, Spec §7.1)."""

    #: `0x04` – Speech erkannt und beendet.
    VAD_END = "vad_end"
    #: `0x05` – nie Speech erkannt ⇒ stillschweigen (E6).
    NO_SPEECH_TIMEOUT = "vad_no_speech_timeout"


# ── `/data`-Frame-Typcodes (richtungs-namespaced, Spec §7) ────────────────
#: D→C – erstes Byte des Mic-PCM-Frames und der Sentinel-Frames.
MIC_FRAME_TYPE: Final[int] = 0x01
#: D→C – viertes Byte des VAD_END-Sentinels (`[0x01][..][..][0x04]`).
VAD_END_TYPE: Final[int] = 0x04
#: D→C – viertes Byte des No-Speech-Sentinels (`[0x01][..][..][0x05]`).
VAD_NO_SPEECH_TIMEOUT_TYPE: Final[int] = 0x05
#: C→D – erstes Byte eines Speaker-PCM-Frames.
SPEAKER_FRAME_TYPE: Final[int] = 0x02
#: C→D – einbyteiger Speaker-EOS-Frame.
SPEAKER_EOS_TYPE: Final[int] = 0x03
#: C→D – erstes Byte eines Music-PCM-Frames (**nur** an `audio_mix`-Geräte).
MUSIC_FRAME_TYPE: Final[int] = 0x04
#: C→D – einbyteiger Music-EOS-Frame (**nur** an `audio_mix`-Geräte).
MUSIC_EOS_TYPE: Final[int] = 0x05

#: 0x06 (BLE) / 0x07 (Session-Audio) werden von v2.22.0 **nicht** gesendet
#: (kein `features` im `ack`, E27/D2) – EVA implementiert sie nicht.
#: D→C – BLE-Adverts laufen als JSON `{"type":"ble_adverts",…}` auf `/control`.
#: D→C – Session-Audio existiert nur bei angekündigtem `listen_session`.


# ── Längen (Spec §7, Referenz `em_controller.py:165,276-290`) ─────────────
#: C→D – `NUM_LEDS`, nur mit `led_anim`-Capability Default 12 (Spec §3).
NUM_LEDS: Final[int] = 12
#: 3-Byte-Mic-Header `[type][seq_hi][seq_lo]` (K2/E28).
MIC_HEADER_LEN: Final[int] = 3
#: 4 Byte gesamt für die Sentinel `[0x01][0x00][0x00][0x04|0x05]` (E28).
MIC_SENTINEL_LEN: Final[int] = MIC_HEADER_LEN + 1
#: uint16-Sequenzraum; Wrap **explizit** `65535 → 0` (K2/E28).
SEQ_MODULUS: Final[int] = 0x10000

#: 2560 B = 80 ms @ 16 kHz S16_LE mono – wiederverwendet aus `app.config`.
CHUNK_BYTES: Final[int] = settings.oww_chunk_bytes
#: 2563 B = Header + PCM (K2/E28).
MIC_FRAME_BYTES: Final[int] = MIC_HEADER_LEN + CHUNK_BYTES
#: 4096 B Speaker-PCM pro Periode – wiederverwendet aus `app.config`.
SPEAKER_BYTES: Final[int] = settings.speaker_chunk_bytes
#: 4097 B = `0x02`-Typebyte + PCM (Spec §7.2).
SPEAKER_FRAME_BYTES: Final[int] = 1 + SPEAKER_BYTES


# ── `/control`-Nachrichtentypen (Spec §2/§3) ──────────────────────────────
#: D→C – erstes Gerätemessage auf `/control`.
CTRL_REGISTER: Final[str] = "register"
#: D→C – Button-Event; nur `clickType == 138` und `down: false` zählen (K6).
CTRL_BUTTON: Final[str] = "button"
#: D→C – Antwort auf den App-Ping; nur mit `id` als RTT-Sample gültig.
CTRL_PONG: Final[str] = "pong"
#: C→D – wörtlich 2 Keys, **kein** `features` (K1/E27).
CTRL_ACK: Final[str] = "ack"
#: C→D – Gerät unbekannt/nicht freigegeben (danach Close).
CTRL_PENDING: Final[str] = "pending"
#: C→D – `{"type":"config", **effektive_config}` (43 Felder + `type` = 44, K4).
CTRL_CONFIG: Final[str] = "config"
#: C→D – `{"type":"mic_start"}` bzw. `{"type":"mic_start","lock_mic":true}` (K3).
CTRL_MIC_START: Final[str] = "mic_start"
#: C→D – `{"type":"mic_stop"}`.
CTRL_MIC_STOP: Final[str] = "mic_stop"
#: C→D – Gerätepuffer verwerfen (K6).
CTRL_SPEAKER_FLUSH: Final[str] = "speaker_flush"
#: C→D – binärer RGB-Fallback, 12 × `{id,r,g,b}`.
CTRL_LEDS: Final[str] = "leds"
#: C→D – JSON-LED-Animation (E11), **kein** binärer `0x08`-Frame.
CTRL_LED_ANIM: Final[str] = "led_anim"
#: C→D – App-Ebene-Ping alle 5 s (Spec §4.1).
CTRL_PING: Final[str] = "ping"


# ── Ergebnis-Typen ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Ack:
    """Geparster `ack` (C→D). Bewusst **ohne** `features` (K1/E27)."""

    device_id: str


@dataclass(frozen=True)
class MicFrame:
    """D→C – `[0x01][seq_hi][seq_lo]` + PCM."""

    seq: int
    pcm: bytes


@dataclass(frozen=True)
class MicSentinel:
    """D→C – 4-Byte-Turn-Ende-Sentinel (K5/E28)."""

    kind: SentinelKind


@dataclass(frozen=True)
class SpeakerFrame:
    """C→D – `0x02` + Speaker-PCM (S16_LE mono 48 kHz)."""

    pcm: bytes


@dataclass(frozen=True)
class SpeakerEos:
    """C→D – `0x03` Speaker-Ende."""


@dataclass(frozen=True)
class MusicFrame:
    """C→D – `0x04` + Music-PCM (nur `audio_mix`-Geräte)."""

    pcm: bytes


@dataclass(frozen=True)
class MusicEos:
    """C→D – `0x05` Music-Ende (nur `audio_mix`-Geräte)."""


#: Vereinigung aller geparsten `/data`-Frames.
DataFrame = Union[
    MicFrame, MicSentinel, SpeakerFrame, SpeakerEos, MusicFrame, MusicEos
]


# ── Sequenznummern (uint16 big-endian, Wrap explizit) ─────────────────────
def next_sequence(seq: int) -> int:
    """Nächste Mic-Sequenznummer, Wrap **65535 → 0** (K2/E28)."""
    return (int(seq) + 1) % SEQ_MODULUS


class SequenceCounter:
    """Fortlaufender uint16-Zähler mit explizitem Wrap (K2/E28)."""

    def __init__(self, start: int = 0) -> None:
        if not 0 <= start < SEQ_MODULUS:
            raise ProtocolError(f"Sequenz-Start {start} liegt außerhalb 0..65535")
        self._value = start

    @property
    def value(self) -> int:
        """Die **nächste** auszugebende Sequenznummer."""
        return self._value

    def next(self) -> int:
        """Aktuelle Nummer zurückgeben und um eins weiterschalten (Wrap 65535→0)."""
        current = self._value
        self._value = next_sequence(current)
        return current

    def reset(self, start: int = 0) -> None:
        """Zähler neu setzen (z. B. nach `model.reset()`)."""
        if not 0 <= start < SEQ_MODULUS:
            raise ProtocolError(f"Sequenz-Start {start} liegt außerhalb 0..65535")
        self._value = start


# ── binäre Builder (C→D und D→C) ──────────────────────────────────────────
def _as_bytes(data: Union[bytes, bytearray, memoryview]) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtocolError(f"PCM muss bytes-artig sein, ist {type(data).__name__}")
    return bytes(data)


def build_mic_frame(
    seq: int, pcm: Union[bytes, bytearray, memoryview], *, strict: bool = True
) -> bytes:
    """D→C – `[0x01][seq_hi][seq_lo]` + PCM; Seq als uint16 **big-endian**.

    `strict=True` erzwingt exakt `CHUNK_BYTES` (2560) PCM-Bytes.  Die
    Sequenznummer wird auf 16 Bit maskiert (Wrap 65535 → 0).
    """
    payload = _as_bytes(pcm)
    if strict and len(payload) != CHUNK_BYTES:
        raise ProtocolError(
            f"Mic-Payload hat {len(payload)} B, erwartet {CHUNK_BYTES} (K2/E28)"
        )
    if len(payload) > CHUNK_BYTES:
        raise ProtocolError(
            f"Mic-Payload hat {len(payload)} B, maximal {CHUNK_BYTES}"
        )
    value = int(seq) % SEQ_MODULUS
    return bytes([MIC_FRAME_TYPE, value >> 8, value & 0xFF]) + payload


def build_mic_sentinel(kind: SentinelKind) -> bytes:
    """D→C – 4-Byte-Sentinel `[0x01][0x00][0x00][0x04|0x05]` (E28).

    Die beiden mittleren Byte sind bei v2.22.0 immer `0x00`; der Controller
    wertet ohnehin nur Länge, erstes und viertes Byte aus (Spec §7.1).
    """
    code = {
        SentinelKind.VAD_END: VAD_END_TYPE,
        SentinelKind.NO_SPEECH_TIMEOUT: VAD_NO_SPEECH_TIMEOUT_TYPE,
    }[kind]
    return bytes([MIC_FRAME_TYPE, 0x00, 0x00, code])


def build_vad_end_frame() -> bytes:
    """D→C – `[0x01][0x00][0x00][0x04]` VAD_END (K5)."""
    return build_mic_sentinel(SentinelKind.VAD_END)


def build_no_speech_frame() -> bytes:
    """D→C – `[0x01][0x00][0x00][0x05]` No-Speech-Timeout (K5/E6)."""
    return build_mic_sentinel(SentinelKind.NO_SPEECH_TIMEOUT)


def build_speaker_frame(
    pcm: Union[bytes, bytearray, memoryview], *, pad: bool = True
) -> bytes:
    """C→D – `0x02` + Speaker-PCM. Letzter Frame wird auf 4096 B genullt."""
    payload = _as_bytes(pcm)
    if len(payload) > SPEAKER_BYTES:
        raise ProtocolError(
            f"Speaker-Payload hat {len(payload)} B, maximal {SPEAKER_BYTES}"
        )
    if pad and len(payload) < SPEAKER_BYTES:
        payload = payload + bytes(SPEAKER_BYTES - len(payload))
    return bytes([SPEAKER_FRAME_TYPE]) + payload


def build_speaker_eos() -> bytes:
    """C→D – einbyteiger Speaker-EOS-Frame `0x03`."""
    return bytes([SPEAKER_EOS_TYPE])


def build_music_frame(
    pcm: Union[bytes, bytearray, memoryview], *, pad: bool = True
) -> bytes:
    """C→D – `0x04` + Music-PCM; nur an `audio_mix`-Geräte."""
    payload = _as_bytes(pcm)
    if len(payload) > SPEAKER_BYTES:
        raise ProtocolError(
            f"Music-Payload hat {len(payload)} B, maximal {SPEAKER_BYTES}"
        )
    if pad and len(payload) < SPEAKER_BYTES:
        payload = payload + bytes(SPEAKER_BYTES - len(payload))
    return bytes([MUSIC_FRAME_TYPE]) + payload


def build_music_eos() -> bytes:
    """C→D – einbyteiger Music-EOS-Frame `0x05`; nur `audio_mix`-Geräte."""
    return bytes([MUSIC_EOS_TYPE])


# ── binäre Parser (je Richtung getrennt wegen Typcode-Kollision) ──────────
def parse_device_data_frame(
    raw: Union[bytes, bytearray, memoryview],
) -> Union[MicFrame, MicSentinel, None]:
    """D→C-Parser für `/data` (Mic + Sentinel).

    Verhalten exakt wie der Referenz-Controller (`em_controller.py:4038-4060`):
    Frames mit `len(raw) <= 3` oder `raw[0] != 0x01` werden **stillschweigend
    verworfen** ⇒ `None`.  `0x04`/`0x05` zählen hier **nur** als viertes Byte
    eines 4-Byte-Sentinels, niemals als eigenständiger Typ.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ProtocolError(f"/data-Frame muss bytes-artig sein, ist {type(raw).__name__}")
    data = bytes(raw)
    if len(data) <= MIC_HEADER_LEN or data[0] != MIC_FRAME_TYPE:
        return None
    if (
        len(data) == MIC_SENTINEL_LEN
        and data[MIC_HEADER_LEN] in (VAD_END_TYPE, VAD_NO_SPEECH_TIMEOUT_TYPE)
    ):
        kind = (
            SentinelKind.VAD_END
            if data[MIC_HEADER_LEN] == VAD_END_TYPE
            else SentinelKind.NO_SPEECH_TIMEOUT
        )
        return MicSentinel(kind=kind)
    seq = (data[1] << 8) | data[2]
    return MicFrame(seq=seq, pcm=data[MIC_HEADER_LEN:])


def parse_controller_data_frame(
    raw: Union[bytes, bytearray, memoryview],
) -> Union[SpeakerFrame, SpeakerEos, MusicFrame, MusicEos, None]:
    """C→D-Parser für `/data` (Speaker/Music).

    Hier ist `0x04`/`0x05` das **erste** Byte (Music-PCM/EOS) – die Kollision
    mit dem D→C-Sentinel wird allein durch die Richtung aufgelöst.  Unbekannte
    Typcodes ⇒ `None`.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ProtocolError(f"/data-Frame muss bytes-artig sein, ist {type(raw).__name__}")
    data = bytes(raw)
    if not data:
        return None
    code = data[0]
    if code == SPEAKER_FRAME_TYPE:
        return SpeakerFrame(pcm=data[1:])
    if code == SPEAKER_EOS_TYPE:
        return SpeakerEos()
    if code == MUSIC_FRAME_TYPE:
        return MusicFrame(pcm=data[1:])
    if code == MUSIC_EOS_TYPE:
        return MusicEos()
    return None


def parse_data_frame(
    raw: Union[bytes, bytearray, memoryview], direction: Direction
) -> Union[DataFrame, None]:
    """Richtungs-bewusster `/data`-Dispatcher (löst die `0x04`/`0x05`-Kollision)."""
    if direction is Direction.DEVICE_TO_CONTROLLER:
        return parse_device_data_frame(raw)
    if direction is Direction.CONTROLLER_TO_DEVICE:
        return parse_controller_data_frame(raw)
    raise ProtocolError(f"Unbekannte Richtung: {direction!r} (E28)")


# ── `/control`-JSON: Parser ───────────────────────────────────────────────
def parse_control_message(message: Union[str, bytes, Mapping[str, Any]]) -> dict[str, Any]:
    """`/control`-Text-Frame in ein `dict` überführen.

    Akzeptiert JSON-Text/Bytes oder bereits geparste Mappings (Tests, Fake-Dot).
    """
    if isinstance(message, Mapping):
        return dict(message)
    if isinstance(message, (bytes, bytearray, memoryview)):
        message = bytes(message).decode("utf-8")
    if not isinstance(message, str):
        raise ProtocolError(
            f"/control-Nachricht muss str/bytes/Mapping sein, ist {type(message).__name__}"
        )
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Ungültiges `/control`-JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("/control-JSON muss ein Objekt sein")
    return parsed


def parse_ack(message: Union[str, bytes, Mapping[str, Any]]) -> Ack:
    """`ack` (C→D) parsen – **ohne** `features` (K1/E27).

    Der `ack` hat wörtlich 2 Keys.  Zusätzliche Keys (auch ein von fremder
    Software angehängtes `features`) werden **ignoriert**, nicht gelesen und
    nicht erzwungen – unbekannte Felder sind per Protokoll kein Fehler
    (Vorwärtskompatibilität, Spec §2).  Fehlt `device_id`, ist das ein
    `ProtocolError`.
    """
    msg = parse_control_message(message)
    if msg.get("type") != CTRL_ACK:
        raise ProtocolError(f"Kein ack: type={msg.get('type')!r} (K1)")
    device_id = msg.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise ProtocolError("ack ohne nicht-leere device_id (K1/E27)")
    return Ack(device_id=device_id)


# ── `/control`-JSON: Serializer (C→D) ─────────────────────────────────────
def serialize_ack(device_id: str) -> dict[str, Any]:
    """C→D – wörtlich `{"type":"ack","device_id":…}`; **kein** `features` (K1/E27)."""
    if not isinstance(device_id, str) or not device_id:
        raise ProtocolError("ack benötigt eine nicht-leere device_id (K1/E27)")
    return {"type": CTRL_ACK, "device_id": device_id}


def serialize_pending() -> dict[str, Any]:
    """C→D – `{"type":"pending"}` (Gerät unbekannt/nicht freigegeben)."""
    return {"type": CTRL_PENDING}


def serialize_mic_start(lock_mic: bool = False) -> dict[str, Any]:
    """C→D – `mic_start`; `lock_mic=True` **nur** im Button-Pfad (K3/K6).

    `lock_mic=False` erzeugt exakt `{"type":"mic_start"}`; nur bei `True`
    wird der zweite Key gesetzt – die beiden Payloads sind damit
    unterscheidbar.
    """
    msg: dict[str, Any] = {"type": CTRL_MIC_START}
    if lock_mic:
        msg["lock_mic"] = True
    return msg


def serialize_mic_stop() -> dict[str, Any]:
    """C→D – `{"type":"mic_stop"}` (vor/nach dem Button-Turn zwingend, K3/K6)."""
    return {"type": CTRL_MIC_STOP}


def serialize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """C→D – `{"type":"config", **effektive_config}` (43 Felder + `type` = 44, K4).

    `config` sind die 43 gemergten Felder aus `docs/device-config-reference.json`;
    die 44. Key ist `type`.  Der Merger (P2.T6) übergibt hier keine `type`-Kollision.
    """
    if not isinstance(config, Mapping):
        raise ProtocolError("config muss ein Mapping sein (K4)")
    msg: dict[str, Any] = {"type": CTRL_CONFIG}
    msg.update(config)
    return msg


def serialize_listening_anim(anim: Mapping[str, Any]) -> dict[str, Any]:
    """C→D – schlanker Vorab-Push `{"type":"config","listeningAnim":{…}}` (K7/D6).

    Zweite Form des `config`-Pushes (Spec §3); nur bei `led_anim`-Capability.
    """
    if not isinstance(anim, Mapping):
        raise ProtocolError("listeningAnim muss ein Mapping sein (D6)")
    return {"type": CTRL_CONFIG, "listeningAnim": dict(anim)}


def serialize_speaker_flush() -> dict[str, Any]:
    """C→D – `{"type":"speaker_flush"}` (Cancel/Toggle, K6)."""
    return {"type": CTRL_SPEAKER_FLUSH}


def serialize_ping(seq: int) -> dict[str, Any]:
    """C→D – App-Ebenen-Ping `{"type":"ping","id":<seq>}` (Spec §4.1)."""
    return {"type": CTRL_PING, "id": int(seq)}


def serialize_leds(
    leds: list[Mapping[str, Any]], listening: Union[bool, None] = None
) -> dict[str, Any]:
    """C→D – binärer RGB-Fallback `{"type":"leds","leds":[12×{id,r,g,b}]}`.

    `listening=True` **explizit** mitsenden (Beamformer-Overlay); die alte
    „ganz grün = listening"-Heuristik gilt nicht mehr (D6).  `led_anim`
    (E11) ist der bevorzugte Weg – dieser bleibt Fallback.
    """
    if len(leds) != NUM_LEDS:
        raise ProtocolError(f"leds braucht genau {NUM_LEDS} Einträge, hat {len(leds)}")
    msg: dict[str, Any] = {"type": CTRL_LEDS, "leds": [dict(led) for led in leds]}
    if listening is not None:
        msg["listening"] = bool(listening)
    return msg


def serialize_led_anim(anim: Mapping[str, Any]) -> dict[str, Any]:
    """C→D – `{"type":"led_anim","anim":{pattern,colors,periodMs,ttlSec}}` (E11).

    **JSON auf `/control`**, kein binärer `0x08`-Frame (E27/K7).
    """
    if not isinstance(anim, Mapping):
        raise ProtocolError("led_anim.anim muss ein Mapping sein (E11)")
    return {"type": CTRL_LED_ANIM, "anim": dict(anim)}
