import pytest

from aws_asg_launch_template_drift.analyze import (
    COMPARED_FIELDS,
    PayloadError,
    analyse,
    analyse_group,
    diff_versions,
    exit_code,
    worst,
)


def by_name(result, name):
    return next(g for g in result["groups"] if g["name"] == name)


def test_six_on_target_two_stale_reports_25_percent(prod, now):
    web = by_name(analyse(prod, now=now), "prod-web")
    assert web["instance_count"] == 8
    assert web["stale_count"] == 2
    assert web["stale_pct"] == 25.0
    assert web["versions"] == {"4": 2, "7": 6}
    assert web["oldest_stale_version"] == "4"
    assert web["drifted_fields"] == ["ImageId", "UserData"]


def test_image_id_drift_is_critical_below_the_percentage_threshold(prod, now):
    web = by_name(analyse(prod, now=now, critical_pct=50.0), "prod-web")
    assert web["stale_pct"] < 50.0
    assert web["severity"] == "critical"
    assert any("ImageId differs" in reason for reason in web["reasons"])


def test_security_group_ordering_alone_is_not_drift(prod, now):
    workers = by_name(analyse(prod, now=now), "staging-workers")
    assert workers["stale_count"] == 1
    assert workers["drifted_fields"] == []
    assert workers["severity"] == "warn"


def test_latest_alias_resolves_to_a_numeric_version(prod, now):
    web = by_name(analyse(prod, now=now), "prod-web")
    assert web["configured_version"] == "$Latest"
    assert web["target_version"] == "7"


def test_launch_configuration_group_is_legacy_and_at_least_warn(prod, now):
    batch = by_name(analyse(prod, now=now), "legacy-batch")
    assert batch["legacy"] is True
    assert batch["severity"] == "warn"
    assert "legacy-batch-lc-20190412" in batch["reasons"][0]


def test_group_fully_on_target_is_ok(prod, now):
    api = by_name(analyse(prod, now=now), "prod-api")
    assert api["severity"] == "ok"
    assert api["stale_count"] == 0
    assert api["reasons"] == []


def test_terminating_instances_do_not_count(prod, now):
    api = by_name(analyse(prod, now=now), "prod-api")
    # The fixture has four instances, one of them Terminating:Wait.
    assert api["instance_count"] == 3


def test_old_stale_instance_is_critical_by_age(aged, now):
    group = analyse(aged, now=now)["groups"][0]
    assert group["stale_pct"] == 25.0
    assert group["oldest_stale_instance"] == "i-0f04"
    assert group["oldest_stale_age_days"] == 181
    assert group["severity"] == "critical"
    assert any("181 days old" in reason for reason in group["reasons"])


def test_age_rule_respects_max_age_days(aged, now):
    group = analyse(aged, now=now, max_age_days=365)["groups"][0]
    assert group["severity"] == "warn"


def test_majority_stale_is_critical_by_share(majority, now):
    group = analyse(majority, now=now)["groups"][0]
    assert group["stale_pct"] == 75.0
    assert group["drifted_fields"] == ["InstanceType"]
    assert group["severity"] == "critical"


def test_share_rule_respects_critical_pct(majority, now):
    group = analyse(majority, now=now, critical_pct=90.0)["groups"][0]
    assert group["severity"] == "warn"


def test_results_are_sorted_by_severity_then_staleness(prod, now):
    names = [g["name"] for g in analyse(prod, now=now)["groups"]]
    assert names == ["prod-web", "staging-workers", "legacy-batch", "prod-api"]


def test_summary_counts(prod, now):
    summary = analyse(prod, now=now)["summary"]
    assert summary == {
        "groups": 4,
        "ok": 1,
        "warn": 2,
        "critical": 1,
        "stale_instances": 3,
        "worst": "critical",
    }


def test_empty_payload_is_ok():
    result = analyse({"groups": []})
    assert result["summary"]["worst"] == "ok"
    assert result["groups"] == []


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "must be a JSON object"),
        ({}, "missing a 'groups' array"),
        ({"groups": {}}, "missing a 'groups' array"),
        ({"groups": ["prod-web"]}, "groups[0] is not a JSON object"),
    ],
)
def test_malformed_payload_raises_payload_error(payload, message):
    with pytest.raises(PayloadError) as excinfo:
        analyse(payload)
    assert message in str(excinfo.value)


def test_group_without_any_launch_source_is_warned():
    finding = analyse_group({"name": "orphan", "instances": []})
    assert finding["severity"] == "warn"
    assert "neither a launch template" in finding["reasons"][0]


