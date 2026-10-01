"""L2-Hauptsuite: kompletter Turn gegen den **echten** Manager-Subprozess (P9.T4, `PLAN.md:542`).

Dies ist der **End-to-End-Beweis** des Testsystems: ein **Fake-Echo-Dot**
(`tests/fakes/fake_echomuse.py`, echter WS-Client) spielt die Geräteseite, die
**Fake-Wyoming**-Server (STT/TTS, echte asyncio-TCP-Server) und die
**Fake-HA/LLM**-Stubs (In-Process-uvicorn in Threads) bedienen die Manager-
Clients, und dazwischen läuft der **echte** `python -m app.main`-Subprozess
(uvicorn, Port `18770`) — genau wie in Produktion, nur mit Fakes statt `.123`.

Alles läuft lokal auf `.22`, **kein Docker, kein `.123`, kein echtes
Piper/Whisper/HA/LLM**. Der Marker `integration` (L2) hebt die E36-Netzsperre
für den Loopback-Verkehr auf (`tests/conftest.py`).

**Verdrahtung des Managers mit den Fakes** (E80-Linie): Die Fakes werden
zuerst gestartet; ihre real gebundenen Ports gehen als `env_overrides` in
`start_manager_process()` (`WHISPER_HOST/PORT`, `PIPER_HOST/PORT`,
`HA_BASE_URL`/`HA_TOKEN`, `LLM_BASE_URL`/`LLM_API_KEY`). Der Manager spricht
damit ausschließlich die Fakes an. `TURN_NO_SPEECH_SECONDS=60` macht den
**Hard-Cap (15 s)** zum wirksamen Abbruchgrund (sonst gewinnt das 8-s-Fenster);
`WS_PING_INTERVAL=1`/`WS_PING_TIMEOUT=2` verkürzen den Keepalive für den
`mark_dead`-Test.

**Wake-Trigger (E66):** Der Manager läuft im venv **ohne** onnxruntime/
openwakeword; `on_mic_frame` ruft zwar immer `wake.process()`, der Fehler wird
vom `/data`-Leser aber pro Frame gefangen (`ws_server.py:651-661`) — die
Mic-Sequenz/der Turn-Puffer bleiben intakt. Der Wake/Turn wird deshalb über die
**Test-Hooks** `/internal/wake`, `/internal/vad_end`, `/internal/no_speech`
ausgelöst (nur bei `ENABLE_TEST_HOOKS=true`, E66).

**R1 behoben (P9.T4-Nachtrag):** `app/main.py` baut den `Router` in der
Kompositionswurzel und startet `JevClient`/`DeepSeekClient` in der Lifespan
(und stoppt sie beim Shutdown) — analog zum HA-Client.  Damit läuft der
COMMAND-Pfad Jev → DeepSeek → HA wirklich durch (kein `JEV_ERROR`-Fallback
mehr).  `test_command_turn_reaches_ha_service_call` prüft das end-to-end; die
Szenarien 1–10 prüfen die davon unabhängigen, belegbaren Verträge (Protokoll,
Audio, Latenz, Zustandsfolge, „kein Service-Call").
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Iterator

import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from tests.conftest import (
    ManagerProcess,
    start_manager_process,
    stop_manager_process,
)
from tests.fakes.fake_echomuse import FakeEchoDot
from tests.fakes.fake_services import FakeHaServer, FakeOpenAiServer
from tests.fakes.fake_wyoming import FakeSttServer, FakeTtsServer

pytestmark = pytest.mark.integration

#: Eigener Manager-Port (der Session-`manager_proc` nutzt 18767).
MANAGER_PORT: Final[int] = 18770
#: Laut `build_speaker_frame`: `0x02` + 4096 B (`app/protocol.py:331-342`).
SPEAKER_FRAME_BYTES: Final[int] = 4097
SPEAKER_FRAME_TYPE: Final[int] = 0x02
SPEAKER_EOS: Final[bytes] = b"\x03"
#: Verifizierte Entity des Fake-HA-Caches (`tests/fakes/fake_services.py`).
HA_ENTITY: Final[str] = "light.wohnzimmer"
#: Wörtlicher v4-§7.2-Text (als Literal geprüft, E56/E59).
FALLBACK_NOT_UNDERSTOOD: Final[str] = "Ich habe dich nicht verstanden."
#: DeepSeek-Antwort für den COMMAND-Turn: valide Entity aus dem Fake-HA-Cache
#: (`light.wohnzimmer`, `tests/fakes/fake_services.py`) + Service.  Der Fake
#: antwortet sonst mit `{"antwort","grad"}` (Frage-Form) — für den
#: Service-Call-Beweis braucht der Router eine kommando-förmige Antwort.
COMMAND_JSON: Final[str] = (
    '{"entity_id":"light.wohnzimmer","service":"turn_on",'
    '"response_text":"Wohnzimmer eingeschaltet."}'
)


class _LoopThread:
    """Dauerhafter Event-Loop in einem Daemon-Thread für die Wyoming-Fakes.

    `FakeSttServer`/`FakeTtsServer` sind echte `asyncio.start_server` und
    müssen über die gesamte Modullaufzeit in einem laufenden Loop leben (ein
    `asyncio.run()` pro Test würde sie mit dem Loop beenden). Der Test-Prozess
    spricht sie über Loopback-TCP an; der Fake-Dot läuft dagegen im Test-Loop.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="l2-loop", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def call(self, coro: Any, timeout: float = 20.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10.0)
        try:
            self.loop.close()
        except Exception:  # noqa: BLE001 - Teardown best-effort.
            pass


