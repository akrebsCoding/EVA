/* EVA-Dashboard (P8.D2) – Poller für die API aus `app/dashboard.py`.
 *
 * Harte Invarianten dieses Skripts:
 *   1. **Nur GET – mit genau zwei Ausnahmen (E96 + E111).** Es gibt genau
 *      **einen** `POST` (`POST /api/config/oww-threshold` mit dem Body
 *      `{"threshold": <Zahl>}`, siehe `saveThreshold()`) und genau **einen**
 *      `PUT` (`PUT /api/config` mit den geänderten E111-Feldern, siehe
 *      `saveConfig()`) im ganzen Skript. Beide schreiben **ausschließlich**
 *      Settings, rufen **niemals** Home Assistant und **niemals** ein Gerät.
 *      Kein TTS, kein LED-Puls, kein `/api/services`, kein Test-Hook.
 *      Solange der Server den Schreibmodus nicht bestätigt (`write_enabled`),
 *      lehnt er Schreibversuche ab (403) – die UI zeigt das als Fehler, nicht
 *      als Erfolg.
 *   2. **Nur die Routen des API-Vertrags** (siehe `API` + `API_WRITE_*`
 *      unten). Keine erfundenen Felder: jeder angezeigte Wert kommt aus der
 *      Antwort.
 *   3. **XSS-sicher.** Serverdaten erreichen das DOM ausschließlich über
 *      `textContent` bzw. `createElement` – nirgends `innerHTML`, `outerHTML`,
 *      `insertAdjacentHTML` oder `document.write` mit Dateninhalt.
 *   4. **Ehrliche Grenzen.** `degraded`, `available: false`, `source: "none"`,
 *      `transitions_tracked: false` und `ok: null` werden sichtbar benannt und
 *      nicht als „alles grün" kaschiert. Ein abgewiesener Schreibversuch
 *      (`403`) wird als „Schreibmodus aus" gezeigt, nicht als Erfolg.
 *   5. **Robust.** Jeder Fetch hat einen Timeout (`AbortController`); ein Fehler
 *      setzt das Feld auf „nicht erreichbar", verschwindet aber nicht lautlos.
 *      Überlappende Requests desselben Endpunkts werden verworfen.
 */
"use strict";

/* ── Konstanten ─────────────────────────────────────────────────────── */

// Poll-Intervall. An **einer** Stelle definiert (Auftrag), nicht verteilt.
const POLL_INTERVAL_MS = 2000;
// Hartes Zeitlimit je Fetch: nach 5 s wird der Request abgebrochen.
const FETCH_TIMEOUT_MS = 5000;

/* Die API des Managers (app/dashboard.py, Router-Prefix "/api"). Reihenfolge wie
 * im Modul. Diese Liste ist der einzige Ort, an dem Routen genannt werden. */
const API = {
  status: "/api/status",
  state: "/api/state",
  logs: "/api/logs",
  history: "/api/history",
  config: "/api/config",
  dependencies: "/api/dependencies",
  wake: "/api/wake",
};

/* Die **zwei** Schreib-Routen des Managers.
 * E96: Body genau {"threshold": <Zahl>} – jede andere Angabe ⇒ 400/422.
 * E111: Body = JSON-Object mit den **geänderten** E111-Feldern (Allowlist
 *       auf dem Server) – leeres Object ⇒ 400, Fremdfeld ⇒ 400. */
const API_WRITE_THRESHOLD = "/api/config/oww-threshold";
const API_WRITE_CONFIG = "/api/config";
/* Reset-Zielwert der Schwellen-Anzeige: der Projekt-Default 0,80 (deutsche
 * Dezimalschreibweise nur im Label, im Wert immer Punkt). */
const DEFAULT_THRESHOLD = 0.8;

/* ── Kurzhelfer ─────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

/** Neues Element; `text` wird immer als **Text** gesetzt, nie als Markup. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Leert einen Container, ohne die Kinder einzeln zu entfernen. */
function clear(node) {
  node.replaceChildren();
}

/** `true`/`false`/`null` → verständlicher Text (null ist "unbekannt"). */
function tri(value, yes, no, unknown) {
  if (value === true) return yes;
  if (value === false) return no;
  return unknown;
}

