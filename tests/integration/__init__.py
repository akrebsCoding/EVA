"""`tests`-Paketmarker für den L2-Integration-Layer (P9.T4).

Wie `tests/component/__init__.py`: als Paket, damit `tests.integration.
test_full_turn` importierbar ist und `app.*` über die Projektwurzel
(`pytest.ini: pythonpath = .`) auflösbar bleibt.
"""
