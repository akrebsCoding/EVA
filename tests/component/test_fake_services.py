"""L1/L3-Component-Tests der Fake-HA-/LLM-Stubs (P9.T3, `PLAN.md:541`, §7.1).

Prüfling: `tests/fakes/fake_services.py`.  Zwei Ebenen:

* **Roundtrip-Beweis (der eigentliche L3-Beleg):** der **echte**
  `app/ha_client.HomeAssistantClient` bzw. `app/llm_client.JevClient` /
  `DeepSeekClient` sprechen gegen die In-Process-uvicorn-Fakes über echtes
  Loopback-TCP — der Cache füllt sich, der `call_service`-Payload kommt an,
  Werte/Scores/Antwort werden geparst.
* **Server-Eigenschaften:** Aufrufzähler, Auth-Header, Latenz-/HTTP-500-
  Injektion, Lebenszyklus (Port 0, idempotent, sauberes Schließen).

Marker `component` — nur dieser Marker hebt die E36-Netzsperre für den
Test-Prozess gezielt auf (Loopback, E82).  Kein echter HA/LLM, kein `.123`.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.ha_client import (
    HA_UNAVAILABLE_MESSAGE,
    HaUnavailableError,
    HomeAssistantClient,
)
from app.llm_client import (
    DeepSeekClient,
    JevClient,
    LlmUnavailableError,
)

from tests.fakes.fake_services import (
    CHAT_PATH,
    DEFAULT_CHAT_CONTENT,
    DEFAULT_NOUL,
    DEFAULT_TARGET_CHOICE,
    DEFAULT_TARGET_CONFIDENCE,
    SYSTEMONE_PATH,
    FakeHaServer,
    FakeOpenAiServer,
)

pytestmark = pytest.mark.component

#: Fehlertext **wörtlich** (E56: Literal, nicht die Import-Konstante) — hier
#: zusätzlich gegen `HA_UNAVAILABLE_MESSAGE` gegengeprüft.
_HA_ERROR_TEXT = "Home Assistant antwortet nicht."
_TOKEN = "test-token"
_LLM_KEY = "test-llm-key"


def _run(coro: Any) -> Any:
    """Synchroner pytest-Test-Wrapper (kein `pytest-asyncio` im Projekt)."""
    import asyncio

    return asyncio.run(coro)


# ── Fake-HA: Properties ────────────────────────────────────────────────────
def test_fake_ha_lifecycle_and_env_overrides() -> None:
    """Port 0, idempotentes start/stop, Env zeigt auf den Fake."""

    async def scenario() -> None:
        server = FakeHaServer()
        assert server.running is False
        await server.start()
        try:
            assert server.running is True
            assert server.port > 0
            assert server.base_url == f"http://127.0.0.1:{server.port}"
            env = server.env_overrides()
            assert set(env) == {"HA_BASE_URL", "HA_TOKEN"}
            assert env["HA_BASE_URL"] == server.base_url
            assert env["HA_TOKEN"] == "test-ha-token"
        finally:
            await server.stop()
        assert server.running is False
        await server.stop()  # idempotent

    _run(scenario())


# ── Fake-HA: Roundtrip gegen den echten Client ─────────────────────────────
def test_ha_roundtrip_real_client_cache_and_call_service() -> None:
    """Echter HA-Client gegen den Fake ⇒ Cache, Service-Payload, Zähler."""

    async def scenario() -> None:
        server = FakeHaServer(token=_TOKEN)
        await server.start()
        client = HomeAssistantClient(
            base_url=server.base_url,
            token=_TOKEN,
            request_timeout=2.0,
            cache_interval=600.0,
        )
        try:
            await client.start()
            # Cache ist unmittelbar nach start() gefüllt (ein Initial-Refresh).
            cached = await client.cached_entities()
            assert "light.wohnzimmer" in cached
            assert "climate.heizung" in cached
            assert "sensor.temperatur" not in cached  # Domain-Filter greift
            assert await client.cache.size() == 7
            assert await client.cache.counts() == (8, 7)
            assert client.started is True

            result = await client.call_service(
                "light", "turn_on", "light.wohnzimmer", brightness=200
            )
            assert result == [
                {"entity_id": "light.wohnzimmer", "state": "on", "changed": True}
            ]
        finally:
            await client.stop()

        # Zähler + Mitschnitte
        assert server.states_requests == 1  # nur der Initial-Refresh
        assert server.states_authorization == f"Bearer {_TOKEN}"
        assert server.service_call_count() == 1
        call = server.service_calls[0]
        assert call["domain"] == "light"
        assert call["service"] == "turn_on"
        assert call["path"] == "/api/services/light/turn_on"
        assert call["entity_id"] == "light.wohnzimmer"
        assert call["body"] == {"entity_id": "light.wohnzimmer", "brightness": 200}
        assert call["authorization"] == f"Bearer {_TOKEN}"
        assert server.unauthorized_requests == 0
        assert server.errors == []
        await server.stop()

    _run(scenario())


# ── Fake-HA: Fehlerpfade ───────────────────────────────────────────────────
def test_ha_states_http_500_maps_to_unavailable() -> None:
    """`GET /api/states` 500 ⇒ „Home Assistant antwortet nicht." (wörtlich)."""

    async def scenario() -> None:
        server = FakeHaServer(token=_TOKEN, states_status=500)
        await server.start()
        client = HomeAssistantClient(
            base_url=server.base_url, token=_TOKEN, cache_interval=600.0
        )
        try:
            await client.start()  # _safe_refresh schluckt den ersten Fehler
            with pytest.raises(HaUnavailableError) as excinfo:
                await client.refresh()
            assert str(excinfo.value) == _HA_ERROR_TEXT
            assert str(excinfo.value) == HA_UNAVAILABLE_MESSAGE
        finally:
            await client.stop()
            await server.stop()

    _run(scenario())


