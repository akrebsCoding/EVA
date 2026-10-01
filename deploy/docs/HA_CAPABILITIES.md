# HA-Steuerbarkeit — Ist- vs. Soll-Liste (EVA ⇄ Home Assistant)

**Stand:** 2026-09-30 · **Recherche, keine Code-Änderung** · Live-Daten von `.123:8123` (HA **2026.8.2**), Live-Config von `.106:/opt/eva/.env`

Zweck: Antwort auf die Frage **„kann ich wirklich alles steuern?"** — mit belegten Zahlen statt Bauchgefühl.
Verwandt: [die env-Variablen-Kommentare in `deploy/env.template`, [`INSTALL.md` §8](INSTALL.md) (Config-GUI).

**Methode:** (1) Code gelesen (`app/router.py`, `app/ha_client.py`, `app/config.py`, `app/llm_client.py`),
(2) Live-Env auf `.106` gelesen, (3) `GET /api/states` + `GET /api/services` **read-only** gegen HA.
HA wurde **ausschließlich lesend** abgefragt — kein Service-Call, kein Neustart, kein Schreibzugriff.
Der HA-Token wurde **serverseitig** auf `.106` verwendet und ist in diesem Dokument **nicht** enthalten.

---

## 0. Kurzfassung

| Frage | Antwort |
|---|---|
| Entities in HA | **931** in **32** Domains |
| Davon aktuierfähig (nicht read-only) | **406** (43,6 %) |
| Davon im Entity-Cache des Managers | **375** (seit E113/P12.T2, 16 Domains) = **92,4 %** der aktuierfähigen — vor P12.T2 waren es **209** (7 Domains) |
| Voll steuerbar im engeren Sinn (an **und** aus per `turn_on`/`turn_off`) | **170** → **41,9 %** |
| Obergrenze nach Ausbau | **406** = 100 % (bzw. **404** = 99,5 %, wenn `alarm_control_panel` bewusst draußen bleibt) |
| Kann EVA **Zustände abfragen**? | **Ja, seit P12.T3** (Block C). ≤12 aktuelle HA-Werte mit `Stand` stehen im Frage-Prompt; passt keiner, antwortet EVA ehrlich **ohne** LLM-Call (→ §6, T3/T4) |

> **Update 2026-09-30 (E113/P12.T2) — Ist-Stand, live nachgemessen**
> (`GET /api/states` ∩ `GET /api/services` gegen `.123:8123`, HA 2026.8.2; Token aus der lokalen
> `.env`, **nicht** ausgegeben): `HA_ENTITY_DOMAINS` umfasst jetzt **16** statt 7 Domains
> (die 14 sicheren **plus** `update`/`automation`), Cache **209 → 375**. Neu sichtbar:
> `automation` 47 · `update` 27 · `button` 45 · `number` 23 · `select` 13 · `input_boolean` 4 ·
> `input_select` 3 · `input_number` 1 · `fan` 3 = **+166**. Sicherheits-Domains `lock` (0 Entities,
> 3 Services) / `alarm_control_panel` (2) / `vacuum` (0) bleiben **hart** in `HA_BLOCKED_DOMAINS`
> (`app/ha_client.py`) ausgeschlossen — **kein** Env-Key, *Block schlägt immer Erlaubnis*.
> Read-only bleiben draußen: `sensor` 344, `binary_sensor` 74, `event` 6, `sun` 1 (= 425; der
> Lese-Pfad dafür ist P12.T3). Nicht aufgenommen (bewusst): `notify` 14, `calendar` 8, `todo` 3,
> `tts` 1, `input_datetime` 3 — Freitext-/Datumsparameter = anderer Codepfad, gehört nach Block B.
> Von den 406 aktuierfähigen Entities bleiben damit **31** unsichtbar (406 − 375).

> **Update 2026-09-30 (P12.T4 — Live-Abnahme auf `.106`, Ist-Zahlen aus dem laufenden Produkt)**
> Der Live-Startlog nach Deploy von `main` `7717c05` meldet `Cache=375 Entities, Domains=16,
> blockiert=3, lesbar=115` — die Zahlen aus T2/T3 sind damit am **Produkt** belegt, nicht nur
> berechnet. **Drei Betriebs-Erkenntnisse, die das Produkt erst sichtbar gemacht hat:**
> (1) Die **produktive `.env` enthielt ein veraltetes `HA_ENTITY_DOMAINS=` mit der alten 7er-Liste**
> und überschrieb damit den 16-Domain-Code-Default ⇒ **live lief A zunächst wirkungslos (209/7)**.
> Korrigiert auf die 16er-Liste (byte-identisch zu `deploy/env.template:91`), Backup angelegt,
> unabhängige Gegenprobe read-only: 931 States ∩ 16 Domains = **exakt 375**. **Lehre:** bei jedem
> Wechsel eines Config-Defaults den Env-Override mitprüfen und die Ist-Zahl gegen HA gegenrechnen.
> (2) `docker compose restart` übernimmt `.env`-Änderungen **nicht** (Env hängt am Container-Objekt);
> erst `up -d` (Recreate) zog sie. (3) **E116:** die Dienst-Allowlist führt **nackte** HA-Dienstnamen,
> das LLM liefert die **qualifizierte** Form (`light.turn_on`) ⇒ die **gesamte `light`-Domain war
> live blockiert** (fail-closed, also **nichts** falsch geschaltet). Gefixt in P12.T4-4 durch **eine**
> Normalisierung an der LLM-Grenze (`_normalize_service_name()`), live grün.
> **E114 live bestätigt:** von den **21** Licht-Entities unterstützt **0** `SUPPORT_BRIGHTNESS`
> (unverändert zu §1.4) ⇒ „dimme … auf 40 Prozent" ⇒ **„Diese Lampe kann ich nicht dimmen."**,
> `ENTITY_PARAM_MISSING`, **kein** HA-Call; **reines** An/Aus ohne Zahl bleibt möglich. **Wichtig für
> die Deutung:** weil live keine Lampe dimmbar ist, war nur die **Ablehnung** darstellbar, nicht der
> Erfolgsfall — §1.4 Problem 3 beschreibt also weiterhin die Wahrheit über diese HA-Instanz.

**Die harte Kopplung ist nicht eine, sondern sind drei** — und die wirksame hängt an `ROUTER_VARIANT` (live: `class`).

---

## 1. Die drei Ebenen, die bestimmen, was gesendet wird

Der Auftrag „hartkodierte Mapping-Tabelle in `app/router.py`" trifft zu — aber sie ist aufgeteilt,
und zwei der drei Ebenen liegen in `app/llm_client.py`. Wer nur `router.py` liest, übersieht die stärkste Schranke.

| # | Ebene | Ort | Art | Live wirksam? |
|---|---|---|---|---|
| 1 | **Domain-Filter** + Sicherheits-Block | `app/config.py:179` → `app/ha_client.py:160` `is_target_entity()` | **hart**, beim Cache-Aufbau | **ja** |
| 2 | **Service-Auswahl** | `app/llm_client.py:182` `JEV_SERVICE_CRITERIA` | **hart** (nur `ROUTER_VARIANT=entity`) | **nein** (live `class`) |
| 3 | **Service-Hinweis** | `app/router.py:1320` DeepSeek-Prompt | **weich** — nur „z. B." | **ja** |
| 4 | **Parameter-Allowlist** | `app/router.py:247` `ENTITY_PARAM_ALLOWLIST` | **hart** (nur `ROUTER_VARIANT=entity`) | **nein** (live `class`) |

### 1.1 Ebene 1 — Domain-Filter (die einzige echte Schranke)

```
app/config.py:179
ha_entity_domains: str = "light,switch,cover,climate,media_player,scene,script,button,number,select,input_boolean,input_select,input_number,fan,update,automation"
```

Stand **vor** P12.T2 (7 Domains) war live auf `.106` **identisch** mit dem Default. Seit E113/P12.T2
umfasst der Default **16** Domains; die produktive `.env` auf `.106` setzt `HA_ENTITY_DOMAINS`
**nicht** ⇒ der neue Default greift **ohne** Deploy-Schritt. `HA_ALLOWED_ENTITIES` ist live
**leer** ⇒ **kein** zusätzlicher Vorfilter (`ha_client.py:141`).

`is_target_entity()` (`ha_client.py:160-186`) arbeitet seit P12.T2 so:

```python
if entity_domain(entity_id) in blocked:          # E113: Sicherheitsboden ZUERST
    return False
if entity_domain(entity_id) not in domains:      # Domain-Filter
    return False
if allowed_entities and entity_id not in allowed_entities:   # E17-Allowlist
    return False
return True
```

`blocked` ist die **harte** Modulkonstante `HA_BLOCKED_DOMAINS = ("lock", "alarm_control_panel",
"vacuum")` — **kein** Env-Key, damit eine falsche `.env` die Sicherheitsgrenze nicht aushebeln kann
(*Block schlägt immer Erlaubnis*).

Angewandt wird das **ausschließlich** in `EntityCache.replace()` (`ha_client.py:237-262`) beim
`/api/states`-Refresh. Effekt: **Was nicht im Cache ist, kann der Router nicht auswählen**
(`entity_id not in selection.entities` ⇒ `AllowlistViolationError` — der Vergleich läuft seit
P12.T2 gegen die **dem Modell gesendete** Auswahl, nicht gegen den vollen Cache: der Katalog ist
seitdem auf ≤120 (Prompts) bzw. ≤254 (Jev) Entities begrenzt, und was nicht gesendet wurde, darf
auch nicht geschaltet werden).

> **Wichtig und leicht falsch zu lesen:** `HomeAssistantClient.call_service()` (`ha_client.py:404-450`)
> prüft **weder Domain noch Allowlist** — dort werden nur drei Strings auf „nicht leer" validiert.
> Die Schranke ist also **einseitig**: der Cache filtert, der Aufruf filtert nicht.
> Das ist okay, *weil* `Router.execute()` vorher `entity_id in catalog` erzwingt — aber es heißt:
> **es gibt im gesamten Manager keine Service-Allowlist.** In `execute()` (`router.py:939-980`) sitzt
> als einzige Vorprüfung der **Negations-Guard** (E98), sonst nichts.

### 1.2 Ebene 2/3 — Welche Services der Manager senden *kann*

```
app/llm_client.py:182   JEV_SERVICE_CRITERIA = {"turn_on", "turn_off", "toggle"}   # hart, Variante D
app/router.py:1320      '"service": "<service, z.B. turn_on|turn_off|toggle>"'    # weich, Variante A (live)
```

Live ist `ROUTER_VARIANT=class` (= **Variante A**). In diesem Modus liefert DeepSeek
`{"entity_id", "domain", "service", "service_data", "response_text"}` und **der Service wird
wortwörtlich übernommen** — `router.py:1335-1337` prüft nur, dass er ein nicht-leerer String ist.
Ob HA einen solchen Service kennt, entscheidet **nicht** der Manager, sondern erst HA mit einem 4xx.

**Konsequenz:** In Variante A ist der Manager **nicht** auf `turn_on|turn_off|toggle` beschränkt —
er ist auf *gar nichts* beschränkt. Das ist **kein Feature, sondern eine Lücke**: der E92-Parameter-Schutz
greift in dieser Variante nicht (siehe 1.4).

### 1.3 Ist-Matrix: welche domain+service-Kombinationen funktionieren

Geprüft gegen die Live-Service-Liste von HA (`GET /api/services`) und die Live-Feature-Flags der Entities.
„Hart" = erzwungen, „prompt" = nur durch den LLM-Geist geführt, nicht erzwungen.

| domain | HA-Service (live) | vom Manager erreichbar? | wie | Anmerkung |
|---|---|---|---|---|
| `light` | `turn_on`, `turn_off`, `toggle` | **ja** | prompt (hart in Var. D) | 21 Entities, alle `off`/`unavailable` |
| `switch` | `turn_on`, `turn_off`, `toggle` | **ja** | prompt (hart in Var. D) | 134 Entities — größte Gruppe |
| `climate` | `turn_on`, `turn_off`, `toggle` | **ja** | prompt (hart in Var. D) | 12 Entities, alle Modus `heat` |
| `cover` | `open_cover`, `close_cover`, `stop_cover`, `set_cover_position`, `toggle`, +4 Tilt | **teilweise** | prompt | ⚠️ HA kennt **kein** `cover.turn_on`/`turn_off` — nur `toggle` trifft zuverlässig |
| `media_player` | `turn_on`, `turn_off`, `toggle`, `volume_set`, `volume_mute`, +18 | **teilweise** | prompt | ⚠️ 5 von 8 `unavailable`; Entities melden **kein** `TURN_ON`-Flag |
| `scene` | `turn_on` (+ `apply`, `create`, `delete`, `reload`) | **ja (nur an)** | prompt | 17 Entities; `turn_off`/`toggle` existieren für `scene` **nicht** |
| `script` | `turn_on`, `turn_off`, `toggle` + 3 Custom | **ja** | prompt (hart in Var. D) | 3 Entities (`echo_ansage*`) |
| `cover.set_cover_position` | ✔ vorhanden | **NEIN** | — | nicht in `JEV_SERVICE_CRITERIA`, kein HA-Motivation im Prompt |
| `climate.set_temperature` | ✔ vorhanden | **NEIN** (formal) | — | Prompt sagt `turn_on|turn_off|toggle`; s. Warnung 1.4 |
| `climate.set_hvac_mode` | ✔ vorhanden (`auto`/`off`/`heat`) | **NEIN** | — | fehlt im Prompt vollständig |
| `fan.*` | `turn_on/off/toggle`, `set_percentage`, `oscillate`, `set_preset_mode` | **NEIN** | — | `fan` **nicht** im Domain-Filter → nie im Cache |
| `input_boolean.turn_on/off/toggle` | ✔ vorhanden | **NEIN** | — | Domain nicht im Filter |
| `input_select.select_option` | ✔ (+5) | **NEIN** | — | Domain nicht im Filter |
| `input_number.set_value` | ✔ (+3) | **NEIN** | — | Domain nicht im Filter |
| `number.set_value` | ✔ | **NEIN** | — | Domain nicht im Filter |
| `select.select_option` | ✔ (+4) | **NEIN** | — | Domain nicht im Filter |
| `button.press` | ✔ | **NEIN** | — | Domain nicht im Filter |
| `lock.unlock` / `lock.lock` | ✔ vorhanden | **NEIN** | — | **0 lock-Entities** in dieser HA + nicht im Filter |
| `alarm_control_panel.*` | ✔ 7 Services | **NEIN** | — | 2 Entities; **nicht** im Filter (siehe §5) |
| `todo.add_item` / `remove_item` | ✔ (+3) | **NEIN** | — | 3 Entities; nicht im Filter |
| `notify.send_message` | ✔ | **NEIN** | — | 14 Entities; nicht im Filter |
| `update.install` | ✔ | **NEIN** | — | 27 Entities; nicht im Filter (**bewusst**, siehe §5) |
| `automation.turn_on/off/toggle/trigger` | ✔ | **NEIN** | — | 47 Entities; nicht im Filter |
| `calendar.create_event` | ✔ | **NEIN** | — | 8 Entities; nicht im Filter |
| `tts.speak` | ✔ | **NEIN** | — | 1 Entity; nicht im Filter |
| `media_player.volume_set` | ✔ (`volume_level`) | **NEIN** | — | s. Warnung 1.4 |
| `vacuum.*` | ✔ 9 Services | **NEIN** | — | **0 vacuum-Entities** vorhanden |

### 1.4 Zwei Parameter-Fehler in der Mapping-Tabelle (E92)

```
app/router.py:247-253
ENTITY_PARAM_ALLOWLIST = {
    ("light", "turn_on"):        "brightness_pct",
    ("light", "toggle"):         "brightness_pct",
    ("climate", "turn_on"):      "temperature",     # ← Problem 1
    ("media_player", "turn_on"): "volume_level",    # ← Problem 2
    ("media_player", "toggle"):  "volume_level",    # ← Problem 2
}
```

**Problem 1 — `climate.turn_on` + `temperature`:** Der HA-Service für eine Temperatur ist
`climate.set_temperature` (mit Feld `temperature`). `climate.turn_on` existiert zwar, **ignoriert aber
ein `temperature`-Feld** — die Heizung würde einfach anlaufen, ohne auf den gewünschten Wert zu gehen.
Der Nutzer hörte „Okay, Wohnzimmer auf 21 Grad." und bekäme Raumtemperatur. Das ist die exakte Klasse
Fehler, die E92 mit `EntityParamMissingError` verhindern wollte — nur trifft der Allowlist-Eintrag hier
die **falsche** Kombination.

**Problem 2 — `media_player.turn_on` + `volume_level`:** Richtig wäre `media_player.volume_set`
(mit Feld `volume_level`). Auch hier: der Service startet nur, die Lautstärke bleibt, wie sie ist.

**Problem 3 — `light.brightness_pct` auf diesen 21 Lichtern:** Alle 21 Licht-Entities melden
`supported_features = EFFECT` (Wert 4) **und** `min_color_temp_kelvin = None` (bei allen 21).
Es gibt also **keine Helligkeits- und keine Farbtemperatur-Unterstützung** in dieser HA — `brightness_pct`
ist auf **keinem** einzigen Licht einsetzbar. Der Allowlist-Eintrag beschreibt also eine Fähigkeit, die
real nicht existiert.

> **Alles drei gilt nur in `ROUTER_VARIANT=entity`.** Live ist `class` — dort greift
> `ENTITY_PARAM_ALLOWLIST` **überhaupt nicht**, weil `_resolve_entity_param()` nur vom Jev-#2-Pfad
> (Variante D) aufgerufen wird (`router.py:1158` vs. `1215-1220`).
> In der live konfigurierten Variante A läuft `service_data` aus dem DeepSeek-JSON **ungefiltert**
> durch (`router.py:1351-1356` — entfernt wird **nur** der Key `entity_id`, sonst nichts).
> Die in E92 dokumentierte Zusage „alles andere wird verworfen" gilt für Variante A **nicht**.

> **Nachtrag 2026-09-30 (P12.T2b/T4-4, E116) — nackte vs. qualifizierte Dienstnamen.** Der B-1-Fix
> führt eine **Dienst-Allowlist** ein, und die muss in **HA-Notation** stehen, also **nackt**
> (`turn_on`, `set_temperature`, …), weil `call_service` genau das erwartet. Ein LLM liefert aber
> die **qualifizierte** Form (`light.turn_on`). Solange beide Formen direkt verglichen wurden, war die
> **gesamte `light`-Domain live blockiert** (HARD `PROTOCOL_ERROR`, 2/2 reproduziert) — bei allen
> Domains möglich. Behoben: **eine** Normalisierung an der LLM-Grenze
> (`_normalize_service_name(service, domain)`, `app/router.py:1821`), aufgerufen in **3** Pfaden
> (class, off, Variante D) **vor** der Kriterien-Prüfung; ein fremdes Präfix (Domain-Mismatch) oder
> eine kaputte Form ⇒ `RouterProtocolError`, **kein** Call. **Merke für eigene Erweiterungen:** neue
> Einträge in `DOMAIN_SERVICE_CRITERIA`/`ENTITY_PARAM_ALLOWLIST`/`ENTITY_RESPONSE_TEMPLATES` gehören
> **nackt** hinein — und die Test-Fixtures sollten die **reale** Modell-Ausgabe abbilden, sonst bleibt
> diese Grenze ungeprüft (899 Unit-Tests haben den Fehler nicht gesehen, nur der Live-Lauf).

---

## 2. Soll-Liste: die Live-Inventur (Domains, die HA kann)

`GET /api/states` → **931 Entities**, **32 Domains** (HA 2026.8.2).

| Domain | n | steuerbar? | Beispiel-Entities (live gelesen) |
|---|---|---|---|
| `sensor` | 344 | **read-only** | — |
| `switch` | 134 | **ja** | `Garagenbeleuchtung`, `Pool`, `Zisterne`, `Waschmaschine Steckdose 1`, `Computer Steckdose 1`, `Wohnzimmer Intelligente Raumregelung Comfort Override` |
| `device_tracker` | 85 | read-only | — |
| `binary_sensor` | 74 | **read-only** | — |
| `automation` | 47 | nein | 47 Automationen (nicht im Filter) |
| `button` | 45 | nein | `Garagenbeleuchtung Neu starten`, `Zisterne Neu starten`, `Pool Neu starten`, `Wärmepumpenzähler Neu starten`, `Farbverlauf Ankleide`, `Schminktisch Licht` |
| `update` | 27 | nein (bewusst) | `WebRTC Camera Update`, `Bubble Card Update`, `blueprint_studio Update` |
| `number` | 23 | nein | `Pool-Strompreis` (21,0), `Zisterne-Strompreis` (16,0), `Regler für Rolladenposition` (71,0), `Zeit Strompreisanzeige` (5,0) |
| `light` | 21 | **ja** | `Lichtsteuerung Wohnzimmer`, `Lichtsteuerung Küche`, `Lichtsteuerung Schlafzimmer`, `Lichtsteuerung Ankleide`, `Steckdose Windfang` (**unavailable**) |
| `scene` | 17 | an | `Lights On`, `Morning Lights On`, `Cozy Mode`, `Cooking Mode`, `Guten Morgen`, `Guten Nacht`, `Abwesenheit`, `Willkommen zuhause`, `Film schauen` |
| `notify` | 14 | nein | `Pixel 9`, `motorola edge 50 ultra`, `tibber`, `Maxis Echo Sprechen` |
| `cover` | 14 | teilweise | `Wohnzimmer`, `Schlafzimmer`, `Küche Küchen Rollo`, `Esszimmer Terasse`, `Technikraum`, `FlurEG`, `FlurOG` — alle `open`, `current_position=41` |
| `select` | 13 | nein | `Thomas Ausgewähltes Programm`, `X-Sense Alarmton` (1/2/3), `Flur EG Alarmton` |
| `climate` | 12 | **ja** | `Klima Wohnzimmer`, `Klima Schlafzimmer`, `Klima Küche`, `Klima Milana`, `Klima Max`, `Klima Windfang` — alle `heat` |
| `calendar` | 8 | nein | `Abfalltermine 2026`, `Demnächst fällig`, `Feiertage in deutschland`, `Holidays in germany` |
| `media_player` | 8 | teilweise | `Maxis Echo` (idle), `Milanas Echo` (idle), `Überall` (idle); `Unser Dot`, `Pico4`, `Rundruf`, `Party Time`, `Andreas's Verona` — alle `unavailable` |
| `event` | 6 | read-only | — |
| `person` | 5 | read-only | — |
| `conversation` | 4 | read-only (Gateway) | HA-Conversations-Agenten (nicht our manager, siehe §6) |
| `zone` | 4 | read-only | — |
| `input_boolean` | 4 | nein | `Scene: Cozy Mode`, `Scene: Gameboard Mode`, `Scene: Cooking Mode`, `Mower: Schedule suppressed by HA` |
| `script` | 3 | **ja** | `Echo Ansage`, `Echo Ansage UG`, `Echo Ansage Max` |
| `input_select` | 3 | nein | `Klima Raum` (12 Optionen: Wohnzimmer…Windfang), `Lichtreihen` (2), `Wohnzimmer Lichter` (1/2/3) |
| `input_datetime` | 3 | nein | `Holiday: Start Date`, `Holiday: End Date`, `Mower: Re-enable schedule at` |
| `todo` | 3 | nein | `Einkaufsliste` (0), `…Einkaufsliste` (6), `…To-do-Liste` (8) |
| `fan` | 3 | nein | `Milana Ventilator Luftzirkulator` (25 %), `Max Ventilator Luftzirkulator` (25 %), `Turmventilator` (unavailable) — alle mit `SET_SPEED` |
| `alarm_control_panel` | 2 | nein (bewusst) | `Alarmanlage` (`disarmed`), `X-Sense Alarm` (unavailable) |
| `input_number` | 1 | nein | `Soil moisture threshold (irrigate below)` = −12,0 mm (min −30, max 0, step 0,5) |
| `sun` / `weather` | 1 / 1 | read-only | — |
| `tts` | 1 | nein | `Google Translate en com` |
| `image` | 1 | read-only | — |

**Nicht vorhanden:** `lock` (**0 Entities**), `vacuum` (0), `water_valve` (0), `humidifier` (0).
Die *Services* existieren (`lock.lock`/`unlock`, 9 `vacuum.*`), die *Entities* nicht.
→ Ein „schließ die Haustür ab"-Befehl hätte aktuell **weder** Entity noch Mapping.

**Räume:** `GET /api/states` liefert in dieser HA-Version **kein** `area_id` (0 von 931 Entities),
und `/api/config/area_registry/list` ist per REST nicht verfügbar. Der Raum ist deshalb nur über
`friendly_name` erschließbar (z. B. `Lichtsteuerung Schlafzimmer`). **Kein Raum-Filter** — das ist
auch strukturell so gewollt: `config.py:182-183` hält fest, dass ein Area-/Label-Filter über die
HA-REST-API **nicht umsetzbar** ist, und Ersatz ist die statische `HA_ALLOWED_ENTITIES`-Liste.

> **Korrektur 2026-09-30 (P12.T4 — diese Aussage war vorher falsch bzw. mindestens irreführend).**
> „Der Raum ist nur über `friendly_name` erschließbar" beschrieb den **Zustand in Home Assistant**,
> nicht den in **EVA**. Home Assistant legt den Anzeigenamen in **`attributes.friendly_name`**
> ab, und die A-5-Hilfe las ihn auf **oberster** Ebene des State-Mappings ⇒ bei **echten** Entities
> war der **Namensanteil** des Relevance-Scores live **immer 0** (es zählte nur die `entity_id`),
> und Prompt-Zeilen sowie Jev-`criteria` waren live **namenlos**. Das ist als **E115** dokumentiert
> und in **P12.T3b** an der Wurzel behoben (`attributes.friendly_name` ⇒ flach ⇒ `""`).
> **Live belegt (P12.T4-5):** „Wohnzimmer", „Klima Wohnzimmer", „Maxis Echo" und „Lichtsteuerung"
> werden über die echten `friendly_name` aufgelöst; der **Gegenbeleg** ist wichtig: „Wohnzimmerlicht"
> ist **kein** Live-Anzeigename ⇒ die Auswahl fiel auf 3 `automation.*`-Entities ⇒ `entity_id=None`
> ⇒ **kein** Call (fail-closed, kein Raten). **Merke:** `friendly_name` ist nach T3b die tragende
> Namensquelle — aber sie ersetzt **keinen** Area-Filter, und ein geratener Raumname wird nicht
> stillschweigend akzeptiert.

---

## 3. Ist-vs-Soll-Gap-Tabelle

| # | Wunsch | HA kann es? | Beispiel-Entities (live) | Warum nicht erreichbar | Was es bräuchte |
|---|---|---|---|---|---|
| G1 | **Rollo auf 30 %** | ✔ `cover.set_cover_position` | `Wohnzimmer`, `Schlafzimmer`, `Esszimmer Terasse` (alle `SET_POSITION`) | Dienst nicht in `JEV_SERVICE_CRITERIA`; Prompt nennt nur an/aus | `JEV_SERVICE_CRITERIA` + (`cover`,`set_cover_position`) in `ENTITY_PARAM_ALLOWLIST` mit `position` + Bereich 0–100 |
| G2 | **Heizung auf 21 Grad** | ✔ `climate.set_temperature` | `Klima Wohnzimmer` (22,3 °C), `Klima Schlafzimmer`, `Klima Küche` | Mapping zeigt auf `climate.turn_on` (falscher Dienst, s. 1.4) | Allowlist auf `("climate","set_temperature")` korrigieren + Bereich 5–35 |
| G3 | **Heizung auf Aus/Auto** | ✔ `climate.set_hvac_mode` | alle 12 Klima, `hvac_modes = [auto, off, heat]` | Dienst komplett unerwähnt im Prompt | `set_hvac_mode` in die Service-Auswahl + Werte-Mapping |
| G4 | **Lüfter auf 60 %** | ✔ `fan.set_percentage` | `Milana Ventilator Luftzirkulator`, `Max Ventilator Luftzirkulator` | `fan` **nicht** in `HA_ENTITY_DOMAINS` ⇒ nie im Cache | `fan` in `ha_entity_domains` + `set_percentage` ins Mapping |
| G5 | **Rohloff-Metabolit / Rolladen-Position** | ✔ `number.set_value` | `Regler für Rolladenposition` (71), `Rollos Manuell verfahren` (59) | `number` nicht im Filter | `number` in `ha_entity_domains` + `set_value` ins Mapping |
| G6 | **Strompreis ändern** | ✔ `number.set_value` | `Pool-Strompreis`, `Zisterne-Strompreis` | dito | dito |
| G7 | **Wähle Zimmer für die Heizung** | ✔ `input_select.select_option` | `Klima Raum` (12 Optionen) | `input_select` nicht im Filter | Domain + `select_option` + Options-Validierung |
| G8 | **Bewässerungsschwelle setzen** | ✔ `input_number.set_value` | `Soil moisture threshold` (−12,0 mm) | `input_number` nicht im Filter | Domain + `set_value` + Bereich −30…0 |
| G9 | **Szene „Cozy Mode" einschalten** | ✔ `input_boolean.turn_on` | `Scene: Cozy Mode`, `Scene: Gameboard Mode`, `Scene: Cooking Mode` | `input_boolean` nicht im Filter | Domain + `turn_on` ins Mapping |
| G10 | **Gerät neu starten** | ✔ `button.press` | `Pool Neu starten`, `Zisterne Neu starten`, `Garagenbeleuchtung Neu starten` | `button` nicht im Filter | Domain + `press` ins Mapping |
| G11 | **Auf die Einkaufsliste** | ✔ `todo.add_item` | `Einkaufsliste` (0), `…To-do-Liste` (8) | `todo` nicht im Filter | Domain + `add_item` (Freitext-Param!) |
| G12 | **Morgen wecken / Termin** | ✔ `calendar.create_event`, `tts.speak` | `Abfalltermine 2026`, `Feiertage in deutschland` | Domains nicht im Filter | Domain + Service + Datums-Parsing (aufwendig) |
| G13 | **Handy benachrichtigen** | ✔ `notify.send_message` | `Pixel 9`, `motorola edge 50 ultra` | `notify` nicht im Filter | Domain + `send_message` (Freitext-Pfad!) |
| G14 | **Hautüre abschließen** | ✔ `lock.lock`/`unlock` | **keine** — 0 lock-Entities | keine Entities + `lock` nicht im Filter | erst HA-seitig passende Entities anlegen, dann Filter + Mapping (⇒ Sicherheitsfrage §5) |
| G15 | **Alarmanlage scharf** | ✔ 7 Services | `Alarmanlage` (`disarmed`) | nicht im Filter | **bewusst offen lassen** (§5) |

**Rein read-only in HA — darüber kann EVA *grundsätzlich* nicht schalten, egal wie man es baut:**
`sensor` (344), `binary_sensor` (74), `device_tracker` (85), `person` (5), `zone` (4), `sun` (1),
`weather` (1), `image` (1), `event` (6). Zusammen **521 Entities** — mehr als die Hälfte der Instanz.

---

## 4. Deckungsgrad

| Größe | Wert |
|---|---|
| Entities gesamt | 931 |
| davon aktuierfähig (nicht read-only) | **406** (43,6 %) |
| im Entity-Cache des Managers (7 Domains, Stand vor P12.T2) | **209** → 51,5 % der aktuierfähigen |
| **im Entity-Cache (16 Domains, seit E113/P12.T2)** | **375** → **92,4 %** der aktuierfähigen |
| davon voll an+aus im `turn_on`-/`turn_off`-Schema (`light`, `switch`, `climate`, `script`) | **170** → **41,9 %** |
| inkl. `media_player` (Service existiert, Entities sind aber 5/8 `unavailable`) | 178 → 43,8 % |
| Obergrenze nach Ausbau (alle aktuierfähigen Domains gemappt) | **406** → 100 % |
| Obergrenze ohne Sicherheits-Domains (`alarm_control_panel` = 2) | **404** → 99,5 % |
| tatsächlich noch unsichtbar (16-Domain-Default, E113) | **31** = `notify` 14 + `calendar` 8 + `input_datetime` 3 + `todo` 3 + `tts` 1 + `alarm_control_panel` 2 |

**Antwort auf „kann ich alles steuern?"** — seit E113/P12.T2 fast: **375 der 406 aktuierfähigen
Entities (92,4 %)** sind im Cache; die restlichen 31 gehören zu Domains mit Freitext-/Datumsparametern
(`notify`, `calendar`, `todo`, `tts`, `input_datetime`) plus der bewusst blockierten
`alarm_control_panel`. Die **Hälfte der Instanz bleibt prinzipiell unsteuerbar** — 425 Read-only-
Entities (`sensor` 344, `binary_sensor` 74, `event` 6, `sun` 1); deren **Zustände** kann EVA erst
mit **P12.T3** abfragen (bis dahin: kein Read-Pfad). Vor P12.T2 waren allein wegen
`HA_ENTITY_DOMAINS` noch 197 aktuierfähige Entities unsichtbar.

---

## 5. Was EVA **nicht** kontrolliert — und warum das gut ist

Diese Lücken sind **Absicht bzw. Nebenwirkung der Domain-Liste** und sollten *nicht* „repariert" werden:

* **`lock` / `alarm_control_panel`** — 0 bzw. 2 Entities, beide Domains nicht im Filter.
  Es gibt **keinen** Sprachweg zum Abschließen oder Scharfschalten. Bei einer Sprachsteuerung
  ist das die richtige Voreinstellung: ein Fehl-Trigger darf **nicht** die Haustür öffnen.
* **`update`** (27) — `update.install` würde Firmware von Drittanbietern per Sprache auslösen. Nicht im Filter = richtig.
* **`automation`** (47) — `automation.trigger`/`turn_off` per Sprache kann Sicherheitslogik deaktivieren.
  47 Automationen im Haus sind viel Logik; sie gehören nicht in einen Wake-Word-Pfad.
* **`notify` / `tts` / `calendar`** — möglich, aber sie brauchen **Freitext-Parameter**
  („schreib *Milch* auf die Liste", „sag *gute Nacht*"). Die aktuelle Param-Architektur ist
  **bewusst auf genau einen Zahlenwert pro (domain, service)** ausgelegt (`ENTITY_PARAM_ALLOWLIST`,
  `router.py:223-253`). Freitext braucht einen eigenen, ebenfalls harten Pfad — sonst würde DeepSeek
  hier ungefiltert Text durchreichen (siehe 1.4).
* **`scene` mit „aus"** — `scene` kennt in HA nur `turn_on`. „Schalte Szene *Gute Nacht* aus" ist
  in HA nicht sinnvoll und der Aufruf würde mit 400 fehlschlagen (was der Manager als
  „Home Assistant antwortet nicht." übersetzt — irreführend, aber sicher).

**Achtung, echte Reichweite im Bestand:** `switch` umfasst 134 Entities, davon sind **31 `unavailable`**
— Befehle dagegen laufen ins Leere. Enthalten sind außerdem **8 „…Internetzugang"-Schalter**
(Anzeigename mit Tippfehler, u. a. `TY-WR Internetzugang`, `linux Internetzugang`, `iPad Internetzugang`),
sowie Sperrschalter (`ElternBad Sperre AN`, `Flur Unten Sperre AN`, `Gast WC Sperre AN`) und
Infrastruktur (`Pool`, `Zisterne`, `Poolautomatik`, `Zisternenautomatik`). **„Schalte den Internetzugang aus"
ist über Sprache möglich** — das ist gewollt (Feature) und auch der Grund, warum die
`HA_ALLOWED_ENTITIES`-Allowlist (E17) als Notverschluss existiert, obwohl sie live leer ist.

---

## 6. Zustandsabfragen: der blinde Fleck

> **Update 2026-09-30 (P12.T3 + T4): der Fleck ist geschlossen — der Rest dieser Seite beschreibt
> den Stand *vor* T3.** Block C hat einen zweiten Cache (`ReadableCache`) aus **derselben**
> `/api/states`-Antwort gespeist (**kein** zusätzlicher Request) und schreibt ≤12 relevanz-ausgewählte
> Zeilen mit Wert, `entity_id` und `Stand HH:MM` in **beide** Frage-Prompts; passt kein Wert, kommt
> `QUESTION_NO_DATA_TEXT` **ohne** LLM-Call. **Live belegt (T4):** „wie warm ist es im Wohnzimmer?" ⇒
> `sensor.temp_wohnzimmer` **23,1 °C** — identisch zum HA-Wert, am **echten** Prompt nachgewiesen;
> Gegenprobe Küche **23,2 °C** (zwei Werte ⇒ kein auswendig gelernter Standardwert). „wie hoch ist der
> Vulkan auf Island?" ⇒ Faktenblock **0 Zeilen**, **0** DeepSeek-Calls, **keine** erfundene Zahl.
> `device_tracker`/`unknown`/`unavailable`/`>24 h` sind live **0** von 115 Kandidaten, während HA
> 85/87/24 hat. **Weiterhin wahr (ehrliche Grenze):** die Liste macht Halluzinationen **seltener**,
> nicht unmöglich — verhindert wird nur, dass der Router **selbst** Werte erfindet.

**„Wie warm ist es im Wohnzimmer?" — EVA hat dafür keinen Datenpfad.**

Der Frage-Pfad ist technisch vorhanden und funktioniert *sprachlich*:
`JEV` klassifiziert `intent < 0.5` ⇒ QUESTION ⇒ `router.py:902` → `_route_question()` (`router.py:1376`).
Der Prompt dort ist aber:

```python
prompt = (
    f"Transkript: {transcript}\n\n"
    'Antworte als JSON: {"response_text": "<deutsche Antwort>"}'
)
```

**Kein Katalog. Keine States. Keine Temperaturwerte.** DeepSeek antwortet aus seinem Modellwissen.
`current_temperature` für `Klima Wohnzimmer` ist live **22,3 °C** — DeepSeek hat diese Zahl
prinzipiell nicht gesehen. Auch im `JEV_MODE=off`-Pfad (`router.py:1405-1407`) wird nur
`_entity_lines(catalog)` mitgeschickt, und das reduziert den Cache auf
`- <entity_id>: <friendly_name>` (`router.py:593-602`) — **ohne `state` und ohne `attributes`.**

> **Antwort auf die Sicherheitsfrage im Auftrag:** Die HA-`conversation`-API ist für unseren
> Manager **nicht** nutzbar — sie ist im Code nirgends referenziert (`conversation` kommt in
> `app/` nur als HA-Domain-Name in Fremdlisten vor, nie als Aufruf). Der P7-DeepSeek-QA-Pfad
> funktioniert, ist aber eine **reine Textantwort ohne HA-Ground-Truth**. Praktisch heißt das:
> **Temperatur-/Statusfragen werden derzeit halluziniert.** `HomeAssistantClient.get_entity()`
> (`ha_client.py:453`) existiert, wird vom Router aber nicht benutzt.

Das ist der einzige Punkt in diesem Dokument, der ein **Korrektheits**- und nicht nur ein
*Reichweiten*-Problem ist — und der einzige, der kein Konfigurations-, sondern ein Code-Fix braucht.

---

## 7. Praktische Liste: was du EVA **jetzt** sagen kannst

Echte Anzeigenamen aus der Live-Instanz, alle in Domains, die live im Cache sind:

1. **„Schalte das Wohnzimmerlicht an."** → `light.wohnzimmer` / `light.turn_on`
2. **„Mach das Licht im Schlafzimmer aus."** → `light.schlafzimmer` / `light.turn_off`
3. **„Schalte die Garagenbeleuchtung ein."** → `switch.garagenbeleuchtung` (oder `light.garagenbeleuchtung`)
4. **„Mach den Computer aus."** → `switch.* „Computer Steckdose 1"` / `switch.turn_off`
5. **„Starte die Szene *Gute Nacht*."** → `scene.guten_nacht` / `scene.turn_on` (5 von 17 Szenen sind `unavailable`)
6. **„Schalte den Internetzugang aus."** → `switch.* „… Internetzugang"` / `switch.turn_off`
7. **„Fahr das Rollo im Wohnzimmer auf."** → `cover.wohnzimmer`; ⚠️ verlässt sich auf `toggle` bzw. darauf, dass DeepSeek `open_cover` wählt
8. **„Sag Echo, dass wir essen gehen."** → `script.echo_ansage` / `script.turn_on` (Custom-Service)

**Grenze dieser Liste:** kein einziger dieser acht Sätze nutzt einen **Parameter**. Sobald ein
Zahlwert mitspielt („auf 40 Prozent", „auf 21 Grad"), greift der E92-Pfad — und der ist live
**inaktiv** (§1.4). In Variante A landet der Wert dann ungefiltert im `service_data` und wird von HA
verworfen: „Okay, *Name* eingeschaltet." ohne dass die 40 Prozent irgendwo ankämen.

## 8. Praktische Liste: was du **manuell** machen musst (oder wir bauen es)

Jeweils mit dem kleinstmöglichen Bau-Schritt:

1. **Rollo auf 30 %** → Dashboard, oder `cover` um `set_cover_position` erweitern (G1)
2. **Heizung auf 21 Grad** → Dashboard, oder `ENTITY_PARAM_ALLOWLIST` von `("climate","turn_on")` auf `("climate","set_temperature")` **korrigieren** (G2 — das ist ein Fehler, keine Erweiterung)
3. **Heizung auf Aus / Auto** → Dashboard, oder `set_hvac_mode` ins Mapping (G3)
4. **Lüfter auf 60 %** → Dashboard, oder `fan` in `HA_ENTITY_DOMAINS` + `set_percentage` ins Mapping (G4)
5. **Sperren/„Rolle um" ändern** (`ElternBad Sperre AN`) → Dashboard; `input_boolean` ist nicht im Filter (G9)
6. **Zimmer für die Heizung wählen** (`Klima Raum`) → Dashboard; `input_select.select_option` fehlt komplett (G7)
7. **Bewässerungsschwelle** (`Soil moisture threshold`) → Dashboard; `input_number.set_value` fehlt (G8)
8. **Gerät neu starten** (`Pool Neu starten`) → Dashboard-UI; `button.press` fehlt (G10)
9. **Einkaufsliste** (`todo.add_item`) → HA-UI; bräuchte zusätzlich einen Freitext-Pfad, nicht nur Zahlen (§5)
10. **„Wie warm ist es?"** → HA-UI **oder** HA-App; über EVA derzeit **nicht belastbar** (§6)

---

## 9. Was es bräuchte — priorisiert (Rezept, nicht ausgeführt)

| Prio | Änderung | Ort | Wirkung |
|---|---|---|---|
| **P0** | States in den Frage-Prompt (Temperatur, `state`, `friendly_name`) | `app/router.py:1376-1383` | behebt Halluzination bei allen Statusfragen |
| **P1** | `("climate","turn_on")` → `("climate","set_temperature")` korrigieren | `app/router.py:250` | 12 Klima-Entities werden endlich temperaturfähig |
| **P1** | `("media_player","*")` → `media_player.volume_set` | `app/router.py:251-252` | 3 erreichbare Echo-Player werden lautstärkefähig |
| **P1** | `brightness_pct` aus der Allowlist nehmen **oder** `light`-Support prüfen | `app/router.py:248-249` | 21 Licht-Entities unterstützen es nachweislich nicht |
| ~~**P2**~~ | ~~`ha_entity_domains` um `fan,number,select,input_boolean,input_select,input_number,button` erweitern~~ | **erledigt in P12.T2** (`app/config.py:179`, 16 Domains) | **+92 Entities** (3+23+13+4+3+1+45) — ist im Default passiert, keine `.env`-Zeile nötig |
| **P2** | `cover.set_cover_position` (+ `position`, 0–100) ins Mapping | `router.py:247` + `llm_client.py:182` | +14 |
| **P2** | `climate.set_hvac_mode`, `fan.set_percentage`, `input_select.select_option`, `number.set_value`, `input_number.set_value`, `button.press` | dito | zusammen erreichen die P2-Maßnahmen **404** aktuierfähige Entities (100 % minus `alarm_control_panel`) |
| **P3** | Service-Allowlist **auch** in Variante A erzwingen (statt „z. B."-Prompt) | `app/router.py:1335` | schließt die Lücke aus §1.4 |
| ~~**P3**~~ | ~~`lock` / `alarm_control_panel` **nicht** freischalten~~ | **erledigt in P12.T2** (`HA_BLOCKED_DOMAINS`) | bleibt Absicht (§5) und ist jetzt **hart** im Code, nicht nur in der `.env` |
| **P2** | `update` / `automation` **doch** freischalten | **erledigt in P12.T2** (User-Entscheidung, E113 g überholt) | +74 Entities; Risiko bleibt beherrschbar, weil jeder Service einzeln bestätigt werden muss |

---

## 10. Ehrliche Grenzen dieser Analyse

* **Berechnet, nicht aus dem laufenden Manager gelesen:** Die Cache-Größe **375** ist aus
  `HA_ENTITY_DOMAINS` (16) × `GET /api/states` **gegen die echte HA** gemessen
  (`GET /api/states` ∩ `GET /api/services`, 2026-09-30) — aber sie beschreibt, was der Manager
  beim nächsten Start **laden würde**, nicht was er gerade im Speicher hält: `ha_client.py` loggt
  die Zahl nur auf **DEBUG**, und live läuft `log_level=INFO`. **Bestätigt wird sie erst bei
  P12.T4** (Live-Abnahme auf `.106`).
  `GET /api/status` und `GET /api/state` des Dashboards exponieren die Cache-Größe nicht.
  Die Rechnung ist exakt dieselbe Logik wie `is_target_entity()`, aber sie ist eine Rechnung.
* **Nicht ausgeführt:** kein einziger Test, kein Service-Call, kein Neustart, kein Schreibzugriff
  auf HA oder auf `.106` (außer drei lesende Log-Zeilen und ein `scp` zweier Analyse-Skripte
  nach `/tmp` auf `.106`, danach dort belassen).
* **Nicht end-to-end belegt:** ob DeepSeek im Betrieb tatsächlich `open_cover` statt `cover.turn_on`
  liefert, ist eine **Annahme** aus dem Prompt-Wortlaut. Ein 4xx von HA wäre die Folge eines
  Fehlgriffs — sichtbar als „Home Assistant antwortet nicht.". Ohne Sprachturn nicht entschieden.
* **`state`-Werte sind ein Schnappschuss** (2026-09-30, zwischen 00:00 und 08:00 UTC gelesen).
  `unavailable`-Counts (31/134 switch, 5/8 media_player, 6/17 scene) sind Momentaufnahmen.
* **Nicht geprüft:** ob HA-Integrationen (KNX, KNX-Lichtsteuerung) `turn_off` auf
  Sicherheits-`switch`-Entities (die „Sperre AN"-Gruppe) als Schalthandlung interpretieren
  oder als `turn_on`/Sperr-Auflösung. Das kann die Ist-Liste für diese ~10 Entities verschieben.
* **Kein Raum-/Label-Filter** — bewusst nicht umsetzbar (`config.py:182-183`), siehe §2.
