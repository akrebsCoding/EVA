"""HA-Client-Tests (P4.T1, `PLAN.md` §7 → P4.T1, Layer **L0/`unit`**).

Prüfling ist `app/ha_client.py` (P4.T0).  Getestet wird gegen den
**verifizierten Vertrag** aus `STATE.md` §3 („HA-Client (P4.T0)") und
`PLAN.md:456`, nicht gegen eine Wunschfassung.  Der Prüfling bleibt
**unangetastet** (kein Patch, kein Bug-Workaround).

Abdeckung der vier Pflichtpunkte aus `PLAN.md:456`:

1. **Cache-Aufbau + Lifecycle** — `GET /api/states` (Mock) füllt den Cache
   korrekt; `start()`/`stop()` sind idempotent, der Loop-Task lebt und wird
   sauber beendet → `test_start_builds_cache_*`, `test_start_is_idempotent_*`,
   `test_stop_is_idempotent_*`, `test_async_context_manager_*`, …
2. **Domain-Filter + E17-Allowlist** — nur Entities der Domains aus
   `entity_domains` werden gecacht; leere Allowlist = kein Vorfilter,
   gesetzte Allowlist wirkt **UND**-verknüpft zum Domain-Filter →
   `test_only_target_domain_entities_*`, `test_*allowlist_*`, `test_custom_domains_*`
3. **`call_service`-Payload** — `POST /api/services/{domain}/{service}`,
   `entity_id` **im Body** (nicht in der URL), Auth-Header gesetzt →
   `test_call_service_posts_entity_id_in_body`, …
4. **Fehler ⇒ „Home Assistant antwortet nicht."** — HTTP 500, Timeout,
   Connect-Fehler (Refresh **und** `call_service`) →
   `test_refresh_http_500_*`, `test_refresh_timeout_*`, `test_refresh_connect_*`, …

**Kein echtes Netz.**  Das Gegenüber ist ein `httpx.MockTransport` —
**in-process**, kein Socket, kein `connect`/`getaddrinfo`.  Die injizierten
`httpx.AsyncClient` tragen eine bewusst unresolvable Mock-URL
(`http://mock-ha.invalid`); die aktive Netzsperre aus `tests/conftest.py`
(E36) würde jeden echten Socket-Kontakt als `NetworkAccessBlocked` hochbluten
lassen — die Tests belegen damit positiv, dass keiner stattfindet.

**Deterministisch:** kein `sleep`, keine echte Zeitabhängigkeit, keine
Zufallswerte; alle Handler antworten synchron und reproduzierbar.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.ha_client import (
    HA_BLOCKED_DOMAINS,
    HA_ENTITY_DOMAINS,
    HA_SERVICE_PATH_TEMPLATE,
    HA_STATES_PATH,
    EntityCache,
    HaClientError,
    HaUnavailableError,
    HomeAssistantClient,
    entity_domain,
    is_target_entity,
)
from tests.conftest import NetworkAccessBlocked

pytestmark = pytest.mark.unit

#: Test-Token — bewusst **kein** Geheimnis, nur ein Marker im Auth-Header.
TOKEN = "test-token"
#: Der **wörtliche** Fehlertext (E19/E55) — bewusst als Literal, damit ein
#: geänderter `HA_UNAVAILABLE_MESSAGE` in `app/ha_client.py` die Tests bricht.
EXPECTED_UNAVAILABLE_MESSAGE = "Home Assistant antwortet nicht."
#: Reservierte, **niemals auflösbare** Mock-Basis (`RFC 2606`).  Sie steht nur
#: am `MockTransport`-Client; es wird kein Paket gesendet.
MOCK_BASE_URL = "http://mock-ha.invalid"
#: Groß genug, dass der Refresh-Loop während eines Tests **nie** feuert.
LONG_INTERVAL = 3600.0

Responder = Callable[[httpx.Request], httpx.Response]


# ── Testdaten (deterministisch, keine echten Geräte) ─────────────────────
#: Die 8 Entities, die nach Domain-Filter im Cache landen müssen.
TARGET_IDS = (
    "light.wohnzimmer",
    "light.kueche",
    "switch.steckdose",
    "cover.rollo",
    "climate.thermostat",
    "media_player.tv",
    "scene.abend",
    "script.starte",
)
#: Gültige States insgesamt (inkl. Nicht-Ziel-Domains) — `counts()`-Vertrag.
TOTAL_VALID = 13


def states_payload() -> list[Any]:
    """`/api/states`-Antwort: Ziel- und Nicht-Ziel-Domains + ungültige Einträge.

    Enthält bewusst auch kaputte Einträge (kein `Mapping`, leere/fehlende
    `entity_id`), damit `replace()` deren Robustheit und die
    `counts()`-Semantik („gesehen gültig" vs. „gecacht") belegt.
    """
    return [
        {"entity_id": "light.wohnzimmer", "state": "on", "attributes": {"brightness": 200}},
        {"entity_id": "light.kueche", "state": "off", "attributes": {}},
        {"entity_id": "switch.steckdose", "state": "on", "attributes": {}},
        {"entity_id": "cover.rollo", "state": "open", "attributes": {}},
        {"entity_id": "climate.thermostat", "state": "heat", "attributes": {}},
        {"entity_id": "media_player.tv", "state": "unavailable", "attributes": {}},
        {"entity_id": "scene.abend", "state": "scening", "attributes": {}},
        {"entity_id": "script.starte", "state": "off", "attributes": {}},
        # Nicht-Ziel-Domains (gültig, dürfen NICHT gecacht werden) – E113: der
        # Default umfasst 16 **steuerfähige** Domains; Read-only-Domains
        # (`sensor`, `binary_sensor`, `device_tracker`, `weather`, `person`)
        # bleiben draußen (Sensorfragen = P12.T3).
        {"entity_id": "sensor.temperatur", "state": "21.5", "attributes": {}},
        {"entity_id": "device_tracker.handy", "state": "home", "attributes": {}},
        {"entity_id": "binary_sensor.tuer", "state": "off", "attributes": {}},
        {"entity_id": "weather.vorhersage", "state": "sunny", "attributes": {}},
        {"entity_id": "not-a-valid-id", "state": "?"},
        # Kaputte Einträge (dürfen weder gecacht noch als gültig gezählt werden):
        {"entity_id": "", "state": "?"},
        "not-a-mapping",
        {"state": "x"},
    ]


def make_client(responder: Responder) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """`MockTransport`-Client + Request-Mitschnitt (in-process, kein Socket)."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return responder(request)

    inner = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=MOCK_BASE_URL
    )
    return inner, recorded


