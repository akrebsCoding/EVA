"""Wire-Protokoll-Tests (P2.T1, `PLAN.md` §7 → P2.T1, Layer **L0**).

Prüfling ist `app/protocol.py` (P2.T0).  Getestet wird **gegen die echte API**
(§3 „Wire-Protokoll (P2.T0)" = Vertrag), nicht gegen eine Wunschfassung:
Byte-Längen, Byte-Inhalte, Big-Endian-Reihenfolge und die richtungs-getrennte
`0x04`/`0x05`-Kollision werden **numerisch** geprüft, nicht nur „wirft nicht".

Abdeckung der ≥10 Pflichtfälle aus `PLAN.md:425`:

1. `ack` **ohne** `features` (K1)                       → `test_ack_*`
2. Mic-Header + Payload = **2563 B** (K2/E28)          → `test_mic_*`
3. Sequenznummer inkl. uint16-BE-Wrap 65535→0          → `test_sequence_*`
4. VAD-End (`0x04`)                                    → `test_vad_end_*`
5. no-speech (`0x05`)                                  → `test_no_speech_*`
6. Mic-Start/Stop-Roundtrip mit/ohne `lock_mic` (K3)   → `test_mic_start_*`
7. config-Payload (K4)                                 → `test_config_*`
8. Speaker-Roundtrip `0x02`/`0x03`, 4097-B-Frame       → `test_speaker_*`
9. EOS                                                 → `test_eos_*`
10. Kollision `0x04`/`0x05` richtungsabhängig          → `test_collision_*`

Zusätzlich die billigen Randfälle: Sequenz-Wrap, leere/zu kurze Frames
(definiertes Verhalten), falsche Payload-Längen und nicht-`bytes`-Eingaben
(⇒ `ProtocolError`).  **Kein Netz, kein Gerät, kein Modell** (L0).
"""

from __future__ import annotations

import pytest

from app.protocol import (
    CHUNK_BYTES,
    CTRL_ACK,
    CTRL_CONFIG,
    CTRL_LED_ANIM,
    CTRL_LEDS,
    CTRL_MIC_START,
    CTRL_MIC_STOP,
    CTRL_PING,
    CTRL_SPEAKER_FLUSH,
    MIC_FRAME_BYTES,
    MIC_FRAME_TYPE,
    MIC_HEADER_LEN,
    MIC_SENTINEL_LEN,
    MUSIC_EOS_TYPE,
    MUSIC_FRAME_TYPE,
    NUM_LEDS,
    SEQ_MODULUS,
    SPEAKER_BYTES,
    SPEAKER_EOS_TYPE,
    SPEAKER_FRAME_BYTES,
    SPEAKER_FRAME_TYPE,
    VAD_END_TYPE,
    VAD_NO_SPEECH_TIMEOUT_TYPE,
    Ack,
    Direction,
    MicFrame,
    MicSentinel,
    MusicEos,
    MusicFrame,
    ProtocolError,
    SentinelKind,
    SequenceCounter,
    SpeakerEos,
    SpeakerFrame,
    build_mic_frame,
    build_mic_sentinel,
    build_music_eos,
    build_music_frame,
    build_no_speech_frame,
    build_speaker_eos,
    build_speaker_frame,
    build_vad_end_frame,
    next_sequence,
    parse_ack,
    parse_control_message,
    parse_controller_data_frame,
    parse_data_frame,
    parse_device_data_frame,
    serialize_ack,
    serialize_config,
    serialize_led_anim,
    serialize_leds,
    serialize_listening_anim,
    serialize_mic_start,
    serialize_mic_stop,
    serialize_pending,
    serialize_ping,
    serialize_speaker_flush,
)

pytestmark = pytest.mark.unit

#: Deterministisches PCM-Muster (ohne Zufall/Zeit) – 2560 B Mic-Payload.
MIC_PAYLOAD = bytes(range(256)) * 10
assert len(MIC_PAYLOAD) == CHUNK_BYTES
#: Deterministisches Speaker-/Music-PCM (4096 B).
SPEAKER_PAYLOAD = bytes((i * 3 + 1) & 0xFF for i in range(SPEAKER_BYTES))