def test_unknown_instance_version_counts_as_stale(now):
    finding = analyse_group(
        {
            "name": "half-known",
            "launch_template": {"target_version": "2"},
            "template_versions": {"2": {"ImageId": "ami-1"}},
            "instances": [
                {"id": "i-1", "version": "2", "lifecycle_state": "InService"},
                {"id": "i-2", "version": None, "lifecycle_state": "InService"},
            ],
        },
        now=now,
    )
    assert finding["versions"] == {"2": 1, "unknown": 1}
    assert finding["stale_count"] == 1


def test_missing_launch_time_leaves_age_unknown(now):
    finding = analyse_group(
        {
            "name": "undated",
            "launch_template": {"target_version": "3"},
            "template_versions": {"2": {"ImageId": "a"}, "3": {"ImageId": "a"}},
            "instances": [
                {"id": "i-1", "version": "2", "lifecycle_state": "InService"},
                {"id": "i-2", "version": "3", "lifecycle_state": "InService"},
            ],
        },
        now=now,
    )
    assert finding["oldest_stale_age_days"] is None
    assert finding["severity"] == "warn"


def test_unparseable_launch_time_is_not_fatal(now):
    finding = analyse_group(
        {
            "name": "bad-date",
            "launch_template": {"target_version": "3"},
            "template_versions": {"2": {"ImageId": "a"}, "3": {"ImageId": "a"}},
            "instances": [
                {
                    "id": "i-1",
                    "version": "2",
                    "lifecycle_state": "InService",
                    "launch_time": "yesterday",
                }
            ],
        },
        now=now,
    )
    assert finding["oldest_stale_age_days"] is None


def test_imds_change_is_reported_as_drift(launch_fields, now):
    edge = by_name(analyse(launch_fields, now=now), "edge-imds")
    assert edge["drifted_fields"] == ["MetadataOptions"]
    assert edge["severity"] == "warn"
    assert "version 5 also differs in MetadataOptions" in edge["reasons"]


def test_critical_field_escalates_a_group(launch_fields, now):
    result = analyse(launch_fields, now=now, critical_fields=["ImageId", "MetadataOptions"])
    edge = by_name(result, "edge-imds")
    assert edge["severity"] == "critical"
    assert any(
        reason.startswith("MetadataOptions differs between version 5 and version 6")
        and "--critical-field" in reason
        for reason in edge["reasons"]
    )
    # An escalated field is not repeated in the "also differs" line.
    assert not any("also differs" in reason for reason in edge["reasons"])
    assert by_name(result, "db-proxy")["severity"] == "warn"


def test_root_volume_and_key_changes_are_drift(launch_fields, now):
    proxy = by_name(analyse(launch_fields, now=now), "db-proxy")
    # Interfaces and tag specifications differ only in key and list order.
    assert proxy["drifted_fields"] == ["BlockDeviceMappings", "KeyName"]
    assert "version 2 also differs in BlockDeviceMappings, KeyName" in proxy["reasons"]


def test_fields_limit_the_comparison(launch_fields, now):
    result = analyse(launch_fields, now=now, fields=["ImageId", "KeyName"])
    assert by_name(result, "edge-imds")["drifted_fields"] == []
    assert by_name(result, "db-proxy")["drifted_fields"] == ["KeyName"]
    assert result["compared_fields"] == ["ImageId", "KeyName"]
    assert result["critical_fields"] == ["ImageId"]


def test_image_id_is_not_critical_when_it_is_not_compared(prod, now):
    fields = [f for f in COMPARED_FIELDS if f != "ImageId"]
    web = by_name(analyse(prod, now=now, max_age_days=36500, fields=fields), "prod-web")
    assert web["drifted_fields"] == ["UserData"]
    assert web["severity"] == "warn"


def test_result_lists_compared_and_critical_fields(prod, now):
    result = analyse(prod, now=now)
    assert result["compared_fields"] == list(COMPARED_FIELDS)
    assert result["critical_fields"] == ["ImageId"]