def json_responder(payload: Any, *, status: int = 200) -> Responder:
    return lambda request: httpx.Response(status, json=payload)


def ha_client(responder: Responder, **kwargs: Any) -> tuple[HomeAssistantClient, list[httpx.Request]]:
    inner, recorded = make_client(responder)
    client = HomeAssistantClient(
        client=inner,
        token=TOKEN,
        cache_interval=LONG_INTERVAL,
        **kwargs,
    )
    return client, recorded


def service_requests(recorded: list[httpx.Request]) -> list[httpx.Request]:
    """Nur die Nicht-`/api/states`-Requests (also `call_service`)."""
    return [r for r in recorded if r.url.path != HA_STATES_PATH]


# ── 1. Cache-Aufbau + Lifecycle ──────────────────────────────────────────
def test_start_builds_cache_from_states_and_sets_auth() -> None:
    client, recorded = ha_client(json_responder(states_payload()))

    async def scenario() -> None:
        await client.start()
        try:
            assert client.started is True
            assert client.loop_task is not None
            assert client.loop_task.done() is False
            assert client.client is not None and client.client.is_closed is False
            assert await client.cache.size() == len(TARGET_IDS)
            assert await client.cache.counts() == (TOTAL_VALID, len(TARGET_IDS))
            entity = await client.get_entity("light.wohnzimmer")
            assert entity is not None
            assert entity["state"] == "on"
            assert entity["attributes"] == {"brightness": 200}
            assert set(await client.cached_entities()) == set(TARGET_IDS)
        finally:
            await client.stop()

    asyncio.run(scenario())

    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "GET"
    assert request.url.path == HA_STATES_PATH
    assert request.url.host == "mock-ha.invalid"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_start_is_idempotent_one_initial_refresh() -> None:
    client, recorded = ha_client(json_responder(states_payload()))

    async def scenario() -> None:
        await client.start()
        await client.start()  # No-op, kein zweiter Refresh
        await client.stop()

    asyncio.run(scenario())
    assert len(recorded) == 1