# ── 1. Ack-Shape OHNE `features` (K1/E27, E44) ────────────────────────────
def test_ack_serializes_to_exactly_two_keys_without_features() -> None:
    msg = serialize_ack("G090L91072320Q6E")
    assert msg == {"type": "ack", "device_id": "G090L91072320Q6E"}
    # Wörtlich 2 Keys – die Reihenfolge ist die des Referenz-Controllers.
    assert list(msg.keys()) == ["type", "device_id"]
    assert "features" not in msg
    assert msg["type"] == CTRL_ACK


def test_ack_roundtrip_and_extra_keys_are_ignored() -> None:
    raw = '{"type":"ack","device_id":"dev-1"}'
    ack = parse_ack(raw)
    assert isinstance(ack, Ack)
    assert ack.device_id == "dev-1"
    # Ein `features`-Feld existiert am Ergebnis nicht (K1), auch wenn freie
    # Software es anhängt: unbekannte Keys werden ignoriert, nicht gelesen.
    with_features = parse_ack(
        '{"type":"ack","device_id":"dev-1","features":["listen_session"]}'
    )
    assert with_features == ack
    assert not hasattr(with_features, "features")


def test_ack_requires_a_non_empty_device_id() -> None:
    with pytest.raises(ProtocolError):
        parse_ack({"type": "ack"})
    with pytest.raises(ProtocolError):
        parse_ack({"type": "ack", "device_id": ""})
    with pytest.raises(ProtocolError):
        parse_ack({"type": "nack", "device_id": "dev-1"})
    with pytest.raises(ProtocolError):
        parse_ack("kein json")


# ── 2. Mic-Header + Payload = 2563 B (K2/E28) ─────────────────────────────
def test_mic_frame_is_2563_bytes_with_3_byte_header() -> None:
    frame = build_mic_frame(0x0000, MIC_PAYLOAD)
    assert len(frame) == MIC_FRAME_BYTES == 2563
    assert frame[0] == MIC_FRAME_TYPE == 0x01
    assert frame[MIC_HEADER_LEN:] == MIC_PAYLOAD
    assert len(frame[MIC_HEADER_LEN:]) == CHUNK_BYTES == 2560


def test_mic_frame_roundtrips_header_sequence_and_payload() -> None:
    frame = build_mic_frame(0x1234, MIC_PAYLOAD)
    parsed = parse_device_data_frame(frame)
    assert isinstance(parsed, MicFrame)
    assert parsed.seq == 0x1234
    assert parsed.pcm == MIC_PAYLOAD
    # Konstantes Byte-Muster ⇒ Big-Endian-Reihenfolge direkt sichtbar.
    assert frame[1] == 0x12 and frame[2] == 0x34


# ── 3. Sequenznummer inkl. uint16-BE-Wrap 65535→0 ─────────────────────────
def test_sequence_is_encoded_big_endian() -> None:
    assert build_mic_frame(0x0102, MIC_PAYLOAD)[1:3] == b"\x01\x02"
    assert build_mic_frame(0xFF00, MIC_PAYLOAD)[1:3] == b"\xff\x00"
    # `seq = 1` zeigt die BE-Reihenfolge am eindeutigsten (LE wäre `01 00`).
    assert build_mic_frame(1, MIC_PAYLOAD)[1:3] == b"\x00\x01"


def test_sequence_wraps_65535_to_zero() -> None:
    assert SEQ_MODULUS == 0x10000
    assert next_sequence(0) == 1
    assert next_sequence(65534) == 65535
    assert next_sequence(65535) == 0
    # Builder maskiert auf 16 Bit ⇒ 65536 fällt auf 0 zurück.
    assert build_mic_frame(65536, MIC_PAYLOAD)[1:3] == b"\x00\x00"
    assert build_mic_frame(0x1FFFF, MIC_PAYLOAD)[1:3] == b"\xff\xff"


def test_sequence_counter_advances_and_wraps_through_zero() -> None:
    counter = SequenceCounter(start=65533)
    assert counter.value == 65533
    assert [counter.next() for _ in range(5)] == [65533, 65534, 65535, 0, 1]
    assert counter.value == 2
    counter.reset(0)
    assert counter.next() == 0
    with pytest.raises(ProtocolError):
        SequenceCounter(start=SEQ_MODULUS)
    with pytest.raises(ProtocolError):
        SequenceCounter(start=-1)


# ── 4. VAD-End (`0x04`) ───────────────────────────────────────────────────
def test_vad_end_is_a_four_byte_sentinel() -> None:
    frame = build_vad_end_frame()
    assert frame == bytes([0x01, 0x00, 0x00, VAD_END_TYPE]) == b"\x01\x00\x00\x04"
    assert len(frame) == MIC_SENTINEL_LEN == 4


