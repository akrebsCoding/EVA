"""Testfakes der EVA-Suite (P9, `PLAN.md` §7 → P9.T1–T3, §7.1 L2/L3).

Enthält ausschließlich **Testdouble**, die gegen die **echten** Frame-Konstanten
aus `app/protocol.py` arbeiten — keine duplizierten Magic Numbers.

* `fake_echomuse` (P9.T1) — generalisierter Fake-Echo-Dot (echter WS-Client)
* `fake_wyoming` (P9.T2, folgt) — deterministische Wyoming-STT/TTS-TCP-Server
* `fake_services` (P9.T3, folgt) — In-Process-HA-/LLM-Stubs
"""