def test_stop_is_idempotent_and_closes_client() -> None:
    inner, _ = make_client(json_responder(states_payload()))
    client = HomeAssistantClient(client=inner, token=TOKEN, cache_interval=LONG_INTERVAL)

    async def scenario() -> None:
        await client.start()
        await client.stop()
        assert client.started is False
        assert client.loop_task is None
        assert client.client is None
        assert inner.is_closed is True
        await client.stop()  # 2. und 3. Aufruf dürfen nicht werfen
        await client.stop()

    asyncio.run(scenario())


def test_start_survives_initial_refresh_failure() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("HA down beim Start", request=request)

    client, _ = ha_client(responder)

    async def scenario() -> None:
        await client.start()
        assert client.started is True
        assert await client.cache.size() == 0
        await client.stop()

    asyncio.run(scenario())


def test_async_context_manager_starts_and_stops() -> None:
    inner, _ = make_client(json_responder(states_payload()))
    client = HomeAssistantClient(client=inner, token=TOKEN, cache_interval=LONG_INTERVAL)

    async def scenario() -> None:
        async with client as active:
            assert active is client
            assert client.started is True
            assert await client.cache.size() == len(TARGET_IDS)
        assert client.started is False
        assert client.loop_task is None

    asyncio.run(scenario())


def test_get_entity_and_snapshot_return_copies() -> None:
    client, _ = ha_client(json_responder(states_payload()))

    async def scenario() -> None:
        await client.start()
        try:
            entity = await client.get_entity("light.wohnzimmer")
            assert entity is not None
            entity["state"] = "TAMPERED"
            assert (await client.get_entity("light.wohnzimmer"))["state"] == "on"

            snapshot = await client.cached_entities()
            snapshot["light.wohnzimmer"]["state"] = "TAMPERED"
            assert (await client.get_entity("light.wohnzimmer"))["state"] == "on"
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_cache_clear_and_missing_entity() -> None:
    async def scenario() -> None:
        cache = EntityCache()
        assert await cache.get("light.gibtsnicht") is None
        assert await cache.replace(states_payload()) == len(TARGET_IDS)
        await cache.clear()
        assert await cache.size() == 0
        assert await cache.ids() == ()
        assert await cache.counts() == (0, 0)

    asyncio.run(scenario())


# ── 2. Domain-Filter + E17-Allowlist ─────────────────────────────────────
def test_only_target_domain_entities_are_cached() -> None:
    async def scenario() -> None:
        cache = EntityCache()
        await cache.replace(states_payload())
        assert set(await cache.ids()) == set(TARGET_IDS)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "entity_id",
    [
        "sensor.temperatur",
        "device_tracker.handy",
        "binary_sensor.tuer",
        "weather.vorhersage",
        "not-a-valid-id",
    ],
)
def test_non_target_domains_are_not_cached(entity_id: str) -> None:
    """Read-only-Domains landen **nie** im Cache (E113: 16 steuerfähige Domains)."""
    async def scenario() -> None:
        cache = EntityCache()
        await cache.replace(states_payload())
        assert await cache.get(entity_id) is None

    asyncio.run(scenario())


def test_empty_allowlist_is_no_prefilter() -> None:
    async def scenario() -> None:
        cache = EntityCache(allowed_entities=())
        await cache.replace(states_payload())
        assert set(await cache.ids()) == set(TARGET_IDS)

    asyncio.run(scenario())


def test_nonempty_allowlist_restricts_to_listed_entities() -> None:
    allowed = ("light.wohnzimmer", "switch.steckdose")

    async def scenario() -> None:
        cache = EntityCache(allowed_entities=allowed)
        await cache.replace(states_payload())
        assert set(await cache.ids()) == set(allowed)

    asyncio.run(scenario())


