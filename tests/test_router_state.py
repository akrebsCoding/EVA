"""Sensor-Fragen: HA-Zustände im LLM-Kontext (P12.T3/Block **C**, Layer **L0/`unit`**).

Prüfling ist `app/ha_client.py` (:class:`app.ha_client.ReadableCache`) und
`app/router.py` (:func:`app.router._select_relevant_states`,
:meth:`app.router.Router._route_question`).  Geprüft wird gegen den verbindlichen
Vertrag aus `deploy/docs/P12_DESIGN.md` §C.2 (C-1 … C-4) und der Testmatrix
`:396-411` — **nicht** gegen eine Wunschfassung.  Die zwölf Testnamen sind die
aus der Design-Tabelle (C-1 … C-12), damit der Abgleich maschinell möglich ist.

**Kein echtes Netz.**  Für C-1/C-12 wird der **echte** ``HomeAssistantClient``
gegen einen in-process ``httpx.MockTransport`` gehängt (Reservierungs-URL
``http://mock-ha.invalid``), damit die Verdrahtung ``_refresh()`` →
``readable_cache`` **wirklich** belegt wird und der Request-Zähler (§C-1 „ein
``/api/states``-Call") echt ist.  Für die Router-Pfade genügt ein injizierter
Fake (E59/E55).

**Deterministisch:** Zeitstempel sind **injiziert** (``ReadableCache.replace(now=…)``
bzw. ``readable_age_seconds(..., now=…)``), kein ``sleep``, keine Zufallswerte.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

import httpx
import pytest

from app.ha_client import (
    HA_STATES_PATH,
    HomeAssistantClient,
    ReadableCache,
    readable_value,
)
from app.llm_client import SystemOneResult, NoulResult, ChoiceResult
from app.router import (
    FACTS_AVAILABLE_KEY,
    INTENT_QUESTION,
    READABLE_STATE_LIMIT,
    Router,
    _readable_facts_block,
    _select_relevant_states,
)

pytestmark = pytest.mark.unit

# ── Literale (E59: unabhängig vom Prüfling) ──────────────────────────────
#: **Wörtlich** der Fallback-Text aus `P12_DESIGN.md` §C.2/C-4.  Bewusst lokal
#: als Literal und **nicht** aus `app.router` importiert: sonst wäre die
#: Zusage "unveränderter Wortlaut" eine Tautologie.
NO_DATA_TEXT = (
    "Das weiß ich nicht, ich habe dazu keinen aktuellen Wert aus Home Assistant."
)
#: Block-Kopfzeile wörtlich `P12_DESIGN.md:378`.
FACTS_HEADER = "Aktuelle Werte aus Home Assistant (Stand"

#: Feste "Jetzt"-Zeit inkl. Zonenoffset – alle Zeitstempel werden dagegen
#: gerechnet, damit ``24 h``/``1 h``/``6 h``-Grenzen exakt treffen.
TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 30, 14, 20, 31, tzinfo=TZ)


def stamp(*, minutes_ago: float = 0.0, tz: timezone = TZ) -> str:
    """``last_updated`` ``minutes_ago`` vor :data:`NOW` (ISO-8601 mit Offset)."""
    moment = NOW - timedelta(minutes=minutes_ago)
    return moment.astimezone(tz).isoformat()


def sensor(
    name: str,
    value: str,
    *,
    entity_id: str | None = None,
    device_class: str = "temperature",
    minutes_ago: float = 1.0,
    **attributes: Any,
) -> dict[str, Any]:
    """Ein ``sensor``-State mit ``device_class`` und frischer Zeitmarke."""
    return {
        "entity_id": entity_id or f"sensor.{name.split()[0].lower()}_{len(name)}",
        "state": value,
        "attributes": {
            "friendly_name": name,
            "device_class": device_class,
            **attributes,
        },
        "last_updated": stamp(minutes_ago=minutes_ago),
    }


# ── Fakes ────────────────────────────────────────────────────────────────
class FakeJev:
    """Score < 0.5 ⇒ QUESTION (E59) – wie in `tests/test_router.py`."""

    def __init__(self, *, score: float = 0.1) -> None:
        self.score = score
        self.calls: list[str] = []

    async def classify(self, state: str, **kwargs: Any) -> SystemOneResult:
        self.calls.append(state)
        return SystemOneResult(
            intent=NoulResult(score=self.score),
            target=ChoiceResult(value=None, score=0.0),
        )


class ExplodingJev:
    """Darf bei ``JEV_MODE=off`` **nie** aufgerufen werden."""

    mode = "off"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, state: str, **kwargs: Any) -> SystemOneResult:
        self.calls.append(state)
        raise AssertionError("Jev darf bei JEV_MODE=off nicht aufgerufen werden")


class FakeDeepSeek:
    """Feste JSON-Antwort; zeichnet **Prompt + System-Prompt** auf."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Optional[str]]] = []

    async def complete_json(
        self, prompt: str, *, system_prompt: Optional[str] = None, **extra: Any
    ) -> Any:
        self.calls.append((prompt, system_prompt))
        return self.payload