/** Sekunden → "3 h 12 min" bzw. "45 s". */
function duration(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) return "–";
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min ${s} s`;
  return `${s} s`;
}

/** Zahl mit fester Genauigkeit, sonst "–". */
function num(value, digits) {
  if (typeof value !== "number" || !isFinite(value)) return "–";
  return value.toFixed(digits);
}

/**
 * Phasen-Dauer in Sekunden als Zelle (P9.T0). `null`/`undefined`/nicht
 * numerisch ⇒ "–" = **nicht gemessen**; eine gemessene 0.0 wird als "0.000 s"
 * gezeigt, damit „nicht gemessen" und „gemessen, aber sofort" unterscheidbar
 * bleiben.
 */
function secondsCell(value) {
  if (typeof value !== "number" || !isFinite(value)) return "–";
  return `${num(value, 3)} s`;
}

/** Median einer Zahlenliste; `null` bei leerer Liste (nicht 0). */
function median(values) {
  if (!values.length) return null;
  const ordered = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(ordered.length / 2);
  if (ordered.length % 2 === 1) return ordered[mid];
  return (ordered[mid - 1] + ordered[mid]) / 2;
}

/** Konfigurationswert → Text (bool/Liste/Zahl/Text/null). */
function configValue(value) {
  if (value === null || value === undefined) return "–";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "(leer)";
  if (typeof value === "boolean") return value ? "ja" : "nein";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/* ── Hinweisleisten ─────────────────────────────────────────────────── */

/**
 * Zeigt eine **nicht-rote** Warnleiste. `degraded: true` ist ein Hinweis
 * (unvollständige Daten), kein Fehler ⇒ bewusst gelb, nicht rot. Das Flag wird
 * ausgewertet, damit eine eingeschränkte Antwort auch dann sichtbar wird, wenn
 * die API keine `issues`-Liste mitschickt.
 */
function banner(node, title, issues, degraded) {
  const list = (Array.isArray(issues) ? issues : []).filter((i) => typeof i === "string" && i);
  if (!list.length && degraded !== true) {
    node.hidden = true;
    clear(node);
    return;
  }
  clear(node);
  node.appendChild(el("strong", null, title));
  if (list.length === 1) {
    node.appendChild(document.createTextNode(` ${list[0]}`));
  } else if (list.length > 1) {
    const ul = el("ul");
    for (const issue of list) ul.appendChild(el("li", null, issue));
    node.appendChild(ul);
  } else {
    node.appendChild(document.createTextNode(" eingeschränkte Daten (degraded)."));
  }
  node.hidden = false;
}

/** Markiert einen Knoten als „nicht erreichbar" (Text, kein Overlay). */
function markUnreachable(nodes, reason) {
  for (const node of nodes) {
    if (node) {
      node.textContent = `nicht erreichbar (${reason})`;
      node.className = node.className ? `${node.className} chip-unreachable` : "chip-unreachable";
    }
  }
}

/* ── HTTP ───────────────────────────────────────────────────────────── */

/** Endpunkte, für die gerade ein Request läuft ⇒ kein Request-Spam. */
const inFlight = new Set();

/**
 * EIN GET gegen die API. Bricht nach `FETCH_TIMEOUT_MS` ab. Wirft bei
 * HTTP-Fehlern und Netzfehlern; der Aufrufer stellt den Fehler dar.
 */
async function fetchJson(route, params) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const url = new URL(route, window.location.origin);
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
    // Kein `method` ⇒ GET. Kein Body, keine Header, keine Credentials.
    const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/** Fehler knapp und ohne Technik-Overload beschreiben. */
function reasonOf(error) {
  if (error && error.name === "AbortError") return `Timeout nach ${FETCH_TIMEOUT_MS / 1000} s`;
  return (error && error.message) || "unbekannter Fehler";
}

/**
 * Lädt **einen** Endpunkt. Läuft für `key` schon ein Request, wird dieser
 * Durchgang übersprungen (kein overlapped Request-Spam). Fehler ⇒ `onError`.
 */
async function loadSection(key, route, params, onData, onError) {
  if (inFlight.has(key)) return;
  inFlight.add(key);
  try {
    onData(await fetchJson(route, params));
  } catch (error) {
    onError(reasonOf(error));
  } finally {
    inFlight.delete(key);
  }
}

/**
 * Der **einzige** POST des Dashboards (E96): setzt die Wake-Schwelle.
 * Gleiches Zeitlimit wie `fetchJson`; der Fehler wandert mit Status und
 * Servertext zurück, damit die UI „403 = Schreibmodus aus" sagen kann statt
 * pauschal zu behaupten, es sei gespeichert.
 */
async function postJson(route, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(new URL(route, window.location.origin), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error((payload && payload.detail) || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Der **einzige** PUT des Dashboards (E111): schreibt die geänderten
 * E111-Settings. `token` (leer = ohne) legt den optionalen `Authorization`-
 * Header der E110-Schicht. Gleiches Zeitlimit wie `fetchJson`; der Fehler
 * wandert mit Status und Servertext zurück (401 = Token, 403 = Schreibmodus
 * aus, 400/422 = Inhalt).
 */
async function putJson(route, body, token) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(new URL(route, window.location.origin), {
      method: "PUT",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error((payload && payload.detail) || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

/* ── 1 · Status ─────────────────────────────────────────────────────── */

function renderStatus(data) {
  $("service-name").textContent = data.service || "–";
  $("service-version").textContent = data.version || "–";
  $("t-service").textContent = data.service || "–";
  $("t-version").textContent = data.version || "–";
  $("t-mdns").textContent = tri(data.mdns_running, "läuft", "gestoppt", "unbekannt");
  $("t-devices").textContent = String(
    typeof data.device_count === "number" ? data.device_count : "–"
  );
  $("t-uptime").textContent = duration(data.uptime_seconds);

  const health = data.health || {};
  const apiNode = $("t-health");
  apiNode.textContent = health.router_ok === true ? "ok" : `Fehler (${health.exception || "unbekannt"})`;
  apiNode.className = "tile-value " + (health.router_ok === true ? "" : "chip-unreachable");

  banner($("status-banner"), "Eingeschränkte Daten:", data.issues, data.degraded);

  const rows = $("device-rows");
  clear(rows);
  const devices = Array.isArray(data.devices) ? data.devices : [];
  if (!devices.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Kein Gerät verbunden.");
    td.colSpan = 4;
    tr.appendChild(td);
    rows.appendChild(tr);
    return;
  }
  for (const device of devices) {
    const tr = el("tr");
    tr.appendChild(el("td", null, device.device_id));
    tr.appendChild(el("td", null, tri(device.connected, "ja", "nein", "unbekannt")));
    tr.appendChild(el("td", null, device.state || "–"));
    tr.appendChild(el("td", null, duration(device.connected_seconds)));
    rows.appendChild(tr);
  }
}

function statusFailed(reason) {
  markUnreachable(
    [$("t-service"), $("t-version"), $("t-mdns"), $("t-devices"), $("t-uptime"), $("t-health")],
    reason
  );
  const rows = $("device-rows");
  clear(rows);
  const tr = el("tr");
  const td = el("td", "empty", `Gerätedaten nicht erreichbar (${reason}).`);
  td.colSpan = 4;
  tr.appendChild(td);
  rows.appendChild(tr);
  const bannerNode = $("status-banner");
  clear(bannerNode);
  bannerNode.appendChild(el("strong", null, "Status nicht erreichbar: "));
  bannerNode.appendChild(document.createTextNode(reason));
  bannerNode.hidden = false;
}

/* ── 1b · Abhängigkeiten ─────────────────────────────────────────────── */

/** Setzt ein Ergebnisfeld: grün/bad nur bei echtem `true`/`false`. */
function setProbe(node, ok, text) {
  node.textContent = text;
  node.className = "chip " + (ok === true ? "chip-ok" : ok === false ? "chip-bad" : "chip-unknown");
}

function renderDependencies(data) {
  const rows = $("dependency-rows");
  clear(rows);
  const probes = Array.isArray(data.dependencies) ? data.dependencies : [];

  for (const probe of probes) {
    const tr = el("tr");
    tr.appendChild(el("td", null, probe.name || "–"));

    // `ok === null` heißt "nicht ermittelbar" (kein Health-Endpunkt) – das ist
    // **weder** ein Fehler noch ein Erfolg und wird auch nicht grün dargestellt.
    const status = el("td");
    const chip = el("span");
    if (probe.ok === true) setProbe(chip, true, `ok (${probe.status ?? "–"})`);
    else if (probe.ok === false) setProbe(chip, false, `nicht erreichbar (${probe.status ?? "–"})`);
    else setProbe(chip, null, "kein Health-Endpunkt");
    status.appendChild(chip);
    tr.appendChild(status);

    tr.appendChild(el("td", null, probe.target || "–"));
    tr.appendChild(el("td", null, typeof probe.latency_ms === "number" ? `${num(probe.latency_ms, 1)} ms` : "–"));
    tr.appendChild(el("td", null, probe.detail || probe.reason || "–"));
    rows.appendChild(tr);
  }

  const bannerNode = $("status-banner");
  const issues = (Array.isArray(data.issues) ? data.issues : []).filter((i) => typeof i === "string" && i);
  if (issues.length) {
    clear(bannerNode);
    bannerNode.appendChild(el("strong", null, "Abhängigkeiten:"));
    const ul = el("ul");
    for (const issue of issues) ul.appendChild(el("li", null, issue));
    bannerNode.appendChild(ul);
    bannerNode.hidden = false;
  }
}

function dependenciesFailed(reason) {
  const rows = $("dependency-rows");
  clear(rows);
  const tr = el("tr");
  const td = el("td", "empty", `Abhängigkeiten nicht ermittelbar (${reason}).`);
  td.colSpan = 5;
  tr.appendChild(td);
  rows.appendChild(tr);
}

/* ── 2 · Workflow ────────────────────────────────────────────────────── */

function renderState(data) {
  // Reihenfolge der Zustände kommt **wie geliefert** aus der API – das Frontend
  // hat keine eigene, duplizierte Zustandsliste.
  const list = $("state-list");
  clear(list);
  const states = Array.isArray(data.states) ? data.states : [];
  const devices = Array.isArray(data.devices) ? data.devices : [];

  // Je Gerät den aktuellen Zustand; unbekannte Geräte kommen unten dran.
  const active = new Map();
  for (const device of devices) {
    if (device.current_state) active.set(device.current_state, device.device_id);
  }
  const orphans = devices.filter((d) => !d.current_state).map((d) => d.device_id);

  for (const state of states) {
    const li = el("li", "state");
    const isCurrent = active.has(state.key);
    if (isCurrent) li.classList.add("is-current");

    const main = el("div");
    main.appendChild(el("div", "state-label", state.label || state.key));
    main.appendChild(el("div", "state-key", state.key));
    li.appendChild(main);

    const next = Array.isArray(state.next) ? state.next : [];
    li.appendChild(
      el("div", "state-next", next.length ? `→ ${next.join(" · ")}` : "→ kein Folgezustand")
    );

    li.appendChild(el("div", "state-current", isCurrent ? `● ${active.get(state.key)}` : ""));
    list.appendChild(li);
  }

  // `transitions_tracked: false` ist eine echte Grenze der Pipeline und wird
  // sichtbar benannt, statt eine vorgetäuschte Verweildauer zu erfinden.
  const issues = [];
  if (data.transitions_tracked === false) {
    issues.push("Übergangszeiten werden nicht erfasst – die Pipeline führt keine Previous-State-/Zeit-Historie.");
  }
  for (const issue of Array.isArray(data.issues) ? data.issues : []) {
    if (typeof issue === "string" && issue) issues.push(issue);
  }
  banner($("state-banner"), "Hinweis:", issues, data.degraded);

  $("graph-source").textContent = data.graph_source || "unbekannt";
  $("state-empty").hidden = orphans.length === 0;
  if (orphans.length) {
    $("state-empty").textContent = `Für ${orphans.join(", ")} meldet die Pipeline keinen Zustand.`;
  }
}

function stateFailed(reason) {
  clear($("state-list"));
  $("state-empty").hidden = true;
  markUnreachable([$("graph-source")], reason);
  const bannerNode = $("state-banner");
  clear(bannerNode);
  bannerNode.appendChild(el("strong", null, "Zustandsmaschine nicht erreichbar: "));
  bannerNode.appendChild(document.createTextNode(reason));
  bannerNode.hidden = false;
}

/* ── 3 · Logs ────────────────────────────────────────────────────────── */

/** Aktuelle Filterwerte aus den Eingabefeldern (leer ⇒ Filter aus). */
function logParams() {
  const limit = parseInt($("f-limit").value, 10);
  const since = parseFloat($("f-since").value);
  return {
    // Level wird bewusst unverändert übergeben: die API normalisiert Groß-
    // Kleinschreibung selbst und kennt kommagetrennte Mehrfachauswahl.
    level: $("f-level").value.trim() || null,
    logger: $("f-logger").value.trim() || null,
    limit: Number.isFinite(limit) && limit > 0 ? limit : null,
    since_seconds: Number.isFinite(since) && since > 0 ? since : null,
  };
}

function renderLogs(data) {
  $("logs-source").textContent = data.source || "–";

  const note = $("logs-source-note");
  clear(note);
  if (data.source === "none") {
    // Ehrlich benennen: es ist **keine** Logquelle angebunden. Der Manager
    // schreibt nach stdout; die Container-Logs liegen beim Docker-Daemon.
    note.appendChild(
      document.createTextNode(
        " – keine Logquelle angebunden. Der Manager protokolliert nach stdout; " +
          "die Container-Logs liest Docker. Für Logdateien im Dashboard müsste " +
          "ein Verzeichnis über EVA_LOG_DIR benannt werden."
      )
    );
  } else if (data.source === "file" && data.log_dir) {
    note.appendChild(document.createTextNode(` – aus ${data.log_dir}`));
  } else if (data.source === "memory") {
    note.appendChild(document.createTextNode(" – aus dem In-Memory-Puffer"));
  }

  const rows = $("log-rows");
  // Scrollposition halten: Pollen soll den Log nicht nach oben springen lassen.
  const box = $("log-box");
  const scrollTop = box.scrollTop;
  clear(rows);

  const entries = Array.isArray(data.entries) ? data.entries : [];
  if (!entries.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Keine Log-Einträge für diese Filter.");
    td.colSpan = 4;
    tr.appendChild(td);
    rows.appendChild(tr);
  }

  for (const entry of entries) {
    const tr = el("tr");
    tr.appendChild(el("td", null, entry.ts || "–"));

    const level = el("td");
    const levelName = (entry.level || "UNKNOWN").toUpperCase();
    level.appendChild(el("span", `level level-${levelName.replace(/[^A-Z]/g, "") || "UNKNOWN"}`, levelName));
    tr.appendChild(level);

    tr.appendChild(el("td", null, entry.logger || "–"));
    tr.appendChild(el("td", null, entry.message || ""));
    rows.appendChild(tr);
  }

  box.scrollTop = scrollTop;

  // Bei leerem Filter + `source: "none"` ist der `issues`-Text die Begründung
  // aus der API – die wird übernommen, nicht selbst erfunden.
  banner($("logs-banner"), "Hinweis:", data.issues, data.degraded);
}

function logsFailed(reason) {
  markUnreachable([$("logs-source")], reason);
  clear($("log-rows"));
  const tr = el("tr");
  const td = el("td", "empty", `Logs nicht erreichbar (${reason}).`);
  td.colSpan = 4;
  tr.appendChild(td);
  $("log-rows").appendChild(tr);
  const bannerNode = $("logs-banner");
  clear(bannerNode);
  bannerNode.appendChild(el("strong", null, "Log-Stream nicht erreichbar: "));
  bannerNode.appendChild(document.createTextNode(reason));
  bannerNode.hidden = false;
}

/* ── 4 · Historie ────────────────────────────────────────────────────── */

const HISTORY_COLUMNS = [
  ["ts", "Zeit", (v) => v || "–"],
  ["device_id", "Gerät", (v) => v || "–"],
  ["state", "Zustand", (v) => v || "–"],
  ["intent", "Intent", (v) => v || "–"],
  ["outcome", "Outcome", (v) => v || "–"],
  ["duration_seconds", "Dauer", (v) => (typeof v === "number" ? duration(v) : "–")],
  ["score", "Score", (v) => (typeof v === "number" ? num(v, 3) : "–")],
  ["confidence", "Confidence", (v) => (typeof v === "number" ? num(v, 3) : "–")],
  ["service", "Service", (v) => v || "–"],
  ["domain", "Domain", (v) => v || "–"],
  // P9.T0: Phasen-Dauern. `null` (nicht gemessen) ⇒ "–", **nie** 0,00 – sonst
  // wäre „kein TTS" nicht von „TTS in 0 ms" zu unterscheiden.
  ["latency_after_speech_seconds", "Licht an", (v) => secondsCell(v)],
  ["latency_after_wake_seconds", "ab Wake", (v) => secondsCell(v)],
  ["phase_stt_seconds", "STT", (v) => secondsCell(v)],
  ["phase_route_seconds", "Routing", (v) => secondsCell(v)],
  ["phase_tts_first_audio_seconds", "TTS 1. Audio", (v) => secondsCell(v)],
  ["phase_tts_total_seconds", "TTS gesamt", (v) => secondsCell(v)],
];

function renderHistory(data) {
  const entries = Array.isArray(data.entries) ? data.entries : [];
  const available = data.available === true;
  const wrap = $("history-wrap");
  const empty = $("history-empty");
  const rows = $("history-rows");
  clear(rows);

  if (!available) {
    // Kein "leer sieht aus wie nichts passiert": der Grund aus der API wird
    // wörtlich übernommen.
    wrap.hidden = true;
    empty.hidden = false;
    empty.textContent = data.reason
      ? `Historie noch nicht erfasst. ${data.reason}`
      : "Historie noch nicht erfasst.";
  } else if (!entries.length) {
    wrap.hidden = true;
    empty.hidden = false;
    empty.textContent = "Historie verfügbar, aber der Puffer ist leer.";
  } else {
    wrap.hidden = false;
    empty.hidden = true;
    for (const entry of entries) {
      const tr = el("tr");
      for (const [key, , render] of HISTORY_COLUMNS) {
        tr.appendChild(el("td", null, render(entry[key])));
      }
      rows.appendChild(tr);
    }
  }

  banner($("history-banner"), "Hinweis:", data.issues, data.degraded);
}

function historyFailed(reason) {
  $("history-wrap").hidden = true;
  $("history-empty").hidden = false;
  $("history-empty").textContent = `Historie nicht erreichbar (${reason}).`;
  const bannerNode = $("history-banner");
  clear(bannerNode);
  bannerNode.appendChild(el("strong", null, "Historie nicht erreichbar: "));
  bannerNode.appendChild(document.createTextNode(reason));
  bannerNode.hidden = false;
}

/* ── 4b · Latenz (P9.T0) ──────────────────────────────────────────────── */

/** Die Phasen-Felder, aus denen die Kachel-Mediane gebildet werden. */
const LATENCY_FIELDS = [
  ["latency_after_speech_seconds", "t-lat-speech"],
  ["latency_after_wake_seconds", "t-lat-wake"],
  ["phase_stt_seconds", "t-lat-stt"],
  ["phase_route_seconds", "t-lat-route"],
  ["phase_tts_first_audio_seconds", "t-lat-tts1"],
  ["phase_tts_total_seconds", "t-lat-ttstotal"],
];

/**
 * Mediane der Phasen-Dauern aus der **History**, die zuletzt gerendert
 * wurde. Grundlage sind nur Einträge, die das Feld **wirklich** als Zahl
 * mitbringen: kein Feld ⇒ nicht in die Liste, kein Ersatzwert. Sonst würde ein
 * Buffer voller Vor-P9.T0-Turns jede Kennzahl auf 0 ziehen und eine
 * "Verbesserung" vortäuschen.
 */
function renderLatency(historyData) {
  const entries = Array.isArray(historyData.entries) ? historyData.entries : [];
  let measured = 0;
  for (const [field, nodeId] of LATENCY_FIELDS) {
    const values = [];
    for (const entry of entries) {
      const value = entry[field];
      if (typeof value === "number" && isFinite(value)) values.push(value);
    }
    if (values.length) measured += 1;
    const node = $(nodeId);
    node.textContent = values.length ? secondsCell(median(values)) : "–";
    node.className = "tile-value" + (values.length ? "" : " chip-unreachable");
  }
  $("latency-empty").hidden = measured > 0;
}

function latencyFailed(reason) {
  for (const [, nodeId] of LATENCY_FIELDS) {
    const node = $(nodeId);
    node.textContent = "–";
    node.className = "tile-value chip-unreachable";
  }
  $("latency-empty").hidden = true;
  banner($("latency-banner"), "Latenz nicht erreichbar: ", [reason], true);
}

/* ── 4c · Wake-Wort (P9.T0) + Schwellen-Tuner (E96) ──────────────────── */

const WAKE_REASON_TEXT = {
  accepted: "angenommen",
  warmup_gate: "abgelehnt (Warm-up)",
  cooldown: "abgelehnt (Cooldown)",
  below_threshold: "abgelehnt (unter Schwelle)",
};

function wakeReasonText(reason, accepted) {
  if (WAKE_REASON_TEXT[reason]) return WAKE_REASON_TEXT[reason];
  // Unbekannter Grund wird **nicht** als „angenommen" geraten.
  if (accepted === true) return "angenommen";
  if (accepted === false) return `abgelehnt (${reason || "ohne Grund"})`;
  return "–";
}

/* Der zuletzt vom Server gemeldete Config-Wert. Er ist die Quelle für das
 * Eingabefeld: der Serverwert, **nicht** der lokale Tastendruck – sonst
 * überschreibt ein Poll die Eingabe, die der Mensch gerade macht. */
let thresholdConfigValue = null;
/* "Gerade gespeichert": nach einem eigenen POST nicht mehr überschreiben. */
let thresholdHoldUntil = 0;

function thresholdValueFromWake(data) {
  if (typeof data.default_threshold === "number") return data.default_threshold;
  if (typeof thresholdConfigValue === "number") return thresholdConfigValue;
  return DEFAULT_THRESHOLD;
}

/** Zahl aus dem Eingabefeld, oder `null` wenn unbrauchbar (nie `NaN` senden). */
function thresholdFromInput() {
  const raw = $("wake-threshold-input").value;
  if (raw === "" || raw === null) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** Setzt Eingabefeld **und** Slider gemeinsam (zwei Felder, ein Wert). */
function setThresholdInput(value) {
  const text = typeof value === "number" ? String(value) : "";
  $("wake-threshold-input").value = text;
  $("wake-threshold-slider").value = text;
}

function note(message, kind) {
  const node = $("wake-tune-note");
  node.textContent = message;
  node.className = "tuner-note" + (kind ? " " + kind : "");
}

/**
 * Zeigt/versteckt den Tuner. Maßgeblich ist **allein** `write_enabled` des
 * Servers: ohne diese Bestätigung existiert die Schreib-UI nicht, auch wenn
 * jemand sie im HTML-Stand manipuliert hätte.
 */
function renderThresholdTuner(data) {
  const enabled = data.write_enabled === true;
  const tuner = $("wake-tuner");
  tuner.hidden = !enabled;
  $("wake-write-state").textContent = enabled ? "Schreiben erlaubt" : "nur Lesen";
  if (typeof data.keep_floor === "number") {
    $("wake-floor-note").textContent = num(data.keep_floor, 2);
  }
  if (!enabled) {
    note("Schreibmodus ist aus (DASHBOARD_WRITE_ENABLED ist nicht true) – es kann nichts geändert werden.");
    return;
  }
  const value = thresholdValueFromWake(data);
  if (document.activeElement === $("wake-threshold-input")) return; // Eingabe in Arbeit
  if (Date.now() < thresholdHoldUntil) return; // eigener Speichervorgang läuft noch
  if (typeof thresholdConfigValue !== "number" || Math.abs(thresholdConfigValue - value) > 1e-9) {
    thresholdConfigValue = value;
  }
  setThresholdInput(value);
  $("wake-threshold-range").textContent = `gültig: 0,01 – 1,00 · ${num(value, 2)} aktiv`;
  if (!$("wake-tune-note").textContent) {
    note("Wirkt sofort im laufenden Manager und wird in die .env des Containers "
      + "geschrieben. Achtung: übersteht `docker restart`, aber kein `docker compose "
      + "up --build` – dann zählt wieder die .env auf dem Host.");
  }
}