def test_vad_end_parses_only_in_device_to_controller_direction() -> None:
    frame = build_vad_end_frame()
    parsed = parse_device_data_frame(frame)
    assert isinstance(parsed, MicSentinel)
    assert parsed.kind is SentinelKind.VAD_END
    assert parse_data_frame(frame, Direction.DEVICE_TO_CONTROLLER) == parsed
    # In C→D ist `0x01` kein bekannter Typ ⇒ definiert `None`, kein Sentinel.
    assert parse_data_frame(frame, Direction.CONTROLLER_TO_DEVICE) is None
    assert build_mic_sentinel(SentinelKind.VAD_END) == frame


# ── 5. no-speech (`0x05`) ─────────────────────────────────────────────────
def test_no_speech_is_a_four_byte_sentinel() -> None:
    frame = build_no_speech_frame()
    assert frame == bytes([0x01, 0x00, 0x00, VAD_NO_SPEECH_TIMEOUT_TYPE])
    assert frame == b"\x01\x00\x00\x05"
    assert len(frame) == 4


def test_no_speech_parses_to_its_own_kind() -> None:
    parsed = parse_data_frame(build_no_speech_frame(), Direction.DEVICE_TO_CONTROLLER)
    assert isinstance(parsed, MicSentinel)
    assert parsed.kind is SentinelKind.NO_SPEECH_TIMEOUT
    assert parsed.kind is not SentinelKind.VAD_END


# ── 6. Mic-Start/Stop-Roundtrip mit `lock_mic` (K3/K6) ────────────────────
def test_mic_start_with_and_without_lock_mic_are_distinguishable() -> None:
    open_stream = serialize_mic_start()
    locked_stream = serialize_mic_start(lock_mic=True)
    assert open_stream == {"type": "mic_start"}
    assert list(open_stream.keys()) == ["type"]
    assert locked_stream == {"type": "mic_start", "lock_mic": True}
    assert open_stream != locked_stream
    # Default ist `False`, nicht `None`/fehlend-dann-True.
    assert serialize_mic_start(False) == open_stream
    assert open_stream["type"] == CTRL_MIC_START


def test_mic_start_stop_roundtrip_and_no_extra_keys() -> None:
    for msg in (
        serialize_mic_start(),
        serialize_mic_start(lock_mic=True),
        serialize_mic_stop(),
    ):
        parsed = parse_control_message(msg)
        assert parsed == msg
        assert set(parsed) <= {"type", "lock_mic"}
    assert serialize_mic_stop() == {"type": "mic_stop"}
    assert serialize_mic_stop()["type"] == CTRL_MIC_STOP
    assert serialize_speaker_flush() == {"type": "speaker_flush"}
    assert serialize_speaker_flush()["type"] == CTRL_SPEAKER_FLUSH
    assert serialize_pending() == {"type": "pending"}


# ── 7. config-Payload (K4) ────────────────────────────────────────────────
def test_config_payload_carries_all_fields_plus_type() -> None:
    fields = {f"field{i}": i for i in range(43)}
    msg = serialize_config(fields)
    # 43 Felder + `type` = 44 Top-Level-Keys (K4/E25).
    assert len(msg) == 44
    assert msg["type"] == CTRL_CONFIG
    assert {k: v for k, v in msg.items() if k != "type"} == fields


def test_config_and_listening_anim_have_distinct_shapes() -> None:
    assert serialize_config({}) == {"type": "config"}
    anim = {"pattern": "solid", "colors": [[110, 0, 45]], "ttlSec": 30}
    msg = serialize_listening_anim(anim)
    assert msg == {"type": CTRL_CONFIG, "listeningAnim": anim}
    assert msg["type"] == "config"
    with pytest.raises(ProtocolError):
        serialize_config(["nicht", "mapping"])  # type: ignore[arg-type]


# ── 8. Speaker-Roundtrip `0x02`/`0x03`, 4097-B-Frame ──────────────────────
def test_speaker_frame_is_4097_bytes() -> None:
    frame = build_speaker_frame(SPEAKER_PAYLOAD)
    assert len(frame) == SPEAKER_FRAME_BYTES == 4097
    assert frame[0] == SPEAKER_FRAME_TYPE == 0x02
    assert frame[1:] == SPEAKER_PAYLOAD


