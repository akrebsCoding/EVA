# EchoMuse Wire-Protokoll (EVA)

**Status:** P0.T6, 2026-09-26. Verbindliche Referenz für `app/protocol.py` (P1/P2) und den Fake-Echo-Dot (P9.T1).
**Primärquelle:** produktiver Referenz-Controller im Container `echomuse-controller` auf `10.0.0.10`, **`EM_CONTROLLER_VERSION=v2.22.0`**, WorkingDir/Entrypoint-Cmd `/app`, gestartet als `python -u em_start.py`.
**Gesicherter Quelltext:** `docs/reference/` (40 Python-Dateien, read-only via `docker exec … cat` kopiert; wird in P0.T8 git-ignoriert).
**Sekundärquelle (nur wo markiert):** geklontes Upstream-Repo `/tmp/echomuse` (`wilbowes/EchoMuse`) — Device-Seite (`device/internal/client/*.go`), `docs/device-controller-interface.md`. Wird ausschließlich für Felder benutzt, die der Controller-Stand v2.22.0 nicht selbst enthält (Capabilities-Liste, `ConfigMessage`-Struct, `clickType`-Konstanten).

> **Regel:** Alles ohne „Upstream"-Marker ist gegen den gesicherten Container-Quelltext verifiziert und mit `datei.py:Zeile` belegt. Wo der Plan (§2.4/§2.5) dem Quelltext widerspricht, hat der Quelltext Vorrang; Abweichungen sind in [§9 Abweichungen](#9-abweichungen-vom-plan) markiert.

---

## 1. Topologie und Verbindungsaufbau

Der **Dot ist WebSocket-Client, der Manager ist Server**. Das Gerät öffnet **drei** Ebenen (Planes) zum Manager:

| Ebene | Pfad | Inhalt | Erstes Gerätemessage |
|---|---|---|---|
| Steuerung | `/control` | JSON, text frames | `register` |
| Daten | `/data` | binär (Mic hoch, Speaker runter) | JSON `identify` |
| Shell | `/shell/{device_id}[?pty=1]` | roh-binär, beidseitig | – (Proxy, kein Handshake) |

Routing: `em_controller.py:4206` `_route()` — `/control`, `/data`, `/shell/*`; alles andere ⇒ Close.
Beide Ebenen `/control` und `/data` müssen **von derselben `device_id`** kommen: `/data` wartet bis zu 20×100 ms auf ein `/control`, das die `device_id` in `_devices` registriert hat, sonst Close (`em_controller.py:4007-4017`).
Beide Handler erwarten das erste Message **innerhalb 10 s** (`asyncio.wait_for(ws.recv(), timeout=10.0)`, `em_controller.py:3179`, `em_controller.py:3991`).