function renderWake(data) {
  const stats = data.stats || {};
  const threshold = data.threshold;
  const thresholdNode = $("t-wake-threshold");
  thresholdNode.textContent = typeof threshold === "number" ? num(threshold, 3) : "–";
  thresholdNode.className = "tile-value" + (typeof threshold === "number" ? "" : " chip-unreachable");

  $("t-wake-count").textContent = num(stats.count, 0);
  $("t-wake-accepted").textContent = num(stats.accepted, 0);
  $("t-wake-rejected").textContent = num(stats.rejected, 0);
  $("t-wake-best").textContent =
    typeof stats.best_rejected_score === "number" ? num(stats.best_rejected_score, 3) : "–";
  const scores = $("t-wake-scores");
  if (typeof stats.score_min === "number" && typeof stats.score_max === "number") {
    scores.textContent = `${num(stats.score_min, 3)} / ${num(stats.score_median, 3)} / ${num(stats.score_max, 3)}`;
  } else {
    scores.textContent = "–";
  }

  $("wake-threshold-source").textContent = data.threshold_source || "–";
  $("wake-window-note").textContent = data.window
    ? `· Ringpuffer zeigt die letzten ${num(data.window, 0)} Versuche`
    : "";

  renderThresholdTuner(data);

  const rows = $("wake-rows");
  clear(rows);
  const attempts = Array.isArray(data.attempts) ? data.attempts : [];
  const wrap = $("wake-wrap");
  const empty = $("wake-empty");
  if (!attempts.length) {
    wrap.hidden = true;
    empty.hidden = false;
    empty.textContent = data.count === 0
      ? "Noch kein bewerteter Mikrofon-Chunk."
      : "Wake-Versuche nicht verfügbar.";
  } else {
    wrap.hidden = false;
    empty.hidden = true;
    for (const attempt of attempts) {
      const tr = el("tr");
      tr.appendChild(el("td", null, attempt.ts || "–"));
      tr.appendChild(el("td", null, attempt.device_id || "–"));
      tr.appendChild(el("td", null, typeof attempt.chunk_index === "number" ? String(attempt.chunk_index) : "–"));
      tr.appendChild(el("td", null, typeof attempt.score === "number" ? num(attempt.score, 3) : "–"));
      tr.appendChild(el("td", null, typeof attempt.threshold === "number" ? num(attempt.threshold, 3) : "–"));
      tr.appendChild(el("td", null, wakeReasonText(attempt.reason, attempt.accepted)));
      rows.appendChild(tr);
    }
  }

  banner($("wake-banner"), "Hinweis:", data.issues, data.degraded);
}

