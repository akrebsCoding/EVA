"""Tests des Dashboard-Frontends + der P8.D2-Verdrahtung in `app/main.py`.

Drei Ebenen, alle **ohne** Browser (Layer **L0**, rein, kein Netz):

* **Server** – die Seite `/dashboard` (+ Slash-Alias) liefert 200 `text/html`
  mit nichtleerem Body, unbekannte Pfade 404, die Assets werden ausgeliefert,
  der `/static`-Mount ist nicht traversierbar, und die API bleibt unter
  `/api/*` erreichbar.
* **Schreibzaun** – der Manager hat **genau zwei** Schreib-Routen: E96
  (`POST /api/config/oww-threshold`, eine Zahl) und E111
  (`PUT /api/config`, Multi-Field). Auf **jeder** Lese-Route muss jeder
  schreibende HTTP-Verb weiterhin **405** liefern; auf den Schreib-Routen
  selbst antwortet der jeweilige Verb **nur bei aktivem Schreibmodus** mit
  2xx, sonst **403**.  Zusätzlich wird die *Quelle* geprüft:
  `app/dashboard.py` enthält **keinen** weiteren Schreib-Decorator – eine
  dritte Schreibroute macht den Test rot.
* **Frontend-Dateien** – Existenz/Größe, keine externen Ressourcen, keine
  hartkodierten Geheimnisse, kein HTML-Injection-Pfad, und (der eigentliche
  Konsistenztest) jede im JS genannte `/api/…`-Route existiert auch in
  `app/dashboard.py`.

Zur Prüfung "keine externen Ressourcen / keine Secrets" wird der Quelltext
**erst um Kommentare gekürzt** (Scanner in `_strip_comments`) und danach der
Rest geprüft. Ohne das Kürzen würde ein Kommentar wie „keine Credentials"
fälschlich als Secret-Durchschlag gewertet – und umgekehrt würde ein echtes
`"http://…"` in einem Kommentar den Test grün fälschen.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Final, Optional

import httpx
import pytest

from app import dashboard, main as main_mod

pytestmark = pytest.mark.unit

#: Die drei Frontend-Dateien – geprüft wird, was tatsächlich ausgeliefert wird.
STATIC_ROOT: Final[Path] = main_mod.STATIC_DIR / "dashboard"
FRONTEND_FILES: Final[tuple[str, ...]] = ("index.html", "style.css", "app.js")

#: Die API-Routen des Managers – **abgeleitet** aus `app/dashboard.py`, nicht
#: hier festgeschrieben (sonst könnte der Test die Quelle nicht widerlegen).
API_PREFIX: Final[str] = dashboard.router.prefix


def _source() -> str:
    return (Path(dashboard.__file__)).read_text(encoding="utf-8")


#: `@router.get("/status")` / `@router.post(...)` → (Methode, Pfad).
_ROUTE_DECORATOR_RE: Final[re.Pattern[str]] = re.compile(
    r"@router\.(?P<method>get|post|put|patch|delete|head|options)\(\s*"
    r"[\"'](?P<path>[^\"']*)[\"']",
    re.IGNORECASE,
)


def api_routes() -> list[tuple[str, str]]:
    """Aus `app/dashboard.py`: ``[(Methode, vollständiger Pfad)]`` aller Routen.

    Eine **Liste** und keine Dict: es gibt nur GET-Routen, ein Dict würde alle
    sechs unter dem Schlüssel `"GET"` zusammenfassen und die Prüfung blind machen.
    """
    return [
        (match.group("method").upper(), f"{API_PREFIX}{match.group('path')}")
        for match in _ROUTE_DECORATOR_RE.finditer(_source())
    ]


def api_paths() -> set[str]:
    # P10.T3: `/api/pairing` ist das Onboarding-Wizard-Endpoint (CLI-Summary,
    # `deploy/onboarding/wizard.py`) und bewusst **kein** Dashboard-Feature —
    # die Abdeckungspflicht gilt nur für Routen, die das Frontend auch nutzen
    # soll. Die Ausnahme ist hier dokumentiert statt still weggefiltert.
    return {path for _method, path in api_routes()} - {"/api/pairing"}


def frontend_routes() -> set[str]:
    """Alle ``/api/<name>``-Literale in HTML und JS (Quelle der Wahrheit: JS).

    Zwei Details, die den Test sonst blöde machen würden:

    * Kommentare werden **gekürzt** – sonst zählte ein Prosa-Satz wie „kein
      `/api/services`" als echter Aufruf und der Test schlüge an einer
      Dokumentation fehl.
    * Das Muster erlaubt **mehrere** Pfadsegmente, damit auch die zweigliedrige
      Schreibroute `/api/config/oww-threshold` erkannt wird (E96); vorher
      matchte `[a-z_]+` und schnitt das `oww-threshold` ab.
    """
    pattern = re.compile(r"/api/([a-z0-9_-]+(?:/[a-z0-9_-]+)*)")
    found: set[str] = set()
    for name in FRONTEND_FILES:
        code = _strip_comments(_read(name))
        found.update(f"/api/{part}" for part in pattern.findall(code))
    return found


def _read(name: str) -> str:
    return (STATIC_ROOT / name).read_text(encoding="utf-8")


# ── Kommentar-Kürzer ───────────────────────────────────────────────────
def _strip_comments(text: str) -> str:
    """Entfernt `/* */`, `//`-Zeilen- und `<!-- -->`-Kommentare.

    Der Scanner merkt sich Zeichenketten (`'`, `"`, `` ` ``), damit ein `//`
    **innerhalb** eines Strings (z. B. ``"http://x"``) nicht als Zeilenkommentar
    fehlgedeutet wird und die Prüfung nicht blind wird. Bewusst simpel – für
    Regex-Einrückung reicht es.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    quote: Optional[str] = None
    while index < length:
        char = text[index]
        if quote is not None:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
            out.append(char)
            index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            index = length if end == -1 else end + 3
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = length if end == -1 else end
            continue
        out.append(char)
        index += 1
    return "".join(out)


# ── App-Fixture ────────────────────────────────────────────────────────
@pytest.fixture
def app() -> Any:
    """Die **echte** App aus `create_app()` – kein Attrappen-Build.

    `ASGITransport` führt den Lifespan nicht aus ⇒ kein mDNS, kein HA-Ping,
    kein Modell ⇒ L0 bleibt netzfrei (`conftest._block_network`).
    """
    return main_mod.create_app()


async def _request(application: Any, method: str, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as client:
        return await client.request(method, path)


def call(application: Any, method: str, path: str) -> httpx.Response:
    """Synchroner Wrapper (Repo-Stil: `asyncio.run` im Test)."""
    return asyncio.run(_request(application, method, path))


# ══════════════════════════════════════════════════════════════════════
#  1 · Server: die Seite
# ══════════════════════════════════════════════════════════════════════
def test_dashboard_liefert_html_mit_inhalt(app: Any) -> None:
    """`/dashboard` ⇒ 200, `text/html`, nichtleer, echtes HTML-Dokument."""
    response = call(app, "GET", main_mod.DASHBOARD_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert len(response.content) > 500
    body = response.text
    assert body.lstrip().lower().startswith("<!doctype html")
    # Die Seite darf nicht leer "geliefert" sein, nur weil die Datei fehlt.
    assert "</html>" in body
    assert 'id="log-rows"' in body


def test_dashboard_slash_alias_liefert_dieselbe_seite(app: Any) -> None:
    """`/dashboard/` ⇒ 200 (kein 404 wegen des Slash)."""
    response = call(app, "GET", f"{main_mod.DASHBOARD_PATH}/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == call(app, "GET", main_mod.DASHBOARD_PATH).text


@pytest.mark.parametrize(
    "path",
    ["/nope", "/dashboard/gibtsnicht", "/dashboard/index.html/extra", "/api/gibtsnicht"],
)
def test_unbekannte_pfade_sind_404(app: Any, path: str) -> None:
    """Nur die echten Pfade antworten – kein 404 als „leere Seite" kaschiert."""
    assert call(app, "GET", path).status_code == 404


def test_assets_werden_ausgeliefert(app: Any) -> None:
    """`style.css` und `app.js` kommen als echte Datei heraus."""
    for name, marker in (("style.css", "--bg"), ("app.js", "POLL_INTERVAL_MS")):
        response = call(app, "GET", f"/static/dashboard/{name}")
        assert response.status_code == 200, name
        assert len(response.content) > 200, name
        assert marker in response.text, name


def test_statischer_mount_ist_nicht_traversierbar(app: Any) -> None:
    """`/static` darf nicht aus seinem Wurzelverzeichnis herausführen."""
    for path in ("/static/..%2fmain.py", "/static/../main.py", "/static/dashboard/../../main.py"):
        assert call(app, "GET", path).status_code != 200, path


def test_health_route_unveraendert(app: Any) -> None:
    """`/health` bleibt 200 mit Status-Body (Compose-Healthcheck hängt dran)."""
    response = call(app, "GET", main_mod.HEALTH_PATH)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ══════════════════════════════════════════════════════════════════════
#  2 · Read-only-Invariante – **eine** Ausnahme (E96/P9.T1)
# ══════════════════════════════════════════════════════════════════════
#: Die **Schreib-Routen** des Managers.  Jeder weitere Schreibpfad muss
#: diese Tests rot machen – deshalb werden sie hier als Konstante
#: festgeschrieben und in
#: `test_es_gibt_genau_zwei_schreibrouten_und_sie_sind_die_bekannten` hart
#: verglichen.  E96 = `POST /api/config/oww-threshold` (eine Zahl),
#: E111 = `PUT /api/config` (Multi-Field).
WRITE_ROUTES: Final[tuple[tuple[str, str], ...]] = (
    ("POST", "/api/config/oww-threshold"),
    ("PUT", "/api/config"),
)


def test_api_routen_sind_erreichbar_und_json(app: Any) -> None:
    """Alle **lesenden** Routen liefern 200 JSON (E96/E111 ändern daran nichts)."""
    write_paths = {path for _method, path in WRITE_ROUTES}
    for _method, path in api_routes():
        if path in write_paths:
            continue
        response = call(app, "GET", path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("application/json"), path


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_kein_schreibverb_auf_irgendeiner_api_route(app: Any, method: str) -> None:
    """**Jede** Lese-Route bleibt schreibfeindlich (405).

    Die zwei Schreib-Routen sind die Ausnahme: `POST /api/config/oww-threshold`
    (E96) und `PUT /api/config` (E111) antworten ohne Schreibmodus mit **403**
    und verändern nichts – beides ist „nicht 2xx".
    """
    write_routes = set(WRITE_ROUTES)
    for _m, path in api_routes():
        response = call(app, method, path)
        assert not (200 <= response.status_code < 300), f"{method} {path}"
        if (method, path) in write_routes:
            assert response.status_code == 403   # Schreibmodus aus
        else:
            assert response.status_code == 405, f"{method} {path}"


def test_es_gibt_genau_zwei_schreibrouten_und_sie_sind_die_bekannten() -> None:
    """Die Schreibfläche ist **zwei** Routen, sie sind hier festgeschrieben.

    E96 = `POST /api/config/oww-threshold`, E111 = `PUT /api/config`.
    Wer eine dritte baut (etwa `/api/services`), bekommt diesen Test rot –
    das ist der eigentliche E96/E111-Zaun.
    """
    writes = sorted((method, path) for method, path in api_routes() if method != "GET")
    assert writes == sorted(WRITE_ROUTES), writes


def test_dashboard_quelle_enthaelt_genau_diese_schreibrouten() -> None:
    """Quell-Ebene: GET-Routen **plus** genau ein POST (E96) und ein PUT (E111)."""
    found = api_routes()
    assert found, "keine @router-Route in app/dashboard.py gefunden"
    methods = {method for method, _path in found}
    assert methods == {"GET", "POST", "PUT"}, sorted(methods)
    assert sum(1 for method, _ in found if method == "POST") == 1
    assert sum(1 for method, _ in found if method == "PUT") == 1


def test_frontend_hat_genau_zwei_schreiboperationen() -> None:
    """Im JS gibt es **einen** POST (E96) und **einen** PUT (E111) – nichts sonst.

    Der POST schreibt ausschließlich die Wake-Schwelle, der PUT ausschließlich
    die geänderten E111-Felder; es gibt kein DELETE/PATCH, keinen dritten
    `fetch` mit Body und keinen HA-/Service-Aufruf.
    """
    code = _strip_comments(_read("app.js"))
    assert code.count('method: "POST"') == 1
    assert code.count('method: "PUT"') == 1
    assert "DELETE" not in code and "PATCH" not in code
    # Drei `fetch(` insgesamt: der GET-Poller, der eine POST, der eine PUT.
    assert code.count("fetch(") == 3
    # `JSON.stringify` kommt dreimal vor: einmal im GET-Query-Helfer, je
    # einmal als `body:` des POST und des PUT.
    assert code.count("JSON.stringify(") == 3
    assert code.count("body: JSON.stringify(body)") == 2
    assert re.search(
        r"postJson\(\s*API_WRITE_THRESHOLD\s*,\s*\{\s*threshold:\s*value\s*,?\s*\}\s*\)",
        code,
    )
    assert re.search(
        r"putJson\(\s*API_WRITE_CONFIG\s*,\s*changed\s*,\s*token\s*\)",
        code,
    )
    # Kein Reload, kein „trotzdem neu laden durch Neuladung der Seite".
    assert "location.reload" not in code
    assert "location.href" not in code


def test_frontend_zeigt_den_tuner_nur_wenn_der_server_es_erlaubt() -> None:
    """Die Schreib-UI ist im HTML **versteckt** und kommt vom `write_enabled`.

    Ein manipuliertes HTML allein genügt also nicht – ohne die
    Server-Bestätigung bleibt der Tuner unsichtbar und der POST liefert 403.
    """
    html = _read("index.html")
    assert 'id="wake-tuner"' in html
    # Das `hidden`-Attribut steht **am** Element, nicht per JavaScript gesetzt.
    assert re.search(r'<div class="tuner" id="wake-tuner" hidden>', html), (
        "der Tuner muss ausgeliefert versteckt sein"
    )
    code = _strip_comments(_read("app.js"))
    assert "data.write_enabled === true" in code
    # Und es gibt im Frontend keinen weiteren Schreibweg (1 GET + 1 POST + 1 PUT).
    assert "fetch(" in code and code.count("fetch(") == 3


def test_frontend_zeigt_den_api_token_nur_wenn_der_server_es_verlangt() -> None:
    """Das Token-Feld wird im HTML **versteckt** geliefert (E110/E111).

    Sichtbar wird es ausschließlich über die Server-Bestätigung
    `write_token_required: true` aus `GET /api/config` – ohne sie bleibt es
    unsichtbar und es wird kein `Authorization`-Header gesendet.
    """
    html = _read("index.html")
    assert 'id="cfg-token-field"' in html
    # Das `hidden`-Attribut steht **am** Element, nicht per JavaScript gesetzt.
    assert re.search(r'id="cfg-token-field" hidden>', html), (
        "das Token-Feld muss ausgeliefert versteckt sein"
    )
    assert 'id="cfg-api-token"' in html
    code = _strip_comments(_read("app.js"))
    assert "data.write_token_required === true" in code
    # Der Header entsteht nur aus dem (leeren) Eingabefeld – kein Literal.
    assert "Bearer " + "${" in code


# ══════════════════════════════════════════════════════════════════════
#  3 · Konsistenz Frontend ↔ API-Vertrag
# ══════════════════════════════════════════════════════════════════════
def test_frontend_referenziert_nur_existierende_api_routen() -> None:
    """Jede `/api/…`-Route im HTML/JS existiert auch in `app/dashboard.py`."""
    real = api_paths()
    referenced = frontend_routes()
    assert referenced, "im Frontend wurde keine API-Route gefunden – Test ist blind"
    assert referenced <= real, f"im Frontend, aber nicht in der API: {sorted(referenced - real)}"


def test_frontend_deckt_alle_api_routen_ab() -> None:
    """Das Dashboard nutzt **alle** Routen – keine Area zeigt erfundene Daten."""
    assert frontend_routes() == api_paths()


def test_frontend_fragt_keine_fremden_pfade_ab() -> None:
    """Kein Fetch auf etwas anderes als die API (kein Fremdhost, kein Reload)."""
    code = _strip_comments(_read("app.js"))
    for path in re.findall(r"""["'](/[a-zA-Z0-9_./-]+)["']""", code):
        assert path.startswith("/api/"), f"verdächtiger Pfad im Frontend: {path}"
    assert "location.reload" not in code
    assert "location.href" not in code


# ══════════════════════════════════════════════════════════════════════
#  4 · Frontend-Dateien
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", FRONTEND_FILES)
def test_frontend_datei_existiert_und_ist_nicht_leer(name: str) -> None:
    path = STATIC_ROOT / name
    assert path.is_file(), f"{name} fehlt"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{name} ist leer"
    assert len(text) > 200, f"{name} ist auffällig kurz ({len(text)} B)"


@pytest.mark.parametrize("name", FRONTEND_FILES)
def test_keine_externen_ressourcen(name: str) -> None:
    """Nach dem Kürzen der Kommentare: **keine** absolute http(s)-URL.

    Erklärung der Prüfung: `xmlns`-Deklarationen und Kommentare sind die
    einzigen zulässigen Fundstellen – beides wird entfernt, sodass nur echte
    Referenzen übrig bleiben. Damit ist die Aussage strenger als ein
    `href`-/`src`-Regex: auch ein `fetch("http://…")` oder ein
    `url(https://…)` im CSS würde auffallen. Ziel der Assertion ist der
    Offline-/LAN-Betrieb ohne Internet.
    """
    assert "http://" not in _strip_comments(_read(name))
    assert "https://" not in _strip_comments(_read(name))


@pytest.mark.parametrize("name", FRONTEND_FILES)
def test_keine_hartkodierten_geheimnisse(name: str) -> None:
    """Kein Secret-Name mit Wert im Frontend (Kommentare entfernt)."""
    code = _strip_comments(_read(name))
    pattern = re.compile(
        r"(?i)\b(?:api[_-]?key|apikey|llm[_-]?api[_-]?key|ha[_-]?token|token|secret"
        r"|password|passwd|passphrase|credential|auth[_-]?key)\b"
        r"\s*[:=]\s*[\"'][^\"']{4,}[\"']"
    )
    assert pattern.search(code) is None, f"Secret-artiger Literal in {name}"


def test_frontend_baut_nur_mit_textcontent_und_createelement() -> None:
    """XSS: kein `innerHTML`/`insertAdjacentHTML`/`eval` mit Datenpfad."""
    code = _strip_comments(_read("app.js"))
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert forbidden not in code, forbidden
    assert "textContent" in code
    assert "createElement" in code


def test_polling_konstanten_einmal_und_ueberwacht() -> None:
    """Intervall an **einer** Stelle, Timeout vorhanden, Überlappungsschutz da."""
    code = _strip_comments(_read("app.js"))
    assert code.count("const POLL_INTERVAL_MS") == 1
    assert re.search(r"const POLL_INTERVAL_MS\s*=\s*2000\b", code)
    assert re.search(r"const FETCH_TIMEOUT_MS\s*=\s*5000\b", code)
    assert "AbortController" in code and "signal:" in code
    # Überlappende Requests: ein In-Flight-Set pro Endpunkt.
    assert "inFlight" in code
    assert re.search(r"if \(inFlight\.has\(key\)\) return;", code)


def test_ehrliche_grenzen_sind_im_frontend_benannt() -> None:
    """Die in der API benannten Grenzen werden sichtbar gemacht, nicht kaschiert."""
    code = _strip_comments(_read("app.js"))
    for needle in (
        "transitions_tracked",  # §5-Hinweis „Übergangszeiten werden nicht erfasst"
        "available",            # Historie: verfügbar oder nicht
        "degraded",             # Eingeschränkte Daten
        "source",               # Logquelle memory|file|none
        "EVA_LOG_DIR",       # Folgeschritt bei source: none
        "reason",               # Begründung aus der API übernehmen
    ):
        assert needle in code, needle
    # `ok === null` darf nicht als Erfolg (grün) gelten.
    assert "kein Health-Endpunkt" in code


# ══════════════════════════════════════════════════════════════════════
#  5 · HTML ↔ JS (die Fehlerklasse, die ohne Browser sonst unentdeckt bliebe)
# ══════════════════════════════════════════════════════════════════════
def test_javascript_findet_jede_id_die_es_braucht() -> None:
    """Jede per `$("…")` geholte ID existiert auch im HTML.

    Ohne diesen Test fällt ein Tippfehler in einer ID **erst im Browser** auf
    (`null`-Zugriff ⇒ die ganze Area bleibt leer). Der Test schließt diese
    Lücke, ohne einen Browser zu brauchen.
    """
    used = set(re.findall(r"""\$\(["']([A-Za-z0-9_-]+)["']\)""", _strip_comments(_read("app.js"))))
    assert used, "keine Element-ID im JS gefunden – Test ist blind"
    declared = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", _read("index.html")))
    assert used <= declared, f"im JS benutzt, im HTML nicht deklariert: {sorted(used - declared)}"


def test_html_verweist_auf_vorhandene_assets() -> None:
    """Jedes `href`/`src` im HTML zeigt auf eine Datei, die es wirklich gibt."""
    for ref in re.findall(r"""(?:href|src)=["'](/static/[^"']+)["']""", _read("index.html")):
        relative = ref.removeprefix("/static/dashboard/")
        assert (STATIC_ROOT / relative).is_file(), f"verwaiste Referenz: {ref}"


def test_html_oeffnet_keine_externen_ressourcen() -> None:
    """`href`/`src` sind ausschließlich relativ (kein CDN, kein Fremdhost)."""
    for ref in re.findall(r"""(?:href|src)=["']([^"']+)["']""", _read("index.html")):
        assert not ref.startswith(("http://", "https://", "//")), ref
        assert ref.startswith("/static/") or ref.startswith("#"), ref
