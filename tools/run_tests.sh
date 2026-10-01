#!/usr/bin/env bash
# tools/run_tests.sh — Layer-Gruppen + Exit-Code-Gate (P9.T0, PLAN.md:538, §7.1).
#
# Gruppen (verbindliche Layer-Matrix PLAN.md §7.1):
#   unit         L0  reine Logik, kein Netz, kein Gerät
#   component    L1  eine Komponente mit Mock/Stub
#   integration  L2  Fake-Dot/Fake-Wyoming gegen den Manager-Subprozess
#   wake         L4  Wake-Word-Smoke (Modell lädt, Score relativ)
#   live         L5  Live-HIL gegen das echte Deployment (.123), Default skipped
#   all          L0+L1+L2+L4  (die automatisierbaren Layer; das ist das
#                             Abnahmekriterium der Phase P9)
#
# Exit-Code-Gate: **rot ⇒ Exit ≠ 0.**  `pytest` liefert rc=5, wenn für eine
# Gruppe (noch) kein Test gesammelt wurde — das ist **kein** Rot, sondern eine
# leere, noch nicht implementierte Schicht (P9.T1–T6 folgen) und wird als
# solches mit rc=0 und klarer Meldung quittiert.
#
# Aufruf:  tools/run_tests.sh [Gruppe] [weitere pytest-Argumente …]
# Beispiel: tools/run_tests.sh all
#           tools/run_tests.sh unit -k protocol
#
# Python: $EVA_PYTHON, sonst /tmp/eva-venv/bin/python, sonst python3.

set -u -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if [[ -n "${EVA_PYTHON:-}" ]]; then
    PYTHON="$EVA_PYTHON"
elif [[ -x /tmp/eva-venv/bin/python ]]; then
    PYTHON="/tmp/eva-venv/bin/python"
else
    PYTHON="python3"
fi

GROUP="${1:-all}"
if (( $# > 0 )); then shift; fi

case "$GROUP" in
    all)         EXPR="unit or component or integration or wake"; DESC="L0+L1+L2+L4" ;;
    unit)        EXPR="unit";         DESC="L0 Unit" ;;
    component)   EXPR="component";    DESC="L1 Component" ;;
    integration) EXPR="integration";  DESC="L2 Integration" ;;
    wake)        EXPR="wake";         DESC="L4 Wake-Smoke" ;;
    live)        EXPR="live";         DESC="L5 Live-HIL" ;;
    *)
        echo "Unbekannte Gruppe: '$GROUP'" >&2
        echo "Nutzung: $0 [unit|component|integration|wake|all|live] [pytest-Args…]" >&2
        exit 2
        ;;
esac

echo "== run_tests.sh · Gruppe '${GROUP}' (${DESC}) · Marker: ${EXPR} =="
echo "   Python: ${PYTHON}"

START_NS="$(date +%s%N)"
"$PYTHON" -m pytest -m "$EXPR" "$@"
RC=$?
END_NS="$(date +%s%N)"
ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))

if (( RC == 0 )); then
    echo "== Gruppe '${GROUP}': rc=${RC} · Dauer=${ELAPSED_MS}ms · GRÜN =="
    exit 0
fi

if (( RC == 5 )); then
    echo "== Gruppe '${GROUP}': rc=${RC} · Dauer=${ELAPSED_MS}ms · LEER (kein Test gesammelt," \
         "Layer noch nicht implementiert) – NICHT als Rot gewertet =="
    exit 0
fi

echo "== Gruppe '${GROUP}': rc=${RC} · Dauer=${ELAPSED_MS}ms · ROT ⇒ Gate: Exit≠0 =="
exit 1
