"""`tests`-Paketmarker für den L1-Component-Layer (P9.T1).

Wie `tests/__init__.py` aus P1.T4: als Paket, damit die Module als
`tests.component.test_fake_echomuse` importiert werden und `app.*` über die
Projektwurzel (`pytest.ini: pythonpath = .`) auflösbar bleibt.
"""
