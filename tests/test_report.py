import json

from aws_asg_launch_template_drift.analyze import analyse
from aws_asg_launch_template_drift.report import render, render_json, render_markdown, render_text


def test_text_report_lists_every_group(prod, now):
    out = render_text(analyse(prod, now=now))
    for name in ("prod-web", "prod-api", "staging-workers", "legacy-batch"):
        assert name in out
    assert "GROUP" in out and "DRIFT" in out
    assert "6/8" not in out  # stale count, not healthy count
    assert "2/8 (25.0%)" in out
    assert "4 groups: 1 critical, 2 warn, 1 ok; 3 stale instances." in out


def test_text_report_shows_the_resolved_alias(prod, now):
    out = render_text(analyse(prod, now=now))
    assert "7 ($Latest)" in out


def test_text_report_explains_legacy_groups(prod, now):
    out = render_text(analyse(prod, now=now))
    assert "launch configuration" in out


def test_text_report_handles_no_groups():
    assert render_text(analyse({"groups": []})) == "No Auto Scaling Groups matched."


def test_markdown_report_is_a_table(prod, now):
    out = render_markdown(analyse(prod, now=now))
    assert out.startswith("### ASG launch template drift")
    assert "| --- |" in out
    assert "| `prod-web` |" in out
    assert "**1 critical**" in out
    assert out.endswith("\n")


def test_markdown_report_handles_no_groups():
    out = render_markdown(analyse({"groups": []}))
    assert "No Auto Scaling Groups matched." in out


def test_json_report_round_trips(prod, now):
    result = analyse(prod, now=now)
    parsed = json.loads(render_json(result))
    assert parsed["region"] == "eu-central-1"
    assert parsed["summary"]["critical"] == 1
    assert parsed["groups"][0]["name"] == "prod-web"
    assert parsed["groups"][0]["drifted_fields"] == ["ImageId", "UserData"]


def test_render_dispatches_on_format(prod, now):
    result = analyse(prod, now=now)
    assert render(result, "json") == render_json(result)
    assert render(result, "markdown") == render_markdown(result)
    assert render(result, "text") == render_text(result)
