from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1] / "analysis"
sys.path.insert(0, str(ANALYSIS))

from build_dashboard import format_value, render_html  # noqa: E402


def test_format_value_handles_database_types() -> None:
    assert format_value(None) == "—"
    assert format_value(True) == "yes"
    assert format_value(1234) == "1,234"
    assert format_value(50.0) == "50"
    assert format_value(12.5) == "12.5"
    assert (
        format_value(datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc))
        == "2026-10-30 12:05+00:00"
    )


def test_render_html_escapes_cells_and_adds_filters() -> None:
    results = {
        "sample_overview": [
            {"ecosystem": "<script>alert(1)</script>", "sampled_packages": 2},
            {"ecosystem": "npm", "sampled_packages": 3},
        ],
        "packages_with_vulnerability": [
            {"ecosystem": "npm", "packages_with_vulnerability": 1}
        ],
        "collection_freshness": [
            {"ecosystem": "npm", "started_at": "2026-10-30T12:00:00Z"}
        ],
    }

    page = render_html(results)

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    assert 'data-col="0"' in page
    assert "All ecosystem" in page
    assert "Sample overview" in page


def test_render_html_marks_severity_and_status() -> None:
    results = {
        "vulnerability_counts_and_severity": [
            {"ecosystem": "npm", "severity": "HIGH", "advisories": 3}
        ],
        "collection_freshness": [
            {"ecosystem": "npm", "status": "partial", "errors": 2}
        ],
    }

    page = render_html(results)

    assert '<span class="badge high">HIGH</span>' in page
    assert '<span class="badge partial">partial</span>' in page


def test_render_html_handles_empty_results() -> None:
    page = render_html({"release_cadence": []})

    assert "(no rows)" in page