/**
 * Der Schreibpfad: **eine** Zahl, **eine** Route. Kein Gerät, kein HA-Call.
 * Fehler werden wörtlich benannt; ein `403` bedeutet „Schreibmodus aus" und
 * wird **nicht** als Erfolg dargestellt.
 */
async function saveThreshold(value) {
  const save = $("wake-tune-save");
  save.disabled = true;
  thresholdHoldUntil = Date.now() + 10000;
  try {
    const result = await postJson(API_WRITE_THRESHOLD, { threshold: value });
    const target = result && typeof result.threshold === "number" ? result.threshold : value;
    thresholdConfigValue = target;
    setThresholdInput(target);
    $("wake-threshold-range").textContent = `gültig: 0,01 – 1,00 · ${num(target, 2)} aktiv`;
    const parts = [`Gespeichert: ${num(result && result.previous, 2)} → ${num(target, 2)}.`];
    parts.push(result && result.effective_without_restart
      ? "Wirkt sofort (ohne Neustart)."
      : "⚠️ Laufzeit-Wirksamkeit nicht bestätigt.");
    parts.push(result && result.persisted_env
      ? "In der Container-.env gesichert (übersteht `docker restart`, nicht `up --build`)."
      : "⚠️ .env-Schreibfehler – der Wert gilt nur bis zum nächsten Containerstart.");
    note(parts.join(" "));
    loadSection("wake", API.wake, null, renderWake, wakeFailed);
  } catch (error) {
    if (error && error.status === 403) {
      $("wake-tuner").hidden = true;
      note("Schreibmodus ist aus – der Server hat abgelehnt (403). Es wurde nichts geändert.", "bad");
    } else {
      note(`Nicht gespeichert: ${reasonOf(error)}`, "bad");
    }
  } finally {
    save.disabled = false;
  }
}

