"""In-Process-FastAPI-Stubs für Home Assistant und das LLM-Gateway (P9.T3, `PLAN.md:541`).

Die beiden Fakes bedienen **genau** die HTTP-Verträge, die die echten Clients
`app/ha_client.py` (P4.T0) und `app/llm_client.py` (P4.T2) erwarten:

* **`FakeHaServer`** — `GET /api/states` (Entity-Liste) und
  `POST /api/services/{domain}/{service}` (Service-Aufruf, `entity_id` **im
  Body**).  Zählt Aufrufe und erzeugt auf Kommando HTTP-Status ≠ 200 bzw.
  Latenz (der Client mappt beides auf sein `HaUnavailableError`).
* **`FakeOpenAiServer`** — `POST /zen/v1/systemone` (Jev: `answers.intent.noul`
  + `answers.target.choice`/`.confidence`) und `POST /zen/v1/chat/completions`
  (OpenAI-kompatibel, `choices[0].message.content`).  Feste Antworten in der
  **verifizierten Form** (P0.T2/STATE §3: `noul=0.97`, `choice=light`,
  Konfidenz `1.0`), Latenzinjektion und Fehlerpfade.

**Keine duplizierten Pfade/Magic Numbers:** die URL-Templates kommen aus den
Prüflingen (`HA_STATES_PATH`, `HA_SERVICE_PATH_TEMPLATE` aus `app.ha_client`;
`JEV_SYSTEMONE_PATH`, `DEEPSEEK_CHAT_PATH` aus `app.llm_client`).  Nur der
Gateway-Präfix `/zen/v1` ist die vertragliche Vorgabe aus `PLAN.md:541` (Default
von `LLM_BASE_URL`).

**Ausführung: In-Process-uvicorn in einem pytest-Thread.**  Pro Fake läuft genau
ein `uvicorn.Server` in einem Daemon-Thread (eigener Event-Loop); der Test-Prozess
spricht ihn über Loopback-TCP an.  Dadurch braucht L2 **kein Docker** und **kein
`.123`** — alles lokal auf `.22`.  Marker-Konvention (E82): die zugehörigen Tests
tragen `component` — nur dieser Marker hebt die E36-Netzsperre für den
Loopback-Verkehr auf.

**So nutzt P9.T4 die Fakes (L2-Suite):**

.. code-block:: python

    ha = FakeHaServer(token="test-ha")
    llm = FakeOpenAiServer(token="test-llm")
    await ha.start()
    await llm.start()
    # Manager-Subprozess mit auf die Fake-Ports zeigenden Env starten:
    env = {**ha.env_overrides(), **llm.env_overrides()}
    # start_manager_process(env_overrides=env) bzw. manager_proc + env
    ...
    await llm.stop()
    await ha.stop()

`env_overrides()` liefert exakt die `app/config.py`-Variablen (`HA_BASE_URL`,
`HA_TOKEN`, `LLM_BASE_URL`, `LLM_API_KEY`), damit der Manager die Fakes statt der
echten Endpunkte anspricht.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Final, Mapping, Optional, Sequence

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.ha_client import HA_SERVICE_PATH_TEMPLATE, HA_STATES_PATH
from app.llm_client import DEEPSEEK_CHAT_PATH, JEV_SYSTEMONE_PATH

__all__ = [
    "FakeServiceError",
    "FakeHaServer",
    "FakeOpenAiServer",
    "HA_STATES_PATH",
    "HA_SERVICE_PATH_TEMPLATE",
    "SYSTEMONE_PATH",
    "CHAT_PATH",
    "DEFAULT_HA_TOKEN",
    "DEFAULT_LLM_KEY",
    "DEFAULT_STATES",
    "DEFAULT_NOUL",
    "DEFAULT_TARGET_CHOICE",
    "DEFAULT_TARGET_CONFIDENCE",
    "DEFAULT_PROBABILITIES",
    "DEFAULT_CHAT_CONTENT",
    "DEFAULT_JEV_MODEL",
    "DEFAULT_CHAT_MODEL",
]

#: Gateway-Präfix des LLM-Endpunkts (Default `LLM_BASE_URL=…/zen/v1`, PLAN §4).
#: `PLAN.md:541` nennt wörtlich `POST /zen/v1/systemone` und
#: `POST /zen/v1/chat/completions`.
LLM_API_PREFIX: Final[str] = "/zen/v1"
#: Vollständige Pfade, wie sie der echte `JevClient`/`DeepSeekClient` aufruft
#: (Basis-URL `http://host:port/zen/v1` + Pfad aus `app.llm_client`).
SYSTEMONE_PATH: Final[str] = LLM_API_PREFIX + JEV_SYSTEMONE_PATH
CHAT_PATH: Final[str] = LLM_API_PREFIX + DEEPSEEK_CHAT_PATH

#: Platzhalter-Tokens (bewusst **keine** Secrets, nur Marker im Auth-Header).
DEFAULT_HA_TOKEN: Final[str] = "test-ha-token"
DEFAULT_LLM_KEY: Final[str] = "test-llm-key"

#: Verifizierte Jev-Antwort (P0.T2/STATE §3): Intent `noul=0.97`, Ziel `light`.
DEFAULT_NOUL: Final[float] = 0.97
DEFAULT_TARGET_CHOICE: Final[str] = "light"
DEFAULT_TARGET_CONFIDENCE: Final[float] = 1.0
DEFAULT_PROBABILITIES: Final[Mapping[str, float]] = {"light": 0.99, "switch": 0.01}
#: Verifizierte Chat-Antwort (JSON-Modus, P4.T2-Live-Smoke `{antwort,grad}`).
DEFAULT_CHAT_CONTENT: Final[str] = '{"antwort":"ok","grad":2}'
DEFAULT_JEV_MODEL: Final[str] = "jev-1.13"
DEFAULT_CHAT_MODEL: Final[str] = "deepseek-v4.1-flash"

#: Deterministische Entity-Liste der `GET /api/states`-Antwort.
#: 7 Entities aus den Ziel-Domains (`app.config.entity_domains`) + 1 Fremd-Domain
#: (`sensor.*`) ⇒ der echte Cache filtert sichtbar.  Keine Zufallswerte.
DEFAULT_STATES: Final[tuple[Mapping[str, Any], ...]] = (
    {"entity_id": "light.wohnzimmer", "state": "on", "attributes": {"friendly_name": "Wohnzimmer"}},
    {"entity_id": "switch.steckdose", "state": "off", "attributes": {}},
    {"entity_id": "cover.rollo", "state": "closed", "attributes": {}},
    {"entity_id": "climate.heizung", "state": "heat", "attributes": {}},
    {"entity_id": "media_player.tv", "state": "idle", "attributes": {}},
    {"entity_id": "scene.abend", "state": "scening", "attributes": {}},
    {"entity_id": "script.putzen", "state": "off", "attributes": {}},
    {"entity_id": "sensor.temperatur", "state": "21.5", "attributes": {}},
)

#: Frist für Start/Stop des uvicorn-Threads (Sekunden).
_START_TIMEOUT: Final[float] = 15.0
_STOP_TIMEOUT: Final[float] = 15.0
#: Obergrenze des uvicorn-Graceful-Shutdown (eine in-flight-Latenz darf das
#: `stop()` nicht blockieren).
_GRACEFUL_SHUTDOWN_S: Final[float] = 2.0


class FakeServiceError(RuntimeError):
    """Programmierfehler/ungültige Konfiguration eines Fake-Service."""


# ── In-Process-uvicorn im Thread ───────────────────────────────────────────
class _UvicornThread:
    """Startet/stoppt eine FastAPI-App als uvicorn-Server in einem Thread.

    Der Event-Loop des Servers lebt **im Thread** — der Test-Prozess spricht ihn
    über Loopback an.  `uvicorn.Server.run()` installiert in einem Nicht-Main-
    Thread keine Signal-Handler (`capture_signals` ist dann ein No-op), deshalb
    ist der Betrieb im Thread zulässig.  Ein injizierter Port 0 lässt das OS den
    Port vergeben; er wird nach dem Start aus dem gebundenen Socket gelesen.
    """

    def __init__(self, app: FastAPI, *, host: str, port: int) -> None:
        if port < 0:
            raise FakeServiceError(f"port muss ≥ 0 sein (0 = OS vergibt), ist {port!r}")
        self.host: str = host
        self.port: int = int(port)
        self._app = app
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False

    @property
    def running(self) -> bool:
        """True, solange der uvicorn-Thread läuft und der Server gestartet ist."""
        return self._started and self._thread is not None and self._thread.is_alive()

    async def start(self) -> "_UvicornThread":
        """Server im Thread starten (idempotent); wartet auf `server.started`."""
        if self.running:
            return self
        if self._started:
            raise FakeServiceError("Server ist gestartet, aber der Thread ist tot")
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            name=f"fake-uvicorn-{self.host}:{self.port}",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + _START_TIMEOUT
        while not server.started:
            if not thread.is_alive():
                raise FakeServiceError(
                    "uvicorn-Thread endete vor dem Start "
                    f"(is_alive=False, Port {self.port})"
                )
            if time.monotonic() > deadline:
                server.should_exit = True
                raise FakeServiceError(
                    f"uvicorn nicht binnen {_START_TIMEOUT:g}s gestartet (Port {self.port})"
                )
            await asyncio.sleep(0.01)
        # Bei Port 0 den real gebundenen Port aus dem Socket lesen.
        if self.port == 0:
            servers = server.servers or []
            sockets = servers[0].sockets if servers else []
            if not sockets:
                server.should_exit = True
                raise FakeServiceError("uvicorn meldet keinen gebundenen Socket")
            self.port = int(sockets[0].getsockname()[1])
        self._server = server
        self._thread = thread
        self._started = True
        return self

    async def stop(self) -> None:
        """`should_exit` setzen und den Thread einsammeln (idempotent)."""
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        self._started = False
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=_STOP_TIMEOUT)
            if thread.is_alive():  # pragma: no cover - Notausstieg, sollte nie greifen.
                if server is not None:
                    server.force_exit = True
                thread.join(timeout=_STOP_TIMEOUT)
                if thread.is_alive():
                    raise FakeServiceError("uvicorn-Thread ließ sich nicht beenden")

    async def __aenter__(self) -> "_UvicornThread":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


# ── Fake Home Assistant ────────────────────────────────────────────────────
class FakeHaServer(_UvicornThread):
    """Deterministischer HA-REST-Stub (`/api/states`, `/api/services/…`).

    Zählt Aufrufe (`states_requests`, `service_calls`) und erzwingt per
    Konfiguration HTTP-Fehler (`states_status`, `service_status` ≠ 200) sowie
    Latenz (`states_latency`, `service_latency` in Sekunden) — beides mappt der
    echte `HomeAssistantClient` auf `HaUnavailableError`
    („Home Assistant antwortet nicht.").  Ein erwartetes Token
    (`token=…`) prüft den `Authorization: Bearer`-Header.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: Optional[str] = DEFAULT_HA_TOKEN,
        states: Optional[Sequence[Mapping[str, Any]]] = None,
        states_status: int = 200,
        service_status: int = 200,
        states_latency: float = 0.0,
        service_latency: float = 0.0,
    ) -> None:
        if states_status < 100 or states_status > 599:
            raise FakeServiceError(f"states_status ungültig: {states_status!r}")
        if service_status < 100 or service_status > 599:
            raise FakeServiceError(f"service_status ungültig: {service_status!r}")
        super().__init__(self._build_app(), host=host, port=port)
        self.expected_token: Optional[str] = token
        self._states: list[Mapping[str, Any]] = [
            dict(state) for state in (states if states is not None else DEFAULT_STATES)
        ]
        self.states_status = int(states_status)
        self.service_status = int(service_status)
        self.states_latency = float(states_latency)
        self.service_latency = float(service_latency)
        #: Aufrufzähler + Mitschnitte (Diagnose/Assertions).
        self.states_requests = 0
        self.states_authorization: Optional[str] = None
        self.service_calls: list[dict[str, Any]] = []
        self.unauthorized_requests = 0
        #: Vom Fake protokollierte (nicht-fatale) Request-Fehler.
        self.errors: list[str] = []

    @property
    def base_url(self) -> str:
        """Basis-URL für `HomeAssistantClient(base_url=…)`."""
        return f"http://{self.host}:{self.port}"

    def env_overrides(self) -> dict[str, str]:
        """`app/config.py`-Env für den Manager: HA zeigt auf diesen Fake."""
        key = self.expected_token if self.expected_token is not None else DEFAULT_HA_TOKEN
        return {"HA_BASE_URL": self.base_url, "HA_TOKEN": key}

    # ── Konfiguration zur Laufzeit ─────────────────────────────────────
    def set_states(self, states: Sequence[Mapping[str, Any]]) -> None:
        """Entity-Liste der nächsten `GET /api/states`-Antwort ersetzen."""
        self._states = [dict(state) for state in states]

    def set_states_status(self, status: int) -> None:
        """HTTP-Status der `GET /api/states`-Antwort setzen (≠ 200 = Fehler)."""
        self.states_status = int(status)

    def set_service_status(self, status: int) -> None:
        """HTTP-Status der Service-Antwort setzen (≠ 200 = Fehler)."""
        self.service_status = int(status)

    def set_states_latency(self, latency: float) -> None:
        """Verzögerung vor der `GET /api/states`-Antwort (Timeout-Injektion)."""
        self.states_latency = float(latency)

    def set_service_latency(self, latency: float) -> None:
        """Verzögerung vor der Service-Antwort (Timeout-Injektion)."""
        self.service_latency = float(latency)

    def service_call_count(self) -> int:
        """Anzahl registrierter Service-Aufrufe."""
        return len(self.service_calls)

    # ── App ────────────────────────────────────────────────────────────
    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="Fake Home Assistant",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        def _unauthorized(request: Request) -> Optional[JSONResponse]:
            if self.expected_token is None:
                return None
            if request.headers.get("authorization") == f"Bearer {self.expected_token}":
                return None
            self.unauthorized_requests += 1
            return JSONResponse({"message": "Unauthorized"}, status_code=401)

        @app.get(HA_STATES_PATH)
        async def get_states(request: Request) -> JSONResponse:
            self.states_requests += 1
            self.states_authorization = request.headers.get("authorization")
            denied = _unauthorized(request)
            if denied is not None:
                return denied
            if self.states_latency > 0:
                await asyncio.sleep(self.states_latency)
            return JSONResponse(self._states, status_code=self.states_status)

        @app.post(HA_SERVICE_PATH_TEMPLATE)
        async def call_service(domain: str, service: str, request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception as exc:  # noqa: BLE001 - ungültiger Body sichtbar machen.
                self.errors.append(f"ungültiger JSON-Body: {exc!r}")
                return JSONResponse({"message": "Invalid JSON"}, status_code=400)
            if not isinstance(body, Mapping):
                self.errors.append(f"Body ist kein Objekt: {type(body).__name__}")
                return JSONResponse({"message": "Body must be an object"}, status_code=400)
            self.service_calls.append(
                {
                    "domain": domain,
                    "service": service,
                    "path": HA_SERVICE_PATH_TEMPLATE.format(domain=domain, service=service),
                    "body": dict(body),
                    "entity_id": body.get("entity_id"),
                    "authorization": request.headers.get("authorization"),
                }
            )
            denied = _unauthorized(request)
            if denied is not None:
                return denied
            if self.service_latency > 0:
                await asyncio.sleep(self.service_latency)
            # HA antwortet mit der Liste der geänderten States; hier wird die
            # angefragte Entity deterministisch gespiegelt.
            changed = [{"entity_id": body.get("entity_id"), "state": "on", "changed": True}]
            return JSONResponse(changed, status_code=self.service_status)

        return app


# ── Fake OpenAI / LLM-Gateway ──────────────────────────────────────────────
class FakeOpenAiServer(_UvicornThread):
    """Deterministischer Stub für `POST /zen/v1/systemone` + `/chat/completions`.

    `systemone` antwortet in der **verifizierten** P0.T2-Form
    (`answers.intent.noul` + `answers.target.choice`/`.confidence`), der
    Chat-Endpunkt OpenAI-kompatibel (`choices[0].message.content`).  Beide
    Antworten sind konfigurierbar; Latenz (`systemone_latency`,
    `chat_latency`) und HTTP-Fehler (`…_status` ≠ 200) sind injizierbar.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: Optional[str] = DEFAULT_LLM_KEY,
        noul: float = DEFAULT_NOUL,
        choice: str = DEFAULT_TARGET_CHOICE,
        confidence: float = DEFAULT_TARGET_CONFIDENCE,
        probabilities: Optional[Mapping[str, float]] = None,
        chat_content: str = DEFAULT_CHAT_CONTENT,
        systemone_status: int = 200,
        chat_status: int = 200,
        systemone_latency: float = 0.0,
        chat_latency: float = 0.0,
    ) -> None:
        if systemone_status < 100 or systemone_status > 599:
            raise FakeServiceError(f"systemone_status ungültig: {systemone_status!r}")
        if chat_status < 100 or chat_status > 599:
            raise FakeServiceError(f"chat_status ungültig: {chat_status!r}")
        super().__init__(self._build_app(), host=host, port=port)
        self.expected_token: Optional[str] = token
        self.noul = float(noul)
        self.choice = choice
        self.confidence = float(confidence)
        self.probabilities: dict[str, float] = dict(
            probabilities if probabilities is not None else DEFAULT_PROBABILITIES
        )
        self.chat_content = chat_content
        self.systemone_status = int(systemone_status)
        self.chat_status = int(chat_status)
        self.systemone_latency = float(systemone_latency)
        self.chat_latency = float(chat_latency)
        #: Aufrufzähler + Mitschnitte (Diagnose/Assertions).
        self.systemone_requests = 0
        self.chat_requests = 0
        self.systemone_bodies: list[dict[str, Any]] = []
        self.chat_bodies: list[dict[str, Any]] = []
        self.last_systemone_state: Optional[str] = None
        self.last_systemone_questions: Optional[dict[str, Any]] = None
        self.last_chat_messages: Optional[list[Any]] = None
        self.systemone_authorization: Optional[str] = None
        self.chat_authorization: Optional[str] = None
        self.unauthorized_requests = 0
        self.errors: list[str] = []

    @property
    def base_url(self) -> str:
        """Basis-URL **ohne** Gateway-Präfix (nur Diagnose)."""
        return f"http://{self.host}:{self.port}"

    @property
    def llm_base_url(self) -> str:
        """`LLM_BASE_URL` inkl. `/zen/v1` — so konstruieren die echten Clients."""
        return f"http://{self.host}:{self.port}{LLM_API_PREFIX}"

    @property
    def systemone_url(self) -> str:
        """Vollständige `systemone`-URL (für Diagnose/Literale)."""
        return f"{self.llm_base_url}{JEV_SYSTEMONE_PATH}"

    @property
    def chat_url(self) -> str:
        """Vollständige `/chat/completions`-URL (für Diagnose/Literale)."""
        return f"{self.llm_base_url}{DEEPSEEK_CHAT_PATH}"

    def env_overrides(self) -> dict[str, str]:
        """`app/config.py`-Env für den Manager: LLM zeigt auf diesen Fake."""
        key = self.expected_token if self.expected_token is not None else DEFAULT_LLM_KEY
        return {"LLM_BASE_URL": self.llm_base_url, "LLM_API_KEY": key}

    # ── Feste Antwortformen ────────────────────────────────────────────
    def systemone_payload(self) -> dict[str, Any]:
        """`systemone`-Antwort in der verifizierten Form (P0.T2/STATE §3)."""
        return {
            "model": DEFAULT_JEV_MODEL,
            "answers": {
                "intent": {"type": "noul", "noul": self.noul},
                "target": {
                    "type": "choice",
                    "choice": self.choice,
                    "confidence": self.confidence,
                    "probabilities": dict(self.probabilities),
                },
            },
            "usage": {"prompt_tokens": 444, "completion_tokens": 91},
        }

    def chat_payload(self) -> dict[str, Any]:
        """OpenAI-kompatible Chat-Antwort (`choices[0].message.content`)."""
        return {
            "model": DEFAULT_CHAT_MODEL,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": self.chat_content,
                        "reasoning_content": "kurz",
                    },
                }
            ],
            "usage": {"prompt_tokens": 118, "completion_tokens": 163, "total_tokens": 281},
        }

    # ── Konfiguration zur Laufzeit ─────────────────────────────────────
    def set_systemone_status(self, status: int) -> None:
        """HTTP-Status des `systemone`-Endpunkts setzen (≠ 200 = Fehler)."""
        self.systemone_status = int(status)

    def set_chat_status(self, status: int) -> None:
        """HTTP-Status des Chat-Endpunkts setzen (≠ 200 = Fehler)."""
        self.chat_status = int(status)

    def set_systemone_latency(self, latency: float) -> None:
        """Verzögerung vor der `systemone`-Antwort (Timeout-Injektion)."""
        self.systemone_latency = float(latency)

    def set_chat_latency(self, latency: float) -> None:
        """Verzögerung vor der Chat-Antwort (Timeout-Injektion)."""
        self.chat_latency = float(latency)

    # ── App ────────────────────────────────────────────────────────────
    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="Fake OpenAI Gateway",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        def _unauthorized(request: Request) -> Optional[JSONResponse]:
            if self.expected_token is None:
                return None
            if request.headers.get("authorization") == f"Bearer {self.expected_token}":
                return None
            self.unauthorized_requests += 1
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        @app.post(SYSTEMONE_PATH)
        async def systemone(request: Request) -> JSONResponse:
            body = await _read_json(request, self.errors)
            self.systemone_requests += 1
            self.systemone_authorization = request.headers.get("authorization")
            if isinstance(body, Mapping):
                self.systemone_bodies.append(dict(body))
                state = body.get("state")
                self.last_systemone_state = state if isinstance(state, str) else None
                questions = body.get("questions")
                self.last_systemone_questions = (
                    dict(questions) if isinstance(questions, Mapping) else None
                )
            denied = _unauthorized(request)
            if denied is not None:
                return denied
            if self.systemone_latency > 0:
                await asyncio.sleep(self.systemone_latency)
            return JSONResponse(self.systemone_payload(), status_code=self.systemone_status)

        @app.post(CHAT_PATH)
        async def chat(request: Request) -> JSONResponse:
            body = await _read_json(request, self.errors)
            self.chat_requests += 1
            self.chat_authorization = request.headers.get("authorization")
            if isinstance(body, Mapping):
                self.chat_bodies.append(dict(body))
                messages = body.get("messages")
                self.last_chat_messages = list(messages) if isinstance(messages, list) else None
            denied = _unauthorized(request)
            if denied is not None:
                return denied
            if self.chat_latency > 0:
                await asyncio.sleep(self.chat_latency)
            return JSONResponse(self.chat_payload(), status_code=self.chat_status)

        return app


async def _read_json(request: Request, errors: list[str]) -> Any:
    """Body lesen; Fehler sichtbar protokollieren (kein stiller Fallback)."""
    try:
        return await request.json()
    except Exception as exc:  # noqa: BLE001 - ungültiger Body sichtbar machen.
        errors.append(f"ungültiger JSON-Body: {exc!r}")
        return None