class TestDiffVersions:
    def test_identical_versions_do_not_differ(self):
        version = {"ImageId": "ami-1", "SecurityGroupIds": ["sg-a"]}
        assert diff_versions(version, dict(version)) == []

    def test_security_group_order_is_ignored(self):
        assert (
            diff_versions(
                {"SecurityGroupIds": ["sg-a", "sg-b"]},
                {"SecurityGroupIds": ["sg-b", "sg-a"]},
            )
            == []
        )

    def test_security_group_membership_is_not_ignored(self):
        assert diff_versions(
            {"SecurityGroupIds": ["sg-a"]}, {"SecurityGroupIds": ["sg-a", "sg-b"]}
        ) == ["SecurityGroupIds"]

    def test_userdata_is_compared_by_hash(self):
        assert diff_versions({"UserData": "YQ=="}, {"UserData": "Yg=="}) == ["UserData"]

    def test_metadata_state_is_ignored(self):
        old = {"MetadataOptions": {"HttpTokens": "required", "State": "applied"}}
        new = {"MetadataOptions": {"HttpTokens": "required", "State": "pending"}}
        assert diff_versions(old, new) == []

    def test_imdsv1_regression_is_drift(self):
        old = {"MetadataOptions": {"HttpTokens": "required"}}
        new = {"MetadataOptions": {"HttpTokens": "optional"}}
        assert diff_versions(old, new) == ["MetadataOptions"]

    def test_block_device_mapping_order_is_ignored(self):
        root = {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 20}}
        data = {"DeviceName": "/dev/xvdb", "Ebs": {"VolumeSize": 100}}
        assert (
            diff_versions(
                {"BlockDeviceMappings": [root, data]}, {"BlockDeviceMappings": [data, root]}
            )
            == []
        )

    def test_root_volume_type_change_is_drift(self):
        old = {"BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeType": "gp2"}}]}
        new = {"BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeType": "gp3"}}]}
        assert diff_versions(old, new) == ["BlockDeviceMappings"]

    def test_network_interface_and_group_order_is_ignored(self):
        eth0 = {"DeviceIndex": 0, "Groups": ["sg-a", "sg-b"]}
        eth1 = {"DeviceIndex": 1, "Groups": ["sg-c"]}
        swapped = {"DeviceIndex": 0, "Groups": ["sg-b", "sg-a"]}
        old = {"NetworkInterfaces": [eth0, eth1]}
        new = {"NetworkInterfaces": [eth1, swapped]}
        assert diff_versions(old, new) == []

    def test_group_moved_between_interfaces_is_drift(self):
        old = [{"DeviceIndex": 0, "Groups": ["sg-a"]}, {"DeviceIndex": 1, "Groups": ["sg-b"]}]
        new = [{"DeviceIndex": 0, "Groups": ["sg-b"]}, {"DeviceIndex": 1, "Groups": ["sg-a"]}]
        assert diff_versions({"NetworkInterfaces": old}, {"NetworkInterfaces": new}) == [
            "NetworkInterfaces"
        ]

    def test_public_ip_on_an_interface_is_drift(self):
        old = [{"DeviceIndex": 0, "AssociatePublicIpAddress": False}]
        new = [{"DeviceIndex": 0, "AssociatePublicIpAddress": True}]
        assert diff_versions({"NetworkInterfaces": old}, {"NetworkInterfaces": new}) == [
            "NetworkInterfaces"
        ]

    def test_tag_order_is_ignored_but_values_are_not(self):
        def spec(*tags):
            return {
                "TagSpecifications": [
                    {"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags]}
                ]
            }

        assert diff_versions(spec(("a", "1"), ("b", "2")), spec(("b", "2"), ("a", "1"))) == []
        assert diff_versions(spec(("a", "1")), spec(("a", "2"))) == ["TagSpecifications"]

    def test_key_name_change_is_drift(self):
        assert diff_versions({"KeyName": "old"}, {"KeyName": "new"}) == ["KeyName"]

    def test_empty_list_equals_missing_field(self):
        old = {"ImageId": "ami-1", "BlockDeviceMappings": [], "MetadataOptions": {}}
        assert diff_versions(old, {"ImageId": "ami-1"}) == []

    def test_fields_argument_limits_the_diff(self):
        old = {"ImageId": "ami-1", "KeyName": "a"}
        new = {"ImageId": "ami-2", "KeyName": "b"}
        assert diff_versions(old, new, ["KeyName"]) == ["KeyName"]

    def test_missing_version_yields_no_diff(self):
        assert diff_versions(None, {"ImageId": "ami-1"}) == []
        assert diff_versions({"ImageId": "ami-1"}, {}) == []


class TestExitCode:
    def test_fail_on_none_is_always_zero(self, prod, now):
        assert exit_code(analyse(prod, now=now), "none") == 0

    def test_fail_on_critical_trips_on_critical(self, prod, now):
        assert exit_code(analyse(prod, now=now), "critical") == 1

    def test_fail_on_critical_ignores_warn(self, prod, now):
        warn_only = {"groups": [g for g in prod["groups"] if g["name"] == "staging-workers"]}
        assert exit_code(analyse(warn_only, now=now), "critical") == 0
        assert exit_code(analyse(warn_only, now=now), "warn") == 1

    def test_clean_fleet_exits_zero(self, prod, now):
        clean = {"groups": [g for g in prod["groups"] if g["name"] == "prod-api"]}
        assert exit_code(analyse(clean, now=now), "warn") == 0


def test_worst_of_nothing_is_ok():
    assert worst([]) == "ok"
    assert worst(["ok", "critical", "warn"]) == "critical"