@dataclass
class L2:
    """Alles, was ein Test gegen den Manager + die Fakes braucht."""

    manager: ManagerProcess
    ha: FakeHaServer
    llm: FakeOpenAiServer
    stt: FakeSttServer
    tts: FakeTtsServer
    loop: _LoopThread
    latency_ms: float | None = None

    @property
    def base_uri(self) -> str:
        return f"ws://127.0.0.1:{self.manager.port}"


@pytest.fixture(scope="module")
def l2() -> Iterator[L2]:
    """Fakes + **echter Manager-Subprozess** auf `MANAGER_PORT` (L2, P9.T4)."""
    loop = _LoopThread()
    loop.start()
    stt = FakeSttServer()
    tts = FakeTtsServer()
    ha = FakeHaServer()
    llm = FakeOpenAiServer()
    manager: ManagerProcess | None = None
    try:
        loop.call(stt.start())
        loop.call(tts.start())
        loop.call(ha.start())
        loop.call(llm.start())
        env = {
            "WHISPER_HOST": "127.0.0.1",
            "WHISPER_PORT": str(stt.port),
            "PIPER_HOST": "127.0.0.1",
            "PIPER_PORT": str(tts.port),
            **ha.env_overrides(),
            **llm.env_overrides(),
            "JEV_MODE": "intent",
            "TURN_NO_SPEECH_SECONDS": "60",
            "WS_PING_INTERVAL": "1",
            "WS_PING_TIMEOUT": "2",
        }
        manager = start_manager_process(port=MANAGER_PORT, env_overrides=env)
        yield L2(manager=manager, ha=ha, llm=llm, stt=stt, tts=tts, loop=loop)
    finally:
        if manager is not None:
            stop_manager_process(manager)
        for server in (llm, ha, stt, tts):
            try:
                loop.call(server.stop())
            except Exception:  # noqa: BLE001 - Teardown best-effort.
                pass
        loop.stop()


# ── Helfer (async, laufen im Test-Loop) ───────────────────────────────────
async def _post(client: httpx.AsyncClient, path: str, **params: Any) -> dict[str, Any]:
    response = await client.post(path, params=params)
    response.raise_for_status()
    return response.json()


async def _status(client: httpx.AsyncClient, device_id: str) -> str | None:
    response = await client.get("/internal/status")
    response.raise_for_status()
    return response.json()["states"].get(device_id)


