# INSTALL — EVA per `install.sh` deployen (P10.T1/T2)

Zielbild (E102): `git clone` + `install.sh` deployt EVA auf **jedem**
Rechner. Standard ist **ein Host** (Manager + Whisper + Piper lokal);
verteilt (Manager auf Host A, STT/TTS auf Host B) rendert dieselbe Vorlage
— die Wahl trifft der Onboarding-Wizard (E102). Stand T2: der
**CLI-Onboarding-Wizard** (`deploy/onboarding/wizard.py`) ist Teil des
Produktions-Flows; `--test` bleibt das Wizard-freie Skelett mit Fake-Werten.

---

## 1. Voraussetzungen

| Voraussetzung | Anmerkung |
|---|---|
| Linux (Debian/Ubuntu getestet; Arch implementiert, **ungetestet**) | Root oder `sudo` nötig |
| `git` | zum Klonen des Repos |
| **Internet** beim ersten Start | Modell-Auto-Download ~574 MB (Whisper-small 461 MiB, Piper thorsten-high 109 MiB) + Container-Images |
| **RAM** | single-host: ~700 MB für `small`+int8 (Idle ~677 MB gemessen) + Manager (~54–400 MB, Limit 512m) + Piper (~42–250 MB, Limit 1g). Bei engem Host: `mem_limit`s in der gerenderten `docker-compose.yml` anpassen oder Topologie verteilt (T2) |
| Ports am Host | `8767` (Manager, **host-Netz**), `10300` (Whisper), `10200` (Piper) — belegt müssen frei sein |

## 2. Der Ein-Befehls-Flow (Produktions-Deploy, E103/E104)

Der Freund braucht genau **zwei** Befehle — `install.sh` (ohne `--test`)
endet im Onboarding-Wizard:

```bash
git clone <repo-url> eva-src
cd eva-src
sudo ./deploy/install.sh            # Docker prüfen/installieren → Wizard startet
```

### Was der Wizard fragt (E104 „schlank")

1. **Willkommen**: kurz, was installiert wird (Manager + Whisper + Piper);
   Secrets landen **nur lokal** in der `.env` (chmod 600) — nie ins Git.
2. **Topologie (E102)**: „Alles auf einer Maschine? [J/n]“ — Default **Ja**.
   Bei „nein“: Adresse des zweiten Hosts (STT/TTS) ⇒ `WHISPER_HOST`/
   `PIPER_HOST` werden abgeleitet; der Wizard erzeugt **zusätzlich** ein
   zweites Env/Compose-Set (`docker-compose-remote.yml`) und gibt die
   Anleitung für Host B aus (`scp` + `docker compose up -d` dort;
   SSH-Automatisierung ist bewusst **nicht** implementiert). Bei
   Single-Host-Wahl: **RAM-Check** (freier RAM < ~1,5 GB ⇒ Warnung mit den
   gemessene ~700 MB idle für Whisper `small`+int8) — bei verteilter
   Wahl entfällt sie.
3. **Pflichtfelder** (3 Stück):
   - `HA_BASE_URL` — Vorschlag `http://<LAN-IP>:8123` (erste IPv4 des Hosts).
   - `HA_TOKEN` — Eingabe **verdeckt** (getpass).
   - `LLM_API_KEY` — Eingabe verdeckt; Format-Check (erwartet ~51 Zeichen).
   **Validierung ist Warnung, kein Blocker:** der HA-Check (`GET /api/`
   mit Bearer) unterscheidet „erreichbar, Token ok“ (200) vs. „erreichbar,
   Token falsch“ (401/403) vs. „nicht erreichbar“; bei Fehlschlag kommt
   „Trotzdem fortfahren? [j/N]“. Die LLM-Reachability (`GET …/zen/v1/models`)
   ist **nicht blockierend** (manche Gateways erlauben keine Gratis-Verifizierung).
