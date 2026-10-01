"""`tests` ist ein Paket — angelegt in P1.T4 (`PLAN.md` §7 → P1.T4).

Warum: ohne `__init__.py` legt pytest (`importmode=prepend`) das Verzeichnis
`tests/` selbst auf `sys.path` und importiert die Testmodule als **Top-Level**
(`test_logger`).  Mit `__init__.py` wandert der `sys.path`-Eintrag eine Ebene
nach oben auf die **Projektwurzel**, und die Module heißen `tests.test_logger`.
Das ist hier die richtige Form, weil die Tests `app.*` importieren — dafür muss
die Projektwurzel im Suchpfad liegen (`pytest.ini: pythonpath = .`).

Ohne diese Datei hätte ein Modul `tests/logger.py` (P1.T1) den Import von
`app/logger.py` verdeckt, sobald der Namensraum `app` nicht mehr der erste
Treffer ist — dieselbe Falle, nur umgekehrt.

Bekannte Grenze: `tests` ist ein sehr generischer Paketname.  Im Projekt ist
das unkritisch (es gibt kein installiertes Paket namens `tests` — geprüft in
`/tmp/eva-venv`), sollte P9 aber im Blick behalten, falls später eine
externe Distribution `tests` mitbringt.
"""
