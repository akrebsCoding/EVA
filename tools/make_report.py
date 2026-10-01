#!/usr/bin/env python3
"""JUnit-XML → ``reports/TEST-REPORT.md`` mit Layer-Matrix (P9.T0, PLAN.md:538).

Liest einen von ``pytest`` erzeugten JUnit-XML-Bericht (Default: die jüngste
Datei unter ``reports/junit/``) und baut daraus einen Markdown-Report mit der
verbindlichen Layer-Matrix aus ``PLAN.md`` §7.1 (L0/L1/L2/L4/L5).

Der Layer jedes Tests steht als JUnit-`<property name="layer" value="…"/>` im
XML — gesetzt von ``tests/conftest.py::pytest_collection_modifyitems`` (JUnit
kennt Marker sonst nicht).  Fehlt die Eigenschaft, wird der Layer aus dem
Dateinamen des Tests abgeleitet; gelingt auch das nicht, zählt der Test unter
`unknown` (sichtbar, nicht stillschweigend).

Exit-Code: ``0`` = Report geschrieben und **grün** (0 failure, 0 error),
``1`` = Report geschrieben, aber **rot**, ``2`` = kein/ungültiges XML.

Aufruf:
    tools/make_report.py                      # jüngstes reports/junit/*.xml
    tools/make_report.py --xml reports/junit/junit-…xml
    tools/make_report.py --out reports/TEST-REPORT.md
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JUNIT_DIR = PROJECT_ROOT / "reports" / "junit"
DEFAULT_OUT = PROJECT_ROOT / "reports" / "TEST-REPORT.md"

#: Reihenfolge und Sprechweise der Layer (PLAN.md §7.1).
LAYERS: tuple[tuple[str, str], ...] = (
    ("unit", "L0 Unit"),
    ("component", "L1 Component"),
    ("integration", "L2 Integration"),
    ("wake", "L4 Wake-Smoke"),
    ("live", "L5 Live-HIL"),
    ("unknown", "— (kein Layer-Marker)"),
)

#: Dateiname-Präfix → Layer (Fallback, wenn die JUnit-Eigenschaft fehlt).
FILE_LAYER_HINTS: tuple[tuple[str, str], ...] = (
    ("test_manager_proc", "integration"),
    ("test_full_turn", "integration"),
    ("test_robustness", "integration"),
    ("test_fake_echomuse", "component"),
    ("test_wake_smoke", "wake"),
    ("test_ha_client", "component"),
    ("test_llm_client", "component"),
    ("test_stt_tts", "unit"),
    ("test_router", "unit"),
)


@dataclass
class LayerStats:
    """Zählwerk je Layer."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    seconds: float = 0.0
    cases: list[str] = field(default_factory=list)


def _layer_from_property(testcase: ET.Element) -> str | None:
    props = testcase.find("properties")
    if props is None:
        return None
    for prop in props.findall("property"):
        if prop.get("name") == "layer":
            return prop.get("value")
    return None


def _layer_from_filename(testcase: ET.Element) -> str | None:
    name = testcase.get("classname") or testcase.get("name") or ""
    for hint, layer in FILE_LAYER_HINTS:
        if hint in name:
            return layer
    return None


def _status_of(testcase: ET.Element) -> str:
    if testcase.find("failure") is not None:
        return "failed"
    if testcase.find("error") is not None:
        return "errors"
    if testcase.find("skipped") is not None:
        return "skipped"
    return "passed"


def collect(xml_path: Path) -> tuple[dict[str, LayerStats], float]:
    """XML parsen und je Layer zählen; gibt (Matrix, Gesamtdauer) zurück."""
    root = ET.parse(xml_path).getroot()
    matrix = {key: LayerStats() for key, _ in LAYERS}
    total_seconds = 0.0

    for testcase in root.iter("testcase"):
        layer = _layer_from_property(testcase) or _layer_from_filename(testcase) or "unknown"
        if layer not in matrix:
            # Unbekannter Marker → als `unknown` führen, nicht verwerfen.
            layer = "unknown"
        stats = matrix[layer]
        stats.total += 1
        status = _status_of(testcase)
        if status == "passed":
            stats.passed += 1
        elif status == "skipped":
            stats.skipped += 1
        else:
            stats.__dict__[status] += 1
        try:
            seconds = float(testcase.get("time", "0") or 0)
        except ValueError:
            seconds = 0.0
        stats.seconds += seconds
        total_seconds += seconds
        full = f"{testcase.get('classname', '')}::{testcase.get('name', '')}".strip(":")
        stats.cases.append(full)

    return matrix, total_seconds