async def _new_dot(l2: L2, device_id: str) -> FakeEchoDot:
    dot = FakeEchoDot(l2.base_uri, device_id=device_id)
    await dot.start()
    return dot


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _wait_state(
    client: httpx.AsyncClient, device_id: str, state: str, *, timeout: float = 15.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _status(client, device_id) == state:
            return True
        await asyncio.sleep(0.02)
    return False


async def _arm_turn(
    client: httpx.AsyncClient,
    dot: FakeEchoDot,
    device_id: str,
    *,
    frames: int = 3,
) -> float:
    """Wake ⇒ LISTENING, Mic-Frames, VAD-End. Liefert den Latenz-Start (t0)."""
    await _post(client, "/internal/wake", device_id=device_id, score=0.95)
    await dot.send_mic_frames(frames)
    t0 = time.perf_counter()
    await _post(client, "/internal/vad_end", device_id=device_id)
    return t0


def _speaker_frames(dot: FakeEchoDot) -> list[Any]:
    return [ev for ev in dot.events if ev.kind == "speaker_frame"]


def _eos_frames(dot: FakeEchoDot) -> list[Any]:
    return [ev for ev in dot.events if ev.kind == "speaker_eos"]


# ── Szenario 1: Handshake-Reihenfolge exakt (E67) ─────────────────────────
def test_handshake_order_ack_config_mic_start(l2: L2) -> None:
    """`register` ⇒ `ack`→`config`(44)→`config`(listeningAnim)→`mic_start` (E67)."""

    async def _body() -> None:
        dot = await _new_dot(l2, "hs-dot")
        try:
            assert dot.handshake_events == ["ack", "config", "config", "mic_start"]
            assert dot.normalized_handshake == ("ack", "config", "mic_start")
            # K1/E27: `ack` wörtlich 2 Keys, kein `features`.
            assert dot.ack is not None and set(dot.ack) == {"type", "device_id"}
            assert dot.ack_features_present is False
            # config-Push (44 Felder) + schlanker 2-Key-`listeningAnim`-Push.
            assert sorted(len(c) for c in dot.received_configs) == [2, 44]
            anim = [c for c in dot.received_configs if len(c) == 2][0]
            assert set(anim) == {"type", "listeningAnim"}
            assert anim["type"] == "config"
            assert anim["listeningAnim"]["pattern"] == "solid"
            assert anim["listeningAnim"]["listening"] is True
            # K3: permanentes `mic_start`, ohne `lock_mic`.
            assert dot.received_mic_start == {"type": "mic_start"}
        finally:
            await dot.stop()

    asyncio.run(_body())


# ── Szenario 2: kompletter Turn mit Latenz + Audio-Header ─────────────────
def test_full_turn_latency_and_audio_header(l2: L2) -> None:
    """Voller Turn: STT→Router→TTS, `0x02`-Header `[0x02]+4096`, `0x03`-EOS."""

    async def _body() -> None:
        dot = await _new_dot(l2, "cmd-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                t0 = await _arm_turn(client, dot, "cmd-dot")
                ok = await _wait_until(lambda: dot.received_count("speaker_eos") >= 1)
                l2.latency_ms = (time.perf_counter() - t0) * 1000.0
                assert ok, dot.transcript()
                assert "transcribe" in l2.stt.received_events
                assert l2.tts.received_events and l2.tts.received_events[0] == "synthesize"
                assert l2.tts.last_text  # nicht leerer Piper-Text
                assert (l2.tts.last_voice or {}).get("name") == "de_DE-thorsten-high"
                # Audio-Header: jedes Speaker-Frame `0x02` + exakt 4096 B, EOS `0x03`.
                frames = _speaker_frames(dot)
                assert frames, dot.transcript()
                for ev in frames:
                    assert ev.raw is not None
                    assert ev.raw[0] == SPEAKER_FRAME_TYPE
                    assert len(ev.raw) == SPEAKER_FRAME_BYTES
                    assert ev.pcm is not None and len(ev.pcm) == SPEAKER_FRAME_BYTES - 1
                eos = _eos_frames(dot)
                assert eos and eos[0].raw == SPEAKER_EOS
                assert await _wait_state(client, "cmd-dot", "idle")
        finally:
            await dot.stop()

    asyncio.run(_body())
    assert l2.latency_ms is not None and l2.latency_ms > 0
    print(f"[P9.T4] COMMAND-Turn-Latenz (VAD-End → 0x03): {l2.latency_ms:.1f} ms")


# ── Szenario 3: QUESTION-Turn ⇒ kein HA-Service-Call ──────────────────────
def test_question_turn_makes_no_service_call(l2: L2) -> None:
    """Kein HA-Service-Call; der Turn endet dennoch mit TTS-Audio."""
    before = l2.ha.service_call_count()

    async def _body() -> None:
        dot = await _new_dot(l2, "q-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                i_before = len(l2.tts.received_events)
                await _arm_turn(client, dot, "q-dot")
                assert await _wait_until(lambda: dot.received_count("speaker_eos") >= 1)
                assert len(l2.tts.received_events) > i_before
                assert await _wait_state(client, "q-dot", "idle")
        finally:
            await dot.stop()

    asyncio.run(_body())
    assert l2.ha.service_call_count() == before


# ── Szenario 4: Allowlist-Verletzung ⇒ kein Service-Call ──────────────────
def test_allowlist_violation_makes_no_service_call(l2: L2) -> None:
    """Die Entity wird nicht aufgelöst/abgelehnt ⇒ kein HA-Service-Call."""
    before = l2.ha.service_call_count()

    async def _body() -> None:
        dot = await _new_dot(l2, "allow-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                await _arm_turn(client, dot, "allow-dot")
                assert await _wait_until(lambda: dot.received_count("speaker_eos") >= 1)
                assert l2.tts.last_text == FALLBACK_NOT_UNDERSTOOD
                assert await _wait_state(client, "allow-dot", "idle")
        finally:
            await dot.stop()

    asyncio.run(_body())
    assert l2.ha.service_call_count() == before


# ── Szenario 5: Gate-Unterschreitung ⇒ kein Service-Call ──────────────────
def test_confidence_gate_underrun_makes_no_service_call(l2: L2) -> None:
    """Confidence unter dem Gate ⇒ kein HA-Service-Call, definierter Fallback."""
    before = l2.ha.service_call_count()

    async def _body() -> None:
        dot = await _new_dot(l2, "gate-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                await _arm_turn(client, dot, "gate-dot")
                assert await _wait_until(lambda: dot.received_count("speaker_eos") >= 1)
                assert l2.tts.last_text == FALLBACK_NOT_UNDERSTOOD
        finally:
            await dot.stop()

    asyncio.run(_body())
    assert l2.ha.service_call_count() == before


# ── Szenario 6: `0x05`-Stille ⇒ kein TTS, kein Fehlertext (E6) ────────────
def test_no_speech_is_silent(l2: L2) -> None:
    """`0x05` beendet den Turn **still** (kein TTS/Fehlertext, E6)."""
    before = l2.ha.service_call_count()

    async def _body() -> None:
        dot = await _new_dot(l2, "silence-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                i_before = len(l2.tts.received_events)
                await _post(client, "/internal/wake", device_id="silence-dot", score=0.95)
                await dot.send_mic_frames(2)
                await dot.send_no_speech()
                assert await _wait_state(client, "silence-dot", "idle")
                assert dot.received_count("speaker_frame") == 0
                assert dot.received_count("speaker_eos") == 0
                assert len(l2.tts.received_events) == i_before  # kein TTS-Aufruf
                assert "led_anim" in dot.received_kinds()  # Silence-LED
        finally:
            await dot.stop()

    asyncio.run(_body())
    assert l2.ha.service_call_count() == before


# ── Szenario 7: Hard-Cap 15 s bricht den Turn ab ──────────────────────────
def test_hard_cap_aborts_turn(l2: L2) -> None:
    """Ohne Turn-Ende bricht der **15-s-Hard-Cap** ab (kein TTS, `IDLE`)."""
    before = l2.ha.service_call_count()

    async def _body() -> None:
        dot = await _new_dot(l2, "cap-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=40.0) as client:
                await _post(client, "/internal/wake", device_id="cap-dot", score=0.95)
                await dot.send_mic_frames(2)
                t0 = time.perf_counter()
                assert await _wait_until(
                    lambda: "Timeout (hard_cap" in l2.manager.log_text(), timeout=20.0
                ), l2.manager.log_text()[-2000:]
                elapsed = time.perf_counter() - t0
                assert 13.0 <= elapsed <= 20.0, elapsed
                assert await _wait_state(client, "cap-dot", "idle")
                assert dot.received_count("speaker_eos") == 0
        finally:
            await dot.stop()

    asyncio.run(_body())
    assert l2.ha.service_call_count() == before


# ── Szenario 8: Barge-in ⇒ speaker_flush + neuer Turn ─────────────────────
def test_barge_in_flushes_and_starts_new_turn(l2: L2) -> None:
    """Wake im `SPEAKING` ⇒ `speaker_flush` + neuer Turn (erneut TTS/EOS)."""

    async def _body() -> None:
        dot = await _new_dot(l2, "barge-dot")
        l2.tts.chunk_latency = 0.3  # verlangsamt TTS ⇒ SPEAKING-Fenster
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                await _arm_turn(client, dot, "barge-dot")
                assert await _wait_state(client, "barge-dot", "speaking", timeout=10.0), (
                    l2.manager.log_text()[-2000:]
                )
                configs_before = dot.received_count("config")
                await _post(client, "/internal/wake", device_id="barge-dot", score=0.95)
                assert await _wait_until(
                    lambda: "speaker_flush" in dot.received_kinds()
                ), dot.transcript()
                # Neuer Turn: erst nach Mic-Frames + VAD-End kommt ein EOS.
                await dot.send_mic_frames(2)
                await _post(client, "/internal/vad_end", device_id="barge-dot")
                assert await _wait_until(lambda: dot.received_count("speaker_eos") >= 1)
                assert dot.received_count("config") > configs_before  # neuer Turn-Start
        finally:
            l2.tts.chunk_latency = 0.0
            await dot.stop()

    asyncio.run(_body())


# ── Szenario 9: Mic-Lücke/Seq-Resync ──────────────────────────────────────
def test_mic_gap_counted_and_resynced(l2: L2) -> None:
    """Eine injizierte Sequenzlücke wird erkannt und re-synchronisiert (E28)."""

    async def _body() -> None:
        dot = await _new_dot(l2, "gap-dot")
        try:
            assert await _wait_until(
                lambda: "Datenverbindung etabliert: gap-dot" in l2.manager.log_text()
            )
            marker = len(l2.manager.log_text())
            await dot.send_mic_frames(2)
            dot.inject_gap(3)
            await dot.send_mic_frames(1)
            assert await _wait_until(
                lambda: "Mic-Lücke bei gap-dot" in l2.manager.log_text()[marker:]
            ), l2.manager.log_text()[marker:]
            # Folgeframe lückenlos ⇒ keine weitere Warnung.
            await dot.send_mic_frames(1)
            await asyncio.sleep(0.3)
            tail = l2.manager.log_text()[marker:]
            assert tail.count("Mic-Lücke bei gap-dot") == 1, tail
        finally:
            await dot.stop()

    asyncio.run(_body())


# ── Szenario 10: Reconnect nach `mark_dead` ───────────────────────────────
def test_reconnect_after_mark_dead(l2: L2) -> None:
    """Pong-Verweigerer ⇒ Close 1011 + Registry-Räumung; danach Connect + Handshake."""

    async def _body() -> None:
        uri = f"ws://127.0.0.1:{l2.manager.port}/control"
        close_code: int | None = None
        async with ws_connect(uri, proxy=None, open_timeout=10.0) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "dead-dot",
                        "version": "v2.15.0-fake",
                        "capabilities": ["mic"],
                    }
                )
            )
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=8.0)
            except ConnectionClosed as exc:
                close_code = exc.rcvd.code if exc.rcvd is not None else None
            except asyncio.TimeoutError:
                close_code = None
        assert close_code == 1011, close_code

        async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
            deadlines = time.monotonic() + 10.0
            devices = -1
            while time.monotonic() < deadlines:
                response = await client.get("/health")
                devices = response.json().get("devices", -1)
                if devices == 0:
                    break
                await asyncio.sleep(0.05)
            assert devices == 0, devices
            # Der `mark_dead`-Pfad ist belegt; ein frischer Dot verbindet sich wieder.
            dot = await _new_dot(l2, "re-dot")
            try:
                assert dot.normalized_handshake == ("ack", "config", "mic_start")
                assert dot.received_mic_start is not None
            finally:
                await dot.stop()

    asyncio.run(_body())


# ── COMMAND-Turn erreicht den HA-Service-Call (R1 behoben, E84) ───────────
def test_command_turn_reaches_ha_service_call(l2: L2) -> None:
    """Der eigentliche COMMAND-Beweis: Jev ``noul=0.97`` → DeepSeek-Entity → HA.

    R1 (LLM-Clients nie ``.start()``ed) ist behoben (P9.T4-Nachtrag): der
    Manager startet `JevClient`/`DeepSeekClient` in der Lifespan, sodass der
    COMMAND-Pfad Jev → DeepSeek → HA wirklich durchläuft.  Der Fake-LLM muss
    dafür eine kommando-förmige Chat-Antwort liefern (Default ist die
    Frage-Form); die Antwort wird nach dem Turn restauriert.
    """
    before = l2.ha.service_call_count()
    previous_chat = l2.llm.chat_content
    l2.llm.chat_content = COMMAND_JSON

    async def _body() -> None:
        dot = await _new_dot(l2, "svc-dot")
        try:
            async with httpx.AsyncClient(base_url=l2.manager.base_url, timeout=30.0) as client:
                await _arm_turn(client, dot, "svc-dot")
                assert await _wait_until(lambda: dot.received_count("speaker_eos") >= 1)
        finally:
            await dot.stop()

    try:
        asyncio.run(_body())
        assert l2.llm.systemone_requests > 0 and l2.llm.chat_requests > 0
        assert l2.ha.service_call_count() == before + 1
        assert l2.ha.service_calls[-1]["entity_id"] == HA_ENTITY
        assert l2.ha.service_calls[-1]["service"] == "turn_on"
    finally:
        l2.llm.chat_content = previous_chat
