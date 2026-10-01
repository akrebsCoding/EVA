"""L1-Component-Tests des Fake-Echo-Dots (P9.T1, `PLAN.md:539`, §7.1 L1).

Prüfling: `tests/fakes/fake_echomuse.py`.  Der Fake ist ein echter
WebSocket-Client; als Gegenstelle dient ein **In-Process-Stub-Manager**
(`StubManager`) auf `127.0.0.1` mit ephemerem Port — kein echtes Gerät, kein
`.123`.  Der Stub verwendet die **echten** Serializer aus `app.protocol`, damit
der Test an der Wire-Grenze prüft (nicht an einer Attrappe).

Marker `component` (L1) — nur dieser Marker hebt die E36-Netzsperre für den
Test-Prozess gezielt auf (localhost).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from websockets.asyncio.server import serve

from app.protocol import (
    MIC_FRAME_BYTES,
    MicFrame,
    build_no_speech_frame,
    build_speaker_eos,
    build_speaker_frame,
    build_vad_end_frame,
    parse_device_data_frame,
    serialize_ack,
    serialize_config,
    serialize_led_anim,
    serialize_leds,
    serialize_listening_anim,
    serialize_mic_start,
    serialize_mic_stop,
    serialize_speaker_flush,
)
from tests.fakes.fake_echomuse import (
    CONTROL_PATH,
    DATA_PATH,
    DEFAULT_IDENTIFY_TYPE,
    FakeEchoDot,
    FakeEchoDotError,
    Plane,
    Scenario,
    ScenarioError,
)

pytestmark = pytest.mark.component


# ── In-Process-Stub-Manager (Gegenstelle, echte Serializer) ────────────────
class StubManager:
    """Minimaler Controller: Handshake + Mitschnitt (kein echter Manager)."""

    def __init__(self, *, features_in_ack: bool = False, listening_anim: bool = True) -> None:
        self.features_in_ack = features_in_ack
        self.listening_anim = listening_anim
        self.received_control: list[dict[str, Any]] = []
        self.received_data: list[bytes] = []
        self.mic_frames: list[MicFrame] = []
        self.register: dict[str, Any] | None = None
        self.identify: dict[str, Any] | None = None
        self._server: Any | None = None
        self._control_ws: Any | None = None
        self._data_ws: Any | None = None
        self.port: int | None = None

    @property
    def base_uri(self) -> str:
        assert self.port is not None, "StubManager nicht gestartet"
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, connection: Any) -> None:
        path = connection.request.path
        if path == CONTROL_PATH:
            self._control_ws = connection
            await self._serve_control(connection)
        elif path == DATA_PATH:
            self._data_ws = connection
            await self._serve_data(connection)
        else:
            await connection.close(code=1008)

    async def _serve_control(self, connection: Any) -> None:
        first = json.loads(await connection.recv())
        if first.get("type") != "register":
            await connection.close(code=1008)
            return
        self.register = first
        ack = serialize_ack(first["device_id"])
        if self.features_in_ack:
            ack["features"] = ["mic"]
        await connection.send(json.dumps(ack))
        await connection.send(json.dumps(serialize_config({"owwOnDevice": False})))
        if self.listening_anim:
            await connection.send(json.dumps(serialize_listening_anim({"pattern": "solid"})))
        await connection.send(json.dumps(serialize_mic_start()))
        async for raw in connection:
            self.received_control.append(json.loads(raw))

    async def _serve_data(self, connection: Any) -> None:
        first = json.loads(await connection.recv())
        self.identify = first
        async for raw in connection:
            blob = bytes(raw)
            self.received_data.append(blob)
            frame = parse_device_data_frame(blob)
            if isinstance(frame, MicFrame):
                self.mic_frames.append(frame)

    async def wait_connected(self, *, timeout: float = 2.0) -> None:
        await _wait_until(
            lambda: self._control_ws is not None and self._data_ws is not None,
            timeout=timeout,
        )

    async def push_control(self, message: dict[str, Any]) -> None:
        await _wait_until(lambda: self._control_ws is not None)
        await self._control_ws.send(json.dumps(message))

    async def push_data(self, raw: bytes) -> None:
        await _wait_until(lambda: self._data_ws is not None)
        await self._data_ws.send(bytes(raw))


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.01
) -> None:
    """Auf eine Bedingung warten (nur Synchronisation, keine Fachlogik-Wartezeit)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("Bedingung nicht binnen Timeout erfüllt")
        await asyncio.sleep(interval)


