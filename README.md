# EVA — Externer Voice Agent

Self-hosted Sprachassistent für Home Assistant auf gerooteten Echo Dots
(EchoMuse-Firmware). EVA ist ein eigenständiger Python-Service (FastAPI +
uvicorn), der den Dot per WebSocket bedient und Sprach-Turns über eine
Wyoming-Pipeline verarbeitet:

```
Echo Dot (EchoMuse) ──WebSocket──▶ EVA-Manager
                                     │  openWakeWord (hey_jarvis)
                                     ▼
              Whisper (STT) → LLM-Routing → Home-Assistant-API → Piper (TTS)
```

Das Wake-Word erkennt der Manager selbst; der Dot streamt permanent,
das Turn-Ende entscheidet das Gerät (VAD device-seitig). Das LLM-Routing
steuert kurze Befehle als **Intents** direkt an Home Assistant (schnell,
deterministisch) und alles andere an ein LLM — beides über eine
Konfidenz-Schwelle (`ROUTER_CONFIDENCE_GATE`).

## Features

- **Ein-Befehls-Deploy**: `install.sh` + interaktiver Onboarding-Wizard
  (Single-Host oder verteilt auf zwei Maschinen)
- **Dashboard** mit Config-GUI unter `http://<host>:8767/dashboard`
  (Auth über `DASHBOARD_API_TOKEN`)
- **Hybrides Routing**: Intent-Erkennung für Haussteuerung, LLM für
  Freitext — inkl. Entity-Allowlist und hart blockierten sensiblen
  Domains (`lock`, `alarm_control_panel`, `vacuum`)
- **Barge-in**: Einsprechen während der Antwort wird erkannt und
  umgesetzt
- Komplett **Docker-basiert** (Manager + Wyoming-Whisper + Wyoming-Piper)

## Voraussetzungen

| Voraussetzung | Anmerkung |
|---|---|
| Linux (Debian/Ubuntu getestet) | Root oder `sudo` nötig |
| `git` | zum Klonen |
| Internet beim ersten Start | Modell-Download ~574 MB (Whisper, Piper) + Container-Images |
| RAM | Single-Host: ~1,5 GB frei empfohlen (Whisper `small`+int8 ≈ 700 MB idle); bei engem Host Topologie „verteilt“ wählen |
| Freie Ports | `8767` (Manager), `10300` (Whisper), `10200` (Piper) |
| Echo Dot | gerootet mit EchoMuse-Firmware (siehe `docs/ECOMUSE_PROTOCOL.md`) |

## Quickstart

```bash
git clone https://github.com/akrebsCoding/EVA.git eva-src
cd eva-src
sudo ./deploy/install.sh
```

Der Installer prüft/installiert Docker und startet den **Onboarding-Wizard**,
der dich durch Topologie-Wahl, Secrets (Home-Assistant-Token, LLM-API-Key)
und Geräte-Setup führt. Secrets landen nur lokal in der `.env`
(`chmod 600`) — nie im Git.

Details, Non-Interactive-Modus, verteilte Topologie und Fehlersuche:
**[deploy/docs/INSTALL.md](deploy/docs/INSTALL.md)**.

## Nach dem Deploy

- **Verifizieren**: `deploy/docs/INSTALL.md` §5
- **Dot verbinden**: `deploy/docs/INSTALL.md` §5b
- **Dashboard**: `http://<host>:8767/dashboard` — inkl. Config-GUI für
  die editierbaren `.env`-Settings (§8)
- **Konfiguration**: alle Variablen mit Kommentaren in
  [`deploy/env.template`](deploy/env.template) bzw. [`.env.example`](.env.example)

## Dokumentation

| Dokument | Inhalt |
|---|---|
| [`deploy/docs/INSTALL.md`](deploy/docs/INSTALL.md) | Deploy, Wizard, Topologien, Betrieb, Rollback |
| [`deploy/docs/HA_CAPABILITIES.md`](deploy/docs/HA_CAPABILITIES.md) | Was EVA in Home Assistant steuern kann (Domains, Parameter) |
| [`deploy/docs/WLAN_CONNECT.md`](deploy/docs/WLAN_CONNECT.md) | Echo Dot ins WLAN bringen / WLAN-Wechsel |
| [`docs/ECOMUSE_PROTOCOL.md`](docs/ECOMUSE_PROTOCOL.md) | EchoMuse-Geräteprotokoll (WebSocket-Frames, Sequenzen) |

## Entwicklung & Tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                # L0-L4 (unit, component, integration, wake)
```

Die Testsuite ist in Layer unterteilt (`unit` / `component` /
`integration` / `wake` / `live` / `slow`, siehe `pytest.ini`); Live-Tests
gegen ein echtes Deployment sind per Voreinstellung geskippt. Praktische
Helfer: `tools/run_tests.sh`, `tools/make_report.py`.

## Lizenz

[MIT](LICENSE)