/** Verdrahtung des Tuners (Slider ⇄ Zahl, Speichern, Reset). */
function initThresholdTuner() {
  const input = $("wake-threshold-input");
  const slider = $("wake-threshold-slider");
  if (!input || !slider) return;
  slider.addEventListener("input", () => {
    input.value = slider.value;
  });
  input.addEventListener("input", () => {
    const value = thresholdFromInput();
    if (value !== null) slider.value = String(value);
  });
  $("wake-tune-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = thresholdFromInput();
    if (value === null || value <= 0 || value > 1) {
      note("Bitte einen Wert zwischen 0,01 und 1,00 eingeben.", "bad");
      return;
    }
    saveThreshold(value);
  });
  $("wake-tune-reset").addEventListener("click", () => {
    setThresholdInput(DEFAULT_THRESHOLD);
    note(`Reset auf ${num(DEFAULT_THRESHOLD, 2)} – erst Speichern überträgt ihn.`);
  });
}

/** Verdrahtung des E111-Schreibblocks: nur geänderte Felder, ein PUT. */
function initConfigEditor() {
  const form = $("config-form");
  if (!form) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const changed = collectConfigChanges();
    if (!Object.keys(changed).length) {
      noteConfig("Keine Änderungen – nichts zu speichern.", "warn");
      return;
    }
    saveConfig(changed);
  });
}