def test_speaker_frame_pads_short_payload_only() -> None:
    short = build_speaker_frame(b"\x01\x02")
    assert len(short) == 4097
    assert short[0] == 0x02
    assert short[1:3] == b"\x01\x02"
    assert short[3:] == bytes(SPEAKER_BYTES - 2)
    # `pad=False` lässt den Frame kürzer (kein stilles Auffüllen).
    assert build_speaker_frame(b"\x01\x02", pad=False) == b"\x02\x01\x02"


def test_speaker_roundtrip_parses_to_pcm() -> None:
    frame = build_speaker_frame(SPEAKER_PAYLOAD)
    parsed = parse_controller_data_frame(frame)
    assert isinstance(parsed, SpeakerFrame)
    assert parsed.pcm == SPEAKER_PAYLOAD
    assert parse_data_frame(frame, Direction.CONTROLLER_TO_DEVICE) == parsed


def test_speaker_frame_rejects_oversized_payload() -> None:
    with pytest.raises(ProtocolError):
        build_speaker_frame(bytes(SPEAKER_BYTES + 1))


def test_music_frame_uses_0x04_and_is_4097_bytes() -> None:
    frame = build_music_frame(SPEAKER_PAYLOAD)
    assert len(frame) == 4097
    assert frame[0] == MUSIC_FRAME_TYPE == 0x04
    parsed = parse_controller_data_frame(frame)
    assert isinstance(parsed, MusicFrame)
    assert parsed.pcm == SPEAKER_PAYLOAD


# ── 9. EOS ────────────────────────────────────────────────────────────────
def test_eos_frames_are_single_bytes() -> None:
    speaker_eos = build_speaker_eos()
    music_eos = build_music_eos()
    assert speaker_eos == bytes([SPEAKER_EOS_TYPE]) == b"\x03"
    assert music_eos == bytes([MUSIC_EOS_TYPE]) == b"\x05"
    assert len(speaker_eos) == 1 and len(music_eos) == 1


def test_eos_roundtrip_through_controller_parser() -> None:
    assert isinstance(parse_controller_data_frame(build_speaker_eos()), SpeakerEos)
    assert isinstance(parse_controller_data_frame(build_music_eos()), MusicEos)
    assert parse_data_frame(
        build_speaker_eos(), Direction.CONTROLLER_TO_DEVICE
    ) == SpeakerEos()


# ── 10. Kollision `0x04`/`0x05` richtungsabhängig ─────────────────────────
def test_music_codes_are_direction_namespaced() -> None:
    music_pcm = b"\x04" + bytes(16)
    music_eos = b"\x05"
    # C→D: `0x04`/`0x05` sind das **erste** Byte (Music-PCM/EOS).
    assert isinstance(
        parse_data_frame(music_pcm, Direction.CONTROLLER_TO_DEVICE), MusicFrame
    )
    assert isinstance(
        parse_data_frame(music_eos, Direction.CONTROLLER_TO_DEVICE), MusicEos
    )
    # D→C: `0x04…` beginnt nicht mit `0x01` ⇒ vom Mic-Parser verworfen (`None`).
    assert parse_data_frame(music_pcm, Direction.DEVICE_TO_CONTROLLER) is None
    assert parse_data_frame(music_eos, Direction.DEVICE_TO_CONTROLLER) is None


def test_sentinels_are_direction_namespaced() -> None:
    vad = build_vad_end_frame()
    nospeech = build_no_speech_frame()
    # D→C: `0x04`/`0x05` sind das **vierte** Byte eines Sentinels.
    assert isinstance(
        parse_data_frame(vad, Direction.DEVICE_TO_CONTROLLER), MicSentinel
    )
    assert isinstance(
        parse_data_frame(nospeech, Direction.DEVICE_TO_CONTROLLER), MicSentinel
    )
    # C→D: `0x01` ist kein Playback-Typ ⇒ `None` (kein Speaker/Music).
    assert parse_data_frame(vad, Direction.CONTROLLER_TO_DEVICE) is None
    assert parse_data_frame(nospeech, Direction.CONTROLLER_TO_DEVICE) is None


