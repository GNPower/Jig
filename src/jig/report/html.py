"""One self-contained HTML page per run: every plan, case, step and recorded value.

Links to transcripts and recordings are relative to the page, so the page
and the run directories keep working when CI uploads them together (a file
on another drive than the page keeps an absolute link)."""

from __future__ import annotations

import html
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jig.report.report import CaseReport, SuiteReport, renode_line

_STYLE = """
:root { --bg: #ffffff; --fg: #1d2330; --muted: #5d6676; --line: #d9dee7; --panel: #f5f7fa;
        --pass: #1f7a3a; --fail: #b3261e; --error: #8a4b00; --skip: #5d6676;
        --mono: Consolas, "SF Mono", monospace; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171c; --fg: #e3e7ee; --muted: #9aa3b2; --line: #2c323c; --panel: #1b1f26;
          --pass: #5cc77d; --fail: #ff7a70; --error: #f0a64a; --skip: #9aa3b2; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.45 system-ui, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 6px; }
.muted { color: var(--muted); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-weight: 600; color: var(--muted); }
.badge { display: inline-block; min-width: 52px; text-align: center; font-weight: 700;
         border-radius: 4px; padding: 0 6px; }
.PASS { color: var(--pass); } .FAIL { color: var(--fail); }
.ERROR { color: var(--error); } .SKIP { color: var(--skip); }
details { border: 1px solid var(--line); border-radius: 6px; margin: 8px 0;
          background: var(--panel); }
summary { cursor: pointer; padding: 6px 10px; }
.steps { font-family: var(--mono); font-size: 12px; overflow-x: auto; }
.steps td { white-space: pre-wrap; word-break: break-word; }
.fail td, td.fail { color: var(--fail); } td.info { color: var(--muted); }
pre { font-family: var(--mono); font-size: 12px; white-space: pre-wrap; margin: 6px 10px; }
.wrap { overflow-x: auto; }
svg { vertical-align: middle; }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def link(path: str, base: Path | None) -> str:
    """The href for ``path`` on a page in ``base``: relative where it can be."""
    target = Path(path)
    if base is not None and target.is_absolute():
        try:
            return quote(Path(os.path.relpath(target, base)).as_posix())
        except ValueError:  # another drive (Windows)
            pass
    return target.as_uri() if target.is_absolute() else quote(target.as_posix())


def _sparkline(values: list[tuple[float, Any]], width: int = 220, height: int = 36) -> str:
    points = [(t, v) for t, v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(points) < 2:
        return ""
    t0, t1 = points[0][0], points[-1][0]
    lo = min(v for _, v in points)
    hi = max(v for _, v in points)
    span_t = (t1 - t0) or 1.0
    span_v = (hi - lo) or 1.0
    coords = " ".join(
        f"{(t - t0) / span_t * (width - 4) + 2:.1f},"
        f"{height - 2 - (v - lo) / span_v * (height - 4):.1f}"
        for t, v in points
    )
    return (
        f'<svg width="{width}" height="{height}" role="img" aria-label="trend">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{coords}"/></svg>'
    )


def _format(value: Any) -> str:
    if isinstance(value, float):
        return "nan" if math.isnan(value) else f"{value:.9g}"
    return str(value)


def _case(case: CaseReport, index: str) -> str:
    rows = []
    for step in case.steps:
        detail = f" <span class='muted'>-- {_e(step.detail)}</span>" if step.detail else ""
        rows.append(
            f"<tr class='{_e(step.outcome)}'><td>{step.time:.6f}</td><td>{_e(step.task)}</td>"
            f"<td class='{_e(step.outcome)}'>{_e(step.text)}{detail}</td>"
            f"<td class='muted'>{_e(step.where)}</td></tr>"
        )
    records = []
    for name, values in sorted(case.records.items()):
        last = values[-1][1] if values else ""
        records.append(
            f"<tr><td>{_e(name)}</td><td>{len(values)}</td><td>{_e(_format(last))}</td>"
            f"<td>{_sparkline(values)}</td></tr>"
        )
    parts = [
        f"<details id='{_e(index)}'{' open' if case.status not in ('PASS', 'SKIP') else ''}>",
        f"<summary><span class='badge {case.status}'>{case.status}</span> {_e(case.name)} "
        f"<span class='muted'>{case.simulated_seconds:.3f} s simulated, {case.checks} checks, "
        f"{_e(case.where)}</span></summary>",
    ]
    if case.detail:
        parts.append(f"<pre class='{case.status}'>{_e(case.detail)}</pre>")
    if records:
        parts.append(
            "<div class='wrap'><table><tr><th>record</th><th>values</th><th>last</th>"
            "<th>trend</th></tr>" + "".join(records) + "</table></div>"
        )
    if rows:
        parts.append(
            "<div class='wrap steps'><table><tr><th>time (s)</th><th>task</th><th>step</th>"
            "<th>where</th></tr>" + "".join(rows) + "</table></div>"
        )
    parts.append("</details>")
    return "".join(parts)


def render(suites: list[SuiteReport], base: Path | None = None) -> str:
    """The page; ``base`` is the directory it will be written to (links to
    run files become relative to it)."""
    total = sum(len(s.cases) for s in suites)
    passed = sum(s.count("PASS") for s in suites)
    summary_rows = []
    for number, suite in enumerate(suites):
        status = suite.status
        summary_rows.append(
            f"<tr><td><span class='badge {status}'>{status}</span></td>"
            f"<td><a href='#plan-{number}'>{_e(suite.name)}</a></td>"
            f"<td>{suite.count('PASS')}/{len(suite.cases)}</td>"
            f"<td>{suite.simulated_seconds:.1f}</td><td>{suite.wall_seconds:.1f}</td>"
            f"<td class='muted'>{suite.fingerprint()[:16]}</td></tr>"
        )
    body = [
        "<h1>jig run</h1>",
        f"<p class='muted'>{passed} of {total} tests passed in {len(suites)} plan runs</p>",
        "<div class='wrap'><table><tr><th></th><th>plan</th><th>passed</th>"
        "<th>simulated (s)</th><th>wall (s)</th><th>fingerprint</th></tr>"
        + "".join(summary_rows)
        + "</table></div>",
    ]
    for number, suite in enumerate(suites):
        body.append(f"<h2 id='plan-{number}'>{_e(suite.name)}</h2>")
        body.append(
            f"<p class='muted'>{_e(suite.source)} &middot; seed {suite.seed} &middot; "
            f"run directory {_e(suite.run_directory)}</p>"
        )
        renode = renode_line(suite)
        if renode:
            body.append(f"<p class='muted'>{_e(renode)}</p>")
        if suite.system:
            body.append("<pre>" + _e("\n".join(suite.system)) + "</pre>")
        if suite.transcripts:
            links = ", ".join(
                f"<a href='{_e(link(path, base))}'>{_e(label)}</a>"
                for label, path in suite.transcripts.items()
            )
            body.append(f"<p class='muted'>transcripts: {links}</p>")
        if suite.recordings:
            links = ", ".join(
                f"<a href='{_e(link(path, base))}'>{_e(name)}</a> ({entries})"
                for name, (path, entries) in suite.recordings.items()
            )
            body.append(f"<p class='muted'>recorded: {links}</p>")
        if suite.events:
            cells = "".join(
                f"<tr><td>{_e(kind)}</td><td>{count}</td></tr>"
                for kind, count in suite.events.items()
            )
            body.append(
                "<details><summary>events by source and kind</summary><div class='wrap'>"
                f"<table><tr><th>source kind</th><th>events</th></tr>{cells}</table>"
                "</div></details>"
            )
        if suite.aborted:
            body.append(f"<pre class='ERROR'>aborted: {_e(suite.aborted)}</pre>")
        for kind, case in (("setup", suite.setup),):
            if case is not None:
                body.append(_case(case, f"plan-{number}-{kind}"))
        for index, case in enumerate(suite.cases):
            body.append(_case(case, f"plan-{number}-case-{index}"))
        if suite.teardown is not None:
            body.append(_case(suite.teardown, f"plan-{number}-teardown"))
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>jig report</title><style>{_STYLE}</style></head><body><main>"
        + "".join(body)
        + "</main></body></html>"
    )


def write_html(suites: list[SuiteReport], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    page = render(suites, destination.parent.resolve())
    destination.write_text(page, encoding="utf-8", newline="\n")
    return destination