def build_markdown(xml_path: Path, matrix: dict[str, LayerStats], total_seconds: float) -> str:
    total = sum(s.total for s in matrix.values())
    passed = sum(s.passed for s in matrix.values())
    failed = sum(s.failed for s in matrix.values())
    errors = sum(s.errors for s in matrix.values())
    skipped = sum(s.skipped for s in matrix.values())
    green = failed == 0 and errors == 0
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = []
    lines.append("# EVA — Testreport")
    lines.append("")
    lines.append(f"- **Erzeugt:** {generated}")
    lines.append(f"- **Quelle:** `{xml_path.relative_to(PROJECT_ROOT)}`")
    lines.append(f"- **Gesamtergebnis:** {'GRÜN' if green else 'ROT'} "
                 f"({passed}/{total} passed, {failed} failed, {errors} errors, "
                 f"{skipped} skipped, {total_seconds:.2f}s)")
    lines.append("")
    lines.append("## Layer-Matrix (PLAN §7.1)")
    lines.append("")
    lines.append("| Layer | Marker | Tests | Passed | Failed | Errors | Skipped | Dauer | Status |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for key, title in LAYERS:
        stats = matrix[key]
        status = "—" if stats.total == 0 else ("GRÜN" if stats.failed == stats.errors == 0 else "ROT")
        lines.append(
            f"| {title} | `{key}` | {stats.total} | {stats.passed} | {stats.failed} | "
            f"{stats.errors} | {stats.skipped} | {stats.seconds:.2f}s | {status} |"
        )
    lines.append("")
    lines.append("## Gesamt")
    lines.append("")
    lines.append(f"- Tests: **{total}**")
    lines.append(f"- Passed: **{passed}**")
    lines.append(f"- Failed: **{failed}**")
    lines.append(f"- Errors: **{errors}**")
    lines.append(f"- Skipped: **{skipped}**")
    lines.append(f"- Dauer (Summe testcase-Zeiten): **{total_seconds:.2f}s**")
    lines.append(f"- Gate: **{'GRÜN (rc=0)' if green else 'ROT (rc≠0)'}**")
    lines.append("")
    lines.append("## Testfälle je Layer")
    lines.append("")
    for key, title in LAYERS:
        stats = matrix[key]
        if stats.total == 0:
            continue
        lines.append(f"### {title} · `{key}` ({stats.total})")
        lines.append("")
        for case in stats.cases:
            lines.append(f"- `{case}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JUnit-XML → reports/TEST-REPORT.md")
    parser.add_argument("--xml", type=Path, default=None,
                        help="JUnit-XML-Datei (Default: jüngste unter reports/junit/)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="Zieldatei (Default: reports/TEST-REPORT.md)")
    args = parser.parse_args(argv)

    xml_path = args.xml
    if xml_path is None:
        candidates = glob.glob(str(JUNIT_DIR / "*.xml"))
        if not candidates:
            print(f"Kein JUnit-XML unter {JUNIT_DIR} gefunden.", file=sys.stderr)
            return 2
        xml_path = Path(max(candidates, key=os.path.getmtime))
    if not xml_path.is_file():
        print(f"XML-Datei nicht gefunden: {xml_path}", file=sys.stderr)
        return 2

    try:
        matrix, total_seconds = collect(xml_path)
    except ET.ParseError as exc:
        print(f"XML nicht parsebar ({xml_path}): {exc}", file=sys.stderr)
        return 2

    markdown = build_markdown(xml_path, matrix, total_seconds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown, encoding="utf-8")

    failed = sum(s.failed for s in matrix.values())
    errors = sum(s.errors for s in matrix.values())
    print(f"Report geschrieben: {args.out} (Quelle: {xml_path.name})")
    print(f"Gesamt: {sum(s.total for s in matrix.values())} Tests, "
          f"{failed} failed, {errors} errors")
    return 0 if failed == 0 and errors == 0 else 1


if __name__ == "__main__":  # pragma: no cover – CLI-Einstieg.
    raise SystemExit(main())
