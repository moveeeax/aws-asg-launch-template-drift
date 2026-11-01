"""Render a drift result as text, JSON or Markdown."""

from __future__ import annotations

import json

_MARKS = {"ok": "ok", "warn": "WARN", "critical": "CRIT"}


def _drift_cell(finding: dict) -> str:
    if finding.get("legacy"):
        return "launch configuration"
    fields = finding.get("drifted_fields") or []
    if fields:
        return ",".join(fields)
    return "-" if finding["stale_count"] == 0 else "no field diff"


def _target_cell(finding: dict) -> str:
    target = finding.get("target_version")
    if target is None:
        return "-"
    configured = finding.get("configured_version")
    if configured and not str(configured).isdigit():
        return f"{target} ({configured})"
    return str(target)


def _stale_cell(finding: dict) -> str:
    if finding.get("legacy"):
        return "-"
    return f"{finding['stale_count']}/{finding['instance_count']} ({finding['stale_pct']}%)"


def _age_cell(finding: dict) -> str:
    age = finding.get("oldest_stale_age_days")
    return "-" if age is None else f"{age}d"


_COLUMNS = (
    ("", lambda f: _MARKS[f["severity"]]),
    ("GROUP", lambda f: f["name"]),
    ("TARGET", _target_cell),
    ("STALE", _stale_cell),
    ("OLDEST", _age_cell),
    ("DRIFT", _drift_cell),
)


def render_text(result: dict) -> str:
    groups = result["groups"]
    if not groups:
        return "No Auto Scaling Groups matched."

    rows = [[header for header, _ in _COLUMNS]]
    rows += [[fn(f) for _, fn in _COLUMNS] for f in groups]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]

    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))

    detail = [f for f in groups if f["reasons"]]
    if detail:
        lines.append("")
        for finding in detail:
            lines.append(f"{finding['name']} [{finding['severity']}]")
            for reason in finding["reasons"]:
                lines.append(f"  - {reason}")

    s = result["summary"]
    lines.append("")
    lines.append(
        f"{s['groups']} groups: {s['critical']} critical, {s['warn']} warn, "
        f"{s['ok']} ok; {s['stale_instances']} stale instances."
    )
    return "\n".join(lines)


def render_markdown(result: dict) -> str:
    groups = result["groups"]
    s = result["summary"]
    region = result.get("region") or "default region"
    out = [
        "### ASG launch template drift",
        "",
        f"`{region}` — {s['groups']} groups, **{s['critical']} critical**, "
        f"{s['warn']} warn, {s['ok']} ok, {s['stale_instances']} stale instances.",
        "",
    ]
    if not groups:
        out.append("No Auto Scaling Groups matched.")
        return "\n".join(out)

    out.append("| | Group | Target | Stale | Oldest | Drift |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for f in groups:
        out.append(
            f"| {_MARKS[f['severity']]} | `{f['name']}` | {_target_cell(f)} | "
            f"{_stale_cell(f)} | {_age_cell(f)} | {_drift_cell(f)} |"
        )

    detail = [f for f in groups if f["reasons"]]
    if detail:
        out.append("")
        for f in detail:
            out.append(f"**`{f['name']}`** — {f['severity']}")
            for reason in f["reasons"]:
                out.append(f"- {reason}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_json(result: dict) -> str:
    return json.dumps(result, indent=2, sort_keys=False, default=str)


def render(result: dict, fmt: str) -> str:
    if fmt == "json":
        return render_json(result)
    if fmt == "markdown":
        return render_markdown(result)
    return render_text(result)