function wakeFailed(reason) {
  for (const nodeId of ["t-wake-threshold", "t-wake-count", "t-wake-accepted", "t-wake-rejected", "t-wake-best", "t-wake-scores"]) {
    const node = $(nodeId);
    node.textContent = "–";
    node.className = "tile-value chip-unreachable";
  }
  $("wake-wrap").hidden = true;
  $("wake-empty").hidden = false;
  $("wake-empty").textContent = `Wake-Versuche nicht erreichbar (${reason}).`;
  $("wake-tuner").hidden = true;
  banner($("wake-banner"), "Wake-Wort nicht erreichbar: ", [reason], true);
}

/* ── 5 · Konfiguration (+ E111-Schreibblock) ─────────────────────────── */

/**
 * Die elf E111-Felder des Schreibblocks. `kind` steuert, wie das Feld
 * gefüllt und gelesen wird:
 *   `number` = Dezimalzahl (leer = unverändert),
 *   `int`    = Ganzzahl ≥ 0 (leer = unverändert),
 *   `check`  = Checkbox (immer ein Wert),
 *   `select` = Auswahl (immer ein Wert),
 *   `text`   = Freitext (CSV bei `ha_entity_domains`).
 * Gespeichert wird **nur**, was sich gegenüber dem zuletzt geladenen
 * Serverwert geändert hat.
 */