4. **Defaults bestätigen statt abfragen**: kompakte Tabelle
   (Whisper `small`+int8, `OWW_THRESHOLD` 0,80, `TURN_NO_SPEECH_SECONDS` 8,
   initial-prompt 63 Begriffe, mDNS an, Port 8767) — `[Enter]` = übernehmen;
   nur bei explizitem **`e`** (expert) wird je Feld nachgefragt.
5. **Zusammenfassung + Bestätigung**: „So wird deployt: … Starten? [J/n]“.
6. **Generieren + Hochfahren**: `.env` (chmod **0600**), Compose rendern,
   `docker compose up -d --build`, Warten bis 3 Container healthy (1 bei
   verteilter Topologie — Manager nur), `/health` prüfen, Summary
   (Dashboard `http://<ip>:8767/dashboard` — Auth kommt später). Der
   **Echo-Dot verbindet sich danach selbst** (mDNS, zustandsloses Pairing).
7. **Re-Run/idempotent**: existiert die `.env` bereits, fragt der Wizard
   übernehmen (Wizard-Keys aktualisieren, Rest bleibt) / neu generieren.
   Ctrl-C bricht jederzeit sauber ab (Meldung; falls der Stack schon fuhr:
   `docker compose --project-directory <dest> down`).

### Non-Interactive-Modus (für automatisierte Tests)

```bash
sudo ./deploy/install.sh --non-interactive --answers-file <datei>
```

Format = env-Datei mit genau den Wizard-Fragen; ohne sie bricht der
Non-Interactive-Modus mit Usage-Fehler ab:

```bash
TOPOLOGY=single|distributed      # Default single
REMOTE_HOST=<ip>                 # Pflicht bei TOPOLOGY=distributed
HA_BASE_URL=http://<ha-ip>:8123  # Pflicht
HA_TOKEN=<token>                 # Pflicht
LLM_API_KEY=<key>                # Pflicht
WIZARD_ENV_MODE=keep|fresh       # Default keep (existierende .env mergen)
# … beliebige weitere KEY=VALUE-Zeilen gelten als Override, z. B.
# MANAGER_MDNS_ENABLED=false für ein Test-Deploy (mDNS-Isolation!).
```

Validierungen laufen auch non-interactive — als **WARN im Log**, dann
fortfahren (damit Tests automatisierbar bleiben). Der Wizard rendert mit
`--no-up` auch nur (ohne Stack-Start) — nützlich für
Topologie-/Render-Prüfungen.

### Verteilte Topologie — Reihenfolge für Host B

Der Wizard erzeugt bei „verteilt“ zwei Sets: `docker-compose.yml`
(**nur** Manager-Service, für Host A) und `docker-compose-remote.yml`
(Whisper+Piper im Bridge-Netz mit Port-Publish `0.0.0.0:10300/10200`,
Live-`.106`-Muster, für Host B). **Host B zuerst deployen** (siehe die
vom Wizard ausgegebene Anleitung), dann Host A hochfahren — sonst bleiben
STT/TTS tot. Voraussetzung Host B: Docker + ca. 2 GB RAM für Whisper
`small`+int8 (Modell-Download ~574 MB beim ersten Start).

### Deploy ohne git-Remote (rsync-Variante, P10.T5b/1 belegt)

Hat der Ziel-Host keinen Zugriff auf das Repo (E4: bewusst kein
git-Remote), ersetzt **rsync** das `git clone` — vom Arbeitsrechner aus:

```bash
rsync -a --exclude .git --exclude .env --exclude .env.test \
      --exclude WLAN_CONNECT.md --exclude docs/reference \
      --exclude __pycache__ --exclude reports \
      ./ root@<ziel-host>:/opt/eva-src/
cd /opt/eva-src && sudo ./deploy/install.sh
```

Wichtig: **Secrets und Klartext-Passwörter niemals mit rsync mitrollen**
(`.env`, `WLAN_CONNECT.md`) — echte Werte kommen über den Wizard bzw.
serverseitig in ein chmod-600-Answers-File, das **nach dem Wizard-Lauf
gelöscht** wird.

