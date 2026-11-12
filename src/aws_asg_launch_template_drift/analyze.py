"""Compare what an ASG is configured to launch against what it is actually running.

Everything in here works on plain dicts, the same shape the collector produces and
``--from-json`` replays, so the rules are testable without touching AWS.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

SEVERITY_ORDER = {"ok": 0, "warn": 1, "critical": 2}

#: Launch template fields that change what a replacement instance actually runs.
COMPARED_FIELDS = (
    "ImageId",
    "InstanceType",
    "SecurityGroupIds",
    "IamInstanceProfile",
    "UserData",
    "MetadataOptions",
    "BlockDeviceMappings",
    "KeyName",
    "NetworkInterfaces",
    "TagSpecifications",
)

#: Fields whose drift makes a group critical on its own, whatever the stale share.
#: MetadataOptions is here because a stale version can be the one that still allows
#: IMDSv1 (``HttpTokens: optional``); ``--no-default-critical`` drops it again.
DEFAULT_CRITICAL_FIELDS = ("ImageId", "MetadataOptions")

#: Default critical fields that ``--no-default-critical`` keeps.
ALWAYS_CRITICAL_FIELDS = ("ImageId",)

#: Why drift in a critical field matters, as printed in the group's reasons.
_CRITICAL_CONSEQUENCES = {
    "ImageId": "the next scale-out changes the running image",
    "MetadataOptions": "stale instances run with different IMDS settings than new ones",
}

#: Instances on their way out of the group are not drift, they are churn.
_IGNORED_LIFECYCLE_PREFIXES = ("Terminat", "Detach")


class PayloadError(ValueError):
    """The payload handed to the analyser is not a drift payload."""


def worst(severities: Iterable[str]) -> str:
    """Return the highest severity in ``severities``, ``ok`` when empty."""
    return max(severities, key=lambda s: SEVERITY_ORDER[s], default="ok")


def _canonical(value: Any) -> str:
    """Serialise a nested launch template structure so key order cannot cause a diff."""
    return json.dumps(value, sort_keys=True, default=str)


def _sorted_by_canonical(items: Iterable[Any]) -> list[Any]:
    return sorted(items, key=_canonical)


def _normalise_interface(interface: dict) -> dict:
    out = dict(interface)
    if out.get("Groups") is not None:
        out["Groups"] = sorted(str(g) for g in out["Groups"])
    return out


def _normalise(field: str, value: Any) -> Any:
    """Reduce a launch template field to something comparable across versions."""
    if value is None or value == [] or value == {}:
        return None
    if field == "SecurityGroupIds":
        # Order is an artefact of the API response, not a configuration change.
        return tuple(sorted(str(v) for v in value))
    if field == "UserData":
        # Userdata is base64 and can be enormous; the hash is enough to spot a change.
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    if field == "MetadataOptions":
        # State is pending/applied: it reports rollout progress, not configuration.
        options = {k: v for k, v in dict(value).items() if k != "State"}
        return _canonical(options) if options else None
    if field == "BlockDeviceMappings":
        # One mapping per device name; the list order means nothing.
        return _canonical(_sorted_by_canonical(value))
    if field == "NetworkInterfaces":
        # Interfaces are identified by card and device index, not by list position.
        interfaces = sorted(
            (_normalise_interface(i) for i in value),
            key=lambda i: (
                i.get("NetworkCardIndex") or 0,
                i.get("DeviceIndex") or 0,
                _canonical(i),
            ),
        )
        return _canonical(interfaces)
    if field == "TagSpecifications":
        specs = [
            {**spec, "Tags": _sorted_by_canonical(spec.get("Tags") or [])}
            for spec in value
        ]
        return _canonical(_sorted_by_canonical(specs))
    if isinstance(value, dict | list):
        return _canonical(value)
    return str(value)


def diff_versions(
    old: dict | None, new: dict | None, fields: Iterable[str] = COMPARED_FIELDS
) -> list[str]:
    """Return the compared fields that differ between two launch template versions."""
    if not old or not new:
        return []
    return [
        field
        for field in fields
        if _normalise(field, old.get(field)) != _normalise(field, new.get(field))
    ]


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_days(launch_time: Any, now: datetime) -> int | None:
    parsed = _parse_time(launch_time)
    if parsed is None:
        return None
    return max((now - parsed).days, 0)


def _live_instances(group: dict) -> list[dict]:
    out = []
    for inst in group.get("instances") or []:
        state = str(inst.get("lifecycle_state") or "")
        if state.startswith(_IGNORED_LIFECYCLE_PREFIXES):
            continue
        out.append(inst)
    return out


def _version_sort_key(version: str) -> tuple[int, Any]:
    text = str(version)
    return (0, int(text)) if text.isdigit() else (1, text)


def analyse_group(
    group: dict,
    *,
    critical_pct: float = 50.0,
    max_age_days: int = 30,
    fields: Iterable[str] = COMPARED_FIELDS,
    critical_fields: Iterable[str] = DEFAULT_CRITICAL_FIELDS,
    now: datetime | None = None,
) -> dict:
    """Analyse a single group payload and return its finding."""
    now = now or datetime.now(UTC)
    critical_fields = set(critical_fields)
    name = group.get("name") or "<unnamed>"
    instances = _live_instances(group)
    total = len(instances)

    versions: dict[str, int] = {}
    for inst in instances:
        key = str(inst.get("version")) if inst.get("version") is not None else "unknown"
        versions[key] = versions.get(key, 0) + 1

    finding: dict[str, Any] = {
        "name": name,
        "legacy": False,
        "launch_template_id": (group.get("launch_template") or {}).get("id"),
        "launch_template_name": (group.get("launch_template") or {}).get("name"),
        "configured_version": (group.get("launch_template") or {}).get("configured_version"),
        "target_version": None,
        "instance_count": total,
        "stale_count": 0,
        "stale_pct": 0.0,
        "versions": dict(sorted(versions.items(), key=lambda kv: _version_sort_key(kv[0]))),
        "oldest_stale_version": None,
        "oldest_stale_instance": None,
        "oldest_stale_age_days": None,
        "drifted_fields": [],
        "reasons": [],
        "severity": "ok",
    }

    launch_config = group.get("launch_configuration")
    if launch_config:
        # A launch configuration has no versions, so there is nothing to compare and
        # no way to roll a change out: the group is reported on principle.
        finding["legacy"] = True
        finding["launch_configuration"] = launch_config
        finding["severity"] = "warn"
        finding["reasons"].append(
            f"group still uses launch configuration {launch_config}, "
            "which cannot be versioned — migrate it to a launch template"
        )
        return finding

    target = group.get("launch_template") or {}
    target_version = target.get("target_version")
    if target_version is None:
        finding["severity"] = "warn"
        finding["reasons"].append(
            "group has neither a launch template nor a launch configuration resolved"
        )
        return finding

    target_version = str(target_version)
    finding["target_version"] = target_version

    stale = [inst for inst in instances if str(inst.get("version")) != target_version]
    finding["stale_count"] = len(stale)
    finding["stale_pct"] = round(100.0 * len(stale) / total, 2) if total else 0.0

    if not stale:
        return finding

    stale_versions = sorted({str(i.get("version")) for i in stale}, key=_version_sort_key)
    oldest_version = stale_versions[0]
    finding["oldest_stale_version"] = oldest_version

    template_versions = group.get("template_versions") or {}
    finding["drifted_fields"] = diff_versions(
        template_versions.get(oldest_version), template_versions.get(target_version), fields
    )

    dated = [(_age_days(i.get("launch_time"), now), i) for i in stale]
    dated = [(age, inst) for age, inst in dated if age is not None]
    if dated:
        age, inst = max(dated, key=lambda pair: pair[0])
        finding["oldest_stale_age_days"] = age
        finding["oldest_stale_instance"] = inst.get("id")

    finding["severity"] = "warn"
    plural = "s" if len(stale) != 1 else ""
    finding["reasons"].append(
        f"{len(stale)} of {total} instance{plural} still on "
        f"{', '.join('version ' + v for v in stale_versions)} "
        f"instead of version {target_version}"
    )

    escalated = [f for f in finding["drifted_fields"] if f in critical_fields]
    for field in escalated:
        finding["severity"] = "critical"
        consequence = _CRITICAL_CONSEQUENCES.get(field, "marked critical by --critical-field")
        finding["reasons"].append(
            f"{field} differs between version {oldest_version} and version "
            f"{target_version} — {consequence}"
        )
    if finding["stale_pct"] > critical_pct:
        finding["severity"] = "critical"
        finding["reasons"].append(
            f"{finding['stale_pct']}% of the fleet is stale, over the "
            f"{critical_pct}% threshold"
        )
    if finding["oldest_stale_age_days"] is not None and (
        finding["oldest_stale_age_days"] > max_age_days
    ):
        finding["severity"] = "critical"
        finding["reasons"].append(
            f"stale instance {finding['oldest_stale_instance']} is "
            f"{finding['oldest_stale_age_days']} days old, over the "
            f"{max_age_days} day limit"
        )

    other = [f for f in finding["drifted_fields"] if f not in critical_fields]
    if other:
        finding["reasons"].append(
            f"version {oldest_version} also differs in {', '.join(other)}"
        )

    return finding


def analyse(
    payload: dict,
    *,
    critical_pct: float = 50.0,
    max_age_days: int = 30,
    fields: Iterable[str] = COMPARED_FIELDS,
    critical_fields: Iterable[str] = DEFAULT_CRITICAL_FIELDS,
    now: datetime | None = None,
) -> dict:
    """Analyse a whole payload, returning findings sorted by severity then staleness."""
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise PayloadError("payload is missing a 'groups' array")
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise PayloadError(f"groups[{index}] is not a JSON object")

    fields = list(fields)
    critical_fields = list(critical_fields)
    findings = [
        analyse_group(
            group,
            critical_pct=critical_pct,
            max_age_days=max_age_days,
            fields=fields,
            critical_fields=critical_fields,
            now=now,
        )
        for group in groups
    ]
    findings.sort(
        key=lambda f: (-SEVERITY_ORDER[f["severity"]], -f["stale_pct"], f["name"])
    )

    counts = {"ok": 0, "warn": 0, "critical": 0}
    for finding in findings:
        counts[finding["severity"]] += 1

    return {
        "region": payload.get("region"),
        "compared_fields": fields,
        "critical_fields": [f for f in fields if f in set(critical_fields)],
        "summary": {
            "groups": len(findings),
            "ok": counts["ok"],
            "warn": counts["warn"],
            "critical": counts["critical"],
            "stale_instances": sum(f["stale_count"] for f in findings),
            "worst": worst(f["severity"] for f in findings),
        },
        "groups": findings,
    }


def exit_code(result: dict, fail_on: str) -> int:
    """Translate a result into a process exit code according to ``--fail-on``."""
    if fail_on == "none":
        return 0
    threshold = SEVERITY_ORDER[fail_on]
    return 1 if SEVERITY_ORDER[result["summary"]["worst"]] >= threshold else 0