class FakeHa:
    """HA-Fake mit **Lese**-Cache – zählt `cached_entities`/`readable_snapshot`."""

    def __init__(
        self,
        entities: Optional[Mapping[str, Any]] = None,
        readable: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.entities = dict(entities or {})
        self.readable = dict(readable or {})
        self.cached_calls = 0
        self.readable_calls = 0
        self.refresh_calls = 0
        self.service_calls: list[tuple[str, str, str, dict[str, Any]]] = []

    async def cached_entities(self) -> dict[str, Any]:
        self.cached_calls += 1
        return dict(self.entities)

    async def readable_snapshot(self) -> dict[str, Any]:
        self.readable_calls += 1
        return dict(self.readable)

    async def refresh(self) -> int:
        self.refresh_calls += 1
        return len(self.entities)

    async def call_service(
        self, domain: str, service: str, entity_id: str, **data: Any
    ) -> Any:
        self.service_calls.append((domain, service, entity_id, dict(data)))
        return {"status": "ok"}


CATALOG: dict[str, Any] = {
    "light.wohnzimmer": {"friendly_name": "Wohnzimmerlicht", "attributes": {}},
}
QUESTION_PAYLOAD: dict[str, Any] = {"response_text": "Im Wohnzimmer sind es 22,4 Grad."}


def make_router(*, deepseek: Any, ha: Any, jev: Any = None, jev_mode: str = "intent") -> Router:
    return Router(
        jev_client=jev if jev is not None else FakeJev(),
        deepseek_client=deepseek,
        ha_client=ha,
        jev_mode=jev_mode,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


#: Ein Real-Client am `MockTransport` + Request-Mitschnitt (kein Socket).
def ha_client(
    payload: Any,
) -> tuple[HomeAssistantClient, list[httpx.Request]]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=payload)

    inner = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mock-ha.invalid",
    )
    client = HomeAssistantClient(client=inner, token="test-token")
    return client, recorded


def states_requests(recorded: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in recorded if r.url.path == HA_STATES_PATH]


# ── C-1 ──────────────────────────────────────────────────────────────────
def test_readable_cache_filled_from_same_states_payload() -> None:
    """**Ein** ``/api/states`` ⇒ beide Caches gefüllt (kein zweiter Roundtrip)."""
    payload = [
        # Schalt-Entity (EntityCache) + Lese-Entities (ReadableCache) in
        # **einer** Antwort – genau der Live-Fall (931 States, alle Domains).
        {"entity_id": "light.wohnzimmer", "state": "on", "attributes": {}},
        sensor("Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer",
               unit_of_measurement="°C"),
        {"entity_id": "person.andi", "state": "home",
         "attributes": {"friendly_name": "Andi"}, "last_updated": stamp()},
    ]
    client, recorded = ha_client(payload)

    async def scenario() -> tuple[int, int, dict[str, Any]]:
        await client.refresh()
        return (
            await client.cache.size(),
            len(states_requests(recorded)),
            await client.readable_snapshot(),
        )

    entity_count, request_count, readable = run(scenario())

    assert request_count == 1, "ein /api/states ⇒ kein zusätzlicher Call (C-1)"
    assert entity_count == 1  # EntityCache sieht nur die Schalt-Domain
    assert set(readable) == {"sensor.temp_wohnzimmer", "person.andi"}
    # Beide Caches aus derselben Antwort – der Lese-Zugriff selbst ruft nichts ab.
    requests_after_refresh = len(states_requests(recorded))
    assert run(client.readable_snapshot()) == readable
    assert len(states_requests(recorded)) == requests_after_refresh


