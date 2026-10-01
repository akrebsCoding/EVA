# `tools/` — Hilfsskripte rund um Test und Betrieb

Angelegt in **P1.T4** (`PLAN.md` §7 → P1.T4), zunächst **bewusst ohne Skript**:
die Werkzeuge, für die es gedacht ist, entstehen in ihren späteren Schritten
**funktional**, nicht hier als Platzhalter.

**Stand P3.T2 (2026-09-26):** `gen_fixture.py` ist **umgesetzt** (s. u.). Die
übrigen Werkzeuge der Tabelle sind weiterhin **geplant** und entstehen erst in
ihren Schritten (`run_tests.sh`/`make_report.py` in P9.T0,
`make_wake_fixtures.py` in P9.T6) — **kein** Platzhalter.

**Stand P9.T0 (2026-09-26):** `run_tests.sh` und `make_report.py` sind
**umgesetzt** (s. u.).  Offen bleibt nur `make_wake_fixtures.py` (P9.T6).

## Geplant (Reihenfolge = Plan-Reihenfolge)

| Datei | Schritt | Zweck |
|---|---|---|
| `gen_fixture.py` | P3.T2 | `tests/fixtures/sample_text.txt` per **liveem** Piper auf `.123:10200` zu WAV synthetisieren, via SoXR auf 16 kHz mono S16_LE normalisieren, Header + Rate verifizieren |
| `run_tests.sh` | P9.T0 | Gruppen `unit \| component \| integration \| wake \| all \| live`; **Exit-Code-Gate: rot ⇒ ≠ 0** |
| `make_report.py` | P9.T0 | JUnit-XML aus `reports/junit/` → `reports/TEST-REPORT.md` mit Layer-Matrix |
| `make_wake_fixtures.py` | P9.T6 | idempotente Positiv-/Negativ-Fixtures für den Wake-Smoke (Piper-Synthese, pink noise + AM, fester Seed) |

## Umgesetzt

### `gen_fixture.py` (P3.T2, 2026-09-26)

Erzeugt `tests/fixtures/sample_16k.wav` aus `tests/fixtures/sample_text.txt`
(„Schalte das Licht im Wohnzimmer ein"): Synthese über **`app.tts_client.TtsClient`**
gegen das **live laufende Piper auf `.123:10200`**, Normalisierung auf
**16 kHz/S16_LE/mono** über **`app.audio_bridge.SpeakerResampler`** (`out_rate=16000`),
danach **programmatische Header-/Raten-Prüfung** (roher 44-Byte-RIFF-Header
**und** stdlib `wave`). Eingangsrate kommt aus dem `audio-start` (22050 Hz für
`de_DE-thorsten-high`), **nicht** hart verdrahtet. Idempotent, **ohne Argumente**
lauffähig (Zielpfade aus dem Repo abgeleitet); `--host`/`--port` optional.
Exit-Code `0` = Fixture valide, `1` = Header-/Raten-Prüfung fehlgeschlagen.

```
/tmp/eva-venv/bin/python tools/gen_fixture.py
/tmp/eva-venv/bin/python tools/gen_fixture.py --host 10.0.0.10 --port 10200
```

Kein Netzzugriff außer der Piper-Verbindung, kein `docker`-Eingriff auf `.123`.

### `run_tests.sh` (P9.T0, 2026-09-26)

Wählt einen Testlayer über den passenden `pytest`-Marker und **gatet den
Exit-Code**: rote Tests ⇒ Exit ≠ 0.

```
tools/run_tests.sh [unit|component|integration|wake|all|live] [pytest-Args…]
```

| Gruppe | Marker-Ausdruck | Layer |
|---|---|---|
| `unit` | `-m unit` | L0 |
| `component` | `-m component` | L1 |
| `integration` | `-m integration` | L2 |
| `wake` | `-m wake` | L4 |
| `live` | `-m live` | L5 (Default skipped) |
| `all` (Default) | `-m "unit or component or integration or wake"` | **L0+L1+L2+L4** |

Exit-Code `0` = alle selektierten Tests grün (oder die Gruppe ist noch leer,
`pytest`-rc=5 — dann mit klarer Meldung, nicht als Rot gewertet); `1` = rot;
`2` = unbekannte Gruppe.  Gibt `rc` und die **Dauer in Millisekunden** aus.
Python: `$EVA_PYTHON`, sonst `/tmp/eva-venv/bin/python`, sonst `python3`.

### `make_report.py` (P9.T0, 2026-09-26)

Baut aus dem JUnit-XML (Default: jüngste Datei unter `reports/junit/`) den
Report `reports/TEST-REPORT.md` mit der **Layer-Matrix** (PLAN §7.1).  Den
Layer jedes Tests liefert das von `tests/conftest.py::pytest_collection_modifyitems`
gesetzte JUnit-`<property name="layer" …>`.

```
tools/make_report.py [--xml reports/junit/junit-…xml] [--out reports/TEST-REPORT.md]
```

Exit-Code `0` = Report grün, `1` = Report rot (failures/errors), `2` = kein
parsebares XML.  `reports/` ist git-ignoriert (`.gitignore:34`).

## Warum dieses Verzeichnis existiert

`PLAN.md` §7/P1.T4 verlangt es, und die Testinfrastruktur ist an dieser Stelle
sonst unvollständig: `pytest.ini` und `tests/conftest.py` liefern die
Bausteine, aber der **planbare, dokumentierte Aufruf** (Layer-Auswahl über
`pytest -m …`, JUnit-Ausgabe nach `reports/junit/`, Report-Erzeugung) braucht
einen festen Ort, der nicht `app/` ist — `app/` bleibt der Laufzeitcode des
Managers und kommt ins Docker-Image.

## Regeln für spätere Schritte

* **Kein Platzhalter.** Kein Skript, das eine Zeile `pass` enthält oder
  „coming soon“ ankündigt; ein halbes Werkzeug ist schlimmer als keines.
* **Lesbar von außen.** Ein Skript wird von einem Subagenten in einem
  einmaligen Lauf aufgerufen, nicht von einem Menschen in einer Shell-Session —
  Ausgaben deshalb mit Zahlen statt mit „OK“.
* **Kein Netzzugang im L0-Bereich.** Was hier wohnt, greift bewusst über die
  Wire zu (Piper `.123`, Fake-Dot gegen `.22`) und gehört damit zu L1–L5, nie
  zu den `unit`-Tests.
* **`reports/` ist git-ignoriert** (`.gitignore:34`). Ein späterer
  `reports/TEST-REPORT.md` (P9.T0/P9.T9) ist damit **nicht** Teil des Repos;
  falls er versioniert werden soll, braucht es `git add -f` oder eine
  Negation in `.gitignore` — das ist eine **P9-Entscheidung**, keine
  Eigenmächtigkeit von P1.T4.