**Modell-Wiederverwendung bei Folge-Deploys** (hier: Produkt-Install nach
alter P9-Compose auf demselben Host): der Stack mountet
`/opt/eva/data/{whisper,piper}` → `/data` im Container. Lagen die
Modelle (HF-Cache `models--Systran--faster-whisper-small`, Piper-`.onnx`)
schon unter `/opt/eva/{whisper,piper}`, genügen Symlinks — der erste
Start überspringt den ~17-min-Download:

```bash
mkdir -p /opt/eva/data
ln -s ../whisper /opt/eva/data/whisper
ln -s ../piper   /opt/eva/data/piper
```

Vor dem `up` ggf. alte Container **stoppen + umbenennen** (die Templates
nutzen dieselben `container_name`): `docker stop eva-whisper
eva-piper && docker rename eva-whisper eva-whisper-p9t4 &&
docker rename eva-piper eva-piper-p9t4` — **nicht rm** (Rollback
bleibt: `docker start eva-whisper-p9t4 eva-piper-p9t4`). Beleg
P10.T5b/1: Whisper-Log ohne Download-Zeilen, direkt `Ready`, alle 3
Container healthy nach Sekunden; Smoke über Produktions-Clients grün.

**Cutover-Beleg (P10.T5b/1, 2026-09-29):** exakt nach §5c — neuer Manager
gestoppt (14:56:49), Alt-Manager `.123` gestoppt (14:56:55), neuer Manager
gestartet (14:56:58) und damit konfliktfrei als `echomuse` annonciert
(`mdns_name_conflict:false`); der Dot `G090L91072320Q6E` verband sich
selbst und zeigte ~20 s nach dem Alt-Stop `/api/pairing` = `paired`,
`connected:true`, `mic_synced:true` (erster Versuch, kein Rollback).

## 3. Test-Deploy (ohne echte Secrets, ohne Dot, ohne Wizard)

```bash
sudo ./deploy/install.sh --test
```

`--test` tut alles wie oben, aber:

- **Fake-Werte** statt echter Secrets: `HA_TOKEN=FAKE-HA-TOKEN-…`,
  `LLM_API_KEY=FAKE-LLM-KEY-…`, `HA_BASE_URL=http://127.0.0.1:8123`
  (nicht erreichbar — der Manager startet trotzdem, Robustheitstests
  P9.T5; HA-Calls scheitern erst zur Laufzeit).
- **`MANAGER_MDNS_ENABLED=false`** — kritische Isolation: der
  Test-Manager announciert `_emcontroller._tcp` **nicht**. Der echte
  Dot (der seinen Manager per mDNS findet) kann den Test-Deploy damit
  **nicht** erreichen und bleibt beim Produktions-Manager.
- Smoke-Tests (STT/TTS) laufen trotzdem — Whisper/Piper sind lokal da.

## 4. Was der Installer tut (Schritte)

**Ohne `--test` (Produktion):** Docker prüfen/installieren (1), Zielverzeichnis
anlegen (2), dann übernimmt der **Wizard** alles Weitere — die Schritte 3–6
unter „Was der Wizard fragt" (§2), inklusive `up -d`, Health-Wait und Summary.
**Mit `--test`:** voller Ablauf ohne Wizard:

1. Docker + compose-plugin prüfen, notfalls installieren
   (Debian: offizielles `docker-ce`-Repo; Arch: `pacman` — **ungetestet**).
2. Zielverzeichnis anlegen (Default `/opt/eva`, `--dest` überschreibt):
   `data/{whisper,piper}` (HF-Cache, dauerhaft), `share/`.
3. `.env` aus `deploy/test.env.example` rendern, `chmod 600`.
   Single-host setzt `WHISPER_HOST`/`PIPER_HOST=127.0.0.1` (explizit,
   E106 b).
4. `docker-compose.yml` aus `deploy/compose/docker-compose.yml.tmpl`
   rendern (Platzhalter: Build-Kontext = Repo-Wurzel, `DATA_DIR`,
   Modell-Args, `initial-prompt` aus `deploy/initial-prompt.txt`).