# ── C-2 ──────────────────────────────────────────────────────────────────
def test_readable_cache_excludes_unknown_unavailable_and_stale() -> None:
    """``unknown``/``unavailable`` und >24 h ⇒ draußen; frische Werte drin."""
    states = [
        sensor("Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer"),
        sensor("Temperatur Alt", "19.0", entity_id="sensor.temp_alt", minutes_ago=60 * 24 + 1),
        {"entity_id": "sensor.bewegung", "state": "unknown",
         "attributes": {"device_class": "motion", "friendly_name": "Bewegung"},
         "last_updated": stamp()},
        {"entity_id": "sensor.tuer_status", "state": "unavailable",
         "attributes": {"device_class": "door", "friendly_name": "Türstatus"},
         "last_updated": stamp()},
        # `device_class` nicht in der Whitelist (Laufzeit/Dauer sind keine
        # *Satz-Messwerte*) ⇒ nicht lesbar, sonst fluten Versions-/Laufzeit-
        # Sensoren den Block.
        sensor("Laufzeit Heizung", "1234", entity_id="sensor.laufzeit_heizung",
               device_class="duration"),
    ]
    cache = ReadableCache()

    async def scenario() -> tuple[str, ...]:
        await cache.replace(states, now=NOW)
        return await cache.ids()

    ids = run(scenario())

    assert "sensor.temp_wohnzimmer" in ids
    assert "sensor.temp_alt" not in ids  # 24 h + 1 min ⇒ hart raus (C-2)
    assert "sensor.bewegung" not in ids  # unknown
    assert "sensor.tuer_status" not in ids  # unavailable
    assert "sensor.laufzeit_heizung" not in ids  # device_class nicht whitelisted


# ── C-3 ──────────────────────────────────────────────────────────────────
def test_readable_cache_includes_person_weather_sun_and_number_sensors() -> None:
    """``person``/``weather``/``sun`` und Zahlensensoren landen im Lese-Cache."""
    states = [
        sensor("Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer",
               unit_of_measurement="°C"),
        {"entity_id": "weather.forecast_home", "state": "cloudy",
         "attributes": {"friendly_name": "Wetter draußen", "temperature": 24.3,
                        "humidity": 46, "pressure": 1019.5, "wind_speed": 10.4},
         "last_updated": stamp()},
        {"entity_id": "sun.sun", "state": "above_horizon",
         "attributes": {"sunrise": stamp(minutes_ago=8 * 60),
                        "sunset": "2026-09-30T21:48:00+02:00"},
         "last_updated": stamp()},
    ]
    states += [
        {"entity_id": f"person.person{i}", "state": "home" if i % 2 else "not_home",
         "attributes": {"friendly_name": f"Person {i}"}, "last_updated": stamp()}
        for i in range(5)
    ]
    cache = ReadableCache()

    async def scenario() -> dict[str, Any]:
        await cache.replace(states, now=NOW)
        return await cache.snapshot()

    readable = run(scenario())

    assert set(readable) == {
        "sensor.temp_wohnzimmer",
        "weather.forecast_home",
        "sun.sun",
        "person.person0",
        "person.person1",
        "person.person2",
        "person.person3",
        "person.person4",
    }
    # Die Zahl wird **deutsch** formatiert – "22.4 °C" wäre kein TTS-Text.
    assert readable_value(readable["sensor.temp_wohnzimmer"]) == "22,4 °C"
    assert readable_value(readable["person.person1"]) == "anwesend"


# ── C-4 ──────────────────────────────────────────────────────────────────
def test_readable_cache_excludes_device_tracker() -> None:
    """85 ``device_tracker`` (MAC-/Gerätenamen) bleiben draußen – Präsenz über ``person``."""
    states = [
        {"entity_id": f"device_tracker.geraet{i:02d}", "state": "home",
         "attributes": {"friendly_name": f"iPhone Geraet {i}"}, "last_updated": stamp()}
        for i in range(85)
    ]
    states.append(
        {"entity_id": "person.andi", "state": "home",
         "attributes": {"friendly_name": "Andi"}, "last_updated": stamp()}
    )
    cache = ReadableCache()

    async def scenario() -> tuple[str, ...]:
        await cache.replace(states, now=NOW)
        return await cache.ids()

    ids = run(scenario())

    assert ids == ("person.andi",)
    assert not any(entity_id.startswith("device_tracker.") for entity_id in ids)