def test_ha_service_http_500_maps_to_unavailable() -> None:
    """`POST /api/services/…` 500 ⇒ „Home Assistant antwortet nicht." """

    async def scenario() -> None:
        server = FakeHaServer(token=_TOKEN, service_status=500)
        await server.start()
        client = HomeAssistantClient(
            base_url=server.base_url, token=_TOKEN, cache_interval=600.0
        )
        try:
            await client.start()
            with pytest.raises(HaUnavailableError) as excinfo:
                await client.call_service("light", "turn_on", "light.wohnzimmer")
            assert str(excinfo.value) == _HA_ERROR_TEXT
            # Der Aufruf wurde serverseitig trotzdem registriert.
            assert server.service_call_count() == 1
        finally:
            await client.stop()
            await server.stop()

    _run(scenario())


def test_ha_states_timeout_maps_to_unavailable() -> None:
    """Injizierte Latenz > Client-Timeout ⇒ „Home Assistant antwortet nicht." """

    async def scenario() -> None:
        server = FakeHaServer(token=_TOKEN)
        await server.start()
        client = HomeAssistantClient(
            base_url=server.base_url,
            token=_TOKEN,
            request_timeout=0.2,
            cache_interval=600.0,
        )
        try:
            await client.start()
            server.set_states_latency(0.6)
            start = time.monotonic()
            with pytest.raises(HaUnavailableError) as excinfo:
                await client.refresh()
            elapsed = time.monotonic() - start
            assert str(excinfo.value) == _HA_ERROR_TEXT
            assert elapsed < 0.6, f"Timeout griff nicht ({elapsed:.3f}s)"
        finally:
            await client.stop()
            await server.stop()

    _run(scenario())


def test_ha_wrong_token_is_rejected() -> None:
    """Falsches Token ⇒ 401; der Client meldet Unavailable, Zähler steigt."""

    async def scenario() -> None:
        server = FakeHaServer(token="richtig")
        await server.start()
        client = HomeAssistantClient(
            base_url=server.base_url, token="falsch", cache_interval=600.0
        )
        try:
            await client.start()
            with pytest.raises(HaUnavailableError):
                await client.refresh()
            assert server.unauthorized_requests == 2  # start()-Refresh + refresh()
        finally:
            await client.stop()
            await server.stop()

    _run(scenario())