**Link-Auth:** alle drei Planes prüfen `em_linkauth.decide()` (`em_controller.py:3150`). TLS-Listener `SERVER_TLS_PORT` (Default 8770) parallel zu `SERVER_PORT` (8767) (`em_controller.py:4342-4358`).
**Keepalive auf Transportebene:** `websockets.serve(..., ping_interval=20, ping_timeout=10, max_size=10 MB)` (`em_controller.py:4345-4350`) — **das ist WebSocket-Protokoll-Ping, nicht die App-`ping`/`pong`-Nachrichten**. Beides existiert, siehe [§4](#4-keepalive).

---

## 2. `/control` — Device → Controller

JSON-Objekte, ein Text-Frame pro Nachricht. Unbekannte `type` werden ignoriert (`em_controller.py:3874-3878`, `log.debug`).

| `type` | Pflichtfelder | Optionale Felder | Bedeutung / Quellverweis |
|---|---|---|---|
| `register` | `device_id` | `ip`, `version`, `capabilities[]`, `ambient_light_status` | **Muss das erste Message sein**, sonst Close ohne Antwort (`em_controller.py:3182-3188`). `em_controller.py:3193-3196` |
| `button` | — | `clickType`, `down`, `heldMs`, `muted`, `button` | Dot-Button. Siehe [§6](#6-button-sequenz-k6) |
| `pong` | — | `id`, `mono` | Antwort auf App-`ping`. **Nur `pong` mit `id` zählt** als RTT-Sample; unsolicited Pongs werden bewusst ignoriert (`em_controller.py:3854-3871`) |
| `log` | — | `level` (default `info`), `message` | Gerätelog → Dashboard (`em_controller.py:3839`) |
| `stats` | — | Hardware-/Link-Telemetrie | ~30 s-Tick + einmal bei Connect |
| `volume_state` | — | `level` (roher tinymix-Index 0…127) | Gerät meldet echten Lautstärke-Stand (`em_controller.py:3534`) |
| `mute_state` | — | `muted` (bool) | Mute ist **device-sovereign**. Mute während laufendem Turn ⇒ Cancel + `speaker_flush` (`em_controller.py:3510-3530`) |
| `oww_wake` | — | `score`, `threshold`, `ageMs`, `capturedMono`, `level`, `peak`, `session`, `floor`, `barge` | Nur bei `owwOnDevice=on`. EVA setzt `owwOnDevice:false` ⇒ **tritt nie auf** (`em_controller.py:3789`) |
| `oww_shadow_cross` | — | Score/Threshold/Age | Shadow-Modus-Report, nur Report (`em_controller.py:3778`) |
| `ble_adverts` | — | `adverts[]` | **Der von v2.22.0 genutzte BLE-Pfad** (Control-Plane-JSON). `0x06` nur wenn der Controller `ble_adverts_data` im `ack` ankündigt — das tut er nicht, siehe [§9](#9-abweichungen-vom-plan) |
| `playback_stats` | — | `periods`, `underruns` | Gerät meldet Speaker-Unterläufe (`em_controller.py:3704`) |
| `ambient_light` | — | `lux` (int) | nur bei Capability `ambient_light` (`em_controller.py:3499`) |
| `wifi_result` | — | `ok`, `ssid`, `error?` | Antwort auf `wifi_change` (`em_controller.py:3674`) |
| `wifi_scan_result` | — | `networks[]` oder `error` | Antwort auf `wifi_scan` (`em_controller.py:3834`) |
| `listen_state` | — | `state` (`local`/`stream`/`degraded`), `reason?` | **Upstream-Protokoll, im Referenz-Controller v2.22.0 nicht implementiert** — kein Empfänger im gesicherten Quelltext |
| `listen_end` | — | `session`, `reason` | dito (Private Listening, von v2.22.0 nicht angekündigt) |
| `mic_start` / `mic_end` | – | – | **Existieren nicht.** Beide Richtungen ausgeschlossen, siehe [§5](#5-mic_start-lebenszyklus-k3) |

### 2.1 `register` (Device → Controller)

```json
{"type":"register","device_id":"G0K0XXXXXXXX","version":"v2.x.y",
 "ip":"192.168.x.y","capabilities":["mic","speaker","leds", …],
 "ambient_light_status":"…"}
```

Der Controller liest `device_id`, `ip` (Default = Peer-IP), `version`, `capabilities` (Default `[]`), `ambient_light_status` (`em_controller.py:3193-3196`, `3266`).

**Capabilites, die der Referenz-Controller auswertet** (Properties in `em_controller.py`, alle `in (self.capabilities or [])`):

| Capability | Property | Zeile | Wirkung |
|---|---|---|---|
| `led_anim` | `led_anim_capable` | 751 | Gerät animiert lokal ⇒ `led_anim` statt `leds` |
| `audio_mix` | `audio_mix_capable` | 755 | Musik auf eigenem Plane (`0x04`/`0x05`), Voice **duckt** statt pausiert |
| `button_hold` | `button_hold_capable` | 792 | `heldMs` vorhanden, HA-Event-Entity |
| `oww_shadow` | `oww_shadow_capable` | 797 | Gerät **kann** lokal scoren |
| `oww_trigger` | `oww_trigger_capable` | 812 | Gerät kann **handeln** (eigenes `owwOnDevice=on` erlaubt) |
| `aec_hw_ref` | `aec_hw_ref_capable` | 827 | AEC-Referenz aus Hardware-Loopback statt Soft-Tap |

**Upstream-Basisliste der Capabilities** (`device/internal/client/control.go:1151-1155`): `mic, speaker, leds, led_anim, buttons, oww_shadow, oww_trigger, button_hold, audio_mix, aec_hw_ref, oww_local_only, output_chain, wake_cue` (+ `ambient_light` optional). Negotiierung **immer über Capabilities, nie über Versionsstrings**.

---

## 3. `/control` — Controller → Device

| `type` | Payload | Länge | Quellverweis |
|---|---|---|---|
| `ack` | `{"type":"ack","device_id":"<id>"}` | **wörtlich 2 Felder** | `em_controller.py:3280` — **K1 bestätigt** |
| `pending` | `{"type":"pending"}` | 1 Feld | `em_controller.py:3220`, `3237` (Gerät unbekannt/nicht freigegeben ⇒ danach Close) |
| `config` | `{"type":"config", **effektive_config}` | 43 Felder + `type` = **44 Keys** | `em_controller.py:3285` |
| `config` (schlank) | `{"type":"config","listeningAnim":{…}}` | 2 Keys | `em_controller.py:3331-3334` — Vorab-Push, damit ein **geräteseitig** erkannter Wake den Ring sofort malt (0 RTT) |
| `mic_start` | `{"type":"mic_start"}` oder `{"type":"mic_start","lock_mic":true}` | 1–2 Felder | `em_controller.py:874-879` |
| `mic_stop` | `{"type":"mic_stop"}` | 1 Feld | `em_controller.py:881-882` |
| `leds` | `{"type":"leds","leds":[{"id":0,"r":0,"g":180,"b":0}, …12]}`, optional `"listening":true` | 12 × `{id,r,g,b}` | `em_controller.py:738-749`, `NUM_LEDS=12` (`em_controller.py:266`) |
| `led_anim` | `{"type":"led_anim","anim":{"pattern":…,"colors":[…],"periodMs":…,"ttlSec":…}}` | variabel | `em_controller.py:860-871`; Specs in `em_scenes.py:172-282` |
| `ping` | `{"type":"ping","id":<seq>}` | 2 Felder | `em_controller.py:3467` |
| `speaker_flush` | `{"type":"speaker_flush"}` | 1 Feld | `em_controller.py:1437`, `1655`, `3052` |
| `duck` | `{"type":"duck","on":true/false}` | 2 Felder | `em_player.py:315`, `372` |
| `volume_set` | `{"type":"volume_set","level":<0…127>}` | 2 Felder | `em_controller.py:3375`; Skala `em_volume.py:22-30` |
| `beam_lock` / `beam_unlock` | `{"type":"beam_lock"}` / `{"type":"beam_unlock"}` | 1 Feld | `em_controller.py:887-894` |
| `shell_open` | `{"type":"shell_open"}` oder `{"type":"shell_open","pty":true}` | 1–2 Felder | `em_api.py:2004`, `2623` |
| `shell_close` | `{"type":"shell_close"}` | 1 Feld | `em_api.py:2031`, `2630` |
| `wifi_scan` | `{"type":"wifi_scan"}` | 1 Feld | `em_controller.py` (gesichert, keine Zeile im Frame-Header nötig) |
| `wifi_change` / `wifi_commit` | `{"type":"wifi_change","ssid":…,"ssid_hex":…,"psk":…}` / `{"type":"wifi_commit"}` | variabel | dito |
| `music_flush` | `{"type":"music_flush"}` | 1 Feld | nur an `audio_mix`-Geräte, sonst `speaker_flush` (`em_player.py:584-586`) |
| `listen_ack` | `{"type":"listen_ack","session":<u32>}` | – | **Upstream-Protokoll, im v2.22.0-Controller nicht implementiert** — kein Empfänger im gesicherten Quelltext |

**Nicht vorhanden im Referenz-Controller:** `features` im `ack` (K1), `play_cue`, `led_anim` ohne `led_anim`-Capability, `listen_close` (Upstream, Private Listening).

### 3.1 Connect-Sequenz (verifizierte Reihenfolge)

```
1  Device → /control : register{device_id, …}
2  Controller        : {"type":"pending"}           (nur wenn Gerät unbekannt/nicht approved → dann Close)
3  Controller        : {"type":"ack","device_id":…}               em_controller.py:3280
4  Controller        : {"type":"config", **43 Felder}             em_controller.py:3282-3284
5  Controller        : {"type":"config","listeningAnim":{…}}      em_controller.py:3331-3334  (nur bei led_anim)
6  Controller        : {"type":"mic_start"}                       em_controller.py:2409 (im Wake-Listener-Start)
   … Dauerbetrieb: permanenter Mic-Stream, /data identify, App-ping
```

Schritt 2 entfällt, wenn `DEVICE_APPROVAL=auto` (`em_controller.py:3203-3217`) — dann wird das Gerät registriert **und** freigegeben, es kommt direkt `ack`.
`capabilities` werden **nach** dem `ack` ausgewertet (`device = Device(…, capabilities, ws)`, `em_controller.py:3266`).

---

## 4. Keepalive

Zwei **unabhängige** Mechanismen — beide müssen implementiert werden:

### 4.1 App-Ebene: JSON `ping`/`pong` (RTT-Messung)

| Aspekt | Wert | Quelle |
|---|---|---|
| Intervall | **5.0 s** pro Gerät | `PING_INTERVAL_SEC` `em_controller.py:254`, Loop `em_controller.py:3449-3467` |
| Payload | `{"type":"ping","id":<monoton steigender int>}` | `em_controller.py:3467` |
| Antwort | `{"type":"pong","id":<echo>,"mono":<ms>}` | `em_controller.py:3854-3871` |
| Timeout | **kein Close.** Offene Pings älter als `PING_TIMEOUT_SEC = 60.0 s` werden **verworfen** (kein RTT-Sample) | `em_controller.py:263`, `3455-3458` |
| „Busy"-Flag | wird beim **Senden** festgehalten (`ping_busy[seq] = device.is_busy()`) | `em_controller.py:3465` |
| Ausreißer | RTT ≥ `RTT_EXCURSION_MS = 200` ⇒ Log-Zeile | `em_controller.py:258`, `3864-3868` |

Der Controller **schließt die Verbindung nie wegen fehlender Pongs.** Es gibt **kein** `ping_timeout` auf App-Ebene. Ein Gerät, das nicht antwortet, wird nur in der RTT-Statistik als verloren geführt.

### 4.2 Transportebene: WebSocket-Protokoll-Ping

`websockets.serve(ping_interval=20, ping_timeout=10)` (`em_controller.py:4345-4350`) — der **Server** pingt alle 20 s und schließt nach 10 s ohne Pong-Payload (Close-Code 1011). Dies ist der Mechanismus, der einen wirklich toten Gerät-Link abräumt. Kommentar `em_controller.py:195-206` dokumentiert, warum Audio-Pacing existiert: ein blockiertes Gerät kann keinen Protokoll-Pong beantworten und fliegt genau deshalb raus.

---

## 5. `mic_start`-Lebenszyklus (K3)

**Es gibt kein eingehendes `mic_start` und kein `mic_end`.** `mic_start` ist **ausschließlich** ausgehend.

### 5.1 Dauer-Stream (permanent)

Einmalig nach `ack` + `config`, sobald der Wake-Listener startet:
```
{"type":"mic_start"}     → dauerhaft ungated Omni-Stream (ch6), AGC-frei
```
`em_controller.py:2409`. Dieser Stream **läuft durchgehend**, auch in IDLE und (bei `bargeInEnabled`) während der Wiedergabe. Es gibt **kein** `mic_stop` im Normalbetrieb.

### 5.2 Wake-Pfad — **kein** Stop/Start

Bei Wake über den permanenten Stream wird **kein** `mic_stop`/`mic_start` gesendet. Stattdessen setzt der Controller `oww_paused`; nachfolgende Frames werden in `voice_queue` statt `mic_queue` geroutet (`em_controller.py:2772-2782`, `em_controller.py:4048`, `4061`).
Begründung im Quelltext: „*The stream stays running continuously. Flipping oww_paused routes subsequent frames to voice_queue.*"

### 5.3 Button-Pfad (K6) — **die einzige** Stelle mit `lock_mic`

```
{"type":"mic_stop"}                      em_controller.py:3082
{"type":"mic_start","lock_mic":true}     em_controller.py:3084  → begrenzter, VAD-gegateter Turn-Stream,
                                                                    Beamformer lockt auf bestes Perimeter-Mikro
   … Turn läuft (endet über 0x04 / 0x05 / Caps) …
{"type":"mic_stop"}                      em_controller.py:3095
{"type":"mic_start"}                     em_controller.py:3096  → zurück auf Omni-Wake-Stream
```
Das abschließende `mic_stop` ist **zwingend**: ohne TTS (Cancel/Error/No-Speech) läuft der `lock_mic`-Stream noch, und ein nacktes `mic_start` würde daran no-open — der GATED Turn-Stream bliebe als permanenter Wake-Stream stehen (Kommentar `em_controller.py:3085-3094`).

`lock_mic` wird **auch** von HA-Session-Starts benutzt (`em_controller.py:3410`, `_start_conversation`).

### 5.4 Vor TTS: `mic_stop` als Akustik-Schutz

Bei `bargeInEnabled=false` stoppt der Controller das Mic **vor** der Wiedergabe (`em_controller.py:2136-2137`) und startet es danach neu. Bei `bargeInEnabled=true` **bleibt** es während der Wiedergabe an (AEC im Gerät macht das sicher).

### 5.5 Selbstheilung (defensive Re-Sends)

| Störung | Aktion | Quelle |
|---|---|---|
| 10 s keine Frames, Streak < 3 | `{"type":"mic_start"}` | `em_controller.py:2551-2553` |
| Streak ≥ 3 (Zombie-Stream) | `mic_stop` + `mic_start` | `em_controller.py:2570-2571` |
| Frische `/data`-Verbindung 30 s still (`FRESH_CONN_GRACE_S`) | wie oben | `em_controller.py:238`, `2545` |

**Mic-Queue-Verhalten:** `asyncio.Queue`, `put_nowait`; bei voller Queue wird der **älteste** Frame gedroppt, nie blockiert (`em_controller.py:4062-4076`). Für VAD-Sentinel gilt dasselbe, dort mit Fehler-Log (`em_controller.py:4046-4058`).

---

## 6. Button-Sequenz (K6)

### 6.1 Format

```json
{"type":"button","clickType":138,"down":false,"heldMs":0,"muted":false,
 "button":{"type":"Dot"}}
```

| Feld | Quelle | Bemerkung |
|---|---|---|
| `clickType` | `device/pkg/buttons/button.go:13-16` | **`138` Dot · `115` VolumeUp · `114` VolumeDown · `113` Mute** |
| `down` | `em_controller.py:2975-2978` | **`down:true` ⇒ `return`, sofort verworfen.** Nur `down:false` zählt |
| `heldMs` | `em_controller.py:3000` | fehlt bei Press und bei älterer Firmware ⇒ 0 ⇒ **liest als Tap** |
| `muted` | `em_controller.py:3005` | Zustand **im Event** ist autoritativ, nicht `device.muted` |
| `button.type` | `device/internal/client/control.go:1161+` | Upstream; der Referenz-Controller wertet es nicht aus |

### 6.2 Klassifikation (nur für `clickType == 138`)

`em_button.decide()` (`em_button.py:45-77`), **Reihenfolge ist bedeutungstragend**:

| # | Bedingung | Ergebnis | Aktion im Referenz-Controller |
|---|---|---|---|
| 0 | `device.timer_alarm_task` aktiv | Timer stumm | `dismiss_timer_alarm` — **vor** allem anderen, wirkt auch muted |
| 1 | `held_ms >= 750` (`BUTTON_HOLD_MS`, `em_esphome.py:249`) | `HOLD` | HA-Event `long`, **kein Turn** |
| 2 | `tap_event` (`buttonSingleTapEvent` **AND** `button_hold`) | `TAP_EVENT` | HA-Event `single` bzw. Multi-Tap-Burst |
| 3 | `muted` | `BLOCKED` | **nichts** (nur der Turn ist blockiert) |
| 4 | Turn aktiv (`voice_lock.locked()`) | `CANCEL` | `cancel_event` + `esphome.cancel_voice_turn` + **`speaker_flush`** (`em_controller.py:3047-3053`) |
| 5 | sonst | `TURN` | mic_stop → mic_start{lock_mic} → Turn → mic_stop → mic_start |

Ohne erreichbares HA-Backend (`can_serve_turn == false`) wird auch Fall 5 zu einem Stand-down: Ring an, kein Turn (`em_controller.py:3055-3072`).
`buttonMultiTapMs` (Default 0 = aus) steuert nur die Tap-Burst-Zählung (`em_controller.py:3024-3038`).

---

## 7. `/data` — binäre Frames

Erstes Byte = Frame-Typ. **Die Typcodes sind richtungs-namespaced** — `0x04`/`0x05` bedeuten in jeder Richtung etwas anderes (`device/internal/client/data.go:25-64`). Keine globale Tabelle lesen.

### 7.1 Device → Controller (Capture)

| Code | Name | Byte-Layout | Länge | Bedeutung / Quelle |
|---|---|---|---|---|
| `0x01` | Mic-PCM | `[0x01][seq_hi][seq_lo][PCM S16_LE mono 16 kHz]` | **3 + 2560 = 2563 B** | `MIC_FRAME_TYPE` `em_controller.py:276`, `MIC_HEADER_LEN = 3` `em_controller.py:290`, `CHUNK_BYTES = 1280*2 = 2560` `em_controller.py:165` (80 ms). **K2 bestätigt.** Die 2 Seq-Bytes werden vom Controller **verworfen** (`em_controller.py:4060`) |
| `0x01` | Mic-PCM (Sentinel) | `[0x01][0x00][0x00][0x04]` | **4 B** | VAD_END — Speech erkannt und beendet (`VAD_END_TYPE` `em_controller.py:277`) |
| `0x01` | Mic-PCM (Sentinel) | `[0x01][0x00][0x00][0x05]` | **4 B** | No-Speech-Timeout — **nie** Speech erkannt (`VAD_NO_SPEECH_TIMEOUT_TYPE` `em_controller.py:287`) |
| `0x06` | BLE-Adverts | `[0x06][UTF-8-JSON {"adverts":[…]}]` | variabel | **nur wenn der Controller `ble_adverts_data` im `ack` ankündigt — v2.22.0 tut das nicht ⇒ Gerät nutzt `ble_adverts` auf `/control`** (`em_controller.py:3827`, Upstream `data.go:65`) |
| `0x07` | Session-Audio | `[0x07][session u32 BE][seq u16 BE][PCM]` | variabel | Private Listening, **nur bei `listen_session` in `ack`** — von v2.22.0 nicht angekündigt ⇒ **tritt nie auf** (Upstream `data.go:68`) |

**Sentinel-Erkennung im Controller** (`em_controller.py:4041-4062`): `len(raw) == 4` **und** `raw[0] == 0x01` **und** `raw[3] ∈ {0x04, 0x05}`. Frames mit `len(raw) <= 3` oder `raw[0] != 0x01` werden **stillschweigend verworfen**.

Sentinel → String-Sentinel in der Queue (`em_controller.py:4046-4058`): `"vad_end"` / `"vad_no_speech_timeout"` (`em_esphome.py:103-104`). Der Typ **reist mit dem Queue-Item** (kein Side-Channel — B5-Fix, Kommentar `em_controller.py:278-286`).

### 7.2 Controller → Device (Playback)

| Code | Name | Byte-Layout | Länge | Bedeutung / Quelle |
|---|---|---|---|---|
| `0x02` | Speaker-PCM | `[0x02][PCM S16_LE mono 48 kHz]` | **1 + 4096 = 4097 B** | `SPEAKER_FRAME_TYPE` `em_controller.py:288`; `SPEAKER_RATE=48000`, `SPEAKER_PERIOD=2048`, `SPEAKER_BYTES=4096`, ≈42.7 ms/Frame (`em_controller.py:177-180`) |
| `0x03` | Speaker-EOS | `[0x03]` | **1 B** | Ende des Voice-Streams (`em_controller.py:289`, `971`, `1087`) |
| `0x04` | Music-PCM | `[0x04][PCM 48 kHz]` | 1 + 4096 | **nur an `audio_mix`-Geräte** (`MUSIC_FRAME_TYPE` `em_player.py:67`, Auswahl `em_player.py:70-76`) |
| `0x05` | Music-EOS | `[0x05]` | **1 B** | dito (`em_player.py:68`) |

Letzter Frame wird auf volle 4096 B **nullgepaddet** (`em_controller.py:955`, `1080`).

### 7.3 Speaker-Timing (relevant für die Implementierung)

| Aspekt | Wert | Quelle |
|---|---|---|
| Gerät puffert, bis | `SPEAKER_PRIME_SECONDS = 1.1` ≈ 1.1 s Audio (oder EOS bei kürzerer Antwort) | `em_controller.py:219` |
| Eigene Kapazität des Geräts | `audioChanDepth = 128` Perioden ≈ 5.46 s | `em_player.py:80-81` |
| Pacing (Voice) | `VOICE_LEAD_S = 4.0` s über Realtime — **darf nie darüber** | `em_controller.py:214`, Begründung `em_controller.py:181-213` |
| Pacing (Musik) | `em_player.LEAD_S = 4.0` s | `em_player.py:92` |
| Draft-Schutz | `speaker_flush` verwerft Gerätepuffer inkl. ~5.5 s `audioChanDepth` | `em_controller.py:3045-3049` |

---

## 8. `config` — 44 Keys

Push = `{"type":"config", **effektive_config}` (`em_controller.py:3285`). Effektive Config = Fleet-Config, überlagert von geräteeigenen Werten der übersteuerten Sektionen (`em_db.py:1475-1515`).
**43 Felder + `type` = 44 Keys.** Vollständige Ist-Werte: `docs/device-config-reference.json` (P0.T5).

### 8.1 Semantik (Upstream, `device/internal/config/config.go:397-470`)

**Partieller Update:** nicht-`nil`/nicht-Null-Felder werden angewandt, `null`/Null-Felder ignoriert. Deshalb sind `false`/`0` als echte Werte **Pointer-Typen** (`*bool`, `*int`, `*float64`, `*string`). Wirkung **sofort**, kein Neustart. Unbekannte Keys werden ignoriert (korrektes Degrade).

### 8.2 K4-Pflichtfelder für EVA

| Feld | Ist-Wert | Pflicht |
|---|---|---|
| `owwOnDevice` | `"off"` | **MUSS `"off"` bleiben** — EVA scort manager-seitig (E5) |
| `bleProxyEnabled` | `false` | **MUSS `false`** |
| `vadThreshold` / `vadSpeechMs` / `vadSilenceMs` | `0.001` / `32` / `900` | **unverändert** — die geräteseitige VAD bestimmt die Turn-Grenzen (K5) |
| `startupVolume` | `85` | unverändert (Ist-Zustand des Geräts) |
| `micGainDb` / `adcDigitalGain` / `adcMicpga` | `24` / `88` / `40` | unverändert (Audioqualität) |
| `aecEnabled` / `aecDelayMs` / `aecTailMs` / `aecRefSource` | `true` / `0` / `300` / `"auto"` | unverändert |
| `beamformingEnabled` / `beamAngle` | `true` / `-1` | unverändert (`-1` = auto) |
| `ledScene` / `ledListenColor` / `ledThinkColor` | `"standard"` / `"#00b400"` / `"#00c800"` | unverändert (diese 3 sind **device-ignoriert**, s. 8.4) |
| `duckDb` | `-18.0` | unverändert |
| `agcEnabled` | `true` | unverändert |

### 8.3 Vollständige Feldliste (43) mit Consumer

`W` = **W**irkung im Gerät · `C` = nur **C**ontroller-seitig · `C+W` = beides

| Feld | Ist | Consumer | Anmerkung |
|---|---|---|---|
| `owwOnDevice` | `"off"` | C+W | `"off"`/`"shadow"`/`"on"`; `on` nur bei Capability `oww_trigger` (`em_controller.py:3301-3304`) |
| `owwThreshold` | `0.9` (Ist) | C+W | Controller-Score-Schwelle (`em_controller.py:3286`, `2654`) |
| `owwModel` | `hey_jarvis_v0.1` | C+W | an `OWWModel(wakeword_models=[…])` |
| `vadThreshold` | `0.001` | W | normalisierter RMS **pre-Gain** |
| `vadSpeechMs` | `32` | W | |
| `vadSilenceMs` | `900` | W | bestimmt `0x04`-Verspätung |
| `adcDigitalGain` | `88` | W | tinymix-Index |
| `adcMicpga` | `40` | W | tinymix-Index |
| `micGainDb` | `24` | W | Gerät klemmt auf `[0,42]` |
| `startupVolume` | `85` | W | **STATE-KEY**: kommt immer vom Gerät, nie von der Fleet (`em_db.py:1484-1487`) |
| `agcEnabled` | `true` | W | **kein AGC auf dem Wake-Stream** (Firmware v2.7.0+, Kommentar `em_controller.py:2112-2115`) |
| `aecEnabled` | `true` | W | coupled mit `bargeInEnabled` |
| `aecDelayMs` | `0` | W | Gerät klemmt `[0,1000]` |
| `aecTailMs` | `300` | W | Gerät klemmt `[50,500]` |
| `aecRefSource` | `"auto"` | W | `auto`/`hw`/`sw` |
| `beamformingEnabled` | `true` | W | |
| `beamAngle` | `-1` | W | `-1` = auto |
| `bargeInEnabled` | `true` | C | Opt-in „AEC ist vertrauenswürdig" |
| `bargeInThreshold` | `0.6` (Ist) | C | |
| `duckDb` | `-18.0` | W | |
| `bleProxyEnabled` | `false` | W | |
| `ledScene` / `ledListenColor` / `ledThinkColor` | s. 8.2 | **C (ignoriert)** | s. 8.4 |
| `meterAttack` / `meterDecay` / `meterFloor` / `meterGamma` / `meterRef` / `meterCurve` | `0.6`/`0.3`/`0.06`/`2.2`/`0.22`/`0.7` | **C (ignoriert)** | s. 8.4 |
| `eqBands` / `eqLoudness` | `[0.0]×8` / `false` | **C** | EQ läuft **im Controller**, `em_eq.py` (`em_controller.py:1324`) |
| `bassGuardEnabled` / `bassGuardDb` | `true` / `-30.0` | **C** | `em_mbc.py` |
| `limiterEnabled` / `limiterThreshold` / `limiterRelease` | `true` / `-1.0` / `150` | **C** | `em_limiter.py` |
| `owwSpeexNs` | `false` | **C (ignoriert)** | Bool → `enable_speex_noise_suppression` (`em_controller.py:2401`) |
| `nsAsr` | `false` | **C (ignoriert)** | DTLN-NS, **aus** (E9) |
| `saveUtterances` | `false` | **C (ignoriert)** | |
| `wakeArbitrationMs` | `700` | **C (ignoriert)** | nur bei >1 Gerät relevant |
| `buttonSingleTapEvent` | `false` | **C (ignoriert)** | |
| `buttonMultiTapMs` | `0` | **C (ignoriert)** | |

### 8.4 Vom Gerät ignorierte Config-Felder (15)

Die Felder werden gepusht, haben aber **keine Entsprechung** in `ConfigMessage` (`device/internal/config/config.go:397-470`) und werden **stillschweigend verworfen**:

```
buttonMultiTapMs        ledScene           meterAttack
buttonSingleTapEvent    ledListenColor     meterCurve
ledThinkColor           meterDecay         meterFloor
nsAsr                   meterGamma         meterRef
owwSpeexNs              saveUtterances     wakeArbitrationMs
```

**Nur im Gerät, nicht in den 43 Defaults** (nicht pushen): `consolePassword`, `consoleTimeoutMin`, `hasBeamforming`, `wakeSound`, `wakeSoundLevel`, `listeningAnim`.

---

## 9. Abweichungen vom Plan

Wo der Quelltext dem Plan widerspricht, **gilt der Quelltext**.

| # | Plan (§) sagt | Quelltext zeigt | Bewertung |
|---|---|---|---|
| 1 | §2.4: GerätseCapabilities = 6 ausgewertete; **§2.4 nennt `owwThreshold` als device-ignoriert** | `owwThreshold` **ist** ein Feld der Device-`ConfigMessage` (`config.go:413`) — das Gerät wertet es für eigenes Scoring aus. Die echte Ignorier-Liste ist **15 Felder** (s. 8.4), nicht 6. | **Abweichung.** Die 6 vom Plan genannten (`owwThreshold`, `owwSpeexNs`, `nsAsr`, `saveUtterances`, `wakeArbitrationMs`, `buttonMultiTapMs`) sind alle tatsächlich controller-seitig; `owwThreshold` ist zusätzlich **auch** device-seitig. Da EVA `owwOnDevice:false` setzt, ist die Doppelrolle ohne Wirkung — aber die Liste „device-ignoriert" im Plan ist zu kurz |
| 2 | §2.4/`STATE.md`: `0x06` BLE und `0x07` Session-Audio als reguläre Frames | v2.22.0-`ack` enthält **kein** `features` ⇒ das Gerät sendet `0x06`/`0x07` **nicht**; BLE läuft über `ble_adverts` auf `/control` (`em_controller.py:3827`) | **Bestätigt, mit Nuance.** EVA implementiert `ble_adverts`-JSON, **nicht** `0x06`/`0x07` |
| 3 | §2.4 / STATE.md: Controller→Device enthält `listen_ack`, `listen_close` | Im gesamten gesicherten Quelltext kommt **keines** vor — das sind Upstream-Private-Listening-Nachrichten, deren Voraussetzung (`listen_session`-Ankündigung) v2.22.0 nicht erfüllt | **Einschränkung.** Beide sind für EVA **nicht implementierbar/notwendig** |
| 4 | §2.4: `bargeInThreshold` 0.6, `wakeArbitrationMs` 300, `owwThreshold` 0.9 | `DEFAULT_DEVICE_CONFIG`: `bargeInThreshold=0.25`, `wakeArbitrationMs=700`, `owwThreshold=0.5` (`em_db.py:40-…`). Die Werte 0.6/300/0.9 sind der **Geräte-Zustand aus P0.T5**, nicht die Defaults | **Kein Widerspruch** — zwei verschiedene Quellen. Für EVA gilt der **Ist-Wert** (P0.T5) |
| 5 | §2.5 K5: Hard-Cap 15 s, No-Speech 8 s | Controller-Netz: `asyncio.wait_for(…, timeout=20.0)` (`em_esphome.py:1301-1303`), `NO_SPEECH_TIMEOUT=5.0`, `FIRST_AUDIO_GRACE=5.0` (`em_turnclock.py:26-34`) | **Plan hat Vorrang** (bewusste EVA-Härtung: 15 s / 8 s). Die Referenzwerte sind als Ist dokumentiert |
| 6 | §2.4: Controller-Ping | Es gibt **zwei** Keepalives: App-JSON `ping`/`pong` (5 s, **kein** Close) und WS-Protokoll-Ping (20 s/10 s, **Close 1011**) | **Ergänzung.** Beide sind nötig |
| 7 | §2.4: `led_anim`/`0x08` | `led_anim` ist eine **Control-Plane-JSON**-Nachricht `{"type":"led_anim","anim":{…}}` (`em_controller.py:860-871`) — **kein** `0x08`-Binärframe. Im v2.22.0-Quelltext existiert kein `0x08` | **K7-Korrektur.** `0x08` ist zu verwerfen; `led_anim` geht über `/control` |

---

## 10. Wake-Word / OWW-Parameter

| Parameter | Wert | Quelle |
|---|---|---|
| `CHUNK_BYTES` | `1280*2 = 2560` B = **80 ms** @ 16 kHz S16_LE mono | `em_controller.py:165` |
| `FEATURE_WINDOW` | **16** Chunks = **1.28 s** Kontext | `em_oww_warmup.py` (`FEATURE_WINDOW = 16`) |
| Warm-up-Gate | `WarmupGate(window=16)`; `feed()` liefert `False` für die ersten **16** Chunks nach jedem `model.reset()` | `em_oww_warmup.py` `WarmupGate` |
| Gate-Anker | `warmup.reset()` **immer** neben `model.reset()`; `feed()` **genau einmal** je gescorten Chunk — auch bei `device.speaking`-Skip | `em_controller.py:2628` (`trusted = warmup.feed()`), Reset `em_controller.py:2783-2784` |
| Preroll | `VOICE_PREROLL_DISCARD = 3` Chunks = **240 ms** | `em_esphome.py:189` |
| Preroll-Geltung | **nur** Wake-Pfad. Button/Continuation/Barge-in: `preroll_discard = 0` | `em_controller.py:2181`, `2227`, `2264` |
| Modell-Load | `OWWModel(wakeword_models=[name], enable_speex_noise_suppression=<bool>)` | `em_controller.py:2396-2403` |
| NS | **ein Konstruktor-Bool**, sonst keine Logik. `speexdsp-ns==0.1.2` | `em_controller.py:2401`, PLAN §2.4 |
| Prediction-Key | `em_oww_models.prediction_key(name)` — Dateiendung `.onnx` ⇒ **Filename-Stem** | `em_oww_models.py:34-41` |
| Schwellwert | `device.oww_threshold` = `config["owwThreshold"]`, Default `OWW_THRESHOLD=0.5` (env) | `em_controller.py:270`, `3286` |
| Barge-Schwelle | `bargeInThreshold`, effektiv `min(oww, barge)` **während** Musik/Timer-Alarm **und** `bargeInEnabled` | `em_controller.py:2653-2664` |
| Wake-Arbitration | `wakeArbitrationMs` (Default 700), nur bei >1 Gerät | `em_controller.py:3289` |
| **ASR-NS** | `nsAsr` = **aus**. DTLN wirkt **nur** auf Turn-Audio, **nie** auf den Wake-Stream (`em_ns.py:3-8`, E9) | `em_ns.py` |
| Rauschboden | asymmetrische EWMA: α↓ 0.3 / α↑ 0.008 (≈10 s) — **nur Messung**, Audio wird nie verändert | `em_controller.py:2613-2619` |
| Score-Skip | bei `device.speaking` (ohne Timer-Alarm) werden Chunks **nicht** gescort | `em_controller.py:2604` |
| Modell-Reset | bei JEDEM Wake: `model.reset(); warmup.reset(); buf.clear(); cancel_event.clear()` | `em_controller.py:2783-2785` |
| Barge-Watcher | `em_barge.py`, scort durchgehend, setzt `device.barge_detected` ⇒ Cancel + `speaker_flush` | `em_controller.py:2107-2131` |

### 10.1 Turn-Ende und Caps (K5)

| Mechanismus | Wert | Quelle |
|---|---|---|
| **`0x04` VAD_END** | Gerät schließt das VAD-Gate nach Speech | `em_controller.py:277` |
| **`0x05` No-Speech** | Gerät hat **nie** Speech erkannt ⇒ stillschweigen | `em_controller.py:287` |
| Controller `NO_SPEECH_TIMEOUT` | **5.0 s** Stille, gemessen ab **erstem echten Frame** | `em_turnclock.py:26` |
| Controller `FIRST_AUDIO_GRACE` | **5.0 s** auf ersten Frame, gemessen ab Turn-Start | `em_turnclock.py:34` |
| Controller Hard-Cap | **20.0 s** auf die gesamte Streaming-Phase | `em_esphome.py:1301-1303` |
| **EVA (K5)** | **15 s** Hard-Cap, **8 s** No-Speech | PLAN §2.5 — Vorrang |
| Turn-Ende-Sentinel bei Preroll | Teilframes werden beim Sentinel geflusht, damit OWW nie über eine Stream-Grenze scort | `em_controller.py:2577-2581` |

---

## 11. Referenz und Reproduktion

**Gesicherter Quelltext:** `docs/reference/` — 40 Dateien, read-only kopiert am 2026-09-26:
```
ssh user@<ha-host> 'for f in $(docker exec echomuse-controller ls /app | grep "\.py$"); do
    docker exec echomuse-controller cat /app/$f; done'   # → docs/reference/
```
Kern-Dateien: `em_controller.py` (224 KB, WebSocket-Server, OWW-Loop, Button, Keepalive), `em_esphome.py` (160 KB, Turn-State-Machine, Preroll, VAD-Stream), `em_db.py` (118 KB, 43 Config-Defaults), `em_api.py` (207 KB, HTTP-API/Dashboard), `em_oww_warmup.py` (Warm-up-Gate), `em_oww_models.py` (Modell-Discovery), `em_ns.py` (DTLN-NS), `em_shadow.py` (Shadow-Modus), `em_scenes.py` (LED-Scenes/Anim-Specs), `em_turnclock.py` (No-Speech-Logik), `em_button.py` (Button-Politik), `em_player.py` (Musik-Plane), `em_volume.py` (Lautstärken-Skala), `em_wsclose.py`, `version.py`.

**Upstream (Device-Seite, sekundär):** `/tmp/echomuse` — `device/internal/client/control.go`, `device/internal/client/data.go`, `device/internal/config/config.go`, `device/pkg/buttons/button.go`, `docs/device-controller-interface.md`.

**Was EVA nicht braucht** (bewusst ausgelassen): ESPHome-Satellite-Pfad (`:16001`, HA-native), `em_scenes`-Presets jenseits Listening/Spin/Outcome, `em_timers` (Timer-Alarm), `em_scenes`-LED-Paletten, `em_ble_proxy`-Weiterleitung an HA, OTA-Firmware-Pfad, Shell-Dashboard-Proxy, `em_api`/Dashboard/SPA.
