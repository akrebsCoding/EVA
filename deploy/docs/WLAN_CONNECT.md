# Dot ins WLAN bringen (E105)

> **Hinweis:** Diese Anleitung wurde aus dem Alt-Setup übernommen und für das
> Produkt angepasst (Quelle: `WLAN_CONNECT.md` im Wipe-Archiv
> **Wichtig:** Das Archiv enthielt die WLAN-Credentials in Klartext. Sie sind
> bewusst **nicht** Bestandteil dieser Datei (E105: niemals ins Repo) — WLAN-Name
> und -Passwort beim Betreiber erfragen bzw. aus dem (zugriffsbeschränkten)
> Archiv auf `.123` beziehen.

## 1. Überblick

Es gibt zwei getrennte Dinge:

1. **Dot ins WLAN** — geräteseitige Anmeldung am Funknetz. Das macht der
   Manager **nicht** mit: EVA richtet kein Geräte-WLAN ein, der Wizard
   deckt kein Geräte-WiFi-Setup ab (E105).
2. **Dot findet den Manager** — läuft danach **automatisch**. Der Dot sucht
   per mDNS (`_emcontroller._tcp.local`, Instanzname `echomuse`) und
   verbindet sich selbst; in der Regel binnen ~1 Minute nach Manager-Start
   (INSTALL.md §5b). Ein „Koppeln“ gibt es nicht — das Pairing ist
   zustandslos.

Die WLAN-Provisionierung ist **unabhängig vom Manager**: Der Dot muss erst
im Funknetz sein, um überhaupt mDNS hören zu können. Der Manager-Host
(beliebig, aktuell `.106`) spielt dafür keine Rolle.

## 2. Dot ins WLAN bringen — Schritt für Schritt

**Ehrliche Grenze:** Die Alt-Doku dokumentierte **keine** Provisionier-
Schritte — nur WLAN-Name und Passwort. Der exakte Tasten-/Menü-Weg am
EchoMuse-Dot ist **unverifiziert** und wird hier nicht geraten. Was
verifiziert ist, steht unter „Fakten“.

Schritte (verifiziert / offen):

1. **Credentials bereitlegen** — WLAN-Name (SSID) und Passwort vom Betreiber
   erfragen (siehe Hinweiskasten). *(verifiziert: die Alt-Doku bestätigte,
   dass genau diese beiden Angaben gebraucht werden)*
2. **Provisionierung am Dot ausführen** — **unverifiziert/offen:** der
   gesicherte Referenz-Quelltext kennt nur den **WiFi-Wechsel** einer
   bereits verbundenen Verbindung über die Controller-API
   (`POST /api/devices/{id}/wifi` + Geräte-Report `wifi_result`, TTL ~240 s,
   `em_api.py:191-235`). Ein Initial-AP/Portal-Setup
   ist im gesicherten Quelltext **nicht** dokumentiert. Bis zur Verifizierung
   gilt: am Gerät vorgehen, wie es der Betreiber beim Erst-Setup getan hat
   (Standard-Echo-App-Weg für den Echo Dot 2. Gen mit EchoMuse-Firmware ist
   **nicht** geprüft).
3. **Netz-Zugehörigkeit prüfen** — der Dot muss im **selben Netz** (WLAN,
   kein VLAN-Split) wie der Manager hängen und eine Adresse haben
   (Alt-Stand: `.24`). *(verifiziert als Voraussetzung; Prüfweg: Router/
   DHCP-Liste oder Ping)*
4. **Weiter mit Abschnitt 3** — der Dot findet den Manager danach selbst.

## 3. Dot findet den Manager (automatisch)

Voraussetzungen (INSTALL.md §5b):

- Der Manager announciert mDNS `_emcontroller._tcp.local.` mit Instanzname
  **`echomuse`** (TXT `version=1`, `server=echomuse`), Port **8767** — der
  Instanzname **muss** `echomuse` bleiben, der Dot filtert darauf.
- Der Dot dialt daraufhin `ws://<manager-ip>:8767` und macht den
  Handshake (`register` → `ack` → `config` → `mic_start`); „verbunden“
  gilt erst mit `mic_synced:true` (erster Mic-Frame).
- Kein `tls_port` in der Ansage ⇒ plain `ws://` (kein Zertifikatsaufwand).

## 4. Pairing-Status prüfen

Am Manager-Host (oder im LAN, Port 8767):

```bash
curl -s http://<manager-host>:8767/api/pairing | python3 -m json.tool
```

Ampel (`status`):

| Wert | Bedeutung |
|---|---|
| `paired` | Dot verbunden; `mic_synced:true` = Mic-Stream läuft |
| `waiting` | mDNS an, **kein** Dot — warten (der Dot verbindet sich selbst) |
| `mdns_off` | `MANAGER_MDNS_ENABLED=false` — der Dot kann den Manager **nicht** finden |
| `unknown` | Zustand nicht ermittelbar → `issues` lesen |

Das Dashboard (`http://<manager-host>:8767/dashboard`) zeigt denselben
Stand; der Wizard-„Summary“-Schritt fragt denselben Endpoint ab.

## 5. Fehlerfälle

| Symptom | Ursache | Was tun |
|---|---|---|
| `waiting` dauerhaft, Dot gar nicht im Netz | WLAN-Provisionierung fehlgeschlagen / Dot offline | Abschnitt 2 wiederholen; Dot-Erreichbarkeit (Router/DHCP) prüfen |
| `waiting` dauerhaft, Dot **im** Netz | Dot in anderem WLAN/VLAN (sieht die mDNS-Ansage nicht) oder mDNS aus | `MANAGER_MDNS_ENABLED` in `/opt/eva/.env` auf `true`, dann `docker compose --project-directory /opt/eva up -d` (Neustart — `restart` lädt die `.env` nicht neu) |
| Manager nicht sichtbar per `avahi-browse -rt _emcontroller._tcp` | mDNS aus / Firewall | Genau **ein** Announcer muss den Manager-Host zeigen; Port **8767/tcp** muss am Manager-Host inbound offen sein (auch bei sichtbarer Ansage — ohne Port dialt der Dot vergebens) |
| `mdns_announced_as` = `echomuse-2` | **Doppel-Announce**: ein zweiter Announcer hält den Namen | Nur **einen** Manager mit `echomuse` fahren; den anderen stoppen. Der Dot findet `echomuse-2` ggf. nicht |
| Dot verbindet sich und trennt sofort | Port-Erreichbarkeit nur teilweise / Router-Isolation | Manager-Logs prüfen (unten) |

**Log-Quellen:** `docker logs eva-manager` (Manager-Host). Relevant:

- `mDNS announciert echomuse…→<ip>:8767` — Announcement aktiv.
- `Gerät registriert: G090L91072320Q6E (caps=…)` + `Datenverbindung
  etabliert` — Dot verbunden.
- `WARN … Doppel-Announce im Netz?` / Namensänderung auf `echomuse-2` —
  zweiter Announcer im Netz.
- `WARN … Doppel-Device-ID …` — zweites Gerät mit identischer ID (ersetzt
  die Session, dokumentierte Re-Connect-Semantik).
- `Wake-Versuch abgelehnt … Grund=warmup_gate` — normal (Warm-up nach
  Modell-Reset), kein Fehler.

## 6. Quellen

- Alt-Doku `WLAN_CONNECT.md` (Wipe-Archiv auf `.123`, 2026-09-29): enthielt
  **nur** WLAN-Name + Passwort (Klartext, E105) — **keine** Schritte.
- WiFi-Wechsel-Flow (keine Initial-
  Provisionierung dokumentiert) und §3.1/§3.4 (Pairing-Mechanik, Ampel).
- `deploy/docs/INSTALL.md` §5b „Dot verbinden“ (Ampel, Fehlersuche).
- STATE.md §4/E105 (Wizard deckt kein Geräte-WiFi-Setup ab; Creds niemals
  ins Repo).

**Unverifiziert (bewusst offen):** der exakte geräteseitige Weg der
Erst-Provisionierung (Tastenfolgen/Serial-Kommandos/AP-Modus) — nichts
davon ist belegt und nichts wurde hier erfunden.