const CONFIG_FIELDS = [
  { id: "oww_threshold", kind: "number" },
  { id: "oww_barge_in_threshold", kind: "number" },
  { id: "wake_attempt_keep_floor", kind: "number" },
  { id: "router_confidence_gate", kind: "number" },
  { id: "router_needs_param_threshold", kind: "number" },
  { id: "oww_cooldown_ms", kind: "int" },
  { id: "log_level", kind: "select" },
  { id: "audio_dump_enabled", kind: "check" },
  { id: "whisper_language", kind: "text" },
  { id: "jev_mode", kind: "select" },
  { id: "ha_entity_domains", kind: "text" },
];

/* Der zuletzt vom Server geladene Wert je Feld – Vergleichsbasis dafür,
 * was sich geändert hat. `null` = Feld leer (beim nächsten Speichern nicht
 * mitgesendet). */
let configSnapshot = {};
/* "Gerade gespeichert": kurzzeitig nicht von einem Poll überschreiben. */
let configHoldUntil = 0;

/** Eingabefeld-ID und Badge-ID je Feld (an einer Stelle definiert). */
function configInputId(fieldId) {
  return "cfg-" + fieldId;
}

function configBadgeId(fieldId) {
  return "badge-" + fieldId;
}

/** Feldwert → Eingabe (Liste wird bei CSV zu `a,b,c` geklebt). */
function fillConfigInput(field, input, value) {
  if (field.kind === "check") {
    input.checked = value === true;
    return;
  }
  if (field.kind === "select") {
    input.value = typeof value === "string" ? value : input.value;
    return;
  }
  if (field.kind === "text") {
    input.value = Array.isArray(value)
      ? value.join(", ")
      : typeof value === "string"
        ? value
        : value === null || value === undefined
          ? ""
          : String(value);
    return;
  }
  // number/int: nur echte Zahlen, sonst leer (= unverändert).
  input.value = typeof value === "number" && isFinite(value) ? String(value) : "";
}

/** Eingabe → Wert; `null` = "nicht sendbar/leer" (nie `NaN` senden). */
function readConfigInput(field, input) {
  if (field.kind === "check") return input.checked;
  if (field.kind === "select" || field.kind === "text") return input.value.trim();
  const raw = input.value.trim();
  if (raw === "") return null;
  const value = Number(raw);
  if (!Number.isFinite(value)) return null;
  if (field.kind === "int" && !Number.isInteger(value)) return null;
  return value;
}

/** Zwei Feldwerte sind gleich? (Typ-aware: `true` ≠ `"true"`, `0.8` ≠ `"0.8"`.) */
function configValuesEqual(previous, next) {
  if (typeof previous === "boolean" || typeof next === "boolean") {
    return previous === next;
  }
  return previous === next;
}

/** Geänderte Felder seit dem letzten Ladevorgang (leer = nichts tun). */
function collectConfigChanges() {
  const changed = {};
  for (const field of CONFIG_FIELDS) {
    const input = $(configInputId(field.id));
    if (!input) continue;
    const value = readConfigInput(field, input);
    if (value === null) continue; // leer ⇒ bewusst nicht gesendet
    if (!configValuesEqual(configSnapshot[field.id], value)) {
      changed[field.id] = value;
    }
  }
  return changed;
}

/**
 * Badges je Feld: `live` (wirkt ohne Neustart) oder `Neustart` – **aus der
 * Antwort** (`effective_without_restart`), nicht aus lokalem Wissen. Fehlt
 * das Feld in der Antwort (älterer Server), bleibt das Badge neutral "–".
 */
function renderConfigBadges(map) {
  for (const field of CONFIG_FIELDS) {
    const badge = $(configBadgeId(field.id));
    if (!badge) continue;
    const live = map ? map[field.id] : undefined;
    badge.textContent = live === true ? "live" : live === false ? "Neustart" : "–";
    badge.className =
      "chip " + (live === true ? "chip-ok" : live === false ? "chip-warn" : "chip-unknown");
  }
}

function noteConfig(message, kind) {
  const node = $("config-note");
  node.textContent = message;
  node.className = "tuner-note" + (kind ? " " + kind : "");
}

/**
 * Der Schreibpfad (E111): **nur** die geänderten Felder, **eine** Route.
 * Kein Gerät, kein HA-Call. Fehler werden wörtlich benannt:
 * `401` = Token fehlt/falsch, `403` = Schreibmodus aus, `400`/`422` = Inhalt.
 */
async function saveConfig(changed) {
  const save = $("config-save");
  save.disabled = true;
  const tokenField = $("cfg-token-field");
  const token = tokenField.hidden ? "" : $("cfg-api-token").value.trim();
  try {
    const result = await putJson(API_WRITE_CONFIG, changed, token);
    configHoldUntil = Date.now() + 10000;
    for (const [field, value] of Object.entries(changed)) {
      configSnapshot[field] = value;
    }
    const effective = result && result.effective_without_restart ? result.effective_without_restart : {};
    renderConfigBadges(effective);
    const live = Object.keys(changed).filter((field) => effective[field] === true);
    const rest = Object.keys(changed).filter((field) => effective[field] !== true);
    const parts = [`Gespeichert: ${Object.keys(changed).join(", ")}.`];
    if (live.length) parts.push(`Wirkt sofort: ${live.join(", ")}.`);
    if (rest.length) {
      parts.push(`Wirkt nach Neustart: ${rest.join(", ")} (docker compose up -d wyoming-manager auf dem Host).`);
    }
    noteConfig(parts.join(" "), "good");
  } catch (error) {
    if (error && error.status === 401) {
      noteConfig("Token fehlt oder ist falsch (401) – nichts gespeichert.", "bad");
    } else if (error && error.status === 403) {
      noteConfig("Schreibmodus ist aus (403) – der Server hat abgelehnt, nichts geändert.", "bad");
    } else if (error && error.status === 400) {
      noteConfig(`Abgelehnt (400): ${reasonOf(error)} – nichts gespeichert.`, "bad");
    } else if (error && error.status === 422) {
      noteConfig(`Ungültiger Wert (422): ${reasonOf(error)} – nichts gespeichert.`, "bad");
    } else {
      noteConfig(`Nicht gespeichert: ${reasonOf(error)}`, "bad");
    }
  } finally {
    save.disabled = false;
  }
}