5. `docker compose up -d --build` (Manager-Build ~3–10 min je nach Kernzahl;
   Modell-Download ~574 MB beim ersten Whisper-/Piper-Start).
6. Warten bis **3 Container healthy**, dann `/health` prüfen.
7. Smoke-Test über die **Produktions-Clients** im Manager-Container:
   `SttClient` (Fixture-WAV → Transkript) und `TtsClient` (Text → PCM).
8. Summary + Next-Steps.

## 5. Verifizieren nach dem Deploy

```bash
docker compose --project-directory /opt/eva ps          # 3× healthy
curl -s http://127.0.0.1:8767/health                       # devices, mdns
# mDNS sichtbar? (nur Produktions-Deploy, --test hat mDNS AUS)
avahi-browse -rt _emcontroller._tcp
```

Erwartung Produktions-Deploy: `/health` `{"devices":0,"mdns":true}` vor
dem ersten Dot-Connect; der Dot verbindet sich selbst (zustandsloses
Pairing).

## 5b. Dot verbinden (P10.T3)

**Du musst nichts koppeln.** Pairing ist zustandslos: der Manager
announciert mDNS (`_emcontroller._tcp.local`, Instance `echomuse`), und der
Dot findet den Manager **selbst** und verbindet sich von selbst — in der
Regel binnen ~1 Minute nach Manager-Start. Voraussetzung: Dot und Manager
im **selben Netz** (WLAN), und der Manager-Port **8767** ist am Manager-Host
erreichbar (inbound, nur WebSocket/TCP — kein UPnP, kein Internet).

### Was der Freund sieht

Der Wizard endet mit der Pairing-Ampel in der Summary:

```
Dot-Pairing : mDNS an — warte auf deinen Dot… (verbunden: keine)
Dot-Pairing : verbunden: G090L91072320Q6E
```

### Status prüfen

```bash
curl -s http://127.0.0.1:8767/api/pairing | python3 -m json.tool
```

Die Ampel (`status`):

| Wert | Bedeutung |
|---|---|
| `waiting` | mDNS an, **kein** Dot verbunden — warten (Dot verbindet sich selbst) |
| `paired` | Dot verbunden; `mic_synced:true` = Mic-Stream läuft (Seq-Sync) |
| `mdns_off` | `MANAGER_MDNS_ENABLED=false` — der Dot kann den Manager **nicht** finden |
| `unknown` | Zustand nicht ermittelbar → `issues` lesen |

