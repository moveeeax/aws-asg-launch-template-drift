# examples

`prod-drift.json` is a captured payload — the shape `--dump-payload` writes and
`--from-json` replays. It holds the four cases worth seeing at once: a group whose AMI
drifted, a clean group, a group where only security group ordering changed (not drift),
and a group still on a launch configuration.

Replay it without AWS credentials:

```bash
aws-asg-launch-template-drift --from-json examples/prod-drift.json
aws-asg-launch-template-drift --from-json examples/prod-drift.json --format markdown
aws-asg-launch-template-drift --from-json examples/prod-drift.json --fail-on critical
```

To capture your own:

```bash
aws-asg-launch-template-drift --region eu-central-1 --dump-payload /tmp/asg.json
aws-asg-launch-template-drift --from-json /tmp/asg.json --critical-pct 25
```

The payload contains launch template metadata and a userdata string, so treat a dump
from a real account as sensitive before pasting it into a ticket.