def test_allowlist_does_not_bypass_domain_filter() -> None:
    # Allowlist UND Domain-Filter: sensor steht in der Liste, ist aber keine
    # Ziel-Domain ⇒ NICHT gecacht (E17).
    assert is_target_entity("sensor.temperatur", HA_ENTITY_DOMAINS, ("sensor.temperatur",)) is False
    assert is_target_entity("light.wohnzimmer", HA_ENTITY_DOMAINS, ("light.wohnzimmer",)) is True
    # Leere Allowlist: Domain-Filter allein entscheidet.
    assert is_target_entity("sensor.temperatur", HA_ENTITY_DOMAINS, ()) is False
    assert is_target_entity("switch.steckdose", HA_ENTITY_DOMAINS, ()) is True


def test_custom_domains_restrict_cache() -> None:
    async def scenario() -> None:
        cache = EntityCache(domains=("light",))
        await cache.replace(states_payload())
        assert set(await cache.ids()) == {"light.wohnzimmer", "light.kueche"}

    asyncio.run(scenario())


def test_entity_domain_splits_on_first_dot() -> None:
    assert entity_domain("light.wohnzimmer") == "light"
    assert entity_domain("switch.a.b") == "switch"


@pytest.mark.parametrize("bad", ["", None, 5, b"light.x"])
def test_entity_domain_rejects_invalid(bad: Any) -> None:
    with pytest.raises(HaClientError):
        entity_domain(bad)


# ── 3. call_service-Payload ──────────────────────────────────────────────
def test_call_service_posts_entity_id_in_body() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=[{"entity_id": "light.wohnzimmer", "state": "on"}])
        return httpx.Response(200, json=states_payload())

    client, recorded = ha_client(responder)

    async def scenario() -> None:
        await client.start()
        try:
            result = await client.call_service(
                "light", "turn_on", "light.wohnzimmer", brightness=200
            )
            assert result == [{"entity_id": "light.wohnzimmer", "state": "on"}]
        finally:
            await client.stop()

    asyncio.run(scenario())

    posts = service_requests(recorded)
    assert len(posts) == 1
    request = posts[0]
    assert request.method == "POST"
    assert request.url.path == HA_SERVICE_PATH_TEMPLATE.format(
        domain="light", service="turn_on"
    )
    assert request.url.query == b""
    assert "light.wohnzimmer" not in request.url.path
    assert json.loads(request.content) == {
        "entity_id": "light.wohnzimmer",
        "brightness": 200,
    }
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_call_service_merges_extra_data() -> None:
    captured: dict[str, Any] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=[])

    client, _ = ha_client(responder)

    async def scenario() -> None:
        result = await client.call_service(
            "script", "turn_on", "script.starte", variables={"a": 1}
        )
        assert result == {"ok": True}

    asyncio.run(scenario())
    assert captured["body"] == {"entity_id": "script.starte", "variables": {"a": 1}}


def test_call_service_returns_none_for_empty_body() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json=[])

    client, _ = ha_client(responder)

    async def scenario() -> None:
        assert await client.call_service("light", "turn_off", "light.kueche") is None

    asyncio.run(scenario())


def test_call_service_without_start_raises_unavailable() -> None:
    client = HomeAssistantClient(client=None, token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.call_service("light", "turn_on", "light.wohnzimmer")
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "domain,service,entity_id",
    [
        ("", "turn_on", "light.x"),
        ("light", "", "light.x"),
        ("light", "turn_on", ""),
        ("light", "turn_on", "   "),
    ],
)
def test_call_service_rejects_empty_arguments(
    domain: str, service: str, entity_id: str
) -> None:
    client = HomeAssistantClient(client=None, token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaClientError) as excinfo:
            await client.call_service(domain, service, entity_id)
        assert isinstance(excinfo.value, HaClientError)
        assert not isinstance(excinfo.value, HaUnavailableError)

    asyncio.run(scenario())


# ── 4. Fehler ⇒ „Home Assistant antwortet nicht." ────────────────────────
def test_unavailable_error_message_is_literal() -> None:
    error = HaUnavailableError("technischer Grund")
    assert str(error) == EXPECTED_UNAVAILABLE_MESSAGE
    assert error.detail == "technischer Grund"
    assert isinstance(error, HaClientError)


