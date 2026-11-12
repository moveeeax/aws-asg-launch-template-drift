"""The collector is exercised against hand-written API responses, never against AWS."""

import pytest

from aws_asg_launch_template_drift.analyze import analyse
from aws_asg_launch_template_drift.collect import collect, parse_tag_filters


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.pages)


class FakeAsg:
    def __init__(self, groups):
        self.paginator = FakePaginator([{"AutoScalingGroups": groups}])

    def get_paginator(self, name):
        assert name == "describe_auto_scaling_groups"
        return self.paginator


class FakeEc2:
    def __init__(self, versions, instances):
        self.versions = versions
        self.instance_paginator = FakePaginator(
            [{"Reservations": [{"Instances": instances}]}]
        )
        self.version_calls = []

    def get_paginator(self, name):
        assert name == "describe_instances"
        return self.instance_paginator

    def describe_launch_template_versions(self, **kwargs):
        self.version_calls.append(kwargs)
        wanted = set(kwargs["Versions"])
        out = [v for v in self.versions if str(v["VersionNumber"]) in wanted]
        if "$Latest" in wanted or "$Default" in wanted:
            out = list(self.versions)
        return {"LaunchTemplateVersions": out}


class FakeSession:
    region_name = "eu-central-1"

    def __init__(self, asg, ec2):
        self._clients = {"autoscaling": asg, "ec2": ec2}

    def client(self, name, region_name=None):
        return self._clients[name]


VERSIONS = [
    {
        "VersionNumber": 4,
        "LaunchTemplateData": {
            "ImageId": "ami-old",
            "InstanceType": "m6i.large",
            "SecurityGroupIds": ["sg-b", "sg-a"],
            "IamInstanceProfile": {"Arn": "arn:aws:iam::1:instance-profile/web"},
            "UserData": "djE=",
            "KeyName": "ops",
            "MetadataOptions": {"State": "applied", "HttpTokens": "optional"},
        },
    },
    {
        "VersionNumber": 7,
        "DefaultVersion": True,
        "LaunchTemplateData": {
            "ImageId": "ami-new",
            "InstanceType": "m6i.large",
            "NetworkInterfaces": [{"Groups": ["sg-a", "sg-b"]}],
            "IamInstanceProfile": {"Arn": "arn:aws:iam::1:instance-profile/web"},
            "UserData": "djI=",
            "KeyName": "ops",
            "MetadataOptions": {"State": "applied", "HttpTokens": "required"},
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 30}}],
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": [{"Key": "app", "Value": "web"}]}
            ],
        },
    },
]

GROUPS = [
    {
        "AutoScalingGroupName": "prod-web",
        "LaunchTemplate": {
            "LaunchTemplateId": "lt-1",
            "LaunchTemplateName": "prod-web",
            "Version": "$Latest",
        },
        "Instances": [
            {
                "InstanceId": "i-1",
                "LifecycleState": "InService",
                "LaunchTemplate": {"Version": "7"},
            },
            {
                "InstanceId": "i-2",
                "LifecycleState": "InService",
                "LaunchTemplate": {"Version": "4"},
            },
        ],
    },
    {
        "AutoScalingGroupName": "legacy",
        "LaunchConfigurationName": "legacy-lc",
        "Instances": [{"InstanceId": "i-3", "LifecycleState": "InService"}],
    },
]

INSTANCE_DESCRIPTIONS = [
    {"InstanceId": "i-1", "LaunchTime": "2026-10-30T00:00:00+00:00"},
    {"InstanceId": "i-2", "LaunchTime": "2026-09-01T00:00:00+00:00"},
    {"InstanceId": "i-3", "LaunchTime": "2026-01-01T00:00:00+00:00"},
]


def run_collect(**kwargs):
    ec2 = FakeEc2(VERSIONS, INSTANCE_DESCRIPTIONS)
    asg = FakeAsg(GROUPS)
    payload = collect(session=FakeSession(asg, ec2), region="eu-central-1", **kwargs)
    return payload, asg, ec2


def test_collect_resolves_latest_to_a_numeric_version():
    payload, _, _ = run_collect()
    web = payload["groups"][0]
    assert web["launch_template"]["configured_version"] == "$Latest"
    assert web["launch_template"]["target_version"] == "7"


