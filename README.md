# aws-asg-launch-template-drift

[![ci](https://github.com/moveeeax/aws-asg-launch-template-drift/actions/workflows/ci.yml/badge.svg)](https://github.com/moveeeax/aws-asg-launch-template-drift/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Find Auto Scaling Groups whose running instances no longer match the launch template
version the group is configured with.

Updating a launch template does not touch the instances already in the group. AWS keeps
them on the version they launched with and applies the new one only on the next
replacement, so an ASG can sit for weeks with its configuration and its fleet in
disagreement. The console shows you the target version without ever saying how many
instances actually have it. This tool joins the three API calls that answer the question
and prints the groups that will change behaviour on their next scale-out.

## How it works

Three calls, joined:

1. `describe_auto_scaling_groups` — the group's target launch template (direct or inside a
   mixed instances policy), and the version each instance reports having launched with.
2. `describe_launch_template_versions` — resolves `$Latest` and `$Default` to a real
   numeric version and fetches the version bodies so they can be diffed.
3. `describe_instances` — launch times, only so the report can say how long a stale
   instance has been running.

Versions are compared on the five fields that change what a replacement instance actually
runs: `ImageId`, `InstanceType`, `SecurityGroupIds`, `IamInstanceProfile` and `UserData`.
Security groups are compared as a set, because response ordering is not a configuration
change, and userdata is compared by SHA-256 so a 16 KB blob does not end up in the report.

Severity per group:

| Severity | When |
| --- | --- |
| `ok` | every live instance is on the target version |
| `warn` | any instance is stale, or the group still uses a launch configuration |
| `critical` | the drift includes `ImageId`, or the stale share exceeds `--critical-pct` (default 50), or the oldest stale instance is older than `--max-age-days` (default 30) |

An `ImageId` difference is critical on its own, whatever the share: one stale instance with
a different AMI means the next scale-out quietly deploys a different image. Instances in a
`Terminating` or `Detaching` lifecycle state are churn, not drift, and are skipped.

## Install

```bash
pip install git+https://github.com/moveeeax/aws-asg-launch-template-drift
```

Needs `autoscaling:DescribeAutoScalingGroups`, `ec2:DescribeLaunchTemplateVersions` and
`ec2:DescribeInstances`. Read-only throughout; the tool never calls a mutating API.

## Usage

```bash
aws-asg-launch-template-drift --region eu-central-1
aws-asg-launch-template-drift --tag Environment=prod --max-age-days 14
aws-asg-launch-template-drift --format json | jq '.groups[] | select(.severity=="critical")'
aws-asg-launch-template-drift --fail-on critical
```

```console
$ aws-asg-launch-template-drift --from-json examples/prod-drift.json
      GROUP            TARGET        STALE         OLDEST  DRIFT
----  ---------------  ------------  ------------  ------  --------------------
CRIT  prod-web         7 ($Latest)   2/8 (25.0%)   12d     ImageId,UserData
WARN  staging-workers  9 ($Default)  1/3 (33.33%)  4d      no field diff
WARN  legacy-batch     -             -             -       launch configuration
ok    prod-api         3             0/3 (0.0%)    -       -

prod-web [critical]
  - 2 of 8 instances still on version 4 instead of version 7
  - ImageId differs between version 4 and version 7 — the next scale-out changes the running image
  - version 4 also differs in UserData
staging-workers [warn]
  - 1 of 3 instance still on version 8 instead of version 9
legacy-batch [warn]
  - group still uses launch configuration legacy-batch-lc-20190412, which cannot be versioned — migrate it to a launch template

4 groups: 1 critical, 2 warn, 1 ok; 3 stale instances.
```

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--region` | session default | region to query |
| `--name ASG` | all | limit to a group, repeatable |
| `--tag KEY=VALUE` | all | limit to groups carrying a tag, repeatable |
| `--critical-pct PCT` | `50` | stale share above which a group is critical |
| `--max-age-days DAYS` | `30` | stale instance age that makes a group critical |
| `--format` | `text` | `text`, `json` or `markdown` |
| `--fail-on` | `none` | `warn`, `critical` or `none` — drives the exit code |
| `--from-json PATH` | — | replay a saved payload instead of calling AWS (`-` reads stdin) |
| `--dump-payload PATH` | — | save the collected payload for later replay |

Exit codes: `0` clean, `1` findings at or above `--fail-on`, `2` bad arguments or a
malformed payload.

### In CI

```yaml
- name: Check ASG drift
  run: aws-asg-launch-template-drift --region eu-central-1 --fail-on critical --format markdown
```

## Offline replay

`--dump-payload` writes the collected API data and `--from-json` reads it back, so a
finding can be reproduced and argued about without credentials:

```bash
aws-asg-launch-template-drift --region eu-central-1 --dump-payload /tmp/asg.json
aws-asg-launch-template-drift --from-json /tmp/asg.json --critical-pct 25
```

That is also how the test suite works: the collector returns plain dicts, the analyser
consumes them, and every rule is asserted against JSON fixtures under `tests/fixtures/`
with no AWS calls and no mocking library. A dump from a real account carries launch
template metadata and userdata, so treat it as sensitive.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest -q
```

## License

MIT