def test_same_bytes_yield_opposite_results_per_direction() -> None:
    # Genau die Kollision: `b"\x04..."` ist C→D Music, D→C kein Mic-Frame.
    raw = b"\x04\x00\x00\x04"
    as_controller = parse_data_frame(raw, Direction.CONTROLLER_TO_DEVICE)
    as_device = parse_data_frame(raw, Direction.DEVICE_TO_CONTROLLER)
    assert isinstance(as_controller, MusicFrame)
    assert as_device is None
    assert as_controller != as_device


# ── Randfälle: leere/zu kurze Frames und Fehler-Input ─────────────────────
def test_short_and_empty_device_frames_are_defined() -> None:
    # Referenz-Verhalten (`em_controller.py:4041-4062`): len<=3 oder raw[0]!=0x01
    # wird **stillschweigend verworfen** ⇒ `None`.
    for raw in (b"", b"\x01", b"\x01\x00", b"\x01\x00\x00", b"\x02\x00\x00\x04"):
        assert parse_device_data_frame(raw) is None
    # Ein 4-Byte-Frame mit fremdem vierten Byte ist ein Mic-Frame, kein Sentinel.
    parsed = parse_device_data_frame(b"\x01\x00\x00\x00")
    assert isinstance(parsed, MicFrame)
    assert parsed.seq == 0 and parsed.pcm == b"\x00"


def test_short_and_empty_controller_frames_are_defined() -> None:
    assert parse_controller_data_frame(b"") is None
    for code in (0x00, 0x01, 0x06, 0x07, 0x08, 0xFF):
        assert parse_controller_data_frame(bytes([code])) is None


def test_parsers_reject_non_bytes_input() -> None:
    for parser in (parse_device_data_frame, parse_controller_data_frame):
        with pytest.raises(ProtocolError):
            parser("kein bytes")  # type: ignore[arg-type]
    with pytest.raises(ProtocolError):
        parse_data_frame(b"\x01", "D→C")  # type: ignore[arg-type]


def test_build_mic_frame_rejects_wrong_payload_length() -> None:
    with pytest.raises(ProtocolError):
        build_mic_frame(0, bytes(CHUNK_BYTES - 1))
    with pytest.raises(ProtocolError):
        build_mic_frame(0, bytes(CHUNK_BYTES + 1))
    with pytest.raises(ProtocolError):
        build_mic_frame(0, "kein bytes")  # type: ignore[arg-type]
    # `strict=False` erlaubt kürzere Frames, oversized bleibt Fehler.
    short = build_mic_frame(0, b"\xaa\xbb", strict=False)
    assert short == b"\x01\x00\x00\xaa\xbb"
    with pytest.raises(ProtocolError):
        build_mic_frame(0, bytes(CHUNK_BYTES + 1), strict=False)


# ── `/control`-Parser und LED-Serializer (Kontext E11/E44) ────────────────
def test_parse_control_message_accepts_str_bytes_and_mapping() -> None:
    expected = {"type": "ping", "id": 7}
    assert parse_control_message('{"type":"ping","id":7}') == expected
    assert parse_control_message(b'{"type":"ping","id":7}') == expected
    assert parse_control_message(expected) == expected
    with pytest.raises(ProtocolError):
        parse_control_message("[1,2,3]")
    # Randfall (P2.T1): ungültiges UTF-8 in Bytes wird als `UnicodeDecodeError`
    # (Subklasse von `ValueError`) durchgereicht und **nicht** als
    # `ProtocolError` umhüllt. Festgehalten, aber `app/protocol.py` (P2.T0)
    # bleibt unangetastet; der Befund steht in `STATE.md` §3/§5.
    with pytest.raises(ValueError) as excinfo:
        parse_control_message(b"\xff\xfe")
    assert isinstance(excinfo.value, UnicodeDecodeError)
    assert not isinstance(excinfo.value, ProtocolError)


def test_led_serializers_are_json_not_binary() -> None:
    assert serialize_ping(7) == {"type": "ping", "id": 7}
    anim = {"pattern": "spin", "colors": [[210, 45, 0]], "periodMs": 80}
    assert serialize_led_anim(anim) == {"type": CTRL_LED_ANIM, "anim": anim}
    leds = [{"id": i, "r": 0, "g": 0, "b": 0} for i in range(NUM_LEDS)]
    msg = serialize_leds(leds, listening=True)
    assert msg == {"type": CTRL_LEDS, "leds": leds, "listening": True}
    with pytest.raises(ProtocolError):
        serialize_leds(leds[: NUM_LEDS - 1])