# ── Fake-LLM: Properties ───────────────────────────────────────────────────
def test_fake_openai_lifecycle_paths_and_env_overrides() -> None:
    """Port 0, Pfade `/zen/v1/…`, Env zeigt auf den Fake."""

    async def scenario() -> None:
        server = FakeOpenAiServer()
        assert server.running is False
        await server.start()
        try:
            assert server.running is True
            assert SYSTEMONE_PATH == "/zen/v1/systemone"
            assert CHAT_PATH == "/zen/v1/chat/completions"
            assert server.llm_base_url == f"http://127.0.0.1:{server.port}/zen/v1"
            assert server.systemone_url.endswith("/zen/v1/systemone")
            assert server.chat_url.endswith("/zen/v1/chat/completions")
            env = server.env_overrides()
            assert set(env) == {"LLM_BASE_URL", "LLM_API_KEY"}
            assert env["LLM_BASE_URL"] == server.llm_base_url
            assert env["LLM_API_KEY"] == "test-llm-key"
        finally:
            await server.stop()
        assert server.running is False
        await server.stop()  # idempotent

    _run(scenario())


# ── Fake-LLM: Roundtrip gegen die echten Clients ───────────────────────────
def test_openai_roundtrip_real_jev_and_deepseek_clients() -> None:
    """Echte Jev-/DeepSeek-Clients gegen den Fake ⇒ Werte, Scores, Antwort."""

    async def scenario() -> None:
        server = FakeOpenAiServer(token=_LLM_KEY)
        await server.start()
        jev = JevClient(
            base_url=server.llm_base_url, api_key=_LLM_KEY, mode="intent", timeout=2.0
        )
        chat = DeepSeekClient(
            base_url=server.llm_base_url, api_key=_LLM_KEY, timeout=2.0
        )
        try:
            await jev.start()
            await chat.start()
            result = await jev.classify("Schalte das Licht im Wohnzimmer ein")
            assert result.intent.score == DEFAULT_NOUL == 0.97
            assert result.intent.value is True
            assert result.target is not None
            assert result.target.value == DEFAULT_TARGET_CHOICE == "light"
            assert result.target.score == DEFAULT_TARGET_CONFIDENCE == 1.0
            assert result.target.probabilities == {"light": 0.99, "switch": 0.01}
            assert result.model == "jev-1.13"

            first = await chat.complete_prompt("Wie geht es dir?")
            assert first.content == DEFAULT_CHAT_CONTENT
            parsed = await chat.complete_json("Antworte als JSON")
            assert parsed == {"antwort": "ok", "grad": 2}
        finally:
            await jev.stop()
            await chat.stop()

        # Zähler + Mitschnitte
        assert server.systemone_requests == 1
        assert server.chat_requests == 2
        assert server.last_systemone_state == "Schalte das Licht im Wohnzimmer ein"
        assert set(server.last_systemone_questions or {}) == {"intent", "target"}
        assert server.systemone_authorization == f"Bearer {_LLM_KEY}"
        assert server.chat_authorization == f"Bearer {_LLM_KEY}"
        # DeepSeek: temperature=0 und JSON-Modus im zweiten Aufruf.
        assert server.chat_bodies[0]["temperature"] == 0.0
        assert "response_format" not in server.chat_bodies[0]
        assert server.chat_bodies[1]["response_format"] == {"type": "json_object"}
        assert server.last_chat_messages is not None
        assert server.errors == []
        await server.stop()

    _run(scenario())


