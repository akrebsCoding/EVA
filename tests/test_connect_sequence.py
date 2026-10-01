"""Connect-Sequenz-Tests (P5.T7, `PLAN.md` §7 → P5.T7, `PLAN.md:478`, Layer **L0/`unit`**).

Prüfling ist `app/device_session.py` (P5.T5).  Getestet wird gegen den **verbindlichen
Vertrag** aus `STATE.md` §3 („Connect-Sequenz (P5.T5)", „WS-Server Teil 1 (P5.T1)") und
die Festlegungen **K1/K3/K4/K6/E11/E25/E27/E28/E44/E67**, nicht gegen eine Wunschfassung.

Auftrag `PLAN.md:478` — genau diese sechs Punkte:

1. **Mock-Gerät** — die Transporte `send_control`/`send_binary` sind **injiziert**, kein
   Netz, kein WebSocket, kein Gerät (`MockDevice`).
2. **Exakte Frame-Reihenfolge** `ack` → `config` → `mic_start` — als **Index-/Listen-
   Vergleich**, nicht nur „alle vorhanden"; mit `listeningAnim` dazwischen nach **E67**
   (`{"type":"config","listeningAnim":{…}}`, kein Typname `listeningAnim`).
3. **Button-Sequenz** exakt `mic_stop` → `mic_start{lock_mic:true}` → `mic_stop` →
   `mic_start{}` (K6), inkl. `finally`-Abschluss bei Turn-Fehler.
4. **Kein `features` im Ack** — `serialize_ack` hat wörtlich **2 Keys** (K1/E27); ein
   fremdes `features` wird vom `on_ack` **ignoriert** und **nie** gesendet.
5. **Config-Feldliste == K4** — der gesendete `config`-Push trägt die **44** Top-Level-
   Felder (43 effektive + `type`, E25), **keine Fremdfelder**, und enthält **alle 25**
   K4-Felder als Teilmenge; `owwOnDevice`/`bleProxyEnabled` als **Boolean `False`**
   (E24/K4) — auch wenn die Referenz `"off"` führt.
6. **Sequenz-Re-Sync** nach Lücke (uint16 **big-endian**, Wrap `65535 → 0`, E28) — über
   `SequenceTracker`/`ConnectSequence.observe_mic_sequence` und über echte Mic-Frames,
   die mit `app.protocol.build_mic_frame` gebaut und per `parse_data_frame` gelesen werden.

**Kein echtes Netz, kein Gerät:** die autouse-Netzsperre aus `tests/conftest.py` (E36) ist
aktiv und wird **positiv** belegt (`test_connect_sequence_runs_while_network_block_is_active`).

**Deterministisch:** kein `random`, kein `sleep`; alle Sequenzen sind skriptete Listen.
**Prüfwerte sind Literale** (E56/E59): Ack-Shape, Button-Nachrichten und Feldmengen sind
lokal definiert bzw. gegen die autoritative Datei `docs/device-config-reference.json`
geladen, **nicht** aus dem Prüfling zurückgelesen; so bleiben Mutationen wirksam.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from app.device_session import ConnectSequence, DeviceSessionError, SequenceTracker
from app.protocol import (
    SEQ_MODULUS,
    Ack,
    Direction,
    build_mic_frame,
    parse_data_frame,
)
from tests.conftest import NetworkAccessBlocked

pytestmark = pytest.mark.unit

#: Das Mock-Gerät (siehe Auftrag Punkt 1).
DEV = "dev-connect-01"

#: Die autoritative Referenzdatei (P0.T5) — Quelle der 43 Feldnamen/Ist-Werte.
REFERENCE_JSON: Path = (
    Path(__file__).resolve().parent.parent / "docs" / "device-config-reference.json"
)
REFERENCE: dict[str, Any] = json.loads(REFERENCE_JSON.read_text(encoding="utf-8"))
REFERENCE_FIELDS: tuple[str, ...] = tuple(REFERENCE)

#: Die 25 K4-Felder, unabhängig aus `STATE.md:192` (`PLAN.md` K4) hinterlegt.
EXPECTED_K4: frozenset[str] = frozenset(
    {
        "owwOnDevice",
        "bleProxyEnabled",
        "startupVolume",
        "micGainDb",
        "adcDigitalGain",
        "adcMicpga",
        "aecEnabled",
        "aecDelayMs",
        "aecTailMs",
        "aecRefSource",
        "beamformingEnabled",
        "beamAngle",
        "vadThreshold",
        "vadSpeechMs",
        "vadSilenceMs",
        "ledScene",
        "ledListenColor",
        "ledThinkColor",
        "duckDb",
        "eqBands",
        "eqLoudness",
        "limiterEnabled",
        "limiterThreshold",
        "limiterRelease",
        "agcEnabled",
    }
)

#: Wire-Nachrichten wörtlich (bewusst als Literale, E56/E59).
ACK_2_KEYS = {"type": "ack", "device_id": DEV}
CTRL_MIC_START = {"type": "mic_start"}
CTRL_MIC_START_LOCK = {"type": "mic_start", "lock_mic": True}
CTRL_MIC_STOP = {"type": "mic_stop"}
ANIM_MALEVOLENT = {
    "pattern": "solid",
    "colors": [[110, 0, 45]],
    "listening": True,
    "ttlSec": 30,
}


# ── Fakes (injiziert, kein Netz) ─────────────────────────────────────────
@dataclass
class FakeSettings:
    """Nur die Settings-Attribute, die `device_session`/`device_config` lesen."""

    mic_start_lock_mic_button: bool = True
    oww_model: str = "hey_jarvis_v0.1"
    oww_threshold: float = 0.9
    oww_speex_ns: bool = True
    oww_barge_in_enabled: bool = True
    oww_barge_in_threshold: float = 0.15


class MockDevice:
    """Mock-Gerät: zeichnet alle `/control`-JSON- und `/data`-Binär-Frames auf."""

    def __init__(self) -> None:
        self.control: list[tuple[str, dict[str, Any]]] = []
        self.binary: list[tuple[str, bytes]] = []

    async def send_control(self, device_id: str, message: dict[str, Any]) -> None:
        self.control.append((device_id, dict(message)))

    async def send_binary(self, device_id: str, data: bytes) -> None:
        self.binary.append((device_id, bytes(data)))

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [message for _, message in self.control]

    @property
    def types(self) -> list[str]:
        return [message["type"] for message in self.messages]

    @property
    def device_ids(self) -> list[str]:
        return [device_id for device_id, _ in self.control]


class Harness:
    """Bündelt Mock-Gerät + `ConnectSequence` mit injizierten Transporten."""

    def __init__(
        self,
        *,
        reference: Optional[dict[str, Any]] = None,
        settings: Optional[FakeSettings] = None,
        enable_listening_anim: bool = True,
        listening_anim: Optional[dict[str, Any]] = None,
    ) -> None:
        self.device = MockDevice()
        self.settings = settings if settings is not None else FakeSettings()
        self.cs = ConnectSequence(
            DEV,
            send_control=self.device.send_control,
            send_binary=self.device.send_binary,
            settings_obj=self.settings,
            reference=dict(REFERENCE) if reference is None else reference,
            enable_listening_anim=enable_listening_anim,
            listening_anim=listening_anim,
        )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Exakte Frame-Reihenfolge `ack` → `config` → `mic_start`
# ═══════════════════════════════════════════════════════════════════════════
def test_connect_frame_order_is_ack_config_mic_start() -> None:
    """Vollständiger Handshake als **eine** Ereignisliste, Reihenfolge exakt.

    Der `ack` wird per Vertrag von `app/ws_server.py` gesendet (P5.T1) und ist
    der **Auslöser**; `ConnectSequence` antwortet darauf mit `config` →
    `mic_start`.  Der Test modelliert die kombinierte Handshake-Sicht des
    Geräts und vergleicht die Typen **positional**.
    """
    harness = Harness(enable_listening_anim=False)
    events: list[dict[str, Any]] = [ACK_2_KEYS]  # ws_server-`ack` (K1)

    async def scenario() -> None:
        events.extend(await harness.cs.on_ack(ACK_2_KEYS))

    run(scenario())

    assert [event["type"] for event in events] == ["ack", "config", "mic_start"]
    # Index-Vergleich, nicht nur Mengenvergleich (Auftrag Punkt 2).
    assert events.index(next(e for e in events if e["type"] == "config")) == 1
    assert events.index(next(e for e in events if e["type"] == "mic_start")) == 2


def test_on_ack_returns_config_then_mic_start_without_anim() -> None:
    harness = Harness(enable_listening_anim=False)

    frames = run(harness.cs.on_ack(ACK_2_KEYS))

    assert isinstance(frames, tuple)
    assert len(frames) == 2
    assert [frame["type"] for frame in frames] == ["config", "mic_start"]
    assert frames[0]["type"] == "config"
    assert frames[1] == CTRL_MIC_START
    # `frames` spiegelt exakt die gesendeten Frames (Reihenfolge + Inhalt).
    assert harness.cs.frames == frames
    assert harness.device.types == ["config", "mic_start"]


def test_connect_frame_order_with_listening_anim_config_anim_mic_start() -> None:
    """E67: der Anim-Push ist eine **zweite** `config`-Nachricht zwischen den beiden."""
    harness = Harness()  # Default: Anim-Katalog (ledScene="malevolent") bekannt

    frames = run(harness.cs.on_ack(ACK_2_KEYS))

    assert [frame["type"] for frame in frames] == ["config", "config", "mic_start"]
    # Positional: erster `config` = 44-Feld-Push, zweiter = Anim-Push, dann `mic_start`.
    assert "listeningAnim" not in frames[0]
    assert "listeningAnim" in frames[1]
    assert frames[2] == CTRL_MIC_START
    assert harness.device.types == ["config", "config", "mic_start"]


def test_listening_anim_push_shape_is_two_keys_e67() -> None:
    harness = Harness()

    _config, anim, _mic = run(harness.cs.on_ack(ACK_2_KEYS))

    # E67: autoritativ `{"type":"config","listeningAnim":{…}}`, genau 2 Keys,
    # **kein** Typname `listeningAnim` (nicht die PLAN:476-Shorthand).
    assert set(anim) == {"type", "listeningAnim"}
    assert len(anim) == 2
    assert anim["type"] == "config"
    assert anim["listeningAnim"] == ANIM_MALEVOLENT
    assert "type" not in anim["listeningAnim"]


def test_listening_anim_absent_for_unknown_scene() -> None:
    """Unbekannte Szene ⇒ **kein** erfundener Anim-Push (E11/K7)."""
    harness = Harness(reference=dict(REFERENCE, ledScene="pride"))

    frames = run(harness.cs.on_ack(ACK_2_KEYS))

    assert [frame["type"] for frame in frames] == ["config", "mic_start"]
    assert all("listeningAnim" not in frame for frame in frames)


def test_listening_anim_absent_when_disabled() -> None:
    harness = Harness(enable_listening_anim=False)

    frames = run(harness.cs.on_ack(ACK_2_KEYS))

    assert [frame["type"] for frame in frames] == ["config", "mic_start"]


def test_mic_start_is_permanent_without_lock_mic() -> None:
    """K3: der Connect-`mic_start` ist permanent und **ohne** `lock_mic`."""
    harness = Harness(enable_listening_anim=False)

    frames = run(harness.cs.on_ack(ACK_2_KEYS))

    mic_start = frames[-1]
    assert mic_start == {"type": "mic_start"}
    assert "lock_mic" not in mic_start


# ═══════════════════════════════════════════════════════════════════════════
# 4./5. Ack-Shape (K1/E27) und Config-Feldliste (K4/E25)
# ═══════════════════════════════════════════════════════════════════════════
def test_ack_has_exactly_two_keys_no_features() -> None:
    """K1/E27: `ack` ist wörtlich `{"type":"ack","device_id":…}` — 2 Keys."""
    from app.protocol import serialize_ack

    ack = serialize_ack(DEV)
    assert ack == {"type": "ack", "device_id": DEV}
    assert len(ack) == 2
    assert set(ack) == {"type", "device_id"}
    assert "features" not in ack


def test_on_ack_ignores_extra_features_and_never_emits_ack() -> None:
    """Ein fremdes `features` im Ack ist kein Fehler und wird **nie** gesendet."""
    harness = Harness(enable_listening_anim=False)

    frames = run(
        harness.cs.on_ack({"type": "ack", "device_id": DEV, "features": ["led_anim"]})
    )

    assert [frame["type"] for frame in frames] == ["config", "mic_start"]
    assert all("features" not in frame for frame in frames)
    assert "ack" not in harness.device.types


def test_on_ack_rejects_ack_for_another_device_and_sends_nothing() -> None:
    harness = Harness(enable_listening_anim=False)

    with pytest.raises(DeviceSessionError):
        run(harness.cs.on_ack({"type": "ack", "device_id": "someone-else"}))

    assert harness.device.control == []
    assert harness.cs.frames == ()


@pytest.mark.parametrize(
    "ack",
    [
        {"type": "ack", "device_id": DEV},
        json.dumps({"type": "ack", "device_id": DEV}),
        json.dumps({"type": "ack", "device_id": DEV}).encode("utf-8"),
        Ack(device_id=DEV),
    ],
    ids=["mapping", "str", "bytes", "Ack"],
)
def test_on_ack_accepts_all_contract_input_forms(ack: Any) -> None:
    harness = Harness(enable_listening_anim=False)

    frames = run(harness.cs.on_ack(ack))

    assert [frame["type"] for frame in frames] == ["config", "mic_start"]
    assert harness.device.device_ids == [DEV, DEV]


def test_connect_sends_44_fields_43_plus_type_no_foreign() -> None:
    """K4/E25: der `config`-Push trägt alle 43 effektiven Felder plus `type`."""
    harness = Harness(enable_listening_anim=False)

    config, _mic = run(harness.cs.on_ack(ACK_2_KEYS))

    assert len(config) == 44
    assert config["type"] == "config"
    # Feldmenge exakt = Referenzfelder ∪ {"type"} — kein Fremdfeld.
    assert set(config) == set(REFERENCE_FIELDS) | {"type"}
    assert set(config) - {"type"} == set(REFERENCE_FIELDS)
    assert set(config) - set(REFERENCE_FIELDS) == {"type"}
    json.dumps(config)  # JSON-tauglich


def test_config_contains_all_25_k4_fields() -> None:
    harness = Harness(enable_listening_anim=False)

    config, _mic = run(harness.cs.on_ack(ACK_2_KEYS))

    assert len(EXPECTED_K4) == 25
    assert set(EXPECTED_K4) <= set(config)
    assert EXPECTED_K4 <= set(REFERENCE_FIELDS)


def test_config_forces_oww_on_device_and_ble_proxy_false() -> None:
    """K4/E24: die beiden Pflicht-Overrides sind **Boolean `False`**."""
    harness = Harness(enable_listening_anim=False)

    config, _mic = run(harness.cs.on_ack(ACK_2_KEYS))

    assert REFERENCE["owwOnDevice"] == "off"  # Referenz führt den DB-String
    assert config["owwOnDevice"] is False
    assert config["owwOnDevice"] is not True
    assert config["bleProxyEnabled"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. Button-Sequenz exakt (K6)
# ═══════════════════════════════════════════════════════════════════════════
def test_button_start_sequence_exact_k6() -> None:
    harness = Harness(enable_listening_anim=False)

    frames = run(harness.cs.button_start())

    assert [frame["type"] for frame in frames] == ["mic_stop", "mic_start"]
    assert frames[0] == CTRL_MIC_STOP
    assert frames[1] == CTRL_MIC_START_LOCK


def test_button_end_sequence_exact_k6() -> None:
    harness = Harness(enable_listening_anim=False)

    frames = run(harness.cs.button_end())

    assert [frame["type"] for frame in frames] == ["mic_stop", "mic_start"]
    assert frames[0] == CTRL_MIC_STOP
    assert frames[1] == CTRL_MIC_START  # permanent, ohne lock_mic


def test_button_turn_full_sequence_exact_k6() -> None:
    harness = Harness(enable_listening_anim=False)

    async def turn() -> None:
        # Der Turn-Rumpf läuft **zwischen** Start- und End-Paar.
        turn.seen_frames = harness.cs.frames  # type: ignore[attr-defined]

    result = run(harness.cs.run_button_turn(turn))

    assert harness.device.types == ["mic_stop", "mic_start", "mic_stop", "mic_start"]
    assert harness.device.messages[0] == CTRL_MIC_STOP
    assert harness.device.messages[1] == CTRL_MIC_START_LOCK
    assert harness.device.messages[2] == CTRL_MIC_STOP
    assert harness.device.messages[3] == CTRL_MIC_START
    assert [frame["type"] for frame in result] == [
        "mic_stop",
        "mic_start",
        "mic_stop",
        "mic_start",
    ]
    assert len(turn.seen_frames) == 2  # type: ignore[attr-defined]


def test_button_turn_closes_pair_even_when_turn_raises() -> None:
    """`finally`: das Abschluss-Paar wird auch bei Turn-Fehler gesendet (K6)."""
    harness = Harness(enable_listening_anim=False)

    async def broken_turn() -> None:
        raise ValueError("turn boom")

    with pytest.raises(ValueError, match="turn boom"):
        run(harness.cs.run_button_turn(broken_turn))

    assert harness.device.types == ["mic_stop", "mic_start", "mic_stop", "mic_start"]
    assert harness.device.messages[1] == CTRL_MIC_START_LOCK
    assert harness.device.messages[3] == CTRL_MIC_START


def test_button_lock_mic_follows_settings() -> None:
    """Der `lock_mic`-Wert kommt aus `settings.mic_start_lock_mic_button`."""
    harness = Harness(
        enable_listening_anim=False,
        settings=FakeSettings(mic_start_lock_mic_button=False),
    )

    frames = run(harness.cs.button_start())

    assert frames[0] == CTRL_MIC_STOP
    assert frames[1] == CTRL_MIC_START  # kein lock_mic, weil Settings False
    assert "lock_mic" not in frames[1]


def test_connect_and_button_use_only_control_no_binary() -> None:
    """Die Connect-/Button-Sequenz ist reines `/control`-JSON (P5.T5)."""
    harness = Harness(enable_listening_anim=False)

    run(harness.cs.on_ack(ACK_2_KEYS))
    run(harness.cs.run_button_turn(lambda: asyncio.sleep(0)))

    assert harness.device.binary == []
    assert harness.device.device_ids and set(harness.device.device_ids) == {DEV}


# ═══════════════════════════════════════════════════════════════════════════
# 6. Sequenz-Re-Sync (E28: uint16 big-endian, Wrap 65535 → 0)
# ═══════════════════════════════════════════════════════════════════════════
def test_sequence_tracker_resync_after_gap() -> None:
    tracker = SequenceTracker()

    assert tracker.observe(10) is False  # erster Aufruf synchronisiert nur
    assert tracker.synced is True
    assert tracker.last == 10
    assert tracker.expected == 11
    assert tracker.gaps == 0

    assert tracker.observe(20) is True  # Lücke erkannt + nachgezogen
    assert tracker.last == 20
    assert tracker.expected == 21
    assert tracker.gaps == 1
    assert tracker.resyncs == 1

    assert tracker.observe(21) is False  # wieder synchron
    assert tracker.expected == 22
    assert tracker.gaps == 1


def test_sequence_tracker_wraps_uint16() -> None:
    tracker = SequenceTracker(expected=SEQ_MODULUS - 1)

    assert tracker.observe(65535) is False
    assert tracker.expected == 0  # Wrap 65535 → 0
    assert tracker.observe(0) is False
    assert tracker.expected == 1
    assert tracker.gaps == 0

    assert tracker.observe(5) is True  # Lücke direkt nach dem Wrap
    assert tracker.expected == 6
    assert tracker.gaps == 1


def test_sequence_tracker_rejects_bool_and_non_int() -> None:
    tracker = SequenceTracker()

    with pytest.raises(DeviceSessionError):
        tracker.observe(True)
    with pytest.raises(DeviceSessionError):
        tracker.observe("5")  # type: ignore[arg-type]
    # Ein abgewiesener Aufruf darf den Zähler nicht verändert haben.
    assert tracker.expected is None
    assert tracker.gaps == 0


def test_sequence_tracker_start_validation_and_reset() -> None:
    for bad in (-1, SEQ_MODULUS, 65536):
        with pytest.raises(DeviceSessionError):
            SequenceTracker(expected=bad)

    tracker = SequenceTracker(expected=3)
    assert tracker.observe(3) is False
    assert tracker.observe(9) is True
    tracker.reset()
    assert tracker.synced is False
    assert tracker.expected is None
    assert tracker.last is None
    assert tracker.gaps == 0
    assert tracker.resyncs == 0
    tracker.reset(expected=100)
    assert tracker.expected == 100


def test_mic_sequence_is_uint16_be_and_connect_resyncs() -> None:
    """Re-Sync über echte Wire-Frames: uint16 **big-endian**, Lücke wird gezogen."""
    harness = Harness(enable_listening_anim=False)
    pcm = b"\x00" * 2560

    for seq in (10, 11):
        raw = build_mic_frame(seq, pcm)
        assert raw[1:3] == struct.pack(">H", seq)  # big-endian
        frame = parse_data_frame(raw, Direction.DEVICE_TO_CONTROLLER)
        assert frame is not None and frame.seq == seq
        assert harness.cs.observe_mic_sequence(frame.seq) is False

    assert harness.cs.mic_gaps == 0
    assert harness.cs.sequence.expected == 12

    raw = build_mic_frame(20, pcm)
    frame = parse_data_frame(raw, Direction.DEVICE_TO_CONTROLLER)
    assert frame is not None
    assert harness.cs.observe_mic_sequence(frame.seq) is True
    assert harness.cs.mic_gaps == 1
    assert harness.cs.sequence.expected == 21

    # Kein Fehlalarm bei lückenlosen Folgeframes nach dem Re-Sync.
    for seq in (21, 22):
        assert harness.cs.observe_mic_sequence(seq) is False
    assert harness.cs.mic_gaps == 1


def test_connect_mic_gaps_no_false_positive_on_consecutive_sequence() -> None:
    harness = Harness(enable_listening_anim=False)

    for seq in range(0, 5):
        assert harness.cs.observe_mic_sequence(seq) is False

    assert harness.cs.mic_gaps == 0
    assert harness.cs.sequence.resyncs == 0


# ═══════════════════════════════════════════════════════════════════════════
# Netz-Nachweis (positiv): die Sperre ist aktiv, die Sequenz läuft dennoch
# ═══════════════════════════════════════════════════════════════════════════
def test_connect_sequence_runs_while_network_block_is_active() -> None:
    harness = Harness(enable_listening_anim=False)

    # Die autouse-Netzsperre (E36) blutet hier sichtbar hoch ...
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("device.invalid", 8767)

    async def scenario() -> tuple[dict[str, Any], ...]:
        return await harness.cs.on_ack(ACK_2_KEYS)

    frames = run(scenario())  # ... und die Connect-Sequenz läuft trotzdem durch.

    assert [frame["type"] for frame in frames] == ["config", "mic_start"]
    assert harness.device.types == ["config", "mic_start"]
