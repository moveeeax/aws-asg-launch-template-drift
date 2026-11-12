"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .analyze import (
    ALWAYS_CRITICAL_FIELDS,
    COMPARED_FIELDS,
    DEFAULT_CRITICAL_FIELDS,
    PayloadError,
    analyse,
    exit_code,
)
from .report import render

EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aws-asg-launch-template-drift",
        description=(
            "Report Auto Scaling Groups whose running instances no longer match the "
            "launch template version the group is configured with."
        ),
    )
    parser.add_argument("--region", help="AWS region to query")
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="ASG",
        help="limit to this Auto Scaling Group (repeatable)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="limit to groups carrying this tag (repeatable)",
    )
    parser.add_argument(
        "--critical-pct",
        type=float,
        default=50.0,
        metavar="PCT",
        help="stale share above which a group is critical (default 50)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        metavar="DAYS",
        help="age of the oldest stale instance that makes a group critical (default 30)",
    )
    parser.add_argument(
        "--fields",
        action="append",
        default=[],
        metavar="FIELD[,FIELD]",
        help="launch template fields to compare (repeatable, comma-separated; "
        f"default all: {','.join(COMPARED_FIELDS)})",
    )
    parser.add_argument(
        "--critical-field",
        action="append",
        default=[],
        metavar="FIELD",
        help="make drift in this field critical on its own, like ImageId (repeatable)",
    )
    parser.add_argument(
        "--no-default-critical",
        action="store_true",
        help="drop the default critical fields except ImageId "
        f"(default critical: {','.join(DEFAULT_CRITICAL_FIELDS)})",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="output format (default text)",
    )
    parser.add_argument(
        "--fail-on",
        choices=("warn", "critical", "none"),
        default="none",
        help="severity that makes the command exit 1 (default none)",
    )
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        help="replay a saved payload instead of calling AWS ('-' reads stdin)",
    )
    parser.add_argument(
        "--dump-payload",
        metavar="PATH",
        help="write the collected payload to PATH so it can be replayed later",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _split_fields(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out


def _resolve_fields(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Validate --fields and --critical-field, returning both in COMPARED_FIELDS order.

    Critical fields are the defaults (only ImageId with --no-default-critical) plus every
    --critical-field, limited to the fields being compared.
    """
    known = ", ".join(COMPARED_FIELDS)
    chosen = _split_fields(args.fields)
    escalate = _split_fields(args.critical_field)
    for name in chosen + escalate:
        if name not in COMPARED_FIELDS:
            raise ValueError(f"unknown field {name!r}; choose from {known}")
    if args.fields and not chosen:
        raise ValueError(f"--fields needs at least one field; choose from {known}")
    fields = [f for f in COMPARED_FIELDS if not chosen or f in chosen]
    for name in escalate:
        if name not in fields:
            raise ValueError(f"--critical-field {name} is not among the compared --fields")
    defaults = ALWAYS_CRITICAL_FIELDS if args.no_default_critical else DEFAULT_CRITICAL_FIELDS
    critical = set(escalate) | {f for f in defaults if f in fields}
    return fields, [f for f in fields if f in critical]


def _load_payload(path: str) -> dict:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise PayloadError(f"no such file: {path}") from None
    except json.JSONDecodeError as exc:
        raise PayloadError(f"{path} is not valid JSON: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.critical_pct < 0 or args.critical_pct > 100:
        print("--critical-pct must be between 0 and 100", file=sys.stderr)
        return EXIT_USAGE
    if args.max_age_days < 0:
        print("--max-age-days must not be negative", file=sys.stderr)
        return EXIT_USAGE
    try:
        fields, critical_fields = _resolve_fields(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.from_json:
            payload = _load_payload(args.from_json)
        else:
            from .collect import collect

            payload = collect(region=args.region, names=args.name, tags=args.tag)

        if args.dump_payload:
            with open(args.dump_payload, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)

        result = analyse(
            payload,
            critical_pct=args.critical_pct,
            max_age_days=args.max_age_days,
            fields=fields,
            critical_fields=critical_fields,
        )
    except PayloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(render(result, args.format))
    return exit_code(result, args.fail_on)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
