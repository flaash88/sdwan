#!/usr/bin/env bash
# Audit – Mandanten/RBAC-Matrix, Szenarien C1–C3 und Fund-Reproduktionen mit Protokoll-Ausgabe.
# Aufruf: tools/audit/run_scenarios.sh [ausgabeverzeichnis]   (PY=… für den Python-Interpreter)
set -uo pipefail
cd "$(dirname "$0")/../../backend" || exit 1
PY="${PY:-python}"
OUT="${1:-/tmp/audit}"
mkdir -p "$OUT"
AUDIT_MATRIX_OUT="$OUT/matrix.csv" AUDIT_SCENARIO_LOG="$OUT/scenarios.log" \
  "$PY" -m pytest -q -p no:logging -rxX tests/audit | tee "$OUT/pytest.txt"
echo "Ausführung erwarteter Fehler (AUDIT-IDs) mit --runxfail:"
"$PY" -m pytest -q -p no:logging --runxfail tests/audit/test_audit_findings.py 2>&1 | grep -E "^FAILED|passed|failed" | tee "$OUT/findings.txt"