def test_refresh_http_500_raises_unavailable() -> None:
    client = HomeAssistantClient(
        client=make_client(lambda r: httpx.Response(500, text="boom"))[0], token=TOKEN
    )

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.refresh()
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert "500" in (excinfo.value.detail or "")
        assert isinstance(excinfo.value, HaClientError)

    asyncio.run(scenario())


def test_refresh_timeout_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu spät", request=request)

    client = HomeAssistantClient(client=make_client(responder)[0], token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.refresh()
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert "Timeout" in (excinfo.value.detail or "")
        assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)

    asyncio.run(scenario())


def test_refresh_connect_error_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = HomeAssistantClient(client=make_client(responder)[0], token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.refresh()
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert "ConnectError" in (excinfo.value.detail or "")
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)

    asyncio.run(scenario())


def test_refresh_invalid_json_raises_unavailable() -> None:
    client = HomeAssistantClient(
        client=make_client(lambda r: httpx.Response(200, content=b"kein json"))[0],
        token=TOKEN,
    )

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.refresh()
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE

    asyncio.run(scenario())


def test_refresh_non_list_json_raises_unavailable() -> None:
    client = HomeAssistantClient(
        client=make_client(json_responder({"not": "a list"}))[0], token=TOKEN
    )

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.refresh()
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert "Liste" in (excinfo.value.detail or "")

    asyncio.run(scenario())


