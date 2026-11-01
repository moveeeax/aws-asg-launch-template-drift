"""Collect the three API calls nobody joins into one payload.

The payload this module returns is exactly what ``--from-json`` replays, which is why
the analyser never imports boto3 and the test suite never needs credentials.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Launch template fields the analyser compares, as they appear in LaunchTemplateData.
_TEMPLATE_FIELDS = ("ImageId", "InstanceType", "IamInstanceProfile", "UserData")


def parse_tag_filters(tags: Iterable[str]) -> list[dict]:
    """Turn ``Key=Value`` strings into describe_auto_scaling_groups filters."""
    filters = []
    for raw in tags:
        if "=" not in raw:
            raise ValueError(f"--tag expects Key=Value, got {raw!r}")
        key, value = raw.split("=", 1)
        if not key or not value:
            raise ValueError(f"--tag expects Key=Value, got {raw!r}")
        filters.append({"Name": f"tag:{key}", "Values": [value]})
    return filters


def _template_data(version: dict) -> dict:
    """Flatten LaunchTemplateData into the fields the analyser compares."""
    data = version.get("LaunchTemplateData") or {}
    out: dict[str, Any] = {field: data.get(field) for field in _TEMPLATE_FIELDS}

    profile = data.get("IamInstanceProfile") or {}
    if isinstance(profile, dict):
        out["IamInstanceProfile"] = profile.get("Arn") or profile.get("Name")

    groups = list(data.get("SecurityGroupIds") or [])
    for interface in data.get("NetworkInterfaces") or []:
        groups.extend(interface.get("Groups") or [])
    out["SecurityGroupIds"] = groups or None
    return out


def _group_template_spec(group: dict) -> dict | None:
    """Find the launch template spec whether it is direct or inside a mixed policy."""
    spec = group.get("LaunchTemplate")
    if spec:
        return spec
    mixed = group.get("MixedInstancesPolicy") or {}
    return (mixed.get("LaunchTemplate") or {}).get("LaunchTemplateSpecification")


def _resolve_versions(ec2, spec: dict, wanted: set[str]) -> dict[str, dict]:
    """Describe the launch template versions we care about, keyed by version number."""
    kwargs: dict[str, Any] = {"Versions": sorted(wanted)}
    if spec.get("LaunchTemplateId"):
        kwargs["LaunchTemplateId"] = spec["LaunchTemplateId"]
    else:
        kwargs["LaunchTemplateName"] = spec.get("LaunchTemplateName")

    resolved: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for version in ec2.describe_launch_template_versions(**kwargs).get(
        "LaunchTemplateVersions", []
    ):
        number = str(version.get("VersionNumber"))
        resolved[number] = _template_data(version)
        if version.get("DefaultVersion"):
            aliases["$Default"] = number
    # $Latest is the highest version number the call returned for the alias.
    numeric = [v for v in resolved if v.isdigit()]
    if numeric:
        aliases.setdefault("$Latest", max(numeric, key=int))
    resolved["_aliases"] = aliases  # type: ignore[assignment]
    return resolved


def _instance_launch_times(ec2, instance_ids: list[str]) -> dict[str, str]:
    """describe_instances only to learn how long a stale instance has been running."""
    times: dict[str, str] = {}
    for start in range(0, len(instance_ids), 100):
        batch = instance_ids[start : start + 100]
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(InstanceIds=batch):
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    launch = inst.get("LaunchTime")
                    if launch is not None:
                        times[inst["InstanceId"]] = (
                            launch.isoformat() if hasattr(launch, "isoformat") else str(launch)
                        )
    return times


def collect(
    *,
    region: str | None = None,
    names: Iterable[str] = (),
    tags: Iterable[str] = (),
    session=None,
) -> dict:
    """Build a drift payload from the autoscaling and ec2 APIs."""
    if session is None:
        # Imported lazily: the analyser, the renderers and their tests never need boto3.
        import boto3

        session = boto3.Session(region_name=region)
    asg = session.client("autoscaling", region_name=region)
    ec2 = session.client("ec2", region_name=region)

    names = list(names)
    kwargs: dict[str, Any] = {}
    if names:
        kwargs["AutoScalingGroupNames"] = names
    filters = parse_tag_filters(tags)
    if filters:
        kwargs["Filters"] = filters

    raw_groups: list[dict] = []
    paginator = asg.get_paginator("describe_auto_scaling_groups")
    for page in paginator.paginate(**kwargs):
        raw_groups.extend(page.get("AutoScalingGroups", []))

    all_ids = [
        inst["InstanceId"]
        for group in raw_groups
        for inst in group.get("Instances", [])
        if inst.get("InstanceId")
    ]
    launch_times = _instance_launch_times(ec2, all_ids) if all_ids else {}

    groups = []
    for group in raw_groups:
        spec = _group_template_spec(group)
        entry: dict[str, Any] = {
            "name": group.get("AutoScalingGroupName"),
            "launch_configuration": group.get("LaunchConfigurationName"),
            "launch_template": None,
            "template_versions": {},
            "instances": [],
        }

        for inst in group.get("Instances", []):
            version = (inst.get("LaunchTemplate") or {}).get("Version")
            entry["instances"].append(
                {
                    "id": inst.get("InstanceId"),
                    "version": str(version) if version is not None else None,
                    "instance_type": inst.get("InstanceType"),
                    "lifecycle_state": inst.get("LifecycleState"),
                    "launch_time": launch_times.get(inst.get("InstanceId")),
                }
            )

        if spec:
            configured = str(spec.get("Version") or "$Default")
            observed = {
                i["version"] for i in entry["instances"] if i["version"] is not None
            }
            versions = _resolve_versions(
                ec2, spec, observed | {configured} | {"$Latest", "$Default"}
            )
            aliases = versions.pop("_aliases", {})
            target = aliases.get(configured, configured)
            entry["launch_template"] = {
                "id": spec.get("LaunchTemplateId"),
                "name": spec.get("LaunchTemplateName"),
                "configured_version": configured,
                "target_version": target if str(target).isdigit() else None,
            }
            entry["template_versions"] = versions
            entry["launch_configuration"] = None

        groups.append(entry)

    return {
        "region": region or session.region_name,
        "groups": groups,
    }