function renderConfig(data) {
  $("service-name").textContent = data.service || "–";
  $("service-version").textContent = data.version || "–";

  const rows = $("config-rows");
  clear(rows);
  const values = Array.isArray(data.values) ? data.values : [];
  for (const item of values) {
    const tr = el("tr");
    tr.appendChild(el("td", null, item.name || "–"));
    tr.appendChild(el("td", null, configValue(item.value)));
    tr.appendChild(el("td", null, item.group || "–"));
    tr.appendChild(el("td", null, item.source || "–"));
    rows.appendChild(tr);
  }
  if (!values.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Keine Konfigurationswerte geliefert.");
    td.colSpan = 4;
    tr.appendChild(td);
    rows.appendChild(tr);
  }

  /* Schreibblock: Badges + Token-Feld-Sichtbarkeit kommen aus der Antwort.
     `write_token_required: true` ⇒ Token-Feld sichtbar (und beim Speichern
     mitgeschickt); `false` ⇒ unsichtbar, kein Header. */
  renderConfigBadges(data.effective_without_restart);
  const tokenRequired = data.write_token_required === true;
  $("cfg-token-field").hidden = !tokenRequired;
  $("config-write-state").textContent = tokenRequired
    ? "API-Token erforderlich"
    : "ohne Token";

  /* Formular füllen – außer das Feld hat gerade den Fokus (Eingabe in
     Arbeit) oder ein eigener Speichervorgang läuft noch. */
  const byName = new Map();
  for (const item of values) byName.set(item.name, item);
  const busy = document.activeElement instanceof HTMLElement && document.activeElement.id.startsWith("cfg-");
  if (!busy && Date.now() > configHoldUntil) {
    for (const field of CONFIG_FIELDS) {
      const input = $(configInputId(field.id));
      if (!input) continue;
      const item = byName.get(field.id);
      fillConfigInput(field, input, item ? item.value : undefined);
      configSnapshot[field.id] = readConfigInput(field, input);
    }
  }

  banner($("config-banner"), "Hinweis:", data.issues, data.degraded);
}

function configFailed(reason) {
  clear($("config-rows"));
  const tr = el("tr");
  const td = el("td", "empty", `Konfiguration nicht erreichbar (${reason}).`);
  td.colSpan = 4;
  tr.appendChild(td);
  $("config-rows").appendChild(tr);
  const bannerNode = $("config-banner");
  clear(bannerNode);
  bannerNode.appendChild(el("strong", null, "Konfiguration nicht erreichbar: "));
  bannerNode.appendChild(document.createTextNode(reason));
  bannerNode.hidden = false;
}

/* ── Poll-Schleife ───────────────────────────────────────────────────── */

let pollTimer = null;
let lastGood = null;

/** **Nur** GETs auf die 7 Endpunkte, die das Dashboard braucht. */
async function poll() {
  const stamp = new Date();
  $("last-update").textContent = `Stand ${stamp.toLocaleTimeString("de-DE")}`;

  await Promise.all([
    loadSection("status", API.status, null, renderStatus, statusFailed),
    loadSection("dependencies", API.dependencies, null, renderDependencies, dependenciesFailed),
    loadSection("state", API.state, null, renderState, stateFailed),
    loadSection("logs", API.logs, logParams(), renderLogs, logsFailed),
    loadSection("wake", API.wake, null, renderWake, wakeFailed),
    loadSection("config", API.config, null, renderConfig, configFailed),
    // Die Historie liefert **zwei** Ansichten: die Tabelle und die
    // Latenz-Kacheln. Ein Request, zwei Verarbeitungen – `loadSection`
    // verhindert zusätzlich überlappte Durchläufe je Schlüssel.
    loadSection("history", API.history, null, (data) => {
      renderHistory(data);
      renderLatency(data);
    }, (reason) => {
      historyFailed(reason);
      latencyFailed(reason);
    }),
  ]);

  // Erreichbarkeit merken, ohne Inhalte zu erfinden.
  lastGood = stamp;
  $("poll-state").textContent = "aktualisiert";
}

function startPolling() {
  if (pollTimer !== null) return;
  poll();
  pollTimer = setInterval(poll, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer === null) return;
  clearInterval(pollTimer);
  pollTimer = null;
  $("poll-state").textContent = "pausiert";
}

function applyRefreshPreference() {
  if ($("auto-refresh").checked) startPolling();
  else stopPolling();
}

/* ── Start ───────────────────────────────────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  // Filteränderung ⇒ sofort neu laden (rein lesend).
  $("log-filters").addEventListener("change", () => {
    if ($("auto-refresh").checked) poll();
  });
  $("auto-refresh").addEventListener("change", applyRefreshPreference);
  $("refresh-now").addEventListener("click", () => poll());
  // Der Schreib-Tuner wird versteckt geliefert und nur sichtbar, wenn der
  // Server `write_enabled: true` meldet (E96). Der E111-Schreibblock ist
  // immer sichtbar; der Server bleibt der Verweigerer (403 = Schreibmodus
  // aus, 401 = Token), die UI kaschiert das nicht als Erfolg.
  initThresholdTuner();
  initConfigEditor();
  applyRefreshPreference();
});

// Für die Diagnose im Browser, ohne Geheimnisse.
window.evaDashboard = {
  API,
  API_WRITE_THRESHOLD,
  API_WRITE_CONFIG,
  DEFAULT_THRESHOLD,
  POLL_INTERVAL_MS,
  FETCH_TIMEOUT_MS,
  lastPoll: () => lastGood,
};