# ── Fake-LLM: Fehlerpfade ──────────────────────────────────────────────────
def test_openai_systemone_http_500_maps_to_unavailable() -> None:
    """`systemone` 500 ⇒ `LlmUnavailableError` (definierter Fehlerpfad)."""

    async def scenario() -> None:
        server = FakeOpenAiServer(token=_LLM_KEY, systemone_status=500)
        await server.start()
        jev = JevClient(base_url=server.llm_base_url, api_key=_LLM_KEY, timeout=2.0)
        try:
            await jev.start()
            with pytest.raises(LlmUnavailableError) as excinfo:
                await jev.classify("Licht an")
            assert "500" in str(excinfo.value)
        finally:
            await jev.stop()
            await server.stop()

    _run(scenario())


def test_openai_chat_http_500_maps_to_unavailable() -> None:
    """`chat/completions` 500 ⇒ `LlmUnavailableError`."""

    async def scenario() -> None:
        server = FakeOpenAiServer(token=_LLM_KEY, chat_status=500)
        await server.start()
        chat = DeepSeekClient(base_url=server.llm_base_url, api_key=_LLM_KEY, timeout=2.0)
        try:
            await chat.start()
            with pytest.raises(LlmUnavailableError) as excinfo:
                await chat.complete_prompt("Hallo")
            assert "500" in str(excinfo.value)
        finally:
            await chat.stop()
            await server.stop()

    _run(scenario())


def test_openai_systemone_timeout_maps_to_unavailable() -> None:
    """Injizierte `systemone`-Latenz > Client-Timeout ⇒ `LlmUnavailableError`."""

    async def scenario() -> None:
        server = FakeOpenAiServer(token=_LLM_KEY, systemone_latency=0.6)
        await server.start()
        jev = JevClient(
            base_url=server.llm_base_url, api_key=_LLM_KEY, mode="intent", timeout=0.2
        )
        try:
            await jev.start()
            start = time.monotonic()
            with pytest.raises(LlmUnavailableError) as excinfo:
                await jev.classify("Licht an")
            elapsed = time.monotonic() - start
            assert "Timeout" in str(excinfo.value)
            assert elapsed < 0.6, f"Timeout griff nicht ({elapsed:.3f}s)"
        finally:
            await jev.stop()
            await server.stop()

    _run(scenario())


def test_openai_wrong_key_is_rejected() -> None:
    """Falscher Key ⇒ 401; echte Clients melden `LlmUnavailableError`."""

    async def scenario() -> None:
        server = FakeOpenAiServer(token="richtig")
        await server.start()
        jev = JevClient(base_url=server.llm_base_url, api_key="falsch", timeout=2.0)
        try:
            await jev.start()
            with pytest.raises(LlmUnavailableError):
                await jev.classify("Licht an")
            assert server.unauthorized_requests == 1
        finally:
            await jev.stop()
            await server.stop()

    _run(scenario())


# ── Bausteine ───────────────────────────────────────────────────────────────
def test_default_payload_shapes_match_verified_forms() -> None:
    """Die festen Antwortformen entsprechen P0.T2/STATE §3 (Literale)."""
    server = FakeOpenAiServer()
    payload = server.systemone_payload()
    assert payload["answers"]["intent"]["noul"] == 0.97
    assert payload["answers"]["target"]["choice"] == "light"
    assert payload["answers"]["target"]["confidence"] == 1.0
    assert payload["model"] == "jev-1.13"

    chat = server.chat_payload()
    assert chat["choices"][0]["message"]["content"] == '{"antwort":"ok","grad":2}'
    assert chat["model"] == "deepseek-v4.1-flash"


def test_fake_ha_default_states_are_deterministic() -> None:
    """Die Default-Entity-Liste ist unverändert reproduzierbar (8 Einträge)."""
    server = FakeHaServer()
    assert len(server._states) == 8  # noqa: SLF001 - Diagnose des Fixtures.
    ids = [state["entity_id"] for state in server._states]
    assert ids == [
        "light.wohnzimmer",
        "switch.steckdose",
        "cover.rollo",
        "climate.heizung",
        "media_player.tv",
        "scene.abend",
        "script.putzen",
        "sensor.temperatur",
    ]
