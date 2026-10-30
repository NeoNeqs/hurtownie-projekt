from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1] / "analysis"
sys.path.insert(0, str(ANALYSIS))

from scale_report import format_cell, markdown_table, summary_table  # noqa: E402


def test_format_cell_escapes_table_separators() -> None:
    assert format_cell(None) == "—"
    assert format_cell(True) == "yes"
    assert format_cell(1234) == "1,234"
    assert format_cell(12.5) == "12.5"
    assert format_cell("a|b\nc") == "a\\|b c"
    assert (
        format_cell(datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc))
        == "2026-10-30 12:05+00:00"
    )


def test_markdown_table_renders_rows_and_empty_state() -> None:
    rows = [
        {"ecosystem": "npm", "sampled_packages": 300},
        {"ecosystem": "PyPI", "sampled_packages": 300},
    ]

    table = markdown_table(rows)

    assert "| ecosystem | sampled_packages |" in table
    assert "| --- | --- |" in table
    assert "| npm | 300 |" in table
    assert markdown_table([]) == "_(no rows)_"


def test_summary_table_reads_orchestrator_tsv(tmp_path: Path) -> None:
    path = tmp_path / "summary.tsv"
    path.write_text(
        "2026-10-30T12:00:00+00:00\t2026-10-30T12:30:00+00:00\t"
        "nuget\tNuGet\tsucceeded\t0\n",
        encoding="utf-8",
    )

    table = summary_table(path)

    assert "| nuget | NuGet | succeeded | 0 |" in table
    assert summary_table(tmp_path / "missing.tsv") == "_(no summary.tsv)_"