# ── C-5 ──────────────────────────────────────────────────────────────────
def test_select_relevant_states_ranks_room_match_first() -> None:
    """„Wohnzimmer" ⇒ Küche/Schlafzimmer stehen **hinten**."""
    readable = {
        "sensor.temp_wohnzimmer": sensor(
            "Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer"
        ),
        "sensor.temp_kueche": sensor(
            "Temperatur Küche", "26.9", entity_id="sensor.temp_kueche"
        ),
        "sensor.temp_schlafzimmer": sensor(
            "Temperatur Schlafzimmer", "19.3", entity_id="sensor.temp_schlafzimmer"
        ),
    }

    lines = _select_relevant_states("wie warm ist es im Wohnzimmer?", readable)

    assert lines, "Wohnzimmer muss als Treffer gefunden werden"
    assert "Temperatur Wohnzimmer" in lines[0]
    joined = "\n".join(lines)
    assert joined.index("Temperatur Wohnzimmer") < joined.index("Temperatur Küche")
    assert joined.index("Temperatur Wohnzimmer") < joined.index(
        "Temperatur Schlafzimmer"
    )


# ── C-6 ──────────────────────────────────────────────────────────────────
def test_select_relevant_states_respects_limit_twelve() -> None:
    """30 Kandidaten ⇒ **12** Zeilen (harte Prompt-Grenze)."""
    readable = {
        f"sensor.temp_wohnzimmer_{i:02d}": sensor(
            f"Temperatur Wohnzimmer {i:02d}", str(20 + i / 10),
            entity_id=f"sensor.temp_wohnzimmer_{i:02d}",
            unit_of_measurement="°C",
        )
        for i in range(30)
    }

    lines = _select_relevant_states("wie warm ist es im Wohnzimmer?", readable)

    assert len(lines) == READABLE_STATE_LIMIT == 12
    assert lines == _select_relevant_states(
        "wie warm ist es im Wohnzimmer?", readable
    ), "Auswahl muss deterministisch sein (kein Zufall, keine Dict-Reihenfolge)"


# ── C-7 ──────────────────────────────────────────────────────────────────
def test_select_relevant_states_empty_input_returns_empty_list() -> None:
    """Kein Treffer ⇒ ``[]`` ⇒ der ehrliche Fallback statt „erste 12 Werte"."""
    readable = {
        "sensor.temp_wohnzimmer": sensor(
            "Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer"
        ),
        "sensor.luftfeuchte_bad": sensor(
            "Luftfeuchte Bad", "58", entity_id="sensor.luftfeuchte_bad",
            device_class="humidity", unit_of_measurement="%",
        ),
    }

    assert _select_relevant_states("Wie spät ist es?", readable) == []

    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    ha = FakeHa(CATALOG, readable)
    decision = run(
        make_router(deepseek=deep, ha=ha).route("Wie spät ist es?", entities=CATALOG)
    )

    assert deep.calls == [], "ohne Werte kein LLM-Call (kein Raten, C-4)"
    assert decision.response_text == NO_DATA_TEXT
    assert decision.raw[FACTS_AVAILABLE_KEY] is False


# ── C-8 ──────────────────────────────────────────────────────────────────
def test_question_prompt_contains_state_block_with_timestamp() -> None:
    """C-3: Wert + ``Stand`` + ``entity_id`` im Prompt, Regel im System-Prompt."""
    readable = {
        "sensor.temp_wohnzimmer": sensor(
            "Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer",
            unit_of_measurement="°C",
        ),
        "sensor.temp_kueche": sensor(
            "Temperatur Küche", "26.9", entity_id="sensor.temp_kueche",
            unit_of_measurement="°C",
        ),
    }
    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    ha = FakeHa(CATALOG, readable)

    decision = run(
        make_router(deepseek=deep, ha=ha).route(
            "wie warm ist es im Wohnzimmer?", entities=CATALOG
        )
    )

    assert len(deep.calls) == 1
    prompt, system_prompt = deep.calls[0]
    assert FACTS_HEADER in prompt
    assert "22,4 °C" in prompt
    assert "sensor.temp_wohnzimmer" in prompt
    assert re.search(r"Stand \d{1,2}:\d{2}", prompt), "Zeitstempel ist Pflicht (C-3)"
    # Ranking (C-5): die **gefragte** Entity steht vorn, der
    # gleichartige Wert eines anderen Raums hinten (der Prompt darf beide
    # enthalten – die Reihenfolge trägt die Antwort).
    assert prompt.index("sensor.temp_wohnzimmer") < prompt.index(
        "sensor.temp_kueche"
    )
    assert system_prompt is not None
    assert "Aktuelle Werte aus Home Assistant" in system_prompt
    assert "Zeitstempel" in system_prompt
    assert decision.raw[FACTS_AVAILABLE_KEY] is True
    assert decision.response_text == QUESTION_PAYLOAD["response_text"]
    assert ha.service_calls == []


