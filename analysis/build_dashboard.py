"""Generate a self-contained HTML dashboard from analysis/queries.sql.

Every table is one named query from that file, so a figure on the page can be
traced back to documented SQL. The page is static: no server or JavaScript
framework is needed to host it.
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from warehouse.db import connect, get_dsn

sys.path.insert(0, str(Path(__file__).parent))
from run_queries import load_queries  # noqa: E402

TITLES = {
    "sample_overview": "Sample overview",
    "packages_with_vulnerability": "Packages with known vulnerabilities",
    "vulnerability_counts_and_severity": "Advisories and severity",
    "latest_versions_affected": "Latest version affected",
    "dependency_count_vs_vulnerabilities": "Dependencies and vulnerabilities",
    "direct_dependency_exposure": "Direct dependency exposure",
    "transitive_dependency_exposure": "Transitive dependency exposure",
    "popular_vs_random": "Popular versus random",
    "remediation_delay": "Remediation delay",
    "release_cadence": "Release cadence",
    "highest_potential_exposure": "Highest potential exposure",
    "data_completeness": "Data completeness",
    "collection_freshness": "Collection freshness",
    "collection_errors": "Collection errors",
}
DESCRIPTIONS = {
    "sample_overview": "Sampled packages in the most recent successful run per ecosystem.",
    "packages_with_vulnerability": "Share of sampled packages with at least one affected collected version.",
    "vulnerability_counts_and_severity": "Distinct advisories and severity mix for the sampled versions.",
    "latest_versions_affected": "Packages whose latest collected version matches an advisory.",
    "dependency_count_vs_vulnerabilities": "Whether packages with more declared dependencies also match more advisories.",
    "direct_dependency_exposure": "Sampled packages that declare a dependency with a known affected version.",
    "transitive_dependency_exposure": "Dependency paths of at most three hops that reach an affected package.",
    "popular_vs_random": "Vulnerability share by sampling group.",
    "remediation_delay": "Days between the first affected release and the first fixed release.",
    "release_cadence": "Average and median gap between consecutive sampled releases.",
    "highest_potential_exposure": "Packages ranked by affected versions and by how often sampled packages declare them.",
    "data_completeness": "Field and dependency coverage for the sampled packages.",
    "collection_freshness": "Latest collector run per ecosystem, including failures.",
    "collection_errors": "Failures recorded by the latest run of each ecosystem.",
}
FILTER_COLUMNS = ("ecosystem", "severity", "status", "category", "spider")
SEVERITY_CLASSES = {"critical", "high", "moderate", "medium", "low", "unknown"}


def format_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def humanize(name: str) -> str:
    return name.replace("_", " ").capitalize()


def _cell(value: Any, column: str) -> str:
    text = html.escape(format_value(value))
    if column == "severity" and text.lower() in SEVERITY_CLASSES:
        return f'<td><span class="badge {text.lower()}">{text}</span></td>'
    if column == "status":
        return f'<td><span class="badge {text.lower()}">{text}</span></td>'
    return f"<td>{text}</td>"


def _filters(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    columns = list(rows[0])
    parts = ['<input type="search" placeholder="Filter rows" aria-label="Filter rows">']
    for column in columns:
        if column not in FILTER_COLUMNS:
            continue
        values = sorted({format_value(row.get(column)) for row in rows})
        if len(values) < 2:
            continue
        options = "".join(
            f'<option value="{html.escape(value)}">{html.escape(value)}</option>'
            for value in values
        )
        parts.append(
            f'<select data-col="{columns.index(column)}" aria-label="Filter by {column}">'
            f'<option value="">All {html.escape(column)}</option>{options}</select>'
        )
    return '<div class="filters">' + "".join(parts) + "</div>"


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">(no rows)</p>'
    columns = list(rows[0])
    head = "".join(f"<th>{html.escape(humanize(column))}</th>" for column in columns)
    body = "".join(
        "<tr>"
        + "".join(_cell(row.get(column), column) for column in columns)
        + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table></div>"
    )


def _cards(results: dict[str, list[dict[str, Any]]]) -> str:
    overview = results.get("sample_overview") or []
    vulnerable = results.get("packages_with_vulnerability") or []
    freshness = results.get("collection_freshness") or []
    packages = sum(row.get("sampled_packages") or 0 for row in overview)
    with_vulns = sum(
        row.get("packages_with_vulnerability") or 0 for row in vulnerable
    )
    share = round(100.0 * with_vulns / packages, 1) if packages else 0.0
    latest = max(
        (row.get("started_at") for row in freshness if row.get("started_at")),
        default=None,
    )
    cards = [
        ("Sampled packages", f"{packages:,}", f"across {len(overview)} ecosystems"),
        ("With known vulnerabilities", f"{share:.1f}%", f"{with_vulns:,} packages"),
        ("Latest collection", format_value(latest), "most recent collector run"),
    ]
    return '<div class="cards">' + "".join(
        f'<div class="card"><span>{html.escape(label)}</span>'
        f"<strong>{html.escape(value)}</strong><small>{html.escape(note)}</small></div>"
        for label, value, note in cards
    ) + "</div>"


def render_html(
    results: dict[str, list[dict[str, Any]]], generated_at: datetime | None = None
) -> str:
    generated_at = generated_at or datetime.now()
    order = list(TITLES)
    order += [name for name in results if name not in TITLES]

    sections: list[str] = []
    for name in order:
        rows = results.get(name)
        if rows is None:
            continue
        title = TITLES.get(name, humanize(name))
        description = DESCRIPTIONS.get(name, "")
        sections.append(
            '<section class="block">'
            f"<h2>{html.escape(title)}</h2>"
            f"<p>{html.escape(description)}</p>"
            f'<code class="query">{html.escape(name)}</code>'
            + _filters(rows)
            + _table(rows)
            + "</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Package security warehouse</title>
<style>
:root {{ color-scheme: light; --ink:#17202a; --muted:#667085; --line:#e4e7ec;
         --bg:#f7f8fa; --panel:#fff; --accent:#1d4ed8; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2rem; background:var(--bg); color:var(--ink);
        font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
header {{ max-width:1200px; margin:0 auto 1.5rem; }}
h1 {{ margin:0 0 .25rem; font-size:1.6rem; }}
h2 {{ margin:0 0 .25rem; font-size:1.1rem; }}
p {{ margin:0 0 .5rem; color:var(--muted); }}
.note {{ max-width:1200px; margin:0 auto 1.5rem; padding:.75rem 1rem;
         border-left:3px solid var(--accent); background:#eef2ff; color:#27305c; }}
.cards {{ max-width:1200px; margin:0 auto 1.5rem; display:grid;
          grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:1rem; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
         padding:1rem 1.25rem; display:flex; flex-direction:column; gap:.15rem; }}
.card span, .card small {{ color:var(--muted); font-size:.8rem; }}
.card strong {{ font-size:1.5rem; }}
main {{ max-width:1200px; margin:0 auto; display:grid; gap:1rem; }}
.block {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:1rem 1.25rem 1.25rem; overflow:hidden; }}
.query {{ color:var(--accent); font-size:.8rem; }}
.filters {{ display:flex; flex-wrap:wrap; gap:.5rem; margin:.75rem 0; }}
.filters input, .filters select {{ padding:.4rem .55rem; border:1px solid var(--line);
          border-radius:6px; background:#fff; color:var(--ink); font:inherit; }}
.filters input {{ min-width:220px; flex:1; }}
.table-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
th {{ text-align:left; padding:.45rem .5rem; border-bottom:1px solid var(--line);
      color:var(--muted); font-weight:600; white-space:nowrap; }}
td {{ padding:.4rem .5rem; border-bottom:1px solid #f0f2f5; white-space:nowrap; }}
tbody tr:hover {{ background:#f9fafb; }}
.badge {{ display:inline-block; padding:.05rem .45rem; border-radius:999px;
          background:#eef2f6; font-size:.75rem; font-weight:600; }}
.badge.critical, .badge.failed {{ background:#fee4e2; color:#b42318; }}
.badge.high {{ background:#ffead5; color:#b54708; }}
.badge.moderate, .badge.medium, .badge.partial {{ background:#fef7c3; color:#854a0e; }}
.badge.low, .badge.succeeded {{ background:#dcfae6; color:#067647; }}
.empty {{ margin:.5rem 0 0; }}
footer {{ max-width:1200px; margin:1.5rem auto 0; color:var(--muted); font-size:.8rem; }}
@media print {{ body {{ background:#fff; padding:0; }} .filters {{ display:none; }}
                .block {{ break-inside:avoid; border:0; }} }}
</style>
</head>
<body>
<header>
  <h1>Package security warehouse</h1>
  <p>Pilot sample across seven ecosystems. Generated {html.escape(format_value(generated_at))}.</p>
</header>
<p class="note">This sample is not representative of any ecosystem. A matched
advisory means OSV lists the collected version in an affected range; it does not
prove exploitation or that a project installs it. A package with no match is not
known to be secure. Dependencies are declared constraints, not a resolved
install graph.</p>
{_cards(results)}
<main>
{"".join(sections)}
</main>
<footer>Figures come from the named queries in <code>analysis/queries.sql</code>.</footer>
<script>
document.querySelectorAll('section.block').forEach(function (block) {{
  var input = block.querySelector('input[type="search"]');
  var selects = Array.from(block.querySelectorAll('select[data-col]'));
  var rows = Array.from(block.querySelectorAll('tbody tr'));
  if (!input) return;
  function apply() {{
    var needle = input.value.toLowerCase();
    rows.forEach(function (row) {{
      var visible = row.textContent.toLowerCase().indexOf(needle) !== -1;
      selects.forEach(function (select) {{
        if (!select.value) return;
        var cell = row.cells[Number(select.dataset.col)];
        if (!cell || cell.textContent.trim() !== select.value) visible = false;
      }});
      row.hidden = !visible;
    }});
  }}
  input.addEventListener('input', apply);
  selects.forEach(function (select) {{ select.addEventListener('change', apply); }});
}});
</script>
</body>
</html>
"""


def build(
    dsn: str | None = None, output: Path | None = None
) -> dict[str, list[dict[str, Any]]]:
    queries = load_queries()
    conn = connect(dsn or get_dsn())
    try:
        results = {
            name: conn.execute(sql).fetchall() for name, sql in queries.items()
        }
    finally:
        conn.close()

    output = output or Path("output/dashboard.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(results), encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the HTML dashboard")
    parser.add_argument("--dsn", default=None)
    parser.add_argument(
        "--output", type=Path, default=Path("output/dashboard.html")
    )
    args = parser.parse_args(argv)

    results = build(args.dsn, args.output)
    print(f"Wrote {args.output} ({len(results)} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