def _run(coro: Any) -> Any:
    """Synchroner pytest-Test-Wrapper (kein `pytest-asyncio` im Projekt)."""
    return asyncio.run(coro)


@pytest.fixture
def stub() -> StubManager:
    """Frischer StubManager je Test (Port ephemär)."""
    return StubManager()


# ── 1. Handshake: ack ohne features → config → mic_start ──────────────────
def test_handshake_order_ack_without_features_config_mic_start(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-handshake")
        try:
            handshake = await dot.start()
            assert handshake == ("ack", "config", "mic_start")
            assert dot.ack == {"type": "ack", "device_id": "dot-handshake"}
            assert dot.ack_features_present is False
            assert len(dot.received_configs) >= 1
            assert dot.received_configs[0]["type"] == "config"
            assert dot.received_mic_start == {"type": "mic_start"}
            assert stub.register is not None
            assert stub.register["type"] == "register"
            assert stub.register["device_id"] == "dot-handshake"
            assert stub.register["capabilities"] == ["mic", "speaker", "leds"]
            await _wait_until(lambda: stub.identify is not None)
            assert stub.identify == {"type": DEFAULT_IDENTIFY_TYPE, "device_id": "dot-handshake"}
            # Config-Push (E67: der schlanke listeningAnim-Push ist selbst `config`)
            assert dot.received_count("config") >= 2
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


# ── 2. K1/E27: `features` im ack wird verworfen/abgelehnt ─────────────────
def test_ack_with_features_is_rejected(stub: StubManager) -> None:
    async def scenario() -> None:
        stub.features_in_ack = True
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-features")
        try:
            with pytest.raises(FakeEchoDotError, match="features"):
                await dot.start()
            assert dot.ack_features_present is True
            assert dot.received_count("ack") == 1
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


# ── 3. Mic-Frames: 2563 B, Header, monotone Sequenz, Wrap ─────────────────
def test_mic_frames_2563_bytes_monotone_sequence_and_wrap(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-mic")
        try:
            await dot.start()
            seqs = await dot.send_mic_frames(3)
            await _wait_until(lambda: len(stub.mic_frames) == 3)

            assert seqs == [0, 1, 2]
            assert [frame.seq for frame in stub.mic_frames] == [0, 1, 2]
            assert all(len(frame.pcm) == 2560 for frame in stub.mic_frames)
            assert all(len(raw) == MIC_FRAME_BYTES for raw in stub.received_data)
            # Header `[0x01][seq_hi][seq_lo]`, uint16 big-endian (K2/E28)
            sent = dot.mic_frames_sent()
            assert sent[0][0] == 0x01
            assert sent[0][1:3] == b"\x00\x00"
            assert sent[1][1:3] == b"\x00\x01"
            assert sent[2][1:3] == b"\x00\x02"
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


def test_sequence_wraps_uint16(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-wrap", start_sequence=0xFFFE)
        try:
            await dot.start()
            seqs = await dot.send_mic_frames(3)
            assert seqs == [0xFFFE, 0xFFFF, 0x0000]
            assert [f.seq for f in dot.sent_mic] == [0xFFFE, 0xFFFF, 0x0000]
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


# ── 4. Injizierbare Sequenz-Lücke (Resync-Test) ──────────────────────────
def test_injectable_sequence_gap(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-gap")
        try:
            await dot.start()
            assert await dot.send_mic_frames(2) == [0, 1]
            dot.inject_gap(2)
            assert await dot.send_mic_frames(1) == [4]
            await _wait_until(lambda: len(stub.mic_frames) == 3)
            assert [frame.seq for frame in stub.mic_frames] == [0, 1, 4]
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


def test_inject_gap_rejects_non_positive() -> None:
    dot = FakeEchoDot("ws://127.0.0.1:9")
    with pytest.raises(FakeEchoDotError):
        dot.inject_gap(0)


# ── 5. Mitschnitt aller empfangenen Frames ────────────────────────────────
def test_records_all_received_frames(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-record")
        try:
            await dot.start()
            await stub.wait_connected()
            await stub.push_data(build_speaker_frame(bytes(4096)))
            await stub.push_data(build_speaker_eos())
            await stub.push_control(serialize_speaker_flush())
            await stub.push_control(serialize_mic_stop())
            await stub.push_control(serialize_leds([{"id": i, "r": 0, "g": 0, "b": 0} for i in range(12)]))
            await stub.push_control(serialize_led_anim({"pattern": "pulse"}))
            await _wait_until(
                lambda: dot.received_count("speaker_frame") == 1
                and dot.received_count("speaker_eos") == 1
                and dot.received_count("speaker_flush") == 1
                and dot.received_count("mic_stop") == 1
                and dot.received_count("leds") == 1
                and dot.received_count("led_anim") == 1
            )
            kinds = set(dot.received_kinds())
            assert {
                "ack",
                "config",
                "mic_start",
                "speaker_frame",
                "speaker_eos",
                "speaker_flush",
                "mic_stop",
                "leds",
                "led_anim",
            } <= kinds
            # Ebenen korrekt getrennt
            assert "speaker_frame" in dot.received_kinds(Plane.DATA)
            assert "led_anim" in dot.received_kinds(Plane.CONTROL)
            speaker = next(ev for ev in dot.events if ev.kind == "speaker_frame")
            assert len(speaker.pcm or b"") == 4096
            transcript = dot.transcript()
            assert any("speaker_frame" in line for line in transcript)
            assert any("led_anim" in line for line in transcript)
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


# ── 6. Kommandos: 0x04 / 0x05 / button ───────────────────────────────────
def test_button_and_turn_sentinels_are_sent(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-cmd")
        try:
            await dot.start()
            await dot.send_button(click_type=138, down=False, held_ms=0, muted=False)
            await dot.send_vad_end()
            await dot.send_no_speech()
            await _wait_until(lambda: len(stub.received_control) >= 1 and len(stub.received_data) >= 2)

            button = stub.received_control[0]
            assert button["type"] == "button"
            assert button["clickType"] == 138
            assert button["down"] is False
            # 4-Byte-Sentinels byte-exakt (E28)
            assert stub.received_data[0] == build_vad_end_frame() == b"\x01\x00\x00\x04"
            assert stub.received_data[1] == build_no_speech_frame() == b"\x01\x00\x00\x05"
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


# ── 7. Szenario-JSON: Replay + Assert ────────────────────────────────────
def test_scenario_json_replay_and_assert(stub: StubManager) -> None:
    async def scenario() -> None:
        await stub.start()
        dot = FakeEchoDot(stub.base_uri, device_id="dot-scenario")
        blueprint = Scenario.from_json(
            json.dumps(
                {
                    "name": "button-vad",
                    "send": [{"kind": "button", "clickType": 138, "down": False}, {"kind": "vad_end"}],
                    "expect_handshake": ["ack", "config", "mic_start"],
                    "expect_received": ["ack", "config", "mic_start"],
                    "expect_ack_features": False,
                    "mic_frames": 2,
                }
            )
        )
        try:
            await dot.run_scenario(blueprint)  # Replay + Assert (wirft nicht)
            assert Scenario.from_json(blueprint.to_json()) == blueprint
            await _wait_until(lambda: len(stub.received_control) >= 1 and len(stub.mic_frames) == 2)
            assert stub.received_control[0]["clickType"] == 138

            bad = Scenario.from_json(
                json.dumps({"name": "bad", "expect_received": ["speaker_flush"]})
            )
            with pytest.raises(ScenarioError):
                dot.check_scenario(bad)
        finally:
            await dot.stop()
            await stub.stop()

    _run(scenario())


def test_scenario_requires_name() -> None:
    with pytest.raises(ScenarioError):
        Scenario.from_json(json.dumps({"send": []}))