Ein Gerät sieht so aus:
`{"device_id": "G090L91072320Q6E", "connected": true, "state": "idle", "mic_synced": true, …}`
„verbunden" gilt erst mit `mic_synced:true` (erster Mic-Frame mit
Sequenznummer). Der **Instanzname muss `echomuse` bleiben** — der Dot
filtert darauf; steht in `mdns_announced_as` stattdessen `echomuse-2`,
hält ein **zweiter** Announcer den Namen im Netz (der Manager warnt
zusätzlich im Log: „Doppel-Announce im Netz?").

### Fehlersuche

- **`mdns_off` oder kein Dot nach >2 min:** `MANAGER_MDNS_ENABLED` in
  `/opt/eva/.env` prüfen (true) und `docker compose --project-directory
  /opt/eva up -d` neu starten; danach `avahi-browse -rt
  _emcontroller._tcp` — genau **ein** Announcer muss den Manager-Host
  zeigen.
- **Dot nicht im Netz / falsches WLAN:** EVA richtet **kein** Geräte-WLAN
  ein (geräte-seitiges WiFi-Setup — offene Lücke
  **E105**). Ist der Dot in einem anderen WLAN/VLAN, sieht er die mDNS-
  Ansage nicht; `waiting` bleibt dauerhaft stehen.
- **Firewall:** Port **8767/tcp** muss am Manager-Host inbound offen sein
  (WebSocket `/control`, `/data`, `/shell`). Ohne diesen Port verbindet der
  Dot sich nicht, auch wenn mDNS sichtbar ist.
- **Doppel-Announce:** nur **ein** EVA-Manager im Netz mit demselben
  Instanznamen fahren. Ein zweiter (alter) Manager mit `echomuse` kann den
  Dot zur Instanzwechsel-Verwirrung bringen; der Manager warnt dann mit
  „Doppel-Announce" bzw. loggt die Namensänderung auf `echomuse-2`.
- **Dot verbindet sich und trennt sofort:** Manager-Logs prüfen
  (`docker logs eva-manager`); häufigste Ursache: Port-Erreichbarkeit
  nur für `/control`, aber Blockade im Weg (Firewall/Router-Isolation).

## 5c. Cutover auf ein neues Deploy (P10.T4, Alt-Manager läuft noch)

Läuft bereits **ein** EVA-Manager im Netz (Instanzname `echomuse`), gilt beim
Cutover auf ein neues Deploy (z. B. Test-LXC → Produktion) die Reihenfolge unten.
**Hintergrund:** Zeroconf vergibt den Namen `echomuse` genau einmal — startet das
neue Deploy, während der alte Manager noch announciert, registriert es sich als
`echomuse-2` (`mdns_name_conflict:true`, `/api/pairing`), und der Dot (filtert auf
`echomuse`) findet es **nicht**. Deshalb: Konflikt vermeiden, indem zur
Umschalt-Minute **kein zweiter** Announcer im Netz ist.

**Cutover-Reihenfolge (am echten Dot belegt, P10.T4a):**

1. Neues Deploy: Manager-Container **stoppen** (`docker compose stop wyoming-manager`).
2. Alt-Host: `docker compose stop wyoming-manager` — der Dot verliert die Verbindung (normal).
3. Neues Deploy: Manager **starten** — registriert sich jetzt konfliktfrei als
   `echomuse`; der Dot verbindet sich selbst (belegt: ~16 s nach Schritt 2,
   `mic_synced` ~5 s später).

**Rollback (Notfall-Reihenfolge ist bindend — sonst Split-Brain):**

1. **ZUERST** das neue Deploy stillegen: `docker compose --project-directory
   /opt/eva stop wyoming-manager` (oder ganz `down`).
2. **DANACH** den Alt-Manager starten: `docker compose start wyoming-manager`
   (auf dem Alt-Host) — der Dot kommt per mDNS zu ihm zurück (zustandsloses
   Pairing).

Falsche Reihenfolge (erst Alt starten) riskiert zwei gleichzeitige `echomuse`-
Announcer und ein `echomuse-2`-Gerangel — der Dot kann dann am falschen (oder
gar keinem) Manager landen.

## 6. Rollback / Abbau

```bash
# Stack stoppen + entfernen (Daten in /opt/eva/data bleiben):
docker compose --project-directory /opt/eva down
# Komplett inkl. Daten:
sudo rm -rf /opt/eva
# Nur Images (optional):
docker rmi eva-manager:latest rhasspy/wyoming-whisper:latest rhasspy/wyoming-piper:latest
```

Test-LXC (Homelab, falls von PVE erstellt):

```bash
ssh root@<pve-host> pct destroy 130          # eva-deploy-test
```

Der LXC **bleibt für T2/T3/T4 bestehen** — `destroy` erst nach Abschluss
der Phase oder nach Rücksprache.

## 7. Grenzen (ehrlich, Stand T3)

- **Arch-Pfad ungetestet** (Testumgebung war Debian 12) — nutzbar, aber
  ohne Beleg.
- **Verteilte Topologie generiert + plausibel geprüft** (Test B, `--no-up`),
  aber **nicht auf einem zweiten Host deployt** — der Remote-Pfad bleibt bis
  zum echten Einsatz ungeprüft (SSH-Automatisierung bewusst „später“).
- **Interaktiver Durchlauf mit echten Creds** = Abnahme am echten Dot
  (PLAN P10.T4/T5); der E2E-Beweis von T2 lief non-interactive mit Fake-Creds.
- Smoke-Test (STT/TTS) gehört zum `--test`-Pfad; der Wizard endet mit
  Health (3 healthy + `/health` 200) — der STT/TTS-Smoke ist manuell
  nachholbar (`install.sh --test` dokumentiert ihn).
- **`VAD_SILENCE_MS` existiert bewusst nicht als Wizard-Default**: VAD läuft
  geräteseitig (`VAD_DEVICE_SIDED=true`); der nächste Manager-Default ist
  `TURN_NO_SPEECH_SECONDS=8` — die Defaults-Tabelle zeigt die echten
  `app/config.py`-Keys, keine erfundenen.
- Ein Netz mit **laufendem Produktions-Manager** darf nur mit
  `MANAGER_MDNS_ENABLED=false` getestet werden (sonst Risiko, dass der
  Dot zur zweiten Instanz wechselt — `allow_name_change` erzeugt
  `echomuse-2`, der echte Manager bliebe dann ggf. unverbunden).
- **Pairing-Beleg von T3 lief mit dem Fake-Dot** (`tests/fakes/
  fake_echomuse.py`, direkte WS-Adresse, mDNS am Test-LXC per
  Multicast-Firewall isoliert): vollständiger Handshake, Mic-Seq-Sync,
  0x02/0x03-Antwortframes und die drei Fehlfälle sind belegt. Der Beleg
  am **echten** Dot (inkl. echter mDNS-Sichtbarkeit im LAN) ist
  ausdrücklich **T4**.
- **Geräte-WiFi-Setup** (Erst-Provisionierung des Dots ins WLAN) ist
  weiterhin nicht abgedeckt (**E105**).

## 8. Dashboard-Config-GUI (P11, E110/E111)

Das Dashboard zeigt nicht nur Werte — der Konfig-Tab erlaubt das Ändern
eines sicheren Kerns der Konfiguration direkt aus dem Browser (ohne SSH,
ohne `.env` von Hand zu editieren).

### Erreichen

```
http://<manager-host>:8767/dashboard        # Tab „Konfiguration"
```

### Auth (E110): `DASHBOARD_API_TOKEN`

- **Default leer = offen** (Homelab-LAN, Backward-kompatibel): alle Lese-
  endpunkte und Schreibaufrufe funktionieren ohne Token.
- Token setzen: `DASHBOARD_API_TOKEN=<wert>` in die **`.env` des Deploy**
  (`/opt/eva/.env`) und den Manager neu hochfahren:

  ```bash
  cd /opt/eva && docker compose up -d wyoming-manager
  ```

  **Achtung:** ein `docker compose restart` lädt die `.env` **nicht** neu
  (Env ist bei Create eingebacken, live belegt P11.T4) — Recreate über
  `up -d` nötig.
  **Und:** prüfe nach einem Wechsel eines **Default-Werts im Code**, ob die `.env`
  auf dem Ziel ein **eigenes** `KEY=` für denselben Schlüssel führt — ein
  veralteter Override gewinnt still gegen den neuen Default (live belegt
  P12.T4: `HA_ENTITY_DOMAINS` stand dort noch auf der alten 7er-Liste, live
  liefen 209 statt 375 Entities). Gegenprobe: Startlog-Zahl gegen die
  Soll-Zahl im Repo, oder read-only gegen Home Assistant nachrechnen.
- Ist ein Token gesetzt, verlangen **alle** Schreibaufrufe
  (`PUT /api/config`, `POST /api/config/oww-threshold`) den Header
  `Authorization: Bearer <wert>`; fehlend/falsch ⇒ **401** (nicht-leakend).
  **Lese-Endpunkte bleiben ohne Token 200.** Im Browser erscheint dann ein
  Token-Eingabefeld im Konfig-Tab (das der Browser nicht kennt — der Wert
  wird pro Save mitgeschickt).
- Zweite Schicht: `DASHBOARD_WRITE_ENABLED=true` in der `.env` — ohne sie
  antwortet jede Schreibroute **403** (Default `false`).

### Die 11 editierbaren Felder (E111)

| Feld | Wirkung ohne Neustart? |
|---|---|
| `oww_threshold` (0–1) | **live** (Wake-Schwelle, sofort in `/api/wake` sichtbar) |
| `log_level` (DEBUG/INFO/WARNING/ERROR) | **live** (Logger-Level wird nachgezogen) |
| `audio_dump_enabled` | **live** |
| `whisper_language` | **live** (nächster STT-Call) |
| `oww_barge_in_threshold` | Neustart |
| `oww_cooldown_ms` | Neustart |
| `wake_attempt_keep_floor` | Neustart |
| `router_confidence_gate` | Neustart |
| `router_needs_param_threshold` | Neustart |
| `jev_mode` (intent/gate/off) | Neustart |
| `ha_entity_domains` (CSV) | Neustart |

Die GUI zeigt je Feld ein Badge („live" / „Neustart"); der Save antwortet
mit `written`, `effective_without_restart` und `restarted_required`.

### Save-Verhalten

- **Atomar in eine Zeile je Key** über `write_env_value()`: temporäre Datei
  + `os.replace` im selben Verzeichnis, Modus **0600**; Secrets
  (`HA_TOKEN`, `LLM_API_KEY`) und Kommentare bleiben bytegleich; keine
  Duplikat-Zeilen (Idempotenz).
- **Persistenz-Grenze (bewusst, E96):** geschrieben wird die `.env` **im
  Container** (`/app/.env`, Empty-File im Image — P11.T4). Sie übersteht
  `docker compose restart`, **nicht** ein Recreate/Rebuild — dann liest der
  Prozess wieder die **Host**-`.env` (env_file). Für dauerhafte Werte die
  Host-`.env` ändern. Die Host-`.env` wird absichtlich **nicht** gemountet
  (keine Schreibfläche auf der Secret-Datei, E17).
- Jeder Save landet als `INFO`-Audit-Zeile (E111-Marker) im Log — Felder +
  Neuwerte, **nie** ein Secret.

### Was bewusst NICHT editierbar ist (und warum)

- **Modellwechsel** (`whisper_model`, `piper_voice`, `oww_model`): die
  wirksamen Modelle liegen in den **Compose-Args der Whisper-/Piper-Container**
  (`--model`/`--voice`), nicht im Manager-`.env` ⇒ ein Button *im* Manager
  kann sie nicht ändern — das bleibt ein Host-Vorgang (`docker compose up -d`
  nach dem Template-Edit; die Felder erscheinen in der GUI nur informativ).
- **Kein Restart-Button:** der Container hat keinen Host-Zugriff ⇒ Restart
  immer `docker compose up -d` von außen.
- **Secrets/Wizard-Domäne** (`ha_token`, `llm_api_key`, `ha_base_url`,
  `manager_port`, `manager_mdns_name`): sensitiv bzw. am Dot-Pairing
  gekoppelt — Wizard-Neulauf statt GUI.
- **Sonstige:** `enable_test_hooks`/`run_live` (Test-Pforten), Audio-Format
  (S16_LE-Kopplung über alle Instanzen), der Negations-Guard (hartkodiert in
  `app/router.py`, kein Env-Key).

### Reject-Verhalten

| Fall | Antwort |
|---|---|
| Nicht erlaubter/Secret-Key im Body (z. B. `ha_token`) | **400** — Detail nennt nur die unbekannten Felder, **kein** Leaking |
| Leerer Body `{}` | **400** |
| Falscher Typ/Wertebereich | **422** (bestehende Pydantic-Validatoren) |
| Token fehlt/falsch (wenn gesetzt) | **401** (vor der Write-Schicht geprüft) |
| `DASHBOARD_WRITE_ENABLED` ≠ `true` | **403** |

Alle Schritte sind live am Produkt (`.106`, 2026-09-30) belegt: Open-Mode
200 + Wirkung (DEBUG-Pong-Zeilen, `/api/wake` 0,85), Reject 400, Auth
401/200, Lese-Routen offen, Rollback auf Baseline.
