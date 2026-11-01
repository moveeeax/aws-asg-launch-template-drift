import json

import pytest

from aws_asg_launch_template_drift.cli import main

from .conftest import FIXTURES

PROD = str(FIXTURES / "prod.json")
# The fixtures are pinned in the past, so age never decides a CLI test.
AGELESS = ["--max-age-days", "36500"]


def test_fail_on_critical_exits_one(capsys):
    assert main(["--from-json", PROD, "--fail-on", "critical", *AGELESS]) == 1
    assert "prod-web" in capsys.readouterr().out


def test_fail_on_none_exits_zero(capsys):
    assert main(["--from-json", PROD, "--fail-on", "none", *AGELESS]) == 0
    capsys.readouterr()


def test_default_fail_on_is_none(capsys):
    assert main(["--from-json", PROD, *AGELESS]) == 0
    capsys.readouterr()


def test_clean_fleet_exits_zero_even_with_fail_on_warn(tmp_path, prod, capsys):
    payload = {"groups": [g for g in prod["groups"] if g["name"] == "prod-api"]}
    path = tmp_path / "clean.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["--from-json", str(path), "--fail-on", "warn", *AGELESS]) == 0
    capsys.readouterr()


def test_json_output_is_parseable(capsys):
    main(["--from-json", PROD, "--format", "json", *AGELESS])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["summary"]["groups"] == 4


def test_markdown_output(capsys):
    main(["--from-json", PROD, "--format", "markdown", *AGELESS])
    assert "| `prod-web` |" in capsys.readouterr().out


def test_reads_payload_from_stdin(monkeypatch, capsys, prod):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(prod)))
    assert main(["--from-json", "-", *AGELESS]) == 0
    assert "prod-web" in capsys.readouterr().out


def test_missing_file_exits_two_without_a_traceback(capsys):
    assert main(["--from-json", "/nonexistent/payload.json"]) == 2
    captured = capsys.readouterr()
    assert "no such file" in captured.err
    assert "Traceback" not in captured.err


def test_malformed_json_exits_two(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert main(["--from-json", str(path)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_payload_without_groups_exits_two(tmp_path, capsys):
    path = tmp_path / "empty.json"
    path.write_text("{}", encoding="utf-8")
    assert main(["--from-json", str(path)]) == 2
    assert "groups" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--critical-pct", "140"], ["--critical-pct", "-1"]])
def test_out_of_range_critical_pct_exits_two(args, capsys):
    assert main(["--from-json", PROD, *args]) == 2
    assert "--critical-pct" in capsys.readouterr().err


def test_negative_max_age_exits_two(capsys):
    assert main(["--from-json", PROD, "--max-age-days", "-5"]) == 2
    assert "--max-age-days" in capsys.readouterr().err


def test_dump_payload_writes_a_replayable_file(tmp_path, capsys):
    out = tmp_path / "dump.json"
    assert main(["--from-json", PROD, "--dump-payload", str(out), *AGELESS]) == 0
    capsys.readouterr()
    assert main(["--from-json", str(out), "--fail-on", "critical", *AGELESS]) == 1
    capsys.readouterr()


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "aws-asg-launch-template-drift" in capsys.readouterr().out