def test_refresh_without_client_raises_unavailable() -> None:
    client = HomeAssistantClient(client=None, token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.refresh()
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE

    asyncio.run(scenario())


def test_call_service_http_500_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(client=make_client(responder)[0], token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.call_service("light", "turn_on", "light.wohnzimmer")
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert "500" in (excinfo.value.detail or "")

    asyncio.run(scenario())


def test_call_service_timeout_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("zu spät", request=request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(client=make_client(responder)[0], token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.call_service("light", "turn_on", "light.wohnzimmer")
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)

    asyncio.run(scenario())


def test_call_service_connect_error_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(client=make_client(responder)[0], token=TOKEN)

    async def scenario() -> None:
        with pytest.raises(HaUnavailableError) as excinfo:
            await client.call_service("light", "turn_on", "light.wohnzimmer")
        assert str(excinfo.value) == EXPECTED_UNAVAILABLE_MESSAGE
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)

    asyncio.run(scenario())


# ── Netz-Nachweis ────────────────────────────────────────────────────────
def test_mocktransport_is_in_process_while_netz_block_is_active() -> None:
    """Die Netzsperre ist aktiv; `MockTransport` umgeht sie **legitim**.

    Ein echter Socket-Kontakt (DNS) blutet als `NetworkAccessBlocked` hoch —
    genau das würde passieren, wenn ein Test das echte Netz träfe.  Dieselbe
    Mock-URL über `MockTransport` antwortet dagegen **in-process** (kein
    Socket), der Request ist aufgezeichnet.
    """
    with pytest.raises(NetworkAccessBlocked):
        import socket

        socket.getaddrinfo("mock-ha.invalid", 80)

    inner, recorded = make_client(json_responder(states_payload()))

    async def scenario() -> None:
        response = await inner.get(HA_STATES_PATH)
        assert response.status_code == 200
        await inner.aclose()

    asyncio.run(scenario())
    assert len(recorded) == 1
    assert recorded[0].url.host == "mock-ha.invalid"
    assert isinstance(inner._transport, httpx.MockTransport)


# ══════════════════════════════════════════════════════════════════════════
# E113 (P12.T2) – Domain-Sichtbarkeit: 16 Domains + Sicherheitsboden
# (`deploy/docs/P12_DESIGN.md` A-1 … A-6)
# ══════════════════════════════════════════════════════════════════════════
#: Die **14 sicheren** Schalt-Domains (P12_DESIGN A-1) …
SAFE_DOMAINS_14 = (
    "light",
    "switch",
    "cover",
    "climate",
    "media_player",
    "scene",
    "script",
    "button",
    "number",
    "select",
    "input_boolean",
    "input_select",
    "input_number",
    "fan",
)
#: … **plus** `update`/`automation` (User-Entscheidung 2026-09-30) = 16.
EXPECTED_DOMAINS_16 = SAFE_DOMAINS_14 + ("update", "automation")
#: Sicherheitsboden – hart, **nicht** per Env konfigurierbar.
EXPECTED_BLOCKED_DOMAINS = ("lock", "alarm_control_panel", "vacuum")
#: Read-only: kein Schaltziel, bleibt außerhalb des Caches (P12.T3 = Lesen).
READ_ONLY_DOMAINS = (
    "sensor",
    "binary_sensor",
    "device_tracker",
    "person",
    "zone",
    "sun",
    "weather",
    "event",
)


def test_default_domains_cover_sixteen_domains() -> None:
    """A-1 (E113): Default = 14 sichere + `update`/`automation` = **16**."""
    from app.config import Settings

    domains = Settings(_env_file=None).entity_domains
    assert domains == EXPECTED_DOMAINS_16
    assert len(domains) == 16
    # Reihenfolge der Soll-Liste bleibt erhalten (Doku-/Diff-lesbar).
    assert list(HA_ENTITY_DOMAINS) == list(EXPECTED_DOMAINS_16)


def test_blocked_domains_are_hardcoded_and_not_configurable() -> None:
    """A-2 (E113): `lock`/`alarm_control_panel`/`vacuum` stehen **hart** im Modul.

    Sie duerfen **nicht** aus Settings/Env kommen – ein Config-Schlüssel, der
    die Sicherheitsgrenze aushebeln kann, wäre schlechter als ein Hardcode.
    """
    from app.config import Settings

    assert HA_BLOCKED_DOMAINS == EXPECTED_BLOCKED_DOMAINS
    settings_domains = Settings(_env_file=None).entity_domains
    assert not set(HA_BLOCKED_DOMAINS) & set(settings_domains)
    # Kein Feld in den Settings, das die Blockliste überschreiben könnte.
    assert not hasattr(Settings(_env_file=None), "ha_blocked_domains")
    # Und die Blockliste wird in **keiner** Env-Datei *gesetzt* (Erwähnungen in
    # Kommentaren/Erklärungen sind erlaubt, eine Zuweisung wäre ein Loch).
    # Eine lokale `.env` existiert bei einem frischen Clone evtl. nicht — skip.
    for env_file in (".env", ".env.test", "deploy/env.template", ".env.example"):
        if not Path(env_file).exists():
            continue
        assignments = [
            line
            for line in Path(env_file).read_text(encoding="utf-8").splitlines()
            if line.startswith("HA_BLOCKED_DOMAINS")
        ]
        assert not assignments, (env_file, assignments)


def test_blocklist_wins_over_domains_and_allowlist() -> None:
    """A-3 (E113): **Block schlägt immer Erlaubnis**.

    Selbst wenn die Domain in `domains` **und** die Entity in der E17-Allowlist
    steht ⇒ `False`.
    """
    for domain in EXPECTED_BLOCKED_DOMAINS:
        entity_id = f"{domain}.code"
        assert (
            is_target_entity(
                entity_id,
                EXPECTED_DOMAINS_16 + EXPECTED_BLOCKED_DOMAINS,
                (entity_id,),
            )
            is False
        )
        # Auch mit expliziter leerer Domain-Liste bleibt der Block schlagend.
        assert is_target_entity(entity_id, (domain,), (entity_id,)) is False


def test_blocklist_blocks_lock_and_vacuum_even_if_allowed() -> None:
    """A-2 (E113): `lock`/`vacuum`/`alarm_control_panel` nie im Cache."""
    async def scenario() -> None:
        payload = [
            {"entity_id": "lock.haustuer", "state": "locked", "attributes": {}},
            {"entity_id": "vacuum.staubsauger", "state": "docked", "attributes": {}},
            {"entity_id": "alarm_control_panel.code", "state": "disarmed", "attributes": {}},
            {"entity_id": "light.wohnzimmer", "state": "on", "attributes": {}},
        ]
        cache = EntityCache(
            domains=EXPECTED_DOMAINS_16 + EXPECTED_BLOCKED_DOMAINS,
            allowed_entities=tuple(state["entity_id"] for state in payload),
        )
        await cache.replace(payload)
        assert set(await cache.ids()) == {"light.wohnzimmer"}

    asyncio.run(scenario())


def test_empty_blocklist_blocks_nothing() -> None:
    """A-3 (E113): leere Blockliste ⇒ alte Semantik (Domain ∩ Allowlist)."""
    for entity_id in ("lock.haustuer", "vacuum.staubsauger"):
        domain = entity_id.split(".", 1)[0]
        # Domain-Filter allein entscheidet wieder – wie vor E113.
        assert is_target_entity(entity_id, (domain,), (), blocked_domains=()) is True
        assert is_target_entity(entity_id, ("light",), (), blocked_domains=()) is False
        # Domain ∩ Allowlist, wie vor E113.
        assert (
            is_target_entity(entity_id, (domain,), (entity_id,), blocked_domains=())
            is True
        )
        assert is_target_entity(entity_id, (domain,), ("light.x",), blocked_domains=()) is False


def test_read_only_domains_stay_out_of_the_default_catalog() -> None:
    """A-1 (E113): Read-only-Domains sind **kein** Schaltziel (P12.T3 = Lesen)."""
    for domain in READ_ONLY_DOMAINS:
        assert domain not in EXPECTED_DOMAINS_16
        assert (
            is_target_entity(f"{domain}.x", EXPECTED_DOMAINS_16, (f"{domain}.x",))
            is False
        )


def test_env_template_matches_config_default() -> None:
    """**Drift-Wächter** – ``env.template`` == ``app/config.py``.

    Zusätzlich `.env.example`, damit die Doku nicht still zurückbleibt.
    """
    from app.config import Settings

    expected = Settings(_env_file=None).ha_entity_domains
    for env_file in ("deploy/env.template", ".env.example"):
        text = Path(env_file).read_text(encoding="utf-8")
        line = f"HA_ENTITY_DOMAINS={expected}"
        assert line in text, env_file


def test_cache_covers_all_sixteen_domains() -> None:
    """A-6 (E113): je 1 Entity pro Domain ⇒ alle **16** im Cache."""
    payload = [
        {"entity_id": f"{domain}.probe", "state": "on", "attributes": {}}
        for domain in EXPECTED_DOMAINS_16
    ] + [
        {"entity_id": f"{domain}.weg", "state": "?", "attributes": {}}
        for domain in READ_ONLY_DOMAINS
    ] + [
        {"entity_id": f"{domain}.weg", "state": "?", "attributes": {}}
        for domain in EXPECTED_BLOCKED_DOMAINS
    ]

    async def scenario() -> None:
        cache = EntityCache()
        await cache.replace(payload)
        assert set(await cache.ids()) == {f"{domain}.probe" for domain in EXPECTED_DOMAINS_16}
        assert await cache.counts() == (len(payload), 16)

    asyncio.run(scenario())


def test_start_logs_domain_and_blocklist_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beobachtbarkeit (A-5): Start-Log nennt Domains **und** Blockliste."""
    # `manager` propagiert nicht ⇒ caplog-Handler explizit an den Namespace
    # (siehe `tests/test_logger.py::test_record_is_captured_by_caplog`).
    manager_logger = logging.getLogger("manager")
    handler = caplog.handler
    manager_logger.addHandler(handler)
    client, _recorded = ha_client(json_responder(states_payload()))
    try:
        with caplog.at_level(logging.INFO, logger="manager"):

            async def scenario() -> None:
                await client.start()
                await client.stop()

            asyncio.run(scenario())
    finally:
        manager_logger.removeHandler(handler)

    records = [
        record.getMessage()
        for record in caplog.records
        if record.name == "manager.ha_client"
        and "HA-Client gestartet" in record.getMessage()
    ]
    assert records, f"kein Start-Record in {caplog.text!r}"
    start_line = records[-1]
    assert "Domains=16" in start_line
    assert "blockiert=3" in start_line