# ── C-9 ──────────────────────────────────────────────────────────────────
def test_question_prompt_contains_no_facts_when_no_match() -> None:
    """Ohne Treffer darf **keine** Zahl aus dem Katalog im Prompt landen."""
    readable = {
        "sensor.temp_wohnzimmer": sensor(
            "Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer",
            unit_of_measurement="°C",
        )
    }
    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    ha = FakeHa(CATALOG, readable)

    decision = run(
        make_router(deepseek=deep, ha=ha).route(
            "Wer hat das Auto in die Garage gestellt?", entities=CATALOG
        )
    )

    assert deep.calls == [], "kein Prompt ⇒ keine Werte im Prompt (C-9)"
    assert decision.intent == INTENT_QUESTION
    assert decision.response_text == NO_DATA_TEXT
    # Und die **Frage** selbst ist harmlos: kein Service-Call.
    assert ha.service_calls == []
    assert ha.refresh_calls == 0, "kein Refresh wegen leerer Auswahl (C-1)"


# ── C-10 ─────────────────────────────────────────────────────────────────
def test_deepseek_only_question_gets_same_state_block() -> None:
    """``JEV_MODE=off``: identischer Block im **einen** Intent-Call."""
    readable = {
        "sensor.temp_wohnzimmer": sensor(
            "Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer",
            unit_of_measurement="°C",
        )
    }
    deep = FakeDeepSeek(
        {"intent": "QUESTION", "response_text": "Im Wohnzimmer sind es 22,4 Grad."}
    )
    ha = FakeHa(CATALOG, readable)
    jev = ExplodingJev()

    decision = run(
        make_router(deepseek=deep, ha=ha, jev=jev, jev_mode="off").route(
            "wie warm ist es im Wohnzimmer?", entities=CATALOG
        )
    )

    assert jev.calls == []
    assert len(deep.calls) == 1, "off-Pfad bleibt bei **einem** DeepSeek-Call"
    prompt, system_prompt = deep.calls[0]
    assert FACTS_HEADER in prompt
    assert "22,4 °C" in prompt
    assert "sensor.temp_wohnzimmer" in prompt
    assert re.search(r"Stand \d{1,2}:\d{2}", prompt)
    assert system_prompt is not None and "Aktuelle Werte aus Home Assistant" in system_prompt
    assert decision.intent == INTENT_QUESTION
    assert decision.raw[FACTS_AVAILABLE_KEY] is True
    assert ha.service_calls == []


# ── C-11 ─────────────────────────────────────────────────────────────────
def test_question_facts_flag_false_is_logged(caplog: Any) -> None:
    """``raw["facts_available"] is False`` **und** sichtbar im Log (C-4)."""
    ha = FakeHa(CATALOG, {"sensor.temp_wohnzimmer": sensor(
        "Temperatur Wohnzimmer", "22.4", entity_id="sensor.temp_wohnzimmer",
        unit_of_measurement="°C",
    )})

    async def scenario() -> Any:
        router = make_router(deepseek=FakeDeepSeek(QUESTION_PAYLOAD), ha=ha)
        with caplog.at_level(logging.INFO, logger="manager.router"):
            return await router.route("Wo steht der Ball?", entities=CATALOG)

    decision = run(scenario())

    assert decision.intent == INTENT_QUESTION
    assert decision.response_text == NO_DATA_TEXT
    assert decision.raw[FACTS_AVAILABLE_KEY] is False
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "ohne lesbare HA-Werte" in message or "ohne passenden HA-Wert" in message
        for message in messages
    ), f"Fallback nicht im Log: {messages}"


# ── C-12 ─────────────────────────────────────────────────────────────────
def test_readable_cache_is_empty_before_first_refresh() -> None:
    """Start-Zustand: ``{}`` ohne Absturz – eine Frage ist beantwortbar."""
    client, recorded = ha_client([])

    async def scenario() -> tuple[dict[str, Any], int, int]:
        snapshot = await client.readable_snapshot()
        return snapshot, await client.cache.size(), len(states_requests(recorded))

    readable, entity_count, request_count = run(scenario())

    assert readable == {}
    assert entity_count == 0
    assert request_count == 0, "ein Cache-Lesezugriff darf keinen Request auslösen"

    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    decision = run(
        make_router(deepseek=deep, ha=client).route(
            "wie warm ist es im Wohnzimmer?", entities=CATALOG
        )
    )
    assert deep.calls == []
    assert decision.response_text == NO_DATA_TEXT
    assert len(states_requests(recorded)) == 0