def test_collect_flattens_template_fields():
    payload, _, _ = run_collect()
    versions = payload["groups"][0]["template_versions"]
    assert versions["4"]["ImageId"] == "ami-old"
    assert versions["4"]["IamInstanceProfile"].endswith("/web")
    # Security groups come from NetworkInterfaces on version 7 and the top level on 4.
    assert sorted(versions["7"]["SecurityGroupIds"]) == ["sg-a", "sg-b"]


def test_collect_keeps_the_launch_affecting_fields():
    payload, _, _ = run_collect()
    versions = payload["groups"][0]["template_versions"]
    assert versions["4"]["KeyName"] == "ops"
    assert versions["4"]["MetadataOptions"]["HttpTokens"] == "optional"
    assert versions["4"]["BlockDeviceMappings"] is None
    assert versions["7"]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == 30
    assert versions["7"]["NetworkInterfaces"] == [{"Groups": ["sg-a", "sg-b"]}]
    assert versions["7"]["TagSpecifications"][0]["ResourceType"] == "instance"


def test_collected_payload_reports_the_new_fields():
    payload, _, _ = run_collect()
    web = analyse(payload)["groups"][0]
    assert web["drifted_fields"] == [
        "ImageId",
        "UserData",
        "MetadataOptions",
        "BlockDeviceMappings",
        "NetworkInterfaces",
        "TagSpecifications",
    ]


def test_collect_attaches_launch_times():
    payload, _, _ = run_collect()
    stale = payload["groups"][0]["instances"][1]
    assert stale["id"] == "i-2"
    assert stale["launch_time"].startswith("2026-09-01")


def test_collect_keeps_launch_configuration_groups():
    payload, _, _ = run_collect()
    legacy = payload["groups"][1]
    assert legacy["launch_configuration"] == "legacy-lc"
    assert legacy["launch_template"] is None


def test_collected_payload_feeds_the_analyser():
    payload, _, _ = run_collect()
    result = analyse(payload)
    web = result["groups"][0]
    assert web["name"] == "prod-web"
    assert web["severity"] == "critical"
    assert "ImageId" in web["drifted_fields"]
    assert "SecurityGroupIds" not in web["drifted_fields"]


def test_name_and_tag_filters_reach_the_api():
    _, asg, _ = run_collect(names=["prod-web"], tags=["Environment=prod"])
    call = asg.paginator.calls[0]
    assert call["AutoScalingGroupNames"] == ["prod-web"]
    assert call["Filters"] == [{"Name": "tag:Environment", "Values": ["prod"]}]


def test_mixed_instances_policy_template_is_found():
    group = {
        "AutoScalingGroupName": "mixed",
        "MixedInstancesPolicy": {
            "LaunchTemplate": {
                "LaunchTemplateSpecification": {
                    "LaunchTemplateId": "lt-1",
                    "Version": "$Default",
                }
            }
        },
        "Instances": [
            {"InstanceId": "i-1", "LifecycleState": "InService", "LaunchTemplate": {"Version": "4"}}
        ],
    }
    ec2 = FakeEc2(VERSIONS, INSTANCE_DESCRIPTIONS)
    payload = collect(session=FakeSession(FakeAsg([group]), ec2), region="eu-central-1")
    assert payload["groups"][0]["launch_template"]["target_version"] == "7"


def test_collect_without_instances_skips_describe_instances():
    group = {"AutoScalingGroupName": "empty", "LaunchConfigurationName": "lc", "Instances": []}
    ec2 = FakeEc2(VERSIONS, INSTANCE_DESCRIPTIONS)
    payload = collect(session=FakeSession(FakeAsg([group]), ec2), region="eu-central-1")
    assert payload["groups"][0]["instances"] == []
    assert ec2.instance_paginator.calls == []


@pytest.mark.parametrize("bad", ["Environment", "=prod", "Environment="])
def test_bad_tag_filter_is_rejected(bad):
    with pytest.raises(ValueError, match="Key=Value"):
        parse_tag_filters([bad])


def test_tag_filter_keeps_values_with_equals_signs():
    assert parse_tag_filters(["Spec=a=b"]) == [{"Name": "tag:Spec", "Values": ["a=b"]}]
